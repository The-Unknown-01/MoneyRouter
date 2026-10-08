"""DeepSeek 接入（``langchain_deepseek.ChatDeepSeek``）——按官方文档实现。

## 思考模式（官方 https://api-docs.deepseek.com/guides/thinking_mode）

- 开关 ``{"thinking": {"type": "enabled"/"disabled"}}``，OpenAI SDK 下经 ``extra_body`` 传入；
- 力度 ``reasoning_effort: "low"/"high"/"max"``（``ChatDeepSeek`` 的原生字段）；
- 思考模式**不支持** ``temperature`` / ``presence_penalty`` / ``frequency_penalty``
  （不报错但完全无效）→ 本模块在该模式下**一律不发送** ``temperature``；
- ``top_p`` 仅思考模式生效，有效区间 0.95–1.0，发送前钳制；
- 思维链通过 ``reasoning_content`` 返回，本项目**不带 tools**，因此按官方规定
  **不需要回传**（传了也会被忽略）——只取来做观测，绝不写回 ``messages``。

## 结构化输出（官方 https://api-docs.deepseek.com/guides/json_mode）

官方 JSON Output 的三条硬性要求，本模块逐条对齐：

1. 设 ``response_format={'type': 'json_object'}``（由 ``with_structured_output(method="json_mode")`` 完成）；
2. **prompt 里必须出现 "json" 字样，并且给出期望 JSON 格式的示例**；
3. ``max_tokens`` 要足够大，避免 JSON 被截断。

其中第 2 条由 :func:`format_instruction` 完成：说明文本**完全由 Pydantic schema 派生**
（字段清单 + 一个具体 JSON 示例），保持「schema 是格式的唯一事实来源」，因此
``prompts/`` 里的访谈提示词依然不必出现任何字段清单。

官方同时承认 JSON Output「may occasionally return empty content」，因此
:func:`make_structured_runner` 用 ``include_raw=True``（解析失败不抛异常）+ **有界重试**
来兜住这类偶发空响应。
"""

from __future__ import annotations

import json
import time
import types
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Generic,
    Literal,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from ..config import Settings

T = TypeVar("T", bound=BaseModel)

THINKING_ON = {"thinking": {"type": "enabled"}}
THINKING_OFF = {"thinking": {"type": "disabled"}}

# 思考模式下 top_p 的官方有效区间
TOP_P_MIN, TOP_P_MAX = 0.95, 1.0


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
def clamp_top_p(value: float) -> float:
    """思考模式下 top_p 有效区间为 0.95–1.0（官方：低于 0.95 按 0.95 处理）。"""
    return min(TOP_P_MAX, max(TOP_P_MIN, float(value)))


def build_chat_model(settings: Settings, **overrides: Any) -> ChatDeepSeek:
    """按官方规范组装 DeepSeek 客户端参数（无密钥时抛错）。"""
    params: dict[str, Any] = {
        "model": settings.model,
        "api_base": settings.base_url,
        "api_key": settings.require_api_key(),
        "max_tokens": settings.max_tokens,
        "timeout": settings.timeout_s,
        "max_retries": settings.max_retries,  # SDK 层：只覆盖网络错误与 5xx
    }
    if settings.thinking_enabled:
        params["extra_body"] = dict(THINKING_ON)
        params["reasoning_effort"] = settings.reasoning_effort
        if settings.top_p is not None:
            params["top_p"] = clamp_top_p(settings.top_p)
        # 思考模式下 temperature 官方明确无效 —— 不发，避免误导与日志噪声
    else:
        params["extra_body"] = dict(THINKING_OFF)
        params["temperature"] = settings.temperature
        if settings.top_p is not None:
            params["top_p"] = settings.top_p
    params.update(overrides)
    return ChatDeepSeek(**params)


# --------------------------------------------------------------------------- #
# 输出格式说明：由 schema 派生「字段清单 + 具体 json 示例」
# --------------------------------------------------------------------------- #
_FORMAT_CACHE: dict[type[BaseModel], SystemMessage] = {}

_PRIMITIVES: dict[Any, str] = {str: "字符串", int: "整数", float: "数字", bool: "布尔"}


def _is_optional(annotation: Any) -> tuple[Any, bool]:
    """把 ``X | None`` 拆成 ``(X, True)``；非 Optional 原样返回。"""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _list_item(annotation: Any) -> type[BaseModel] | None:
    """``list[SomeModel]`` → ``SomeModel``；不是对象数组则返回 ``None``。"""
    origin = get_origin(annotation)
    if origin in (list, tuple, set):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1 and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    return None


