"""本月实况的降级引导文案。

模型不可用（无密钥 / 调用失败 / 结构化输出解析失败）时使用，保证"没有 AI 也能用"。
此路径是兜底，允许固定文案——与主路径"给模型大自由度"并不冲突。
语气与主路径一致：不用「咱们」「好呀」这类过度口语化表达。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..domain.probe import Probe, open_probes

MONTH_DEGRADED_NOTICE = "（我这边稍慢了一点，我们接着往下聊——）"

MONTH_RULES_GUIDE: dict[str, str] = {
    "income": "先说说这个月大概有多少收入？按您平时拿到的口径讲就行，说个大概就好。",
    "category": "这个月主要花在哪些方面？大概给个范围就行，不用很精确。",
    "allocation": "这个月剩下的钱大概怎么安排的？有多少是留着备用、不拿去投资的，说个大概就行。",
    "investments": "以前有没有在做投资？如果有的话，这个月大概赚了还是亏了、累计收益怎么样，"
    "说个大概就行；如果本来就没投资，直接说一声也可以。",
    "probe": "关于{topic}：{detail}方便说说是什么原因吗？",
    "default": "这个月还有哪方面想再补充的？",
}


def pick_next_month_guide(snapshot: Any, probes: Sequence[Probe] | None) -> str:
    """按"最紧要的待问关注点 → 缺收入 → 缺支出 → 兜底"挑一句规则引导。

    这是降级路径的兜底排序，不代表主路径的推进顺序（主路径由模型自行决定）。
    """
    pending = open_probes(list(probes or []))
    if pending:
        top = sorted(pending, key=lambda p: (p.severity, p.id))[0]
        return MONTH_RULES_GUIDE["probe"].format(topic=top.topic, detail=top.detail)
    if snapshot is None or getattr(snapshot, "income", None) is None:
        return MONTH_RULES_GUIDE["income"]
    if not getattr(snapshot, "categories", None):
        return MONTH_RULES_GUIDE["category"]
    alloc = getattr(snapshot, "allocation", None)
    if alloc is not None and alloc.non_invested_cents is None and alloc.invested_cents is None:
        return MONTH_RULES_GUIDE["allocation"]
    inv = getattr(snapshot, "investments", None)
    if inv is not None and getattr(inv, "has_investments", None) is None:
        return MONTH_RULES_GUIDE["investments"]
    return MONTH_RULES_GUIDE["default"]


def degraded_month_reply(snapshot: Any, probes: Sequence[Probe] | None, *, with_notice: bool = True) -> str:
    """拼出降级回复：一句提示 + 一句规则引导。"""
    body = pick_next_month_guide(snapshot, probes)
    return f"{MONTH_DEGRADED_NOTICE}{body}" if with_notice else body
