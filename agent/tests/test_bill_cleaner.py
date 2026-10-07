"""账单清洗器：平台「收/支」列 → 规范方向、类目查表 / 关键词推断、按月切分、回读校验。

真实账单的两个样本（支付宝 CSV 是 GBK、带前言；微信 XLSX 表头在第 18 行）在这里
都用**内存里造的小样本**复刻，不依赖桌面上的真实文件；另有一组"真文件在就跑"的冒烟测试。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from moneyrouter_agent.domain.month import SPEND_CATEGORIES
from moneyrouter_agent.prompts.classify import CLASSIFY_SYSTEM, render_classify_request
from moneyrouter_agent.tools.bill_classify import BillClassification, CategoryVerdict, make_deepseek_classifier
from moneyrouter_agent.tools.bill_cleaner import (
    ALIPAY_CATEGORY_MAP,
    REFUND_KEEP,
    REFUND_TRANSFER,
    SOURCE_ALIPAY,
    SOURCE_WECHAT,
    build_classify_items,
    build_documents,
    classify_rows,
    clean,
    parse_alipay_csv,
    parse_wechat_xlsx,
    read_wechat_summary,
    render_report,
    RawRow,
)
from moneyrouter_agent.tools.bills import parse_bill

ALIPAY_SAMPLE = """导出信息：
姓名：某某
起始时间：[2026-07-07 00:00:00]    终止时间：[2026-10-07 23:59:59]
共4笔记录
收入：1笔 140.08元
支出：2笔 100.00元
不计收支：1笔 20.00元

特别提示：
1.本回单内容可表明支付宝受理了相应支付交易申请；