def _type_label(annotation: Any) -> str:
    inner, optional = _is_optional(annotation)
    origin = get_origin(inner)
    if origin is Literal:
        label = "枚举（" + " / ".join(str(arg) for arg in get_args(inner)) + "）"
    elif inner in _PRIMITIVES:
        label = _PRIMITIVES[inner]
    elif _list_item(inner) is not None:
        label = "对象数组"
    elif origin in (list, tuple, set):
        label = "数组"
    elif isinstance(inner, type) and issubclass(inner, BaseModel):
        label = "对象"
    else:
        label = str(inner)
    return f"{label}或空" if optional else label


def _iter_fields(schema: type[BaseModel], prefix: str = ""):
    """递归产出 ``(点分字段名, 注解, 描述, 是否必填)``。

    嵌套对象与**对象数组**都会展开——否则模型看不到数组元素的字段名。
    """
    for name, field in schema.model_fields.items():
        dotted = f"{prefix}{name}"
        annotation = field.annotation
        inner, _ = _is_optional(annotation)
        yield dotted, annotation, field.description, field.is_required()
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            yield from _iter_fields(inner, prefix=f"{dotted}.")
        else:
            item = _list_item(inner)
            if item is not None:
                yield from _iter_fields(item, prefix=f"{dotted}.")


def _placeholder(annotation: Any, description: str | None) -> Any:
    """必填字段的占位示例值（取描述首句，让人一眼看懂该填什么）。"""
    inner, _ = _is_optional(annotation)
    origin = get_origin(inner)
    if origin is Literal:
        args = get_args(inner)
        return args[0] if args else None
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return _example_object(inner)
    if inner is bool:
        return False
    if inner is int:
        return 0
    if inner is float:
        return 0.0
    if inner is str:
        hint = (description or "内容").strip()
        for separator in ("（", "(", "，", ",", "；", ";", "。"):
            hint = hint.split(separator)[0]
        return f"<这里写{hint.strip()[:20]}>"
    return None


def _example_value(annotation: Any, field: Any) -> Any:
    """示例值。

    - **对象数组**优先给一个样本元素（``default_factory=list`` 的 ``[]`` 教不会模型形状）；
    - 其余**优先用字段自己的默认值**（未知留空语义因此自动成立）。
    """
    item = _list_item(_is_optional(annotation)[0])
    if item is not None:
        return [_example_object(item)]

    if field.default is not PydanticUndefined:
        return _dump(field.default)
    if field.default_factory is not None:
        try:
            return _dump(field.default_factory())
        except Exception:  # noqa: BLE001 - 工厂需要参数时退回占位
            return _placeholder(annotation, field.description)
    return _placeholder(annotation, field.description)


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    return value


def _example_object(schema: type[BaseModel]) -> dict[str, Any]:
    return {
        name: _example_value(field.annotation, field)
        for name, field in schema.model_fields.items()
    }


