"""「最干净的格式」——本月实况导入文档（规范 JSON）。

这是本模块的**规范输入**：各家账单导出（微信 / 支付宝 / 银行 / 券商……）由**转换器**
统一转成这份文档之后再进来，解析侧就只需面对一种形状。转换器见
:mod:`moneyrouter_agent.tools.bill_cleaner`（目前覆盖支付宝 CSV 与微信 XLSX 两个来源）；
在那之外的格式，手工整理成这份 JSON 也能直接跑通全流程。

## 文档形状

一份 JSON 对象，三段（只有 ``cashflow`` 是必需的）：

```json
{
  "period": "2026-09",
  "cashflow": [
    {"date": "2026-09-01", "direction": "expense", "amount": 88.00,
     "category": "餐饮", "note": "朋友聚餐"},
    {"date": "2026-09-05", "direction": "income",  "amount": 8000.00, "category": "工资"},
    {"date": "2026-09-25", "direction": "transfer", "amount": 1000.00, "note": "还信用卡"}
  ],
  "savings": {"non_invested": 3249.50, "invested": 0, "note": "留着当应急金"},
  "investments": {
    "has_investments": true,
    "holdings": [
      {"name": "沪深300指数基金", "kind": "基金", "cost": 20000.00,
       "market_value": 20800.00, "month_return": 80.00, "total_return": 800.00}
    ],
    "note": "长期定投"
  }
}
```

## 约定（关键：**不用正负号表示方向**）

- **金额一律用「元」**（可带小数），内部自动换算成「分」；
- **方向由 ``direction`` 给定**，不靠金额正负——正负号正是各家导出最容易出错的地方；
  - ``income``（别名 ``收入``）→ 计入收入；
  - ``expense``（别名 ``支出``）→ 计入支出，按 ``category`` 归集；
  - ``transfer``（别名 ``不计收支`` / ``转账`` / ``还款``）→ **不计入收支**，只计数（``skipped_rows``）；
- ``category`` 可写平台原始类目（会走 :func:`~moneyrouter_agent.domain.month.normalize_category`
  归一）；``note`` 会优先作为一次性大额的名字；
- ``savings`` / ``investments`` 可选；给了就按**文件来源**对待，优先于对话口述。

## 校验

**解析永不抛异常**：读不懂的整份文档、认不出的方向、非正的金额，都如实进 ``warnings``
（带**第几条**，便于转换器作者定位），能用的部分照常产出。
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.money import MoneyError, yuan_to_cents
from ..domain.month import FundAllocation, HoldingReturn, InvestmentSnapshot
from .bills import (
    DEFAULT_ONE_OFF_MIN_CENTS,
    BillRow,
    ParsedBill,
    aggregate_rows,
    register_parser,
)

# 规范键 → 可接受的别名（转换器只要命中其一即可）
PERIOD_ALIASES = ("period", "month", "期间", "月份")
CASHFLOW_ALIASES = ("cashflow", "transactions", "流水", "明细", "交易")
SAVINGS_ALIASES = ("savings", "allocation", "结余去向", "储蓄")
INVESTMENTS_ALIASES = ("investments", "holdings", "投资", "持仓")

DATE_ALIASES = ("date", "日期", "交易时间", "time")
DIRECTION_ALIASES = ("direction", "收支", "收/支", "方向", "type")
AMOUNT_ALIASES = ("amount", "金额", "交易金额")
CATEGORY_ALIASES = ("category", "分类", "类目", "交易类型", "类型")
NOTE_ALIASES = ("note", "memo", "remark", "摘要", "备注", "商品", "说明", "description")

INCOME_WORDS = ("income", "收入", "in", "收")
EXPENSE_WORDS = ("expense", "spending", "支出", "out", "支")
TRANSFER_WORDS = ("transfer", "不计收支", "转账", "还款", "内部转账", "neutral")


def _pick(obj: dict[str, Any], *names: str) -> Any:
    """按别名取第一个存在的键（值为 ``None`` 视为未给）。"""
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    return None


def _pick_section(doc: dict[str, Any], aliases: tuple[str, ...]) -> dict[str, Any] | None:
    value = _pick(doc, *aliases)
    return value if isinstance(value, dict) else None


def normalize_direction(raw: Any) -> str | None:
    """把「方向」归一成 ``income`` / ``expense`` / ``transfer``；认不出返回 ``None``。"""
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text in TRANSFER_WORDS:
        return "transfer"
    if text in INCOME_WORDS:
        return "income"
    if text in EXPENSE_WORDS:
        return "expense"
    # 宽松兜底：长词优先，避免「不计收支」被「支」抢先命中
    for word in TRANSFER_WORDS:
        if word in text:
            return "transfer"
    for word in INCOME_WORDS:
        if word in text:
            return "income"
    for word in EXPENSE_WORDS:
        if word in text:
            return "expense"
    return None


_DECORATIONS = ("¥", "￥", ",", "，", "元", " ")


def _clean_amount(raw: Any) -> str:
    """去掉常见的货币装饰（``¥ ￥ , ， 元`` 与空格），只留数字本身。"""
    text = str(raw).strip()
    for token in _DECORATIONS:
        text = text.replace(token, "")
    return text


def _amount_cents(raw: Any, label: str, warnings: list[str]) -> int | None:
    """必需金额（元）→ 分；缺失 / 非数字 / 负数一律记告警并返回 ``None``。"""
    if raw is None:
        warnings.append(f"{label}：缺少金额。")
        return None
    try:
        cents = yuan_to_cents(_clean_amount(raw))
    except (MoneyError, ValueError):
        warnings.append(f"{label}：金额「{raw}」不是有效数字。")
        return None
    if cents < 0:
        warnings.append(f"{label}：金额为负（{raw}），方向请用 direction 表示，已跳过。")
        return None
    return cents


def _opt_amount_cents(raw: Any, label: str, warnings: list[str]) -> int | None:
    """可选金额（元）→ 分：没给就是"未了解到"，**不告警**；给了但不合法才告警。"""
    if raw is None:
        return None
    return _amount_cents(raw, label, warnings)


def _signed_cents(raw: Any, label: str, warnings: list[str]) -> int | None:
    """收益类金额**允许为负**（亏损），且不能与"未了解到"混淆。"""
    if raw is None:
        return None
    try:
        return yuan_to_cents(_clean_amount(raw))
    except (MoneyError, ValueError):
        warnings.append(f"{label}：金额「{raw}」不是有效数字。")
        return None


def _parse_cashflow(rows: Any, warnings: list[str]) -> list[BillRow]:
    """流水段 → ``BillRow`` 列表（支出记为负数，不计收支标 ``excluded``）。"""
    if rows is None:
        warnings.append("文档缺少 cashflow（本月流水），无法解析收支。")
        return []
    if not isinstance(rows, list):
        warnings.append("cashflow 应为数组，每一笔一个对象。")
        return []

    out: list[BillRow] = []
    for index, item in enumerate(rows, start=1):
        label = f"第 {index} 条流水"
        if not isinstance(item, dict):
            warnings.append(f"{label}：不是对象，已跳过。")
            continue
        direction = normalize_direction(_pick(item, *DIRECTION_ALIASES))
        if direction is None:
            raw = _pick(item, *DIRECTION_ALIASES)
            warnings.append(
                f"{label}：direction「{raw if raw is not None else '（空）'}」无法识别"
                f"（应为 income / expense / transfer），已跳过。"
            )
            continue
        cents = _amount_cents(_pick(item, *AMOUNT_ALIASES), label, warnings)
        if cents is None:
            continue

        signed = cents if direction == "income" else (-cents if direction == "expense" else cents)
        out.append(
            BillRow(
                date=str(_pick(item, *DATE_ALIASES) or "").strip(),
                category_raw=str(_pick(item, *CATEGORY_ALIASES) or "").strip(),
                amount_cents=signed,
                description=str(_pick(item, *NOTE_ALIASES) or "").strip(),
                excluded=direction == "transfer",
            )
        )
    if not out:
        warnings.append("cashflow 里没有一条能用的流水。")
    return out


def _parse_savings(section: dict[str, Any], warnings: list[str]) -> FundAllocation:
    """结余去向段（投资 / 非投资）。"""
    return FundAllocation(
        non_invested_cents=_opt_amount_cents(
            _pick(section, "non_invested", "非投资", "留存"), "savings.non_invested", warnings
        ),
        invested_cents=_opt_amount_cents(
            _pick(section, "invested", "投资"), "savings.invested", warnings
        ),
        note=str(_pick(section, "note", "备注", "说明") or "").strip(),
        source="file",
        evidence="导入文档 savings 段",
    )


def _parse_holdings(items: Any, warnings: list[str]) -> list[HoldingReturn]:
    if not isinstance(items, list):
        if items is not None:
            warnings.append("investments.holdings 应为数组。")
        return []
    out: list[HoldingReturn] = []
    for index, item in enumerate(items, start=1):
        label = f"第 {index} 条持仓"
        if not isinstance(item, dict):
            warnings.append(f"{label}：不是对象，已跳过。")
            continue
        name = str(_pick(item, "name", "名称", "产品", "标的") or "").strip()
        if not name:
            warnings.append(f"{label}：缺少 name（产品名称），已跳过。")
            continue
        out.append(
            HoldingReturn(
                name=name,
                kind=str(_pick(item, "kind", "类别", "类型") or "").strip(),
                cost_cents=_opt_amount_cents(_pick(item, "cost", "本金", "成本"), f"{label} cost", warnings),
                market_value_cents=_opt_amount_cents(
                    _pick(item, "market_value", "市值", "当前市值"), f"{label} market_value", warnings
                ),
                month_return_cents=_signed_cents(
                    _pick(item, "month_return", "本月收益", "本月盈亏"), f"{label} month_return", warnings
                ),
                total_return_cents=_signed_cents(
                    _pick(item, "total_return", "累计收益", "累计盈亏"), f"{label} total_return", warnings
                ),
                source="file",
                evidence="导入文档 investments 段",
            )
        )
    return out


def _parse_investments(section: dict[str, Any], warnings: list[str]) -> InvestmentSnapshot:
    """已有投资段（是否持有 / 逐笔持仓 / 说明）。"""
    holdings = _parse_holdings(_pick(section, "holdings", "持仓", "products"), warnings)
    raw_has = _pick(section, "has_investments", "has", "是否持有", "持有")
    has: bool | None
    if isinstance(raw_has, bool):
        has = raw_has
    elif raw_has is None:
        has = True if holdings else None
    elif isinstance(raw_has, str):
        has = str(raw_has).strip().lower() not in ("false", "no", "0", "否", "无", "没有")
    else:
        has = bool(raw_has)
    return InvestmentSnapshot(
        has_investments=has,
        holdings=holdings,
        note=str(_pick(section, "note", "备注", "说明") or "").strip(),
        source="file",
    )


class DossierParser:
    """规范文档（JSON）解析器。"""

    def parse(
        self, content: str | bytes, *, period: str, one_off_min_cents: int = DEFAULT_ONE_OFF_MIN_CENTS
    ) -> ParsedBill:
        text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
        stripped = (text or "").strip()
        if not stripped:
            return ParsedBill(period=period, warnings=["文档为空，未解析到任何数据。"])

        try:
            document = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return ParsedBill(
                period=period,
                warnings=[
                    "文档不是合法 JSON，无法解析："
                    f"第 {exc.lineno} 行第 {exc.colno} 列（{exc.msg}）。"
                ],
                missing=["cashflow"],
            )
        if isinstance(document, list):
            document = {"cashflow": document}
        if not isinstance(document, dict):
            return ParsedBill(
                period=period,
                warnings=["文档应为 JSON 对象（或直接给一个流水数组）。"],
                missing=["cashflow"],
            )

        warnings: list[str] = []
        missing: list[str] = []

        doc_period = str(_pick(document, *PERIOD_ALIASES) or "").strip()
        target = period or doc_period
        if not target:
            warnings.append("文档未给出 period，且调用时也没传统计期间。")

        rows = _parse_cashflow(_pick(document, *CASHFLOW_ALIASES), warnings)
        if not rows:
            missing.append("cashflow")

        parsed = aggregate_rows(
            rows,
            period=target,
            one_off_min_cents=one_off_min_cents,
            warnings=warnings,
            missing=missing,
        )

        savings = _pick_section(document, SAVINGS_ALIASES)
        if savings is not None:
            parsed.allocation = _parse_savings(savings, parsed.warnings)

        investments = _pick_section(document, INVESTMENTS_ALIASES)
        if investments is not None:
            parsed.investments = _parse_investments(investments, parsed.warnings)

        parsed.period = target
        coverage = document.get("coverage") or {}
        if isinstance(coverage, dict):
            parsed.coverage_complete = coverage.get("complete") if isinstance(coverage.get("complete"), bool) else None
            parsed.coverage_start = str(coverage.get("start") or "")
            parsed.coverage_end = str(coverage.get("end") or "")
            if parsed.coverage_complete is False:
                parsed.warnings.append("账单仅覆盖部分月份，不纳入完整月度基线。")
        parsed.review_required_count = sum(bool(item.get("review_reason")) for item in document.get("cashflow", []) if isinstance(item, dict))
        parsed.accounting_basis = str(document.get("accounting_basis") or "platform")
        return parsed


register_parser("json", DossierParser())


def sample_document(period: str = "2026-09") -> dict[str, Any]:
    """一份可直接跑的规范文档样例（供演练脚本与测试使用）。"""
    return {
        "period": period,
        "cashflow": [
            {"date": f"{period}-01", "direction": "expense", "amount": 88.00, "category": "餐饮", "note": "朋友聚餐"},
            {"date": f"{period}-03", "direction": "expense", "amount": 42.50, "category": "外卖"},
            {"date": f"{period}-05", "direction": "income", "amount": 8000.00, "category": "工资", "note": "当月工资"},
            {"date": f"{period}-08", "direction": "expense", "amount": 2500.00, "category": "房租"},
            {"date": f"{period}-12", "direction": "expense", "amount": 2000.00, "category": "数码", "note": "换手机"},
            {"date": f"{period}-20", "direction": "expense", "amount": 120.00, "category": "餐饮", "note": "家庭聚餐"},
            {"date": f"{period}-25", "direction": "transfer", "amount": 1000.00, "note": "还信用卡"},
        ],
        "savings": {"non_invested": 3249.50, "invested": 0, "note": "留着当应急金"},
        "investments": {
            "has_investments": True,
            "holdings": [
                {
                    "name": "沪深300指数基金",
                    "kind": "基金",
                    "cost": 20000.00,
                    "market_value": 20800.00,
                    "month_return": 80.00,
                    "total_return": 800.00,
                    "note": "长期定投",
                }
            ],
            "note": "每月定投，本月小幅盈利",
        },
    }


def sample_dossier(period: str = "2026-09") -> str:
    """样例文档的 JSON 文本。"""
    return json.dumps(sample_document(period), ensure_ascii=False, indent=2)
