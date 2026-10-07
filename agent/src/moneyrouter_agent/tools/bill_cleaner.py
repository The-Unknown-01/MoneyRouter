"""原始账单 → 规范文档的转换器（清洗器）。

把平台导出的**原始账单**转成 :mod:`moneyrouter_agent.tools.dossier` 的**规范文档**，
让下游只面对一种形状。当前只覆盖两个来源（其余格式暂不做）：

- **支付宝「交易明细」CSV**：GBK 编码；前面是导出信息与特别提示，中间一行表头，末尾是回单尾注；
- **微信支付「账单流水文件」XLSX**：前 17 行是元信息，第 18 行表头，末尾是统计行。

## 三条对齐规范文档的口径

1. **方向只认平台写好的「收/支」列**（支付宝 ``支出/收入/不计收支``、微信 ``支出/收入//``），
   不重新猜；``transfer`` 只计数、不进收支。**金额一律取正数**——两家导出的金额都是正的，
   正负号正是最容易出错的地方。
2. **类目直接落枚举值**（:data:`moneyrouter_agent.domain.month.SPEND_CATEGORIES`）：
   支付宝查消费类目，微信按用途关键词推断；转账等支付方式不代表消费用途。
   推不出来明确写「其他」并标记复核，避免下游再根据姓名或备注猜一次。
3. **收入行必须带一个含收入词的 ``category``**：解析侧用 `is_income_text` 判定收入行，
   不带就会被**静默丢掉**（既不计收入也不计支出）。微信没有收入类目可给，这里统一落「收入」。

## 一个自然月一份文档

规范文档本身就是**单期间**形状：`aggregate_rows` 只会统计 ``period`` 那一个月，
混月喂进去会只认其中一个月、其余悄悄不参与汇总。所以这里按日期**按自然月切分**，
一个月出一份文档（`--period` 可以只要其中一个月）。

## 退款 / 交易关闭

两家的口径并不一致，所以默认**不动**，只如实报告：

- 微信把退款记成一笔独立的**收入**（``交易类型`` 含「退款」或状态「已全额退款」），
  原支付那笔仍是支出——收付两边都虚高，但**净额不受影响**；
- 支付宝把退款记为 ``不计收支``，而原始单那笔可能停在被退款前的 ``交易关闭`` 状态，
  于是支出被多算一笔。

``refund_policy="transfer"`` 是排除退款相关流水的兼容选项，不是净额核算。
跨月、部分退款及缺失原单必须复核；默认 keep 保留平台方向。
"""

from __future__ import annotations

import csv
import io
import json
import re
import calendar
from datetime import datetime
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..domain.money import format_yuan
from ..domain.month import SPEND_CATEGORIES, is_income_text, normalize_category

__all__ = [
    "ALIPAY_CATEGORY_MAP",
    "CLASSIFY_BATCH_SIZE",
    "TEXT_CATEGORY_KEYWORDS",
    "WECHAT_TYPE_CATEGORY",
    "ClassifierFn",
    "ClassifyOutcome",
    "CleanOutcome",
    "RawRow",
    "build_classify_items",
    "build_documents",
    "classify_rows",
    "clean",
    "parse_alipay_csv",
    "parse_wechat_xlsx",
    "read_wechat_summary",
    "render_report",
]

SOURCE_ALIPAY = "alipay"
SOURCE_WECHAT = "wechat"
SOURCE_LABELS = {SOURCE_ALIPAY: "支付宝", SOURCE_WECHAT: "微信"}

DIRECTIONS = ("income", "expense", "transfer")
REFUND_KEEP = "keep"
REFUND_TRANSFER = "transfer"
REFUND_NET = "net"
REFUND_POLICIES = (REFUND_KEEP, REFUND_TRANSFER, REFUND_NET)

#: ``note`` 是"一次性大额"的标签，太长没有可读性；超出即截断
NOTE_MAX_CHARS = 50

# --------------------------------------------------------------------------- #
# 类目口径：两张可改的表，改口径只动这里
# --------------------------------------------------------------------------- #

#: 支付宝「交易分类」→ 固定枚举（该列是平台自己分好的，直接查表最稳）。
#: **不写「其他」**：查不到就省略 ``category``，留给解析侧按备注再猜一次，
#: 也让报告把「生活服务 / 商业服务 / 账户存取 / 退款」这类平台类目如实列出来。
ALIPAY_CATEGORY_MAP: dict[str, str] = {
    "餐饮美食": "餐饮",
    "交通出行": "交通",
    "日用百货": "购物",
    "数码电器": "购物",
    "家居家装": "居住",
    "充值缴费": "居住",
    "教育培训": "学习成长",
    "医疗健康": "医疗健康",
    "文化休闲": "娱乐社交",
    "运动户外": "娱乐社交",
    "酒店旅游": "娱乐社交",
}

#: 微信「交易类型」→ 固定枚举（这些类型不给类目，靠类型本身就够了）
WECHAT_TYPE_CATEGORY: dict[str, str] = {}