def render_format_instruction(schema: type[BaseModel]) -> str:
    """按官方 JSON Output 要求渲染格式说明：含 "json" 字样 + 字段清单 + 具体示例。"""
    lines = [
        "本次回答只输出一个 json 对象：不要 markdown 代码块，不要任何解释或多余文字，",
        "直接以 { 开头、以 } 结尾。",
        "",
        "json 字段说明：",
    ]
    for name, annotation, description, _required in _iter_fields(schema):
        text = " ".join((description or "").split())
        lines.append(f"- {name}（{_type_label(annotation)}）：{text}")
    lines += [
        "",
        "json 输出示例（只是形状示例：里面为 null 或空串的字段表示「尚未了解到」，"
        "请按你实际掌握的信息填写，不要照抄示例内容）：",
        json.dumps(_example_object(schema), ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines)


def format_instruction(schema: type[BaseModel]) -> SystemMessage:
    """格式说明（按 schema 缓存，只注入请求、不进 ``prompts/``）。"""
    cached = _FORMAT_CACHE.get(schema)
    if cached is None:
        cached = SystemMessage(content=render_format_instruction(schema))
        _FORMAT_CACHE[schema] = cached
    return cached


# --------------------------------------------------------------------------- #
# 结构化调用：include_raw + 有界重试 + 思维链捕获
# --------------------------------------------------------------------------- #
class StructuredOutputError(RuntimeError):
    """多次尝试后仍未拿到可解析的结构化结果（交给节点降级）。"""

    def __init__(self, schema: type[BaseModel], attempts: int, cause: Any = None) -> None:
        super().__init__(
            f"{schema.__name__} 结构化输出失败（已尝试 {attempts} 次）：{cause!r}"
        )
        self.schema = schema
        self.attempts = attempts
        self.cause = cause


@dataclass(frozen=True)
class StructuredCall(Generic[T]):
    """结构化调用结果：解析对象 + 思维链（仅观测）+ 实际尝试次数。"""

    parsed: T
    reasoning: str | None = None
    attempts: int = 1


def extract_reasoning(raw: BaseMessage | None) -> str | None:
    """取官方 ``reasoning_content``（实测有时为空）。**只用于观测，绝不回传模型。**"""
    if raw is None:
        return None
    value = (getattr(raw, "additional_kwargs", None) or {}).get("reasoning_content")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _retry_reminder(*, truncated: bool = False) -> HumanMessage:
    """重试时的纠正指令：官方 JSON Output 要求 prompt 含 "json" 字样。"""
    return HumanMessage(
        content=(
            ("上一次输出被长度上限截断，请缩短叙述并输出完整 json。" if truncated else
             "上一次结构化输出无效或为空。请只输出一个合法的 json 对象：")
            +
            "从 { 开始、以 } 结束，不要留空，不要 markdown 代码块，不要任何额外说明。"
        )
    )


def make_structured_runner(
    model: BaseChatModel,
    schema: type[T],
    *,
    method: str = "json_mode",
    max_attempts: int = 3,
    backoff_s: float = 0.0,
    initial_max_tokens: int | None = None,
) -> Callable[[list[BaseMessage]], StructuredCall[T]]:
    """返回 ``(messages) -> StructuredCall``；内部做有界重试，失败抛错交给节点降级。"""
    runnable = model.with_structured_output(schema, method=method, include_raw=True)
    instruction = format_instruction(schema)

    def run(messages: list[BaseMessage]) -> StructuredCall[T]:
        last_error: Any = None
        retry_tokens: int | None = None
        truncated = False
        for attempt in range(1, max_attempts + 1):
            payload: list[BaseMessage] = [*messages, instruction]
            if attempt > 1:
                payload.append(_retry_reminder(truncated=truncated))
            try:
                attempt_runnable = runnable
                if retry_tokens:
                    # include_raw wraps the model in RunnableParallel, whose
                    # invoke kwargs are not forwarded. Rebuild the copied model.
                    updates = {"max_tokens": retry_tokens}
                    if truncated and attempt == max_attempts:
                        # Final recovery attempt: schema-only tasks must not spend
                        # their entire budget on reasoning and return no JSON.
                        updates.update(extra_body={**(getattr(model, "extra_body", None) or {}), **THINKING_OFF},
                                       temperature=0, reasoning_effort=None)
                    retry_model = model.model_copy(update=updates)
                    attempt_runnable = retry_model.with_structured_output(schema, method=method, include_raw=True)
                outcome = attempt_runnable.invoke(payload)
            except Exception as exc:  # noqa: BLE001 - 调用失败也算一次尝试
                last_error = exc
            else:
                parsed = outcome.get("parsed")
                if parsed is not None:
                    return StructuredCall(
                        parsed=parsed,  # type: ignore[arg-type]
                        reasoning=extract_reasoning(outcome.get("raw")),
                        attempts=attempt,
                    )
                last_error = outcome.get("parsing_error") or StructuredOutputError(
                    schema, attempt, "空内容"
                )
            truncated = type(last_error).__name__ == "LengthFinishReasonError"
            if truncated and initial_max_tokens is not None:
                # Thinking tokens share the output budget. Retry with more room,
                # bounded by both the attempt limit and a 16k recovery ceiling.
                ceiling = max(initial_max_tokens, 16384)
                retry_tokens = min(ceiling, (retry_tokens or initial_max_tokens) * 2)
            if attempt < max_attempts and backoff_s > 0:
                time.sleep(backoff_s * attempt)
        raise StructuredOutputError(schema, max_attempts, last_error)

    return run


def make_schema_runner(
    settings: Settings, schema: type[T], **overrides: Any
) -> Callable[[list[BaseMessage]], StructuredCall[T]]:
    """通用结构化 runner：按配置建客户端 → 官方 JSON Output → 有界重试。

    所有需要"模型直接吐结构化结果"的节点都用它，避免每个场景各写一份。
    """
    return make_structured_runner(
        build_chat_model(settings, **overrides),
        schema,
        max_attempts=settings.structured_max_attempts,
        initial_max_tokens=overrides.get("max_tokens", settings.max_tokens),
    )


def make_turn_runner(settings: Settings, **overrides: Any) -> Callable[..., Any]:
    """converse 节点用的结构化 runner（返回 ``TurnDecision``）。"""
    from ..domain.turn import TurnDecision

    return make_schema_runner(settings, TurnDecision, **overrides)


def make_finalize_runner(settings: Settings, **overrides: Any) -> Callable[..., Any]:
    """finalize 节点用的结构化 runner（返回 ``ProfileResult``）。"""
    from ..domain.profile import ProfileResult

    return make_schema_runner(settings, ProfileResult, **overrides)