------------------------支付宝支付科技有限公司  电子客户回单------------------------
交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注,
2026-09-01 12:00:00,餐饮美食,遇见小面,/,招牌冒菜,支出,27.63,交通银行储蓄卡(0687),交易成功,2026090123001\t,202609010001	,,
2026-09-02 09:10:00,交通出行,广州地铁,/,搭乘广州地铁,支出,2.00,余额宝,交易成功,2026090223001\t,202609020001	,,
2026-09-03 10:00:00,收入,某某公司,/,工资,收入,140.08,余额,交易成功,2026090323001\t,202609030001	,,
2026-09-04 11:00:00,账户存取,本人,/,提现,不计收支,20.00,余额,交易成功,2026090423001\t,202609040001	,,
"""


def _wechat_book(path) -> str:
    """造一个和真实导出一致结构的微信账单 XLSX。"""
    book = openpyxl.Workbook()
    sheet = book.active
    for line in (
        "微信支付账单明细",
        "微信昵称：[某某]",
        "起始时间：[2026-07-07 00:00:00] 终止时间：[2026-10-07 21:13:40]",
        "导出类型：[全部账单]",
        "导出时间：[2026-10-07 21:13:40]",
        "",
        "共4笔记录",
        "收入：1笔 30.00元",
        "支出：2笔 63.50元",
        "中性交易：1笔 5,000.00元",
    ):
        sheet.append([line])
    sheet.append([])
    sheet.append(["----------------------微信支付账单明细列表--------------------"])
    sheet.append(
        ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)",
         "支付方式", "当前状态", "交易单号", "商户单号", "备注"]
    )
    sheet.append(["2026-09-05 12:00:00", "商户消费", "华南理工大学", "D5食堂6.广式烧腊",
                  "支出", 16, "零钱", "支付成功", "45001", "10001", "/"])
    sheet.append(["2026-09-06 18:00:00", "转账", "吴桐 (Apfel)", "转账备注:微信转账",
                  "支出", 47.5, "零钱", "对方已收钱", "45002", "10002", "/"])
    sheet.append(["2026-09-07 09:00:00", "转账", "妈妈 (zhurong)", "转账备注:微信转账",
                  "收入", 30, "零钱", "已存入零钱", "45003", "10003", "/"])
    sheet.append(["2026-09-08 10:00:00", "购买理财通", "理财通", "格林泓鑫纯债债券C(006185)",
                  "/", 5000, "交通银行储蓄卡(0687)", "支付成功", "45004", "10004", "/"])
    sheet.append(["2026-09-09 20:00:00", "扫二维码付款", "玉枝", "收款方备注:二维码收款",
                  "支出", 5, "零钱", "已转账", "45005", "10005", "/"])
    book.save(path)
    return str(path)


@pytest.fixture()
def wechat_path(tmp_path):
    return _wechat_book(tmp_path / "微信账单.xlsx")


# --------------------------------------------------------------------------- #
# 支付宝：方向 / 金额 / 类目
# --------------------------------------------------------------------------- #


def test_alipay_direction_amount_and_category():
    rows, warnings = parse_alipay_csv(ALIPAY_SAMPLE)

    assert warnings == []
    assert [row.direction for row in rows] == ["expense", "expense", "income", "transfer"]
    assert rows[0].amount_cents == 2763  # 27.63 元
    assert rows[0].category == "餐饮"
    assert rows[1].category == "交通"
    assert rows[2].category == "收入"  # 收入行必须带含收入词的类目，否则解析侧会丢掉这笔钱
    assert rows[3].category == ""  # 不计收支的行不需要类目
    assert rows[0].note == "遇见小面 招牌冒菜"
    assert rows[0].date == "2026-09-01 12:00:00"


def test_alipay_bytes_are_decoded_as_gbk():
    rows, _ = parse_alipay_csv(ALIPAY_SAMPLE.encode("gbk"))
    assert [row.category for row in rows] == ["餐饮", "交通", "收入", ""]


def test_alipay_platform_summary_is_read_back():
    outcome = clean(alipay=ALIPAY_SAMPLE)
    assert outcome.platform_totals[SOURCE_ALIPAY]["expense"] == (2, 10_000)
    assert outcome.platform_totals[SOURCE_ALIPAY]["income"] == (1, 14_008)


def test_alipay_row_with_zero_amount_is_preserved_for_reconciliation():
    text = ALIPAY_SAMPLE.replace(
        "2026-09-02 09:10:00,交通出行,广州地铁,/,搭乘广州地铁,支出,2.00,余额宝,交易成功",
        "2026-09-02 09:10:00,交通出行,广州地铁,/,搭乘广州地铁,支出,0.00,余额宝,交易成功",
    )
    rows, warnings = parse_alipay_csv(text)
    assert len(rows) == 4
    assert rows[1].amount_cents == 0
    assert warnings == []


def test_alipay_without_header_degrades_cleanly():
    rows, warnings = parse_alipay_csv("这不是账单\n")
    assert rows == []
    assert warnings and "表头" in warnings[0]


# --------------------------------------------------------------------------- #
# 微信：类目靠「交易类型 + 关键词」，斜杠表示不计收支
# --------------------------------------------------------------------------- #


def test_wechat_rows_and_slash_transfer(wechat_path):
    rows, warnings = parse_wechat_xlsx(wechat_path)

    assert warnings == []
    assert len(rows) == 5
    assert [row.direction for row in rows] == ["expense", "expense", "income", "transfer", "expense"]
    assert rows[0].amount_cents == 1600
    assert rows[2].category == "收入"


def test_wechat_category_by_type_and_keyword(wechat_path):
    rows, _ = parse_wechat_xlsx(wechat_path)
    by_note = {row.note: row for row in rows}

    # 交易类型「转账」直接定人情往来
    assert by_note["吴桐 (Apfel)"].category == "其他"
    # 关键词：食堂 -> 餐饮
    assert by_note["华南理工大学 D5食堂6.广式烧腊"].category == "餐饮"
    # 关键词：对方是陌生个人 → 不硬塞「其他」，留空交给解析侧再猜
    assert by_note["玉枝"].category == "其他"


def test_wechat_boilerplate_product_does_not_poison_category(wechat_path):
    """「收款方备注:二维码收款」这类样板文本必须剔除，否则会被判成人情往来。"""
    rows, _ = parse_wechat_xlsx(wechat_path)
    notes = [row.note for row in rows]
    assert "玉枝 收款方备注:二维码收款" not in notes
    assert "玉枝" in notes


def test_wechat_platform_summary_is_read_back(wechat_path):
    outcome = clean(wechat=wechat_path)
    assert outcome.platform_totals[SOURCE_WECHAT]["expense"] == (2, 6350)
    assert outcome.platform_totals[SOURCE_WECHAT]["transfer"] == (1, 500_000)


# --------------------------------------------------------------------------- #
# 按月切分 / 退款策略
# --------------------------------------------------------------------------- #


def test_documents_are_split_by_month_and_sorted():
    rows, _ = parse_alipay_csv(ALIPAY_SAMPLE)
    mixed = [replace(rows[0], date="2026-08-01 12:00:00"), *rows[1:]]
    documents = build_documents(mixed)

    assert sorted(documents) == ["2026-08", "2026-09"]
    assert documents["2026-08"]["period"] == "2026-08"
    assert [item["date"] for item in documents["2026-08"]["cashflow"]] == ["2026-08-01 12:00:00"]
    assert len(documents["2026-09"]["cashflow"]) == 3
    assert all(item["date"].startswith("2026-09") for item in documents["2026-09"]["cashflow"])


def test_period_filter_keeps_only_one_month(wechat_path):
    outcome = clean(wechat=wechat_path, period="2026-09")
    assert list(outcome.documents) == ["2026-09"]
    assert len(outcome.documents["2026-09"]["cashflow"]) == 5


def test_refund_policy_transfer_moves_refund_rows_out_of_totals():
    text = ALIPAY_SAMPLE.replace("账户存取,本人,/,提现,不计收支", "退款,某某店,/,退款-订单,支出")
    kept = clean(alipay=text, refund_policy=REFUND_KEEP)
    moved = clean(alipay=text, refund_policy=REFUND_TRANSFER)

    kept_doc = kept.documents["2026-09"]["cashflow"]
    moved_doc = moved.documents["2026-09"]["cashflow"]
    # 样本里那笔「账户存取 / 不计收支」被换成了「退款 / 支出」：keep 时算支出，
    # transfer 策略下改判不计收支——总笔数不变，只是从支出挪走一笔。
    assert len(kept_doc) == len(moved_doc) == 4
    assert [r["direction"] for r in kept_doc].count("expense") == 3
    assert [r["direction"] for r in moved_doc].count("expense") == 2
    assert [r["direction"] for r in moved_doc].count("transfer") == 1


def test_unknown_refund_policy_is_rejected():
    with pytest.raises(ValueError):
        build_documents([], refund_policy="whatever")


def test_alipay_category_map_only_yields_enums():
    assert set(ALIPAY_CATEGORY_MAP.values()) <= set(SPEND_CATEGORIES)
    assert "其他" not in ALIPAY_CATEGORY_MAP.values()


# --------------------------------------------------------------------------- #
# 与下游的契约：产出文档必须能被 tools.dossier 接住
# --------------------------------------------------------------------------- #


def test_output_document_feeds_the_real_parser(wechat_path):
    outcome = clean(wechat=wechat_path)
    document = outcome.documents["2026-09"]
    parsed = parse_bill(json.dumps(document), period="2026-09")

    by_cat = {category.category: category.amount_cents for category in parsed.categories}
    assert parsed.income is not None and parsed.income.amount_cents == 3000
    assert by_cat["餐饮"] == 1600
    assert parsed.skipped_rows == 1  # 理财通那笔是「不计收支」
    assert parsed.period == "2026-09"


def test_income_rows_are_not_silently_dropped():
    """收入行没带含收入词的 category 时，解析侧会把这笔钱整个丢掉——这里锁住这个契约。"""
    outcome = clean(alipay=ALIPAY_SAMPLE)
    parsed = parse_bill(json.dumps(outcome.documents["2026-09"]), period="2026-09")
    assert parsed.income is not None
    assert parsed.income.amount_cents == 14_008


def test_descending_source_order_becomes_ascending_output():
    rows, _ = parse_alipay_csv(ALIPAY_SAMPLE)
    reversed_rows = list(reversed(rows))
    documents = build_documents(reversed_rows)
    dates = [item["date"] for item in documents["2026-09"]["cashflow"]]
    assert dates == sorted(dates)


# --------------------------------------------------------------------------- #
# 类目分类：交给分类器（DeepSeek），关键词表退居兜底
# --------------------------------------------------------------------------- #


def _verdict(index: int, category: str) -> CategoryVerdict:
    return CategoryVerdict(index=index, category=category)


class _FakeStructuredModel(BaseChatModel):
    """按脚本返回结构化结果并记录收到的消息（**不联网**），替掉真实 DeepSeek。"""

    verdicts: list[Any] = Field(default_factory=list)
    seen_messages: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-classify"

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="{}"))])

    def with_structured_output(  # type: ignore[override]
        self, schema: Any, *, method: str = "function_calling", include_raw: bool = False, **kw: Any
    ):
        assert include_raw is True, "runner 必须用 include_raw，避免解析失败直接抛异常"

        def _run(messages: list[BaseMessage]) -> dict[str, Any]:
            self.seen_messages.append(list(messages))
            return {
                "raw": AIMessage(content="{}"),
                "parsed": BillClassification(items=list(self.verdicts)),
                "parsing_error": None,
            }

        return RunnableLambda(_run)


def test_build_classify_items_only_carries_evidence():
    rows, _ = parse_alipay_csv(ALIPAY_SAMPLE)
    items = build_classify_items(rows, [0, 1])

    assert [item["index"] for item in items] == [1, 2]
    assert items[0]["counterparty"] == "遇见小面"
    assert items[0]["product"] == "招牌冒菜"
    assert items[0]["platform_category"] == "餐饮美食"
    assert items[0]["hint"] == "餐饮"  # 关键词/平台类目的参考值
    assert items[0]["amount"] == 27.63
    assert items[0]["date"] == "2026-09-01"


def test_classifier_fills_blanks_and_overrides_wrong_hints():
    rows, _ = parse_alipay_csv(ALIPAY_SAMPLE)  # 2 支出 + 1 收入 + 1 不计收支

    def fake(items):
        # 第 1 条关键词已判「餐饮」→ 不返回即保持；第 2 条改成 娱乐社交
        return {2: "娱乐社交"}

    outcome = classify_rows(rows, classifier=fake)

    assert [row.direction for row in outcome.rows] == ["expense", "expense", "income", "transfer"]
    assert outcome.rows[0].category == "餐饮"      # 模型没动它
    assert outcome.rows[1].category == "娱乐社交"  # 被改判
    assert outcome.rows[2].category == "收入"      # 收入行不进分类器
    assert len(outcome.overrides) == 1
    assert outcome.changed == 1 and outcome.filled == 0


def test_classifier_fills_rows_keywords_could_not_classify(wechat_path):
    rows, _ = parse_wechat_xlsx(wechat_path)
    assert [row.category for row in rows if row.note == "玉枝"] == ["其他"]

    # 编号只数「待判定的支出行」：样本里 3 笔支出，玉枝是第 3 笔
    outcome = classify_rows(rows, classifier=lambda items: {3: "人情往来"})

    assert outcome.rows[4].category == "其他"
    assert outcome.rows[4].review_reason


def test_classifier_never_touches_amount_direction_or_date():
    rows, _ = parse_alipay_csv(ALIPAY_SAMPLE)

    def bogus(items):
        return {1: "交通", 2: "购物", 3: "餐饮", 4: "餐饮", 99: "餐饮"}  # 越界编号要被忽略

    outcome = classify_rows(rows, classifier=bogus)
    for before, after in zip(rows, outcome.rows):
        assert (before.amount_cents, before.direction, before.date) == (
            after.amount_cents, after.direction, after.date
        )
    assert outcome.rows[3].direction == "transfer"  # 不计收支的行没被碰


def test_classifier_unknown_category_falls_back_to_keyword():
    rows, _ = parse_alipay_csv(ALIPAY_SAMPLE)
    outcome = classify_rows(rows, classifier=lambda items: {1: "会飞的钱"})

    assert outcome.rows[0].category == "餐饮"  # 保留关键词参考值
    assert any("认不出的类目" in warning for warning in outcome.warnings)


def test_classifier_failure_degrades_per_batch_without_raising():
    rows, _ = parse_alipay_csv(ALIPAY_SAMPLE)

    def broken(items):
        raise RuntimeError("net down")

    outcome = classify_rows(rows, classifier=broken, batch_size=1)

    assert outcome.calls == 0
    assert len(outcome.warnings) == 2  # 两笔支出、两批都失败
    assert outcome.rows[0].category == "餐饮"  # 退回关键词表，仍然有类目


def test_classifier_batches_restart_numbering(wechat_path):
    rows, _ = parse_wechat_xlsx(wechat_path)
    seen: list[list[int]] = []

    def spy(items):
        seen.append([item["index"] for item in items])
        return {}

    outcome = classify_rows(rows, classifier=spy, batch_size=2)

    assert seen == [[1, 2], [1]]  # 样本只有 3 笔支出，序号每批从 1 重新开始
    assert outcome.batches == 2


def test_clean_with_classifier_rebuilds_documents(wechat_path):
    outcome = clean(
        wechat=wechat_path,
        classifier=lambda items: {1: "娱乐社交"},
        classifier_name="DeepSeek",
    )

    assert outcome.classifier_name == "DeepSeek"
    assert outcome.documents["2026-09"]["cashflow"][0]["category"] == "娱乐社交"
    report = render_report(outcome)
    assert "【分类器】DeepSeek" in report
    assert "【类目分布】" in report


# --------------------------------------------------------------------------- #
# 分类提示词契约
# --------------------------------------------------------------------------- #


def test_classify_prompt_lists_all_categories_and_hint_semantics():
    for category in SPEND_CATEGORIES:
        assert category in CLASSIFY_SYSTEM, f"提示词缺少类目：{category}"
    assert "参考" in CLASSIFY_SYSTEM  # 关键词表给的参考值可能为空或错
    assert "不要硬猜" in CLASSIFY_SYSTEM
    assert "每一笔都要给出结果" in CLASSIFY_SYSTEM


def test_classify_prompt_keeps_schema_out():
    """字段与 JSON 形状由 schema 在模型层注入，prompt 里不出现字段清单。"""
    for field in ("items", "index", "category"):
        assert field not in CLASSIFY_SYSTEM, f"prompt 里混入了字段名：{field}"


def test_classify_request_renders_evidence_and_numbers():
    text = render_classify_request(
        [
            {
                "index": 1,
                "date": "2026-09-05",
                "amount": 16.0,
                "counterparty": "华南理工大学",
                "product": "D5食堂6.广式烧腊",
                "platform_category": "",
                "kind": "商户消费",
                "hint": "餐饮",
                "source": "微信",
            }
        ]
    )
    assert text.startswith("请给下面这批支出逐条定类目")
    evidence = json.loads(text.split("\n", 1)[1])
    assert evidence[0]["index"] == 1
    assert evidence[0]["counterparty"] == "华南理工大学"
    assert evidence[0]["kind"] == "商户消费"
    assert evidence[0]["hint"] == "餐饮"
    assert "date" not in evidence[0] and "amount" not in evidence[0]


def test_deepseek_classifier_goes_through_schema_and_format_instruction():
    """用假模型跑通「提示词 → schema 格式说明 → 结构化结果」整条链路，不联网。"""
    model = _FakeStructuredModel(
        verdicts=[_verdict(1, "餐饮"), _verdict(2, "交通")]
    )
    classifier = make_deepseek_classifier(model=model)

    items = [
        {"index": 1, "date": "2026-09-01", "amount": 27.63, "counterparty": "遇见小面",
         "product": "招牌冒菜", "platform_category": "餐饮美食", "kind": "", "hint": "餐饮", "source": "支付宝"},
        {"index": 2, "date": "2026-09-02", "amount": 2.0, "counterparty": "广州地铁",
         "product": "搭乘广州地铁", "platform_category": "交通出行", "kind": "", "hint": "", "source": "支付宝"},
    ]
    assert classifier(items) == {1: "餐饮", 2: "交通"}

    sent = model.seen_messages[0]
    assert sent[0].content == CLASSIFY_SYSTEM
    assert '"index": 1' in sent[1].content
    # 官方 JSON Output 的要求由模型层追加的格式说明满足（含 json 字样 + 示例）
    assert "json" in sent[-1].content.lower()
    assert "枚举（" in sent[-1].content  # Literal 类目被渲染成枚举清单
    # 字段清单只出现在模型层注入的格式说明里，不出现于提示词
    for field in ("items", "index", "category"):
        assert field in sent[-1].content
        assert field not in CLASSIFY_SYSTEM


# --------------------------------------------------------------------------- #
# 真文件冒烟（文件不在就跳过，绝不把桌面文件当测试依赖）
# --------------------------------------------------------------------------- #

ALIPAY_REAL = Path(r"C:\Users\xbf\Desktop\支付宝交易明细(20260707-20261007).csv")
WECHAT_REAL = Path(r"C:\Users\xbf\Desktop\微信支付账单流水文件(20260707-20261007)_20261007211340.xlsx")


def test_real_alipay_bill_parses_without_raising():
    if not ALIPAY_REAL.is_file():
        pytest.skip("桌面没有这份支付宝账单")
    rows, _ = parse_alipay_csv(ALIPAY_REAL.read_bytes())
    outcome = clean(alipay=ALIPAY_REAL.read_bytes())

    assert len(rows) >= 100
    # 与回单自带汇总对账：收入 / 不计收支 两档分毫不差。
    # 支出刻意不比——支付宝明细的支出累加（含「交易关闭」那两笔）本来就与回单汇总不等，
    # 回单自己在特别提示第 6 条声明了这件事，清洗报告会把差异如实打出来。
    assert outcome.source_totals(SOURCE_ALIPAY)["income"] == outcome.platform_totals[SOURCE_ALIPAY]["income"]
    assert outcome.source_totals(SOURCE_ALIPAY)["transfer"] == outcome.platform_totals[SOURCE_ALIPAY]["transfer"]
    for document in outcome.documents.values():
        assert parse_bill(json.dumps(document, ensure_ascii=False), period=document["period"])


def test_real_wechat_bill_matches_platform_summary():
    if not WECHAT_REAL.is_file():
        pytest.skip("桌面没有这份微信账单")
    outcome = clean(wechat=WECHAT_REAL)

    assert len(outcome.rows_by_source[SOURCE_WECHAT]) >= 200
    # 微信三档都能对上（这是解析正确性最强的信号）
    assert outcome.source_totals(SOURCE_WECHAT) == outcome.platform_totals[SOURCE_WECHAT]
    assert read_wechat_summary(WECHAT_REAL)["expense"] == (209, 793_362)


@pytest.mark.parametrize("product,category", [("", "其他"), ("房租", "居住"), ("随礼", "人情往来"), ("聚餐AA", "其他")])
def test_transfer_requires_purpose_instead_of_person_name(product, category):
    text = ALIPAY_SAMPLE.replace("餐饮美食,遇见小面,/,招牌冒菜", f"转账红包,面粉先生,/,{product}")
    rows, _ = parse_alipay_csv(text)
    assert rows[0].category == category


def test_single_character_keyword_does_not_classify_unknown_merchant():
    rows, _ = parse_alipay_csv(ALIPAY_SAMPLE.replace("餐饮美食,遇见小面,/,招牌冒菜", "生活服务,界面科技,/,服务费"))
    assert rows[0].category == "其他"
    parsed = parse_bill(json.dumps(build_documents(rows)["2026-09"], ensure_ascii=False), period="2026-09")
    assert any(c.category == "其他" and c.amount_cents == 2763 for c in parsed.categories)


def test_invalid_date_and_shifted_columns_are_rejected():
    rows, warnings = parse_alipay_csv(ALIPAY_SAMPLE.replace("2026-09-01", "2026-02-30"))
    assert len(rows) == 3 and any("日期无效" in w for w in warnings)
    rows, warnings = parse_alipay_csv(ALIPAY_SAMPLE.replace("招牌冒菜,支出", "招牌,冒菜,支出"))
    assert len(rows) == 3 and any("列数" in w for w in warnings)


def test_xlsx_upload_bytes_keep_platform_summary(wechat_path):
    outcome = clean(wechat=Path(wechat_path).read_bytes())
    assert outcome.platform_totals[SOURCE_WECHAT]["income"] == (1, 3000)


def test_period_filter_precedes_model_calls():
    def forbidden(items):
        pytest.fail("不应分类期间之外的记录")
    assert clean(alipay=ALIPAY_SAMPLE, period="2026-08", classifier=forbidden).documents == {}


def test_same_purpose_reuses_category_without_reusing_money():
    rows = [RawRow(source="wechat", direction="expense", counterparty="商家", product="商品", amount_cents=100),
            RawRow(source="wechat", direction="expense", counterparty="商家", product="商品", amount_cents=200)]
    seen = []
    def classifier(items):
        seen.extend(items)
        return {1: "购物"}
    outcome = classify_rows(rows, classifier=classifier)
    assert len(seen) == 1 and outcome.reused == 1
    assert [r.amount_cents for r in outcome.rows] == [100, 200]
    assert [r.category for r in outcome.rows] == ["购物", "购物"]


def test_user_correction_wins_and_keeps_provenance():
    outcome = clean(alipay=ALIPAY_SAMPLE, classifier=lambda items: {1: "其他", 2: "交通"},
                    category_overrides={"alipay:2026090123001": "学习成长"})
    row = outcome.documents["2026-09"]["cashflow"][0]
    assert row["category"] == "学习成长" and row["category_source"] == "user"
    assert row["transaction_id"] == "2026090123001" and row["source_row"] > 0


@pytest.mark.parametrize("verdicts", [[_verdict(1, "餐饮"), _verdict(1, "购物")], [_verdict(2, "餐饮")]])
def test_model_duplicate_or_missing_indices_reject_entire_batch(verdicts):
    classifier = make_deepseek_classifier(model=_FakeStructuredModel(verdicts=verdicts))
    with pytest.raises(ValueError, match="编号"):
        classifier([{"index": 1}])


def test_classifier_disables_thinking_despite_global_setting(monkeypatch):
    from moneyrouter_agent.config import Settings
    import moneyrouter_agent.tools.bill_classify as module
    settings_seen = []
    def build(settings, **kwargs):
        settings_seen.append(settings)
        return _FakeStructuredModel(verdicts=[_verdict(1, "餐饮")])
    monkeypatch.setattr(module, "build_chat_model", build)
    module.make_deepseek_classifier(Settings(thinking_enabled=True))([{"index": 1}])
    assert settings_seen[0].thinking_enabled is False
    assert settings_seen[0].temperature == 0


def test_refund_report_does_not_claim_guaranteed_netting():
    outcome = clean(alipay=ALIPAY_SAMPLE.replace("账户存取,本人,/,提现", "退款,商家,/,退款"))
    assert "不能保证" in render_report(outcome)
    assert any(r.get("review_reason") for r in outcome.documents["2026-09"]["cashflow"])
