"""把原始账单（支付宝 CSV / 微信 XLSX）洗成规范文档（JSON）。

用法（在 ``agent/`` 目录下）：

    ./.venv/Scripts/python.exe scripts/clean_bill.py \\
        --alipay "支付宝交易明细.csv" \\
        --wechat "微信支付账单流水文件.xlsx" \\
        --out cleaned

产出：``cleaned/<YYYY-MM>.json``，每个自然月一份，可直接喂给
``scripts/month_repl.py --bill cleaned/2026-09.json --period 2026-09``。

类目默认**交给 DeepSeek 判定**（关键词表退居兜底与参考）；没有密钥时自动降级为纯关键词。

常用开关：

    --period 2026-09          只要这一个月（写一个文件）
    --classifier keyword      不调模型，只用关键词表（离线 / 省钱）
    --batch-size 15           每批分类多少笔
    --thinking                分类时打开思考模式（默认关：批量判定不需要思维链）
    --refund-policy transfer  排除退款相关流水（兼容选项，不等同于净额核算）
    --report-only             只出报告，不落盘
    --no-verify               跳过"回读校验"（默认会用 tools.dossier 解析一遍产出）

退出码：0 = 正常；2 = 一条流水都没解析出来；3 = 参数 / 文件问题。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moneyrouter_agent.config import Settings  # noqa: E402
from moneyrouter_agent.config import AGENT_ROOT
from moneyrouter_agent.bill_imports import BillImportStore, import_bills
from moneyrouter_agent.tools.bill_classify import make_deepseek_classifier  # noqa: E402
from moneyrouter_agent.tools.bill_cleaner import (  # noqa: E402
    CLASSIFY_BATCH_SIZE,
    REFUND_KEEP,
    REFUND_POLICIES,
    SOURCE_ALIPAY,
    SOURCE_WECHAT,
    clean,
    documents_to_text,
    render_report,
)
from moneyrouter_agent.tools.bills import parse_bill  # noqa: E402

KEYWORD = "keyword"
DEEPSEEK = "deepseek"


def verify_documents(documents: dict[str, dict]) -> dict[str, dict]:
    """用真解析器回读产出文档，确认它们真的能被下游接住。"""
    result: dict[str, dict] = {}
    for period, document in sorted(documents.items()):
        parsed = parse_bill(json.dumps(document, ensure_ascii=False), period=period)
        income = parsed.income.amount_cents if parsed.income else 0
        spend = sum(category.amount_cents for category in parsed.categories)
        result[period] = {
            "income_cents": income,
            "spend_cents": spend,
            "balance_cents": income - spend,
            "skipped_rows": parsed.skipped_rows,
            "warnings": parsed.warnings,
        }
    return result


def build_classifier(args: argparse.Namespace):
    """按参数建分类器；没有密钥就如实降级，绝不硬撑。"""
    if args.classifier == KEYWORD:
        return None, "关键词表"
    settings = Settings.from_env()
    if settings.degraded:
        print(
            "！未找到 DeepSeek API Key，类目分类降级为关键词表。"
            "（设置 DEEPSEEK_API_KEY 或写 .env 后可启用模型分类）",
            file=sys.stderr,
        )
        return None, "关键词表"
    name = "DeepSeek" + ("（思考模式）" if args.thinking else "")
    return make_deepseek_classifier(settings, thinking=args.thinking), name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="原始账单 → 规范文档（清洗器）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--alipay", help="支付宝「交易明细」CSV 路径（GBK 编码）")
    parser.add_argument("--wechat", help="微信支付「账单流水文件」XLSX 路径")
    parser.add_argument("--out", default="cleaned", help="产出目录（默认 ./cleaned）")
    parser.add_argument("--user", default="local", help="导入账本用户标识（不同用户必须使用不同标识）")
    parser.add_argument("--ledger", default=str(AGENT_ROOT / ".data/bill-imports.sqlite"), help="幂等导入账本 SQLite 路径")
    parser.add_argument("--period", default=None, help="只要这一个期间 YYYY-MM（默认按月全出）")
    parser.add_argument(
        "--classifier",
        default=DEEPSEEK,
        choices=(DEEPSEEK, KEYWORD),
        help="类目分类后端：deepseek=调模型（默认，无密钥自动降级），keyword=只用关键词表",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=CLASSIFY_BATCH_SIZE,
        help=f"每批分类多少笔（默认 {CLASSIFY_BATCH_SIZE}；批越小越稳但调用越多）",
    )
    parser.add_argument("--thinking", action="store_true", help="分类时打开思考模式（默认关闭）")
    parser.add_argument("--category-overrides", help="人工分类 JSON：{来源:交易编号: 固定类目}，优先于模型")
    parser.add_argument(
        "--refund-policy",
        default="net",
        choices=REFUND_POLICIES,
        help="退款处理：net=净消费归原消费月（默认），keep=平台方向，transfer=兼容排除；无法关联需复核",
    )
    parser.add_argument("--report-only", action="store_true", help="只打印报告，不写文件")
    parser.add_argument("--no-verify", action="store_true", help="跳过回读校验")
    args = parser.parse_args(argv)

    if not args.alipay and not args.wechat:
        parser.error("至少要给一个来源：--alipay 或 --wechat")
    if args.batch_size < 1:
        parser.error("--batch-size 必须为正整数")
    if not args.user.strip():
        parser.error("--user 不能为空")
    if args.period and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.period):
        parser.error("--period 必须为 YYYY-MM")
    overrides = None
    if args.category_overrides:
        try:
            overrides = json.loads(Path(args.category_overrides).read_text(encoding="utf-8-sig"))
            if not isinstance(overrides, dict):
                raise ValueError("须为 JSON 对象")
        except (OSError, ValueError):
            parser.error("人工分类文件必须是有效的 JSON 对象")

    source_files: dict[str, str] = {}
    alipay_bytes: bytes | None = None
    wechat_path: Path | None = None

    if args.alipay:
        path = Path(args.alipay)
        if not path.is_file():
            print(f"！找不到支付宝文件：{path}", file=sys.stderr)
            return 3
        alipay_bytes = path.read_bytes()  # 支付宝导出是 GBK，交给解析侧解码
        source_files[SOURCE_ALIPAY] = str(path)

    if args.wechat:
        wechat_path = Path(args.wechat)
        if not wechat_path.is_file():
            print(f"！找不到微信文件：{wechat_path}", file=sys.stderr)
            return 3
        source_files[SOURCE_WECHAT] = str(wechat_path)

    classifier, classifier_name = build_classifier(args)

    try:
        kwargs = dict(
            alipay=alipay_bytes, wechat=wechat_path, refund_policy=args.refund_policy,
            period=args.period, source_files=source_files, classifier=classifier,
            classifier_name=classifier_name, batch_size=args.batch_size,
            category_overrides=overrides,
        )
        import_stats = None
        if args.report_only:
            outcome = clean(**kwargs)
        else:
            outcome, documents, import_stats = import_bills(BillImportStore(args.ledger, user=args.user), **kwargs)
            outcome.documents = documents
    except Exception as exc:
        print(f"！账单读取失败（{type(exc).__name__}），请检查文件格式与完整性。", file=sys.stderr)
        return 3

    if not outcome.rows or not outcome.documents:
        print(render_report(outcome), file=sys.stderr)
        print("！一条流水都没解析出来，未产出任何文件。", file=sys.stderr)
        return 2

    verify = None if args.no_verify else verify_documents(outcome.documents)

    written: list[Path] = []
    if not args.report_only:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        for period, text in documents_to_text(outcome.documents).items():
            target = out_dir / f"{period}.json"
            target.write_text(text, encoding="utf-8")
            written.append(target)
        review = [item for document in outcome.documents.values()
                  for item in document["cashflow"] if item.get("review_reason")]
        (out_dir / "review.json").write_text(json.dumps(
            {"items": review, "warnings": outcome.warnings,
             "refund_policy": outcome.refund_policy}, ensure_ascii=False, indent=2
        ) + "\n", encoding="utf-8")

    print(render_report(outcome, verify=verify))
    if import_stats:
        print("导入统计：" + json.dumps(import_stats, ensure_ascii=False) + "；产出为当前用户累计去重账本。")

    if written:
        print("\n已写出：")
        for path in written:
            print(f"  {path}")
        example = written[0]
        print(
            "\n喂给本月实况 Agent：\n"
            "  ./.venv/Scripts/python.exe scripts/month_repl.py --real "
            f'--bill "{example}" --period {example.stem}'
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
