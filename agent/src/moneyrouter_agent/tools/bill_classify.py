"""用 DeepSeek 给账单流水定类目（清洗器的分类后端）。

「模型只写类目、绝不写金额」这条铁律在这里同样成立：入参只有商家 / 商品 / 平台类目这类
**文字依据**，出参只有 ``编号 → 类目``；金额、方向、日期一概不经过模型。

接法沿用项目统一的 ``model/deepseek.py``：官方 JSON Output（``json_mode``）+ ``include_raw``
+ 有界重试，字段语义写在 Pydantic schema 的 ``Field(description=...)`` 里，提示词只管判断。

**思考模式默认关闭**：这是成批量的判定任务（一份账单十几批），不需要思维链——
与 ``PLAN_TOOLS_THINKING=false`` 同一个理由（那里是"工具循环不划算"，这里是"批次数不划算"）。
需要更强判断时把 ``thinking=True`` 打开即可。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..config import Settings
from ..domain.month import SpendCategory
from ..model.deepseek import build_chat_model, make_structured_runner
from ..prompts.classify import CLASSIFY_SYSTEM, render_classify_request
from .bill_cleaner import ClassifierFn

__all__ = ["BillClassification", "CategoryVerdict", "make_deepseek_classifier"]


class CategoryVerdict(BaseModel):
    """一笔流水的类目判定。"""

    index: int = Field(gt=0, description="这笔流水在请求里的编号（原样带回）")
    category: SpendCategory = Field(description="从固定枚举里选一个；确实判断不了就选「其他」")


class BillClassification(BaseModel):
    """一批流水的分类结果。"""

    items: list[CategoryVerdict] = Field(
        description="逐条给出结果，编号与请求里的一一对应，一批多少条就给多少条"
    )


def make_deepseek_classifier(
    settings: Settings | None = None,
    *,
    model: BaseChatModel | None = None,
    thinking: bool = False,
    **overrides: Any,
) -> ClassifierFn:
    """建一个「一批流水 → {编号: 类目}」的分类器。

    ``model`` 是**测试用的注入口**（传入即不再自建客户端）——与方案 Agent 收 ``agent_runner``
    是同一个道理：节点只认 runner，测试不联网。

    调用失败（网络 / 结构化输出解析不掉）会向上抛，由
    :func:`~moneyrouter_agent.tools.bill_cleaner.classify_rows` 按批降级到关键词表——
    清洗流程不会因此中断。
    """
    if model is None:
        if settings is None:
            raise ValueError("要么给 settings（自建客户端），要么注入 model（测试）。")
        model = build_chat_model(settings.with_overrides(thinking_enabled=thinking, temperature=0), **overrides)

    runner = make_structured_runner(
        model, BillClassification,
        max_attempts=settings.structured_retries + 1 if settings else 3,
        initial_max_tokens=settings.max_tokens if settings else None,
    )
    stats = {"calls": 0, "attempts": 0, "reasoning_responses": 0, "elapsed_s": 0.0}

    def classify(items: Sequence[dict[str, Any]]) -> dict[int, str]:
        messages = [
            SystemMessage(content=CLASSIFY_SYSTEM),
            HumanMessage(content=render_classify_request(items)),
        ]
        started = time.monotonic()
        call = runner(messages)
        stats["calls"] += 1
        stats["attempts"] += call.attempts
        stats["reasoning_responses"] += bool(call.reasoning)
        stats["elapsed_s"] += time.monotonic() - started
        indices = [verdict.index for verdict in call.parsed.items]
        expected = {item["index"] for item in items}
        if len(indices) != len(set(indices)) or set(indices) != expected:
            raise ValueError("分类结果编号缺失、重复或越界，保留本批规则结果。")
        return {verdict.index: str(verdict.category) for verdict in call.parsed.items}

    classify.stats = stats
    return classify
