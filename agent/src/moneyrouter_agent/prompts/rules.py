"""降级引导文案。

模型不可用（无密钥 / 调用失败 / 结构化输出解析失败）时使用，保证"没有 AI 也能用"。
此路径是兜底，允许固定文案——与主路径"给模型大自由度"并不冲突。
"""

from __future__ import annotations

from typing import Any

DEGRADED_NOTICE = "（我这边稍慢了一点，我们接着往下聊——）"

RULES_GUIDE: dict[str, str] = {
    "open": "您好，我是帮您梳理财务情况的助理。方便先说说您目前是做什么的吗？",
    "identity": "方便聊聊您目前从事什么工作、大概什么年龄段吗？",
    "income": "那收入方面大概是怎样？说个区间就行，不用很精确。",
    "spend": "平时主要开销在哪些地方？房租、吃饭这类大概占多少？",
    "goal": "这笔钱您主要是为了什么在攒？大概什么时候会用得上？",
    "risk": "如果这笔钱短期内出现亏损，您是一点都不想接受，还是能接受一部分？",
    "default": "还有哪方面您想再补充的？",
}


def pick_next_guide(draft: Any) -> str:
    """按当前理解里还缺哪一块，挑一句规则引导。

    这是降级路径的兜底排序，不代表主路径的推进顺序（主路径由模型自行决定）。
    """
    if draft is None:
        return RULES_GUIDE["open"]
    if not getattr(draft, "occupation", None):
        return RULES_GUIDE["identity"]
    if getattr(draft, "income_cents", None) is None:
        return RULES_GUIDE["income"]
    if getattr(draft, "horizon_months", None) is None and not getattr(draft, "goal", None):
        return RULES_GUIDE["goal"]
    if getattr(draft, "max_loss_pct", None) is None:
        return RULES_GUIDE["risk"]
    return RULES_GUIDE["default"]


def degraded_reply(draft: Any, *, with_notice: bool = True) -> str:
    """拼出降级回复：一句提示 + 一句规则引导。"""
    body = pick_next_guide(draft)
    return f"{DEGRADED_NOTICE}{body}" if with_notice else body