#: 类目关键词表：「交易对方 + 商品/说明」里出现即归类。**两个来源共用**——
#: 支付宝先查「交易分类」表，查不到再用它补一次。匹配时**长关键词优先**。
TEXT_CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    # 餐饮
    ("食堂", "餐饮"),
    ("餐饮", "餐饮"),
    ("早餐", "餐饮"),
    ("米线", "餐饮"),
    ("水饺", "餐饮"),
    ("面馆", "餐饮"),
    ("捞面", "餐饮"),
    ("焖锅", "餐饮"),
    ("烧腊", "餐饮"),
    ("冒菜", "餐饮"),
    ("烤肉", "餐饮"),
    ("烧烤", "餐饮"),
    ("煎饼", "餐饮"),
    ("小吃", "餐饮"),
    ("糖水", "餐饮"),
    ("奶茶", "餐饮"),
    ("饮品", "餐饮"),
    ("咖啡", "餐饮"),
    ("点餐", "餐饮"),
    ("外卖", "餐饮"),
    ("餐", "餐饮"),
    ("饭", "餐饮"),
    ("面", "餐饮"),
    ("粉", "餐饮"),
    ("饼", "餐饮"),
    ("牛肉", "餐饮"),
    ("豆腐", "餐饮"),
    ("水果", "餐饮"),
    ("麦当劳", "餐饮"),
    ("肯德基", "餐饮"),
    ("kfc", "餐饮"),
    ("蜜雪冰城", "餐饮"),
    ("萨莉亚", "餐饮"),
    ("老乡鸡", "餐饮"),
    ("可口可乐", "餐饮"),
    # 购物
    ("超市", "购物"),
    ("便利店", "购物"),
    ("market", "购物"),
    ("京东", "购物"),
    ("淘宝", "购物"),
    ("天猫", "购物"),
    ("拼多多", "购物"),
    ("苏宁", "购物"),
    ("闲鱼", "购物"),
    ("无印良品", "购物"),
    ("muji", "购物"),
    ("优衣库", "购物"),
    ("uniqlo", "购物"),
    ("安踏", "购物"),
    ("李宁", "购物"),
    ("百货", "购物"),
    ("数码", "购物"),
    ("电器", "购物"),
    ("摄影", "购物"),
    ("烟酒", "购物"),
    ("食品", "购物"),
    ("商贸", "购物"),
    # 交通
    ("地铁", "交通"),
    ("公交", "交通"),
    ("打车", "交通"),
    ("出行", "交通"),
    ("滴滴", "交通"),
    ("哈啰", "交通"),
    ("单车", "交通"),
    ("骑行", "交通"),
    ("骑安", "交通"),
    ("铁路", "交通"),
    ("12306", "交通"),
    ("交运", "交通"),
    ("一卡通", "交通"),
    ("加油", "交通"),
    ("停车", "交通"),
    ("机场", "交通"),
    # 居住
    ("房租", "居住"),
    ("物业", "居住"),
    ("水费", "居住"),
    ("电费", "居住"),
    ("网费", "居住"),
    ("水电", "居住"),
    ("网络服务", "居住"),
    ("燃气", "居住"),
    ("宽带", "居住"),
    ("话费", "居住"),
    ("缴费", "居住"),
    # 娱乐社交（旅行 / 休闲 / 门票）
    ("旅业", "娱乐社交"),
    ("旅游", "娱乐社交"),
    ("旅行社", "娱乐社交"),
    ("景区", "娱乐社交"),
    ("草原", "娱乐社交"),
    ("民宿", "娱乐社交"),
    ("酒店", "娱乐社交"),
    ("电影", "娱乐社交"),
    ("影城", "娱乐社交"),
    ("steam", "娱乐社交"),
    ("valve", "娱乐社交"),
    ("健身", "娱乐社交"),
    # 学习成长
    ("打印", "学习成长"),
    ("图书", "学习成长"),
    ("书店", "学习成长"),
    ("课程", "学习成长"),
    ("培训", "学习成长"),
    ("学费", "学习成长"),
    ("考试", "学习成长"),
    ("教育", "学习成长"),
    ("文具", "学习成长"),
    # 医疗健康
    ("药房", "医疗健康"),
    ("药店", "医疗健康"),
    ("医院", "医疗健康"),
    ("诊所", "医疗健康"),
    ("体检", "医疗健康"),
    # 订阅服务
    ("会员", "订阅服务"),
    ("订阅", "订阅服务"),
    ("月卡", "订阅服务"),
    ("deepseek", "订阅服务"),
    ("api服务", "订阅服务"),
)

#: 预排序：长关键词优先（「食堂」不该被「餐」抢先，也无所谓，但保持与 domain 同一策略）
_SORTED_KEYWORDS: tuple[tuple[str, str], ...] = tuple(
    sorted((item for item in TEXT_CATEGORY_KEYWORDS if len(item[0]) > 1),
           key=lambda item: len(item[0]), reverse=True)
)

#: 微信「商品」里的样板文本：对类目判断毫无信息量，必须剔除，
#: 否则 18 笔「扫二维码付款」会被「收款」两个字全判成人情往来。
WECHAT_BOILERPLATE_PRODUCTS: tuple[str, ...] = (
    "收款方备注:二维码收款",
    "转账备注:微信转账",
    "收款方备注：二维码收款",
    "转账备注：微信转账",
)

# 反向自检：映射表里只允许出现固定枚举
_BAD_MAPPINGS = {
    value
    for value in (*ALIPAY_CATEGORY_MAP.values(), *WECHAT_TYPE_CATEGORY.values(),
                  *(c for _, c in TEXT_CATEGORY_KEYWORDS))
    if value not in SPEND_CATEGORIES
}
if _BAD_MAPPINGS:  # pragma: no cover - 表写错时立刻炸
    raise ValueError(f"类目映射表出现非枚举值：{sorted(_BAD_MAPPINGS)}")


# --------------------------------------------------------------------------- #
# 行模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawRow:
    """来源无关的中间行（清洗过程的公共形状）。

    ``category`` 是**当前判定**：解析阶段先落关键词表的结论，随后由
    :func:`classify_rows` 交给分类器（DeepSeek）覆盖。
    """

    source: str
    date: str = ""            # 归一成 YYYY-MM-DD[ HH:MM:SS]
    direction: str = "expense"
    amount_cents: int = 0     # 一律正数
    category: str = ""        # 已落枚举；空字符串 = 未归类（字段会被省略）
    note: str = ""
    text: str = ""            # 「对方 + 商品」拼句，供报告与分类器使用
    counterparty: str = ""
    product: str = ""
    platform_category: str = ""
    status: str = ""
    kind: str = ""
    refund_like: bool = False
    transaction_id: str = ""
    source_row: int = 0
    category_source: str = ""
    review_reason: str = ""
    merchant_order_id: str = ""
    original_transaction_id: str = ""
    gross_amount_cents: int | None = None
    original_direction: str = ""
    accounting_note: str = ""

    @property
    def period(self) -> str:
        return self.date[:7]

    @property
    def signed_cents(self) -> int:
        return -self.amount_cents if self.direction == "expense" else self.amount_cents


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

