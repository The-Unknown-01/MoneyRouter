"""模型层单测：官方参数组织、格式说明（含 json 示例）、有界重试、思维链捕获。

全部不联网——用一个假的 BaseChatModel 顶替真实调用。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from moneyrouter_agent.config import Settings
from moneyrouter_agent.domain.turn import TurnDecision
from moneyrouter_agent.model.deepseek import (
    StructuredCall,
    StructuredOutputError,
    build_chat_model,
    clamp_top_p,
    format_instruction,
    make_structured_runner,
    render_format_instruction,
)

# --------------------------------------------------------------------------- #
# 请求体：按官方规范
# --------------------------------------------------------------------------- #
def _payload(settings: Settings) -> dict[str, Any]:
    model = build_chat_model(settings)
    return model._get_request_payload([HumanMessage(content="hi")])


def test_request_payload_thinking_on():
    """思考模式：显式开启 + reasoning_effort；temperature 官方无效，必须不发。"""
    payload = _payload(Settings(api_key="k", thinking_enabled=True, reasoning_effort="low"))

    assert payload["extra_body"]["thinking"]["type"] == "enabled"
    assert payload["reasoning_effort"] == "low"
    assert "temperature" not in payload
    assert payload["max_tokens"] >= 4096  # 推理 token 计入 completion，上限要留足


def test_request_payload_thinking_off():
    payload = _payload(Settings(api_key="k", thinking_enabled=False))

    assert payload["extra_body"]["thinking"]["type"] == "disabled"
    assert "reasoning_effort" not in payload  # 非思考模式不传力度参数
    assert payload["temperature"] == 0.3


def test_thinking_top_p_clamped_to_official_range():
    assert clamp_top_p(0.5) == 0.95
    assert clamp_top_p(0.99) == 0.99
    assert _payload(Settings(api_key="k", top_p=0.5))["top_p"] == 0.95


def test_temperature_respected_when_thinking_off():
    payload = _payload(Settings(api_key="k", thinking_enabled=False, temperature=0.9))
    assert payload["temperature"] == 0.9


# --------------------------------------------------------------------------- #
# 格式说明：官方 JSON Output 的「json 字样 + 具体示例」
# --------------------------------------------------------------------------- #
def test_format_instruction_follows_official_json_recipe():
    text = render_format_instruction(TurnDecision)

    assert "json" in text.lower()  # 官方要求 prompt 里出现 json 字样
    assert "json 输出示例" in text  # 官方要求给出期望格式的示例
    # 字段清单（含嵌套字段）与语义说明一并传达
    for field in ("reply", "understanding", "ready_to_finalize", "rationale"):
        assert field in text
    for nested in ("understanding.occupation", "understanding.max_loss_pct", "understanding.goal"):
        assert nested in text
    assert "每月生活费" in text  # Field(description=...) 的语义


def test_format_instruction_example_is_valid_json_and_teaches_blank():
    text = render_format_instruction(TurnDecision)
    example = json.loads(text[text.index("{", text.index("json 输出示例")):])

    assert set(example) == set(TurnDecision.model_fields)
    assert set(example["understanding"]) == set(TurnDecision.model_fields["understanding"].annotation.model_fields)
    # 示例里 unknown 值为 null：正好教模型「没了解到就留空」
    assert example["understanding"]["occupation"] is None
    assert example["understanding"]["max_loss_pct"] is None


def test_format_instruction_is_cached():
    assert format_instruction(TurnDecision) is format_instruction(TurnDecision)
    assert isinstance(format_instruction(TurnDecision), SystemMessage)


def test_plan_string_arrays_are_explicit_in_json_contract():
    from moneyrouter_agent.domain.plan_turn import PlanTurnDecision, WalletTurnDecision

    text = render_format_instruction(PlanTurnDecision)
    assert "sections（字符串数组）" in text
    assert "questions（字符串数组）" in text
    assert "proposal.wallets.target_cents" in text
    assert "包含已有 reserve_cents" in text
    active = render_format_instruction(WalletTurnDecision)
    assert "proposal.wallets.target_cents" in active
    assert "sections" not in active
    assert "reserve_months" not in active


# --------------------------------------------------------------------------- #
# runner：include_raw + 有界重试 + 思维链
# --------------------------------------------------------------------------- #
class _RecordingModel(BaseChatModel):
    """按脚本返回 ``include_raw`` 结果，并记录每次收到的消息与方法名。"""

    outcomes: list[Any] = Field(default_factory=list)  # BaseModel | Exception | None（解析失败）
    reasoning: str | None = None
    seen_methods: list[str] = Field(default_factory=list)
    seen_payloads: list[list[Any]] = Field(default_factory=list)
    max_tokens: int = 4096
    seen_budgets: list[int] = Field(default_factory=list)
    extra_body: dict[str, Any] | None = None
    seen_thinking: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="{}"))])

    def with_structured_output(  # type: ignore[override]
        self, schema: Any, *, method: str = "function_calling", include_raw: bool = False, **kw: Any
    ):
        self.seen_methods.append(method)
        assert include_raw is True, "runner 必须用 include_raw 以免解析失败直接抛异常"

        def _run(messages: list[BaseMessage]) -> dict[str, Any]:
            self.seen_payloads.append(list(messages))
            self.seen_budgets.append(self.max_tokens)
            self.seen_thinking.append((self.extra_body or {}).get("thinking"))
            outcome = self.outcomes.pop(0) if self.outcomes else None
            if isinstance(outcome, Exception):
                raise outcome
            kwargs = {"reasoning_content": self.reasoning} if self.reasoning else {}
            raw = AIMessage(content="{}", additional_kwargs=kwargs)
            if outcome is None:  # 模拟官方承认的「偶发空内容」
                return {"raw": AIMessage(content=" "), "parsed": None, "parsing_error": ValueError("Invalid json output")}
            return {"raw": raw, "parsed": outcome, "parsing_error": None}

        return RunnableLambda(_run)


def test_runner_uses_json_mode_and_returns_structured_call():
    model = _RecordingModel(outcomes=[TurnDecision(reply="你好")], reasoning="先问身份")
    result = make_structured_runner(model, TurnDecision)([HumanMessage(content="hi")])

    assert model.seen_methods == ["json_mode"]
    assert isinstance(result, StructuredCall)
    assert result.parsed.reply == "你好"
    assert result.reasoning == "先问身份"
    assert result.attempts == 1


def test_runner_appends_json_instruction_as_last_message():
    model = _RecordingModel(outcomes=[TurnDecision(reply="你好")])
    make_structured_runner(model, TurnDecision)([HumanMessage(content="hi")])

    last = model.seen_payloads[0][-1]
    assert isinstance(last, SystemMessage)
    assert "json" in last.content.lower()


def test_runner_retries_on_empty_content_then_succeeds():
    """官方承认 JSON Output 偶发空内容——必须重试，且重试时补一句更强的纠正指令。"""
    model = _RecordingModel(outcomes=[None, TurnDecision(reply="补上了")])
    result = make_structured_runner(model, TurnDecision, max_attempts=3)([HumanMessage(content="hi")])

    assert result.attempts == 2
    assert result.parsed.reply == "补上了"
    reminder = model.seen_payloads[1][-1]
    assert isinstance(reminder, HumanMessage)
    assert "json" in reminder.content.lower()


def test_runner_raises_after_exhausting_attempts():
    model = _RecordingModel(outcomes=[None, None, None])
    try:
        make_structured_runner(model, TurnDecision, max_attempts=3)([HumanMessage(content="hi")])
    except StructuredOutputError as exc:
        assert exc.attempts == 3
        assert exc.schema is TurnDecision
    else:  # pragma: no cover
        raise AssertionError("重试耗尽后必须抛 StructuredOutputError，交给节点降级")


def test_runner_retries_on_call_exception():
    model = _RecordingModel(outcomes=[RuntimeError("网络抖动"), TurnDecision(reply="好了")])
    result = make_structured_runner(model, TurnDecision, max_attempts=2)([HumanMessage(content="hi")])
    assert result.attempts == 2


def test_truncation_grows_budget_with_a_bounded_ceiling():
    # Same exception type returned by the provider's JSON parser.
    LengthFinishReasonError = type("LengthFinishReasonError", (Exception,), {})
    model = _RecordingModel(outcomes=[LengthFinishReasonError(), LengthFinishReasonError(),
                                     TurnDecision(reply="完整结果")],
                            extra_body={"thinking": {"type": "enabled"}})
    result = make_structured_runner(model, TurnDecision, max_attempts=3, initial_max_tokens=4096)([])
    assert result.attempts == 3
    assert model.seen_budgets == [4096, 8192, 16384]
    assert model.max_tokens == 4096  # Do not mutate a shared client.
    assert model.seen_thinking == [{"type": "enabled"}, {"type": "enabled"}, {"type": "disabled"}]
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    assert "截断" in model.seen_payloads[1][-1].content


def test_empty_json_retry_does_not_raise_token_budget():
    model = _RecordingModel(outcomes=[None, TurnDecision(reply="完整结果")])
    make_structured_runner(model, TurnDecision, max_attempts=2, initial_max_tokens=4096)([])
    assert model.seen_budgets == [4096, 4096]


def test_reasoning_never_injected_into_payload():
    """思维链只做观测：本项目不带 tools，官方明确不需回传——绝不能写回 messages。"""
    model = _RecordingModel(outcomes=[None, TurnDecision(reply="你好")], reasoning="这是思维链内容")
    make_structured_runner(model, TurnDecision, max_attempts=2)([HumanMessage(content="hi")])

    for payload in model.seen_payloads:
        for message in payload:
            assert "这是思维链内容" not in str(message.content)


def test_reasoning_none_when_model_returns_nothing():
    model = _RecordingModel(outcomes=[TurnDecision(reply="你好")])
    result = make_structured_runner(model, TurnDecision)([HumanMessage(content="hi")])
    assert result.reasoning is None


# --------------------------------------------------------------------------- #
# 方案工具循环：默认关思考、且绝不强制 tool_choice
# --------------------------------------------------------------------------- #
def test_plan_tool_model_disables_thinking_and_never_forces_tool_choice():
    """工具循环默认关思考（避开 reasoning_content 回传 400），且 tool_choice 保持 auto。"""
    from moneyrouter_agent.domain.plan import PlanContext, PlanInputs
    from moneyrouter_agent.tools.plan_tools import make_plan_tools

    settings = Settings(api_key="k", thinking_enabled=False)
    model = build_chat_model(settings)  # 门面就是这么建的（tools_thinking_enabled 默认 False）
    tools = make_plan_tools(PlanContext(PlanInputs()))

    bound = model.bind_tools(tools)
    payload = model._get_request_payload([HumanMessage(content="hi")], **bound.kwargs)

    assert payload["extra_body"]["thinking"]["type"] == "disabled"
    assert "tool_choice" not in payload  # 思考模式不支持强制 tool_choice
    assert payload["tools"]  # 工具已绑定
