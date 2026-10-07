"""账单解析：原始账单 → 结构化事实（**代码负责，模型碰不到数字**）。

**规范入口是** :mod:`moneyrouter_agent.tools.dossier` 的「本月实况导入文档」（JSON）——
各家的导出格式由 :mod:`moneyrouter_agent.tools.bill_cleaner` 统一转成它之后再进来
（当前覆盖支付宝 CSV 与微信 XLSX）。本模块的 CSV 解析器是**兼容入口**，
供手工整理过的宽表使用。

可插拔：``BillParser`` 协议 + ``PARSERS`` 注册表。``parse_bill`` 不指定解析器时**自动识别**
（以 ``{`` 开头走规范文档，否则走 CSV）。

纪律（对齐 ``tools/market_data`` 的"宁可留空也不编"）：

- 认不出的行 / 未知类目 **如实进 ``warnings``，不静默丢弃**；
- 「不计收支」（转账 / 还款）的行保留在 ``rows`` 里、只计数（``skipped_rows``），不参与汇总；
- 缺列 → 进 ``missing``；
- 金额一律走 ``domain.money`` 换算成「分」，正负号保留。
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..domain.money import MoneyError, yuan_to_cents
from ..domain.month import (
    CategorySpend,
    FundAllocation,
    IncomeFact,
    InvestmentSnapshot,
    MonthSnapshot,
    MonthlyPoint,
    OneOffItem,
    is_income_text,
    normalize_category,
)

# 表头列名关键词（中英皆可）
DATE_KEYS = ("日期", "交易时间", "时间", "date", "time")
CATEGORY_KEYS = ("分类", "类目", "交易类型", "类型", "category", "type")
AMOUNT_KEYS = ("金额", "交易金额", "交易额", "收支", "amount", "money")
DESC_KEYS = ("摘要", "备注", "商品", "说明", "description", "memo", "remark", "note")

_MONTH_RE = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})")

DEFAULT_ONE_OFF_MIN_CENTS = 100_000

# 通常属固定/周期性支出的分类：不参与"一次性大额"的自动判定（例如房租每月都差不多）
RECURRING_CATEGORIES: tuple[str, ...] = ("居住", "订阅服务")


class BillRow(BaseModel):
    """账单里的一行（保留出处，便于追溯）。``amount_cents`` 负数=支出、正数=收入。"""

    model_config = ConfigDict(extra="ignore")

    date: str = ""
    category_raw: str = ""
    amount_cents: int = 0
    description: str = ""
    excluded: bool = Field(
        default=False,
        description="是否为「不计收支」的行（转账 / 还款等）：保留以便追溯，但不参与收支汇总。",
    )


class ParsedBill(BaseModel):
    """账单解析结果：流水聚合出的事实 + 文档另带的「结余去向」与「已有投资收益」。"""

    model_config = ConfigDict(extra="ignore")

    period: str = Field(default="", description="目标统计期间 YYYY-MM。")
    coverage_complete: bool | None = None
    coverage_start: str = ""
    coverage_end: str = ""
    review_required_count: int = 0
    accounting_basis: str = "platform"
    rows: list[BillRow] = Field(default_factory=list)
    categories: list[CategorySpend] = Field(default_factory=list, description="文件来源的分类支出。")
    income: IncomeFact | None = Field(default=None, description="文件里识别到的收入。")
    one_offs: list[OneOffItem] = Field(default_factory=list, description="文件里的一次性大额。")
    allocation: FundAllocation | None = Field(
        default=None, description="文档直接给出的结余去向（投资 / 非投资）；没有就留空。"
    )
    investments: InvestmentSnapshot | None = Field(
        default=None, description="文档直接给出的已有投资收益；没有就留空。"
    )
    monthly_points: list[MonthlyPoint] = Field(default_factory=list, description="按月的总体收支，供趋势图。")
    warnings: list[str] = Field(default_factory=list, description="认不出的行 / 未知类目等，如实记录。")
    missing: list[str] = Field(default_factory=list, description="缺失的关键字段名。")
    skipped_rows: int = Field(default=0, description="被排除在收支之外的「不计收支」笔数。")

    def as_snapshot(self, *, period: str | None = None) -> MonthSnapshot:
        """把解析结果转成"文件来源"的月度快照（供 ingest 节点并入）。"""
        return MonthSnapshot(
            period=period or self.period,
            coverage_complete=self.coverage_complete,
            coverage_start=self.coverage_start, coverage_end=self.coverage_end,
            review_required_count=self.review_required_count, accounting_basis=self.accounting_basis,
            income=self.income,
            categories=list(self.categories),
            one_offs=list(self.one_offs),
            allocation=self.allocation or FundAllocation(),
            investments=self.investments or InvestmentSnapshot(),
        )


class BillParser(Protocol):
    """账单解析器协议。"""

    def parse(self, content: str | bytes, *, period: str, one_off_min_cents: int = ...) -> ParsedBill: ...


PARSERS: dict[str, BillParser] = {}


def register_parser(key: str, parser: BillParser) -> None:
    """注册一个解析器（key 可为扩展名或格式名）。"""
    PARSERS[key.lower()] = parser


def _to_text(content: str | bytes) -> str:
    if isinstance(content, str):
        return content
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_amount(raw: Any) -> int | None:
    """把金额字符串换算成「分」；无法解析返回 ``None``。"""
    text = str(raw or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    cleaned = (
        text.replace(",", "")
        .replace("，", "")
        .replace("¥", "")
        .replace("￥", "")
        .replace("元", "")
        .replace("+", "")
        .replace(" ", "")
    )
    try:
        cents = yuan_to_cents(cleaned)
    except (MoneyError, ValueError):
        return None
    return -cents if negative else cents


def _month_of(date_text: str, fallback: str) -> str:
    match = _MONTH_RE.search(date_text or "")
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return fallback


def _find_column(header: list[str], keys: tuple[str, ...]) -> int | None:
    lowered = [h.strip().lower() for h in header]
    for index, name in enumerate(lowered):
        if any(key.lower() in name for key in keys):
            return index
    return None


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return str(row[index]).strip()


def aggregate_rows(
    rows: list[BillRow],
    *,
    period: str,
    one_off_min_cents: int = DEFAULT_ONE_OFF_MIN_CENTS,
    warnings: list[str] | None = None,
    missing: list[str] | None = None,
) -> ParsedBill:
    """把归一后的行聚合成本月事实（**两个解析器共用**，保证口径一致）。

    - ``amount_cents`` 负数=支出、正数=收入；
    - ``excluded=True`` 的行（不计收支）保留在 ``rows`` 里，但**不参与任何汇总**，只计数。
    """
    warnings = list(warnings or [])
    missing = list(missing or [])

    month_totals: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.excluded:
            continue
        month = _month_of(row.date, period)
        bucket = month_totals.setdefault(month, {"income": 0, "spend": 0})
        if row.amount_cents >= 0 and _looks_like_income(row):
            bucket["income"] += row.amount_cents
        elif row.amount_cents < 0:
            bucket["spend"] += -row.amount_cents

    target = period or (max(month_totals) if month_totals else "")
    target_rows = [r for r in rows if not r.excluded and _month_of(r.date, target) == target]

    spend_by_category: dict[str, int] = {}
    one_offs: list[OneOffItem] = []
    income_cents = 0
    for row in target_rows:
        amount = row.amount_cents
        if amount >= 0 and _looks_like_income(row):
            income_cents += amount
            continue
        if amount >= 0:
            continue
        spend = -amount
        category = normalize_category(row.category_raw or row.description)
        if category == "其他" and (row.category_raw or row.description):
            warnings.append(f"未知类目「{row.category_raw or row.description}」，并入「其他」。")
        spend_by_category[category] = spend_by_category.get(category, 0) + spend
        if spend >= one_off_min_cents and category not in RECURRING_CATEGORIES:
            one_offs.append(
                OneOffItem(
                    label=(row.description or row.category_raw or "较大支出"),
                    amount_cents=spend,
                    source="file",
                    evidence=f"{row.date} {row.description}".strip(),
                )
            )

    categories = [
        CategorySpend(category=name, amount_cents=value, source="file")
        for name, value in sorted(spend_by_category.items())
    ]

    income = None
    if income_cents > 0:
        income = IncomeFact(
            amount_cents=income_cents, basis="账单识别", source="file", evidence="账单收入行合计"
        )
    else:
        missing.append("income")

    monthly_points = [
        MonthlyPoint(
            period=month,
            income_cents=bucket["income"] or None,
            spend_total_cents=bucket["spend"] or None,
            balance_cents=(bucket["income"] - bucket["spend"]) if bucket["income"] else None,
        )
        for month, bucket in sorted(month_totals.items())
    ]

    return ParsedBill(
        period=target,
        rows=rows,
        categories=categories,
        income=income,
        one_offs=one_offs,
        monthly_points=monthly_points,
        warnings=warnings,
        missing=missing,
        skipped_rows=sum(1 for row in rows if row.excluded),
    )


def _looks_like_income(row: BillRow) -> bool:
    """正数金额到底算不算收入：看类目/摘要是否命中收入词。"""
    return is_income_text(row.category_raw) or is_income_text(row.description)


class CSVBillParser:
    """通用 CSV 宽表解析器（兼容入口；规范入口见 :mod:`moneyrouter_agent.tools.dossier`）。"""

    def parse(
        self, content: str | bytes, *, period: str, one_off_min_cents: int = DEFAULT_ONE_OFF_MIN_CENTS
    ) -> ParsedBill:
        text = _to_text(content)
        reader = csv.reader(io.StringIO(text))
        rows = [row for row in reader if any(str(cell).strip() for cell in row)]
        if not rows:
            return ParsedBill(period=period, warnings=["账单为空，未解析到任何数据。"])

        header = rows[0]
        date_col = _find_column(header, DATE_KEYS)
        cat_col = _find_column(header, CATEGORY_KEYS)
        amount_col = _find_column(header, AMOUNT_KEYS)
        desc_col = _find_column(header, DESC_KEYS)

        if amount_col is None:
            return ParsedBill(
                period=period,
                warnings=["账单缺少金额列，无法解析。"],
                missing=["amount"],
            )

        warnings: list[str] = []
        missing: list[str] = []
        if cat_col is None:
            missing.append("category")
        if date_col is None:
            missing.append("date")

        bill_rows: list[BillRow] = []
        for line_no, row in enumerate(rows[1:], start=2):
            amount = _parse_amount(_cell(row, amount_col))
            if amount is None:
                warnings.append(f"第 {line_no} 行金额无法解析，已跳过。")
                continue
            bill_rows.append(
                BillRow(
                    date=_cell(row, date_col),
                    category_raw=_cell(row, cat_col),
                    amount_cents=amount,
                    description=_cell(row, desc_col),
                )
            )

        return aggregate_rows(
            bill_rows,
            period=period,
            one_off_min_cents=one_off_min_cents,
            warnings=warnings,
            missing=missing,
        )


register_parser("csv", CSVBillParser())


JSON_PARSER_KEY = "json"


def looks_like_json(content: str | bytes) -> bool:
    """内容是否像规范文档（以 ``{`` 开头，或直接给一个流水数组）。"""
    text = _to_text(content).lstrip()
    return text.startswith("{") or text.startswith("[")


def _ensure_json_parser() -> None:
    """惰性导入规范文档解析器（避免与 ``dossier`` 循环导入）。"""
    if JSON_PARSER_KEY not in PARSERS:
        from . import dossier  # noqa: F401 - 导入即完成注册


def parse_bill(
    content: str | bytes,
    *,
    period: str,
    one_off_min_cents: int = DEFAULT_ONE_OFF_MIN_CENTS,
    parser: BillParser | str | None = None,
) -> ParsedBill:
    """按解析器解析账单。

    ``parser`` 可以是解析器实例、注册名（``"json"`` / ``"csv"``），或 ``None``——
    ``None`` 时**自动识别**：以 ``{`` 开头按规范文档（``"json"``）处理，其余按 CSV。
    """
    chosen: BillParser
    if parser is None:
        if looks_like_json(content):
            _ensure_json_parser()
            chosen = PARSERS[JSON_PARSER_KEY]
        else:
            chosen = PARSERS["csv"]
    elif isinstance(parser, str):
        if parser.lower() == JSON_PARSER_KEY:
            _ensure_json_parser()
        if parser.lower() not in PARSERS:
            raise KeyError(f"未注册的账单解析器：{parser}")
        chosen = PARSERS[parser.lower()]
    else:
        chosen = parser
    return chosen.parse(content, period=period, one_off_min_cents=one_off_min_cents)