_PLACEHOLDERS = {"", "/", "-", "—", "无", "null", "none", "nan"}
_DATE_RE = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MONEY_DECORATION_RE = re.compile(r"[¥￥,，\s元]")

_TRANSFER_WORDS = ("不计收支", "中性交易", "transfer", "neutral")
_INCOME_WORDS = ("收入", "income")
_EXPENSE_WORDS = ("支出", "expense")


def _text(raw: Any) -> str:
    """单元格 → 干净字符串：压空白、去制表符、把占位符（``/`` 等）当空。"""
    if raw is None:
        return ""
    value = re.sub(r"\s+", " ", str(raw).replace("\u3000", " ")).strip()
    return "" if value.lower() in _PLACEHOLDERS else value


def _cents(raw: Any) -> int | None:
    """「元」→「分」；不是金额返回 ``None``。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    else:
        cleaned = _MONEY_DECORATION_RE.sub("", str(raw))
        if not cleaned:
            return None
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            return None
    if value.is_nan() or value.is_infinite():
        return None
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _yuan(cents: int) -> float:
    """「分」→ JSON 里的「元」数字。"""
    return float((Decimal(cents) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _direction(raw: Any) -> str | None:
    """「收/支」列 → ``income`` / ``expense`` / ``transfer``；认不出返回 ``None``。"""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    if text == "/":  # 微信的中性交易就是一根斜杠，别被占位符规则吃掉
        return "transfer"
    folded = text.lower()
    for words, name in (
        (_TRANSFER_WORDS, "transfer"),
        (_INCOME_WORDS, "income"),
        (_EXPENSE_WORDS, "expense"),
    ):
        if folded in words or any(word in folded for word in words):
            return name
    return None


def _date(raw: Any) -> str:
    """日期时间 → ``YYYY-MM-DD HH:MM:SS``（没有时分秒就只到日）。"""
    text = str(raw if raw is not None else "").strip()
    match = _DATE_RE.search(text)
    if not match:
        return ""
    date = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    clock = _TIME_RE.search(text[match.end():])
    result = (f"{date} {int(clock.group(1)):02d}:{clock.group(2)}:{clock.group(3) or '00'}"
              if clock else date)
    try:
        datetime.fromisoformat(result)
    except ValueError:
        return ""
    return result


def _compose_note(*parts: str) -> str:
    """把「交易对方 / 商品」拼成一句人话，去重去空，超长截断。"""
    seen: set[str] = set()
    kept: list[str] = []
    for part in parts:
        value = _text(part)
        if not value or value in seen:
            continue
        seen.add(value)
        kept.append(value)
    note = " ".join(kept)
    return note[: NOTE_MAX_CHARS - 1] + "…" if len(note) > NOTE_MAX_CHARS else note


def _income_category(platform_category: str) -> str:
    """收入行的 ``category``：解析侧靠关键词认收入行，这里必须给一个含收入词的值。"""
    return platform_category if is_income_text(platform_category) else "收入"


def _decode_csv(content: str | bytes) -> str:
    """支付宝导出是 GBK；也容忍 BOM 与 UTF-8。"""
    if isinstance(content, str):
        return content
    for encoding in ("utf-8-sig", "gbk", "utf-8"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("gbk", errors="replace")


def _header_index(rows: Sequence[Sequence[Any]], must: Sequence[str]) -> int | None:
    """找表头行：这些单元格**逐字命中**（按值比对，避免命中提示语里的同名字样）。"""
    for index, row in enumerate(rows):
        cells = {_text(cell) for cell in row}
        if all(needle in cells for needle in must):
            return index
    return None


def _column(header: Sequence[Any], *names: str) -> int | None:
    cells = [_text(cell) for cell in header]
    for name in names:  # 先精确，再放宽到子串
        for index, cell in enumerate(cells):
            if cell == name:
                return index
    for name in names:
        for index, cell in enumerate(cells):
            if name and name in cell:
                return index
    return None


def _cell(row: Sequence[Any], index: int | None) -> Any:
    if index is None or index >= len(row):
        return None
    return row[index]


# --------------------------------------------------------------------------- #
# 支付宝：交易明细 CSV
# --------------------------------------------------------------------------- #

_ALIPAY_SUMMARY_RE = re.compile(r"(收入|支出|不计收支)[：:]\s*(\d+)\s*笔\s*([\d,]+(?:\.\d+)?)\s*元")


def parse_alipay_csv(content: str | bytes) -> tuple[list[RawRow], list[str]]:
    """支付宝「交易明细」CSV → 行列表（+ 告警）。解析永不抛异常。"""
    text = _decode_csv(content)
    rows = [row for row in csv.reader(io.StringIO(text))]
    header_index = _header_index(rows, ("交易时间", "收/支"))
    if header_index is None:
        return [], ["支付宝 CSV 找不到表头行（应含「交易时间」与「收/支」两列），未解析到流水。"]

    header = rows[header_index]
    columns = {
        "date": _column(header, "交易时间"),
        "category": _column(header, "交易分类"),
        "counterparty": _column(header, "交易对方"),
        "description": _column(header, "商品说明"),
        "direction": _column(header, "收/支"),
        "amount": _column(header, "金额"),
        "status": _column(header, "交易状态"),
        "id": _column(header, "交易订单号"),
        "merchant_id": _column(header, "商家订单号"),
        "original_id": _column(header, "原交易订单号", "原交易单号"),
    }
    missing = [name for name in ("date", "direction", "amount") if columns[name] is None]
    if missing:
        return [], [f"支付宝 CSV 缺少必需列：{'、'.join(missing)}。"]

    out: list[RawRow] = []
    warnings: list[str] = []
    for offset, row in enumerate(rows[header_index + 1:], start=1):
        if not any(_text(cell) for cell in row):
            continue
        label = f"支付宝第 {header_index + 1 + offset} 行"
        if len(row) != len(header):
            warnings.append(f"{label}：列数与表头不符，已跳过以避免金额错位。")
            continue

        date = _date(_cell(row, columns["date"]))
        if not date:
            warnings.append(f"{label}：日期无效，已跳过。")
            continue

        direction = _direction(_cell(row, columns["direction"]))
        if direction is None:
            warnings.append(f"{label}：「收/支」列为「{_cell(row, columns['direction'])}」，认不出方向，已跳过。")
            continue
        amount = _cents(_cell(row, columns["amount"]))
        if amount is None or amount < 0:
            warnings.append(f"{label}：金额不是非负数，已跳过。")
            continue

        platform_category = _text(_cell(row, columns["category"]))
        status = _text(_cell(row, columns["status"]))
        counterparty = _text(_cell(row, columns["counterparty"]))
        product = _text(_cell(row, columns["description"]))
        note = _compose_note(counterparty, product)

        if direction == "income":
            category = _income_category(platform_category)
        elif direction == "expense":
            # 先查平台类目表，查不到再用关键词补一次（后面还会交给分类器复核）
            category = _expense_category(platform_category, product, f"{counterparty} {product}")
        else:
            category = ""

        out.append(
            RawRow(
                source=SOURCE_ALIPAY,
                date=date,
                direction=direction,
                amount_cents=amount,
                category=category,
                note=note,
                text=f"{counterparty} {product}".strip(),
                counterparty=counterparty,
                product=product,
                platform_category=platform_category,
                status=status,
                kind=platform_category,
                refund_like=status == "交易关闭" or platform_category == "退款",
                transaction_id=_text(_cell(row, columns["id"])),
                merchant_order_id=_text(_cell(row, columns["merchant_id"])),
                original_transaction_id=_text(_cell(row, columns["original_id"])),
                source_row=header_index + 1 + offset,
                category_source="rule" if category and category != "其他" else "unknown",
                review_reason="用途不明，需用户确认" if category == "其他" else "",
            )
        )
    return out, warnings


# --------------------------------------------------------------------------- #
# 微信：账单流水 XLSX
# --------------------------------------------------------------------------- #

_WECHAT_SUMMARY_RE = re.compile(r"(收入|支出|中性交易)[：:]\s*(\d+)\s*笔\s*([\d,]+(?:\.\d+)?)\s*元")


def _keyword_category(text: str) -> str:
    """关键词表推断（**确定性兜底**）：摘不出就返回空。"""
    folded = text.lower()
    for keyword, category in _SORTED_KEYWORDS:
        if keyword in folded:
            return category
    return ""


def _wechat_category(kind: str, text: str) -> str:
    """微信类目：先看「交易类型」，再从文本里按关键词猜；都不中就返回空。"""
    return _expense_category(kind, text, text)


def _expense_category(kind: str, product: str, text: str) -> str:
    # 支付渠道不能说明实际用途；个人姓名也不能作为用途依据。
    if any(word in kind for word in ("转账", "红包", "群收款", "二维码")):
        if any(word in product for word in ("礼金", "份子钱", "压岁钱", "随礼", "赠礼")):
            return "人情往来"
        return _keyword_category(product) or "其他"
    return ALIPAY_CATEGORY_MAP.get(kind) or _keyword_category(text) or "其他"


def parse_wechat_xlsx(source: str | Path | bytes) -> tuple[list[RawRow], list[str]]:
    """微信「账单流水文件」XLSX → 行列表（+ 告警）。解析永不抛异常。"""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise RuntimeError("解析微信 XLSX 需要 openpyxl（pip install openpyxl）。") from exc

    if isinstance(source, bytes):
        book = openpyxl.load_workbook(io.BytesIO(source), data_only=True, read_only=True)
    else:
        book = openpyxl.load_workbook(Path(source), data_only=True, read_only=True)
    try:
        sheet = book[book.sheetnames[0]]
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        book.close()

    header_index = _header_index(rows, ("交易时间", "收/支"))
    if header_index is None:
        return [], ["微信 XLSX 找不到表头行（应含「交易时间」与「收/支」两列），未解析到流水。"]

    header = rows[header_index]
    columns = {
        "date": _column(header, "交易时间"),
        "kind": _column(header, "交易类型"),
        "counterparty": _column(header, "交易对方"),
        "product": _column(header, "商品"),
        "direction": _column(header, "收/支"),
        "amount": _column(header, "金额(元)", "金额"),
        "status": _column(header, "当前状态", "交易状态"),
        "id": _column(header, "交易单号"),
        "merchant_id": _column(header, "商户单号"),
        "original_id": _column(header, "原交易单号", "原交易订单号"),
    }
    missing = [name for name in ("date", "direction", "amount") if columns[name] is None]
    if missing:
        return [], [f"微信 XLSX 缺少必需列：{'、'.join(missing)}。"]

    out: list[RawRow] = []
    warnings: list[str] = []
    for offset, row in enumerate(rows[header_index + 1:], start=1):
        if not any(_text(cell) for cell in row):
            continue
        label = f"微信第 {header_index + 1 + offset} 行"
        date = _date(_cell(row, columns["date"]))
        if not date:
            warnings.append(f"{label}：日期无效，已跳过。")
            continue
        direction = _direction(_cell(row, columns["direction"]))
        if direction is None:
            warnings.append(f"{label}：「收/支」列为「{_cell(row, columns['direction'])}」，认不出方向，已跳过。")
            continue
        amount = _cents(_cell(row, columns["amount"]))
        if amount is None or amount < 0:
            warnings.append(f"{label}：金额不是非负数，已跳过。")
            continue

        kind = _text(_cell(row, columns["kind"]))
        status = _text(_cell(row, columns["status"]))
        counterparty = _text(_cell(row, columns["counterparty"]))
        product = _text(_cell(row, columns["product"]))
        if product in WECHAT_BOILERPLATE_PRODUCTS:
            product = ""
        text = _compose_note(counterparty, product)

        if direction == "income":
            category = _income_category("")
        elif direction == "expense":
            category = _expense_category(kind, product, f"{counterparty} {product}")
        else:
            category = ""

        out.append(
            RawRow(
                source=SOURCE_WECHAT,
                date=date,
                direction=direction,
                amount_cents=amount,
                category=category,
                note=text,
                text=f"{counterparty} {product}".strip(),
                counterparty=counterparty,
                product=product,
                platform_category="",
                status=status,
                kind=kind,
                refund_like="退款" in kind or "退款" in status,
                transaction_id=_text(_cell(row, columns["id"])),
                merchant_order_id=_text(_cell(row, columns["merchant_id"])),
                original_transaction_id=_text(_cell(row, columns["original_id"])),
                source_row=header_index + 1 + offset,
                category_source="rule" if category and category != "其他" else "unknown",
                review_reason="用途不明，需用户确认" if category == "其他" else "",
            )
        )
    return out, warnings


# --------------------------------------------------------------------------- #
# 类目分类：交给分类器（DeepSeek）判定，关键词表只做兜底
# --------------------------------------------------------------------------- #

#: 分类器协议：收一批「待判定的支出行」，返回 ``{行号: 类目}``。
#: 行号是**批内编号**（从 1 开始，只数待判定的支出行），与请求里每行前面的编号一致。
ClassifierFn = Callable[[Sequence[dict[str, Any]]], Mapping[int, str]]

CLASSIFY_BATCH_SIZE = 15


def build_classify_items(rows: Sequence[RawRow], indices: Sequence[int], start: int = 1) -> list[dict[str, Any]]:
    """把待判定的行整理成分类器的入参（**只给判断依据，不给结论**）。"""
    items: list[dict[str, Any]] = []
    for offset, index in enumerate(indices, start=start):
        row = rows[index]
        items.append(
            {
                "index": offset,
                "date": row.date[:10],
                "amount": _yuan(row.amount_cents),
                "counterparty": row.counterparty,
                "product": row.product,
                "platform_category": row.platform_category,
                "kind": row.kind,
                "hint": row.category,  # 关键词表的参考值，可能为空
                "source": SOURCE_LABELS.get(row.source, row.source),
            }
        )
    return items


@dataclass
class ClassifyOutcome:
    """分类结果 + 供人复核的增量信息。"""

    rows: list[RawRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    overrides: list[tuple[RawRow, str, str]] = field(default_factory=list)  # (行, 参考值, 模型判定)
    batches: int = 0
    calls: int = 0
    reused: int = 0

    @property
    def filled(self) -> int:
        """关键词表给不出、被模型补上的笔数。"""
        return sum(1 for _row, old, new in self.overrides if not old and new)

    @property
    def changed(self) -> int:
        """关键词表给了参考值、被模型改判的笔数。"""
        return sum(1 for _row, old, new in self.overrides if old and old != new)


def classify_rows(
    rows: Sequence[RawRow],
    *,
    classifier: ClassifierFn | None = None,
    batch_size: int = CLASSIFY_BATCH_SIZE,
) -> ClassifyOutcome:
    """把**支出行**交给分类器判定类目；收入 / 不计收支不需要类目。

    纪律（与项目里其它模型调用一致）：

    - 模型**只写类目**，金额 / 方向 / 日期一概不经过它；
    - 分批调用，**任何一批失败都只影响那一批**（保留关键词表的参考值），不打断整体；
    - 模型返回值一律过 :func:`~moneyrouter_agent.domain.month.normalize_category` 收口，
      认不出就当没给，绝不写进文档。
    """
    outcome = ClassifyOutcome(rows=list(rows))
    if classifier is None or batch_size < 1:
        return outcome

    groups: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for i, row in enumerate(outcome.rows):
        if row.direction == "expense":
            groups[(row.source, row.counterparty, row.product, row.platform_category, row.kind)].append(i)
    positions = [group[0] for group in groups.values()]
    outcome.reused = sum(len(group) - 1 for group in groups.values())
    for start in range(0, len(positions), batch_size):
        chunk = positions[start : start + batch_size]
        items = build_classify_items(outcome.rows, chunk)
        outcome.batches += 1
        try:
            verdicts = classifier(items) or {}
            outcome.calls += 1
        except Exception as exc:  # noqa: BLE001 - 分类失败只降级，不打断清洗
            outcome.warnings.append(f"类目分类（第 {outcome.batches} 批，{len(items)} 笔）失败，改用关键词表：{exc}")
            continue

        for item in items:
            index = item["index"]
            raw = verdicts.get(index)
            if raw is None:
                outcome.warnings.append(f"第 {outcome.batches} 批缺少编号 {index}，保留规则结果。")
                continue
            category = normalize_category(raw)
            if category == "其他" and str(raw).strip() not in ("", "其他"):
                # 模型给了个认不出的词：不写它，退回关键词表的参考值
                outcome.warnings.append(
                    f"第 {outcome.batches} 批第 {index} 条：模型给了认不出的类目「{raw}」，已退回关键词表。"
                )
                continue
            position = chunk[index - 1]
            current = outcome.rows[position]
            # 没有用途信息的个人转账不允许模型凭支付方式或姓名猜类目。
            if (any(word in current.kind for word in ("转账", "红包", "群收款", "二维码"))
                    and not current.product and category != "其他"):
                category = "其他"
            if category == "人情往来" and not any(
                word in current.product for word in ("礼金", "份子钱", "压岁钱", "随礼", "赠礼")
            ):
                category = "其他"
            outcome.rows[position] = replace(
                current, category=category, category_source="model",
                review_reason="用途不明，需用户确认" if category == "其他" else "",
            )
            if category == current.category:
                continue
            outcome.overrides.append((current, current.category, category))
    for group in groups.values():
        representative = outcome.rows[group[0]]
        for position in group[1:]:
            outcome.rows[position] = replace(
                outcome.rows[position], category=representative.category,
                category_source=representative.category_source,
                review_reason=representative.review_reason,
            )
    return outcome




# --------------------------------------------------------------------------- #
# 汇总与自检
# --------------------------------------------------------------------------- #


def _totals(rows: Iterable[RawRow]) -> dict[str, tuple[int, int]]:
    """按方向统计 ``(笔数, 分)``。"""
    counts: Counter[str] = Counter()
    amounts: Counter[str] = Counter()
    for row in rows:
        counts[row.direction] += 1
        amounts[row.direction] += row.amount_cents
    return {name: (counts[name], amounts[name]) for name in DIRECTIONS if counts[name]}


def read_platform_summary(text: str, source: str) -> dict[str, tuple[int, int]]:
    """从导出文件自带的汇总行里读出平台口径的 ``(笔数, 分)``，用于自检。

    支付宝按「不计收支」、微信按「中性交易」——都归到 ``transfer``。
    """
    pattern = _ALIPAY_SUMMARY_RE if source == SOURCE_ALIPAY else _WECHAT_SUMMARY_RE
    mapping = {"收入": "income", "支出": "expense", "不计收支": "transfer", "中性交易": "transfer"}
    out: dict[str, tuple[int, int]] = {}
    for name, count, amount in pattern.findall(text or ""):
        cents = _cents(amount.replace(",", ""))
        if cents is not None:
            out[mapping[name]] = (int(count), cents)
    return out


@dataclass
class CleanOutcome:
    """一次清洗的全部结果。"""

    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    rows: list[RawRow] = field(default_factory=list)
    rows_by_source: dict[str, list[RawRow]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    platform_totals: dict[str, dict[str, tuple[int, int]]] = field(default_factory=dict)
    source_files: dict[str, str] = field(default_factory=dict)
    refund_policy: str = REFUND_KEEP
    duplicates: list[tuple[str, str, int]] = field(default_factory=list)
    classifier_name: str = "keyword"
    classify: ClassifyOutcome = field(default_factory=ClassifyOutcome)
    coverage_ranges: dict[str, tuple[str, str]] = field(default_factory=dict)

    def source_totals(self, source: str) -> dict[str, tuple[int, int]]:
        return _totals(self.rows_by_source.get(source, []))

    def category_totals(self) -> Counter[str]:
        """各类目的支出笔数与金额（只看支出行）。"""
        totals: Counter[str] = Counter()
        for row in self.rows:
            if row.direction == "expense":
                totals[row.category or "（未归类）"] += row.amount_cents
        return totals

    def unmapped(self, source: str) -> Counter[str]:
        """仍未归类的支出行，按「交易对方/类目」计数——这是补表或调提示词的入口。"""
        counter: Counter[str] = Counter()
        for row in self.rows_by_source.get(source, []):
            if row.direction != "expense" or row.category:
                continue
            counter[row.platform_category or row.text or "（无对方信息）"] += 1
        return counter


def build_documents(
    rows: Sequence[RawRow], *, refund_policy: str = REFUND_KEEP, period: str | None = None,
    coverage_ranges: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """把行切成**按月**的规范文档：``{period: 文档}``。

    ``refund_policy="transfer"`` 时，退款 / 交易关闭这类行改判为 ``transfer``（不计收支）。
    """
    if refund_policy not in REFUND_POLICIES:
        raise ValueError(f"未知的退款策略：{refund_policy}（可选 {'/'.join(REFUND_POLICIES)}）")
    if period is not None and not _PERIOD_RE.match(period):
        raise ValueError(f"期间格式应为 YYYY-MM：{period}")

    original_rows = list(rows)
    if refund_policy == REFUND_NET:
        from .bill_accounting import net_rows
        rows = net_rows(rows)
    if refund_policy == REFUND_TRANSFER:
        rows = [
            replace(row, direction="transfer", category="")
            if row.refund_like and row.direction != "transfer"
            else row
            for row in rows
        ]

    grouped: dict[str, list[RawRow]] = defaultdict(list)
    for row in rows:
        month = row.period
        if not _PERIOD_RE.match(month):
            continue
        if period is None or month == period:
            grouped[month].append(row)

    documents: dict[str, dict[str, Any]] = {}
    for month in sorted(grouped):
        cashflow: list[dict[str, Any]] = []
        for row in sorted(grouped[month], key=lambda item: item.date):
            item: dict[str, Any] = {
                "date": row.date,
                "direction": row.direction,
                "amount": _yuan(row.amount_cents),
                "source": row.source,
                "source_row": row.source_row,
                "transaction_id": row.transaction_id,
                "category_source": row.category_source,
            }
            if row.category:
                item["category"] = row.category
            if row.note:
                item["note"] = row.note
            if row.review_reason or (row.refund_like and refund_policy != REFUND_NET):
                item["review_reason"] = row.review_reason or "退款相关流水，核对原单及退款月份"
            if row.gross_amount_cents is not None:
                item["original_amount"] = _yuan(row.gross_amount_cents)
                item["original_direction"] = row.original_direction
                item["accounting_note"] = row.accounting_note
            cashflow.append(item)
        ranges = coverage_ranges or {}
        month_start = month + "-01"
        month_end = month + f"-{calendar.monthrange(int(month[:4]), int(month[5:]))[1]:02d}"
        sources = {row.source for row in grouped[month]}
        known = [ranges[s] for s in sources if s in ranges]
        complete = all(start <= month_start and end >= month_end for start, end in known) if len(known) == len(sources) else None
        documents[month] = {"period": month, "cashflow": cashflow,
                            "accounting_basis": "net_consumption_original_month" if refund_policy == REFUND_NET else "platform",
                            "coverage": {"complete": complete,
                                         "start": max([month_start] + [r[0] for r in known]),
                                         "end": min([month_end] + [r[1] for r in known])}}
        if refund_policy == REFUND_NET:
            documents[month]["raw_cashflow"] = [
                {"date": r.date, "direction": r.direction, "amount": _yuan(r.amount_cents),
                 "source": r.source, "transaction_id": r.transaction_id}
                for r in original_rows if r.period == month]
    return documents


def documents_to_text(documents: dict[str, dict[str, Any]]) -> dict[str, str]:
    """文档 → JSON 文本（统一 ``ensure_ascii=False``）。"""
    return {
        period: json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        for period, document in documents.items()
    }


def _find_duplicates(rows: Sequence[RawRow]) -> list[tuple[str, str, int]]:
    """跨来源疑似重复（同分钟 + 同金额 + 同方向）：只报告，绝不自动删。"""
    seen: dict[tuple[str, int, str], list[RawRow]] = defaultdict(list)
    for row in rows:
        seen[(row.date[:16], row.amount_cents, row.direction)].append(row)
    out: list[tuple[str, str, int]] = []
    for (date, amount, direction), group in seen.items():
        sources = {row.source for row in group}
        if len(sources) > 1:
            who = "、".join(SOURCE_LABELS.get(s, s) for s in sorted(sources))
            out.append((f"{date} {direction} {format_yuan(amount)} 元", who, len(group)))
    return sorted(out)


def clean(
    *,
    alipay: str | bytes | None = None,
    wechat: str | bytes | None = None,
    refund_policy: str = REFUND_KEEP,
    period: str | None = None,
    source_files: dict[str, str] | None = None,
    classifier: ClassifierFn | None = None,
    classifier_name: str = "keyword",
    batch_size: int = CLASSIFY_BATCH_SIZE,
    category_overrides: Mapping[str, str] | None = None,
) -> CleanOutcome:
    """读入原始账单 → 分类 → 产出规范文档。

    ``alipay`` 收**内容**（``str`` 或原始 ``bytes``，CSV 是 GBK）；``wechat`` 收**路径**或
    ``bytes``（XLSX 是压缩包，只能给路径或字节流）——路径解析这类脏活由 CLI 负责。

    ``classifier`` 给了就**由它判定类目**（关键词表退居兜底与参考）；不给就是纯关键词模式。
    """
    if batch_size < 1:
        raise ValueError("batch_size 必须为正整数")
    if refund_policy not in REFUND_POLICIES:
        raise ValueError("未知的退款策略")
    if period is not None and not _PERIOD_RE.fullmatch(period):
        raise ValueError("期间格式应为 YYYY-MM")
    for category in (category_overrides or {}).values():
        if category not in SPEND_CATEGORIES:
            raise ValueError("人工分类必须使用固定类目")
    outcome = CleanOutcome(
        refund_policy=refund_policy,
        source_files=dict(source_files or {}),
        classifier_name=classifier_name,
    )

    if alipay is not None:
        text = alipay if isinstance(alipay, str) else _decode_csv(alipay)
        rows, warnings = parse_alipay_csv(alipay)
        outcome.rows_by_source[SOURCE_ALIPAY] = rows
        outcome.rows.extend(rows)
        outcome.warnings.extend(warnings)
        outcome.platform_totals[SOURCE_ALIPAY] = read_platform_summary(text, SOURCE_ALIPAY)
        dates = re.findall(r"(?:起始时间|终止时间)[：:]\s*\[?(\d{4}-\d{2}-\d{2})", text)
        if len(dates) >= 2:
            outcome.coverage_ranges[SOURCE_ALIPAY] = (dates[0], dates[1])

    if wechat is not None:
        rows, warnings = parse_wechat_xlsx(wechat)
        outcome.rows_by_source[SOURCE_WECHAT] = rows
        outcome.rows.extend(rows)
        outcome.warnings.extend(warnings)
        outcome.platform_totals[SOURCE_WECHAT] = read_wechat_summary(wechat)
        import openpyxl
        book = openpyxl.load_workbook(io.BytesIO(wechat) if isinstance(wechat, bytes) else Path(wechat), data_only=True, read_only=True)
        try:
            header_text = " ".join(_text(c) for r in book.active.iter_rows(max_row=20, values_only=True) for c in r)
        finally:
            book.close()
        dates = re.findall(r"(?:起始时间|终止时间)[：:]\s*\[?(\d{4}-\d{2}-\d{2})", header_text)
        if len(dates) >= 2:
            outcome.coverage_ranges[SOURCE_WECHAT] = (dates[0], dates[1])

    selected = [row for row in outcome.rows if period is None or row.period == period]
    outcome.classify = classify_rows(selected, classifier=classifier, batch_size=batch_size)
    if classifier is not None:
        classified = {(row.source, row.source_row): row for row in outcome.classify.rows}
        outcome.rows = [classified.get((row.source, row.source_row), row) for row in outcome.rows]
        outcome.warnings.extend(outcome.classify.warnings)
        outcome.rows_by_source = {
            source: [row for row in outcome.rows if row.source == source]
            for source in outcome.rows_by_source
        }

    overrides = category_overrides or {}
    matched: set[str] = set()
    for i, row in enumerate(outcome.rows):
        key = f"{row.source}:{row.transaction_id}" if row.transaction_id else f"{row.source}:row:{row.source_row}"
        if row.direction == "expense" and key in overrides:
            outcome.rows[i] = replace(row, category=overrides[key], category_source="user", review_reason="")
            matched.add(key)
    if overrides.keys() - matched:
        outcome.warnings.append(f"有 {len(overrides.keys() - matched)} 条人工分类未匹配到支出流水。")
    outcome.rows_by_source = {source: [row for row in outcome.rows if row.source == source]
                              for source in outcome.rows_by_source}
    outcome.documents = build_documents(outcome.rows, refund_policy=refund_policy, period=period, coverage_ranges=outcome.coverage_ranges)
    outcome.duplicates = _find_duplicates(outcome.rows)
    return outcome


def read_wechat_summary(path: Path | str | bytes) -> dict[str, tuple[int, int]]:
    """微信的汇总行在 XLSX 的单元格里，挑出含「笔记录 / 元」的单元格拼起来再匹配。"""
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - 环境缺依赖
        return {}
    book = openpyxl.load_workbook(io.BytesIO(path) if isinstance(path, bytes) else Path(path), data_only=True, read_only=True)
    try:
        sheet = book[book.sheetnames[0]]
        chunks = [
            _text(cell)
            for row in sheet.iter_rows(max_row=20, values_only=True)
            for cell in row
            if _text(cell)
        ]
    finally:
        book.close()
    return read_platform_summary(" ".join(chunks), SOURCE_WECHAT)


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

_DIRECTION_LABELS = {"income": "收入", "expense": "支出", "transfer": "不计收支"}


def _totals_line(totals: dict[str, tuple[int, int]]) -> str:
    if not totals:
        return "（无）"
    return "  ".join(
        f"{_DIRECTION_LABELS[name]} {totals[name][0]} 笔 / {format_yuan(totals[name][1])} 元"
        for name in DIRECTIONS
        if name in totals
    )


def render_report(outcome: CleanOutcome, *, verify: dict[str, Any] | None = None) -> str:
    """把一次清洗的结果写成可读报告（含**与平台自带汇总对账**）。"""
    lines: list[str] = ["=" * 68, "账单清洗报告", "=" * 68]

    for source in (SOURCE_ALIPAY, SOURCE_WECHAT):
        rows = outcome.rows_by_source.get(source)
        if rows is None:
            continue
        label = SOURCE_LABELS[source]
        path = outcome.source_files.get(source)
        lines.append("")
        lines.append(f"【{label}】{path or ''}")
        lines.append(f"  解析到 {len(rows)} 行：{_totals_line(_totals(rows))}")

        platform = outcome.platform_totals.get(source) or {}
        if platform:
            lines.append(f"  回单自带汇总：{_totals_line(platform)}")
            for name in DIRECTIONS:
                mine, theirs = _totals(rows).get(name), platform.get(name)
                if mine != theirs:
                    mine, theirs = mine or (0, 0), theirs or (0, 0)
                    lines.append(
                        f"    ⚠ {_DIRECTION_LABELS[name]}对不上：明细累加 {mine[0]} 笔 / "
                        f"{format_yuan(mine[1])} 元，回单称 {theirs[0]} 笔 / {format_yuan(theirs[1])} 元。"
                        "（回单自己也声明「明细直接累加可能和统计金额不一致」，以实际交易为准）"
                    )

        unmapped = outcome.unmapped(source)
        if unmapped:
            total = sum(unmapped.values())
            lines.append(f"  仍未归类支出 {total} 笔，最多的几项（可补进关键词表）：")
            for name, count in unmapped.most_common(8):
                lines.append(f"    {count:3} 笔  {name}")

    categories = outcome.category_totals()
    if categories:
        lines.append("")
        lines.append(f"【类目分布】由「{outcome.classifier_name}」判定（支出金额，共 {format_yuan(sum(categories.values()))} 元）")
        for name, cents in categories.most_common():
            share = cents / max(1, sum(categories.values()))
            lines.append(f"  {name:<6} {format_yuan(cents):>12} 元  {share:5.1%}")

    classify = outcome.classify
    if classify.batches:
        lines.append("")
        lines.append(
            f"【分类器】{outcome.classifier_name}：{classify.batches} 批 / {classify.calls} 次成功调用；"
            f"补空 {classify.filled} 笔，改判 {classify.changed} 笔，同用途复用 {classify.reused} 笔"
        )
        for row, old, new in classify.overrides[:10]:
            lines.append(
                f"  {row.date[:10]} {row.note[:28]:<28} {old or '（空）'} → {new}"
            )
        if len(classify.overrides) > 10:
            lines.append(f"  …另有 {len(classify.overrides) - 10} 笔改动")

    refunds = [row for row in outcome.rows if row.refund_like]
    if refunds:
        income_cents = sum(r.amount_cents for r in refunds if r.direction == "income")
        expense_cents = sum(r.amount_cents for r in refunds if r.direction == "expense")
        lines.append("")
        lines.append(
            f"【退款 / 交易关闭】{len(refunds)} 笔（记为收入 {format_yuan(income_cents)} 元、"
            f"支出 {format_yuan(expense_cents)} 元）——当前策略：{outcome.refund_policy}"
        )
        if outcome.refund_policy == REFUND_KEEP:
            lines.append("  两家口径不一致：微信把退款记成收入，支付宝把退款记为不计收支。")
            lines.append("  退款跨月、部分退款及仅单边记录会影响期间净额；不能保证两端抵消。")
        else:
            lines.append("  transfer 为排除退款相关流水的兼容策略，不是净额核算；部分退款或跨月交易需复核。")

    if outcome.duplicates:
        lines.append("")
        lines.append(f"【疑似跨来源重复】{len(outcome.duplicates)} 组（同分钟 + 同金额 + 同方向，仅提示不自动删）")
        for key, who, count in outcome.duplicates:
            lines.append(f"  {key}  ← {who}（{count} 笔）")

    if outcome.warnings:
        lines.append("")
        lines.append(f"【告警】{len(outcome.warnings)} 条")
        for warning in outcome.warnings[:20]:
            lines.append(f"  - {warning}")
        if len(outcome.warnings) > 20:
            lines.append(f"  …另有 {len(outcome.warnings) - 20} 条")

    lines.append("")
    lines.append("【产出文档】" + ("、".join(sorted(outcome.documents)) or "（空）"))
    for period, document in sorted(outcome.documents.items()):
        rows = document["cashflow"]
        income = sum(-r["amount"] if r["direction"] == "expense" else r["amount"]
                     for r in rows if r["direction"] != "transfer")
        lines.append(
            f"  {period}: {len(rows)} 笔，其中收入 {sum(1 for r in rows if r['direction'] == 'income')} 笔、"
            f"支出 {sum(1 for r in rows if r['direction'] == 'expense')} 笔、"
            f"不计收支 {sum(1 for r in rows if r['direction'] == 'transfer')} 笔；结余 {income:,.2f} 元"
        )

    if verify:
        lines.append("")
        lines.append("【回读校验】用 tools.dossier 解析产出文档")
        for period in sorted(verify):
            info = verify[period]
            lines.append(
                f"  {period}: 收入 {format_yuan(info['income_cents'])} 元 / "
                f"支出 {format_yuan(info['spend_cents'])} 元 / 结余 {format_yuan(info['balance_cents'])} 元"
                f"（不计收支 {info['skipped_rows']} 笔）"
            )
            for warning in info["warnings"][:8]:
                lines.append(f"    ⚠ {warning}")
            if len(info["warnings"]) > 8:
                lines.append(f"    …另有 {len(info['warnings']) - 8} 条（多为「类目归到其他」，属正常）")

    lines.append("=" * 68)
    return "\n".join(lines)
