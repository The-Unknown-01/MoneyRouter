"""运行配置。

密钥读取顺序对齐 Go 侧（``agent.go``）：``DEEPSEEK_API_KEY`` → ``DEEPSEEK_API_KEY_FILE``
→ 默认 ``../.env/deepseek_api.key``。

模型参数按 DeepSeek 官方规范组织：
- 思考模式默认**开启**（官方默认），开关走 ``extra_body={"thinking": {...}}``；
- 推理力度用官方参数 ``reasoning_effort``（``low`` / ``high`` / ``max``）；
- 思考模式下 ``temperature`` 官方明确**无效**，因此该模式下不发送；
- ``max_retries`` 是 SDK 层重试（只覆盖网络/5xx），与模型层的结构化重试是两回事。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .domain.probe import ProbeThresholds

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
# 思考模式更慢，超时放宽
DEFAULT_TIMEOUT_S = 120
DEFAULT_KEY_FILE = "../.env/deepseek_api.key"
DEFAULT_TEMPERATURE = 0.3
# 思考模式下 top_p 才生效，官方有效区间 0.95–1.0；默认不发送
DEFAULT_TOP_P: float | None = None
# 思考模式的推理过程计入 completion token，上限必须留足，否则会被截断
DEFAULT_MAX_TOKENS = 4096
# SDK 层重试（仅网络错误与 5xx）
DEFAULT_MAX_RETRIES = 2
# 思考模式默认开启（官方默认）
DEFAULT_THINKING_ENABLED = True
# 官方默认 high；访谈是多轮交互，默认取 low 兼顾延迟与成本，可随时切 high/max
DEFAULT_REASONING_EFFORT = "low"
# 结构化输出失败（空内容 / 解析失败）的额外重试次数（总尝试 = 该值 + 1）
DEFAULT_STRUCTURED_RETRIES = 2

# --------------------------------------------------------------------------- #
# 联网检索（博查 Web Search）
# 官方：POST {base}/v1/web-search，Authorization: Bearer <key>
# 请求体 {"query", "freshness", "summary", "count"}，响应 data.webPages.value[]
# --------------------------------------------------------------------------- #
DEFAULT_SEARCH_PROVIDER = "bocha"
DEFAULT_BOCHA_BASE_URL = "https://api.bochaai.com"
DEFAULT_BOCHA_KEY_FILE = "../.env/bocha_api.key"
# 单条意图取回的材料条数（官方上限 10）
DEFAULT_BOCHA_COUNT = 8
DEFAULT_BOCHA_FRESHNESS = "noLimit"
DEFAULT_BOCHA_TIMEOUT_S = 30
# 并发检索的线程数：小 VPS 上不要开太大
DEFAULT_BOCHA_MAX_PARALLEL = 4
# 检索轮数上限：模型首轮出一批 + 反思后最多补一轮
DEFAULT_SEARCH_ROUNDS = 2
# 入库前是否剔除论坛/自媒体类材料（判定见 domain.finance.classify_source）。
# 全被判成低级别时会自动保留，不会把整轮打空。
DEFAULT_SEARCH_DROP_LOW_TIER = True

_BOCHA_FRESHNESS = ("noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear")

# <repo>/MoneyRouter/agent
AGENT_ROOT = Path(__file__).resolve().parents[2]

# 官方支持的力度取值；其余别名按官方映射表归一化
_EFFORT_ALIASES: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "ultra": "max",
    "max": "max",
}

_FALSE_WORDS = {"disabled", "off", "false", "0", "no"}


def _load_dotenv() -> None:
    """有 python-dotenv 就加载，没有也不报错。"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 依赖缺失时的降级
        return
    for candidate in (AGENT_ROOT / ".env", AGENT_ROOT.parent / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def resolve_api_key(env: Mapping[str, str]) -> str | None:
    """按 DEEPSEEK_API_KEY → DEEPSEEK_API_KEY_FILE → 默认文件 的顺序取密钥。"""
    key = (env.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key

    raw_path = (env.get("DEEPSEEK_API_KEY_FILE") or DEFAULT_KEY_FILE).strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = (AGENT_ROOT / path).resolve()
    if path.is_file():
        content = path.read_text(encoding="utf-8").strip()
        if content:
            return content
    return None


def resolve_base_url(env: Mapping[str, str]) -> str:
    """``DEEPSEEK_API_BASE``（LangChain 标准名）优先，兼容 Go 侧的 ``DEEPSEEK_BASE_URL``。"""
    raw = _first_nonempty(env.get("DEEPSEEK_API_BASE"), env.get("DEEPSEEK_BASE_URL"))
    return (raw or DEFAULT_BASE_URL).rstrip("/")


@dataclass(frozen=True)
class Settings:
    """模型接入配置。``api_key is None`` 表示只能走降级路径。"""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_s: int = DEFAULT_TIMEOUT_S
    temperature: float = DEFAULT_TEMPERATURE
    top_p: float | None = DEFAULT_TOP_P
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_retries: int = DEFAULT_MAX_RETRIES
    thinking_enabled: bool = DEFAULT_THINKING_ENABLED
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    structured_retries: int = DEFAULT_STRUCTURED_RETRIES
    api_key: str | None = None
    key_file: str | None = None

    @property
    def degraded(self) -> bool:
        """没有可用密钥 → 只能走规则降级路径。"""
        return not self.api_key

    @property
    def structured_max_attempts(self) -> int:
        """结构化调用的总尝试次数（首次 + 重试）。"""
        return max(1, self.structured_retries) + 1

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "未找到 DeepSeek API Key：请设置 DEEPSEEK_API_KEY 环境变量，"
                f"或把密钥写入 {self.key_file or DEFAULT_KEY_FILE}。"
            )
        return self.api_key

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, load_dotenv: bool = True) -> "Settings":
        if load_dotenv:
            _load_dotenv()
        env = env if env is not None else os.environ
        key_file = (env.get("DEEPSEEK_API_KEY_FILE") or DEFAULT_KEY_FILE).strip() or None
        return cls(
            model=(env.get("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            base_url=resolve_base_url(env),
            timeout_s=_as_int(env.get("DEEPSEEK_TIMEOUT_S"), DEFAULT_TIMEOUT_S),
            temperature=_as_float(env.get("DEEPSEEK_TEMPERATURE"), DEFAULT_TEMPERATURE),
            top_p=_as_opt_float(env.get("DEEPSEEK_TOP_P")),
            max_tokens=_as_int(env.get("DEEPSEEK_MAX_TOKENS"), DEFAULT_MAX_TOKENS),
            max_retries=_as_int(env.get("DEEPSEEK_MAX_RETRIES"), DEFAULT_MAX_RETRIES),
            thinking_enabled=_as_thinking_enabled(env.get("DEEPSEEK_THINKING")),
            reasoning_effort=_as_reasoning_effort(env.get("DEEPSEEK_REASONING_EFFORT")),
            structured_retries=_as_int(
                env.get("DEEPSEEK_STRUCTURED_RETRIES"), DEFAULT_STRUCTURED_RETRIES
            ),
            api_key=resolve_api_key(env),
            key_file=key_file,
        )

    def with_overrides(self, **changes: Any) -> "Settings":
        return replace(self, **changes)


def _first_nonempty(*candidates: str | None) -> str | None:
    for value in candidates:
        if value and str(value).strip():
            return str(value).strip()
    return None


def _as_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_float(raw: str | None, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_opt_float(raw: str | None) -> float | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _as_thinking_enabled(raw: str | None) -> bool:
    """``DEEPSEEK_THINKING=disabled``（或 off/false/0/no）可关闭思考模式；默认开启。"""
    if raw is None:
        return DEFAULT_THINKING_ENABLED
    return str(raw).strip().lower() not in _FALSE_WORDS


def _as_flag(raw: str | None, default: bool) -> bool:
    """布尔开关：未设置或为空取默认值；``off/false/0/no`` 之类算 False。"""
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in _FALSE_WORDS


def _as_reasoning_effort(raw: str | None) -> str:
    """归一化到官方支持的 ``low`` / ``high`` / ``max``；非法值回落默认。"""
    if raw is None:
        return DEFAULT_REASONING_EFFORT
    return _EFFORT_ALIASES.get(str(raw).strip().lower(), DEFAULT_REASONING_EFFORT)


# --------------------------------------------------------------------------- #
# 检索配置
# --------------------------------------------------------------------------- #
def resolve_search_api_key(env: Mapping[str, str]) -> str | None:
    """按 ``BOCHA_API_KEY`` → ``BOCHA_API_KEY_FILE`` → 默认文件 的顺序取密钥。

    与 DeepSeek 密钥同款策略：优先环境变量，其次文件，便于和 Go 侧共用部署方式。
    """
    key = (env.get("BOCHA_API_KEY") or "").strip()
    if key:
        return key

    raw_path = (env.get("BOCHA_API_KEY_FILE") or DEFAULT_BOCHA_KEY_FILE).strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = (AGENT_ROOT / path).resolve()
    if path.is_file():
        content = path.read_text(encoding="utf-8").strip()
        if content:
            return content
    return None


def _as_freshness(raw: str | None) -> str:
    value = (raw or "").strip()
    return value if value in _BOCHA_FRESHNESS else DEFAULT_BOCHA_FRESHNESS


def _clamp_int(raw: str | None, default: int, low: int, high: int) -> int:
    value = _as_int(raw, default)
    return min(high, max(low, value))


@dataclass(frozen=True)
class SearchSettings:
    """联网检索配置。``api_key is None`` 表示只能走"无材料"的降级路径。"""

    provider: str = DEFAULT_SEARCH_PROVIDER
    base_url: str = DEFAULT_BOCHA_BASE_URL
    api_key: str | None = None
    key_file: str | None = None
    count: int = DEFAULT_BOCHA_COUNT
    freshness: str = DEFAULT_BOCHA_FRESHNESS
    timeout_s: int = DEFAULT_BOCHA_TIMEOUT_S
    max_parallel: int = DEFAULT_BOCHA_MAX_PARALLEL
    max_rounds: int = DEFAULT_SEARCH_ROUNDS
    drop_low_tier: bool = DEFAULT_SEARCH_DROP_LOW_TIER

    @property
    def degraded(self) -> bool:
        """没有可用密钥 → 检索不可用，只能返回空材料。"""
        return not self.api_key

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/web-search"

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "未找到博查 API Key：请设置 BOCHA_API_KEY 环境变量，"
                f"或把密钥写入 {self.key_file or DEFAULT_BOCHA_KEY_FILE}。"
            )
        return self.api_key

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, load_dotenv: bool = True
    ) -> "SearchSettings":
        if load_dotenv:
            _load_dotenv()
        env = env if env is not None else os.environ
        key_file = (env.get("BOCHA_API_KEY_FILE") or DEFAULT_BOCHA_KEY_FILE).strip() or None
        return cls(
            provider=(env.get("SEARCH_PROVIDER") or DEFAULT_SEARCH_PROVIDER).strip()
            or DEFAULT_SEARCH_PROVIDER,
            base_url=(env.get("BOCHA_BASE_URL") or DEFAULT_BOCHA_BASE_URL).rstrip("/"),
            api_key=resolve_search_api_key(env),
            key_file=key_file,
            # 官方 web-search 单次最多返回 10 条
            count=_clamp_int(env.get("BOCHA_COUNT"), DEFAULT_BOCHA_COUNT, 1, 10),
            freshness=_as_freshness(env.get("BOCHA_FRESHNESS")),
            timeout_s=_clamp_int(env.get("BOCHA_TIMEOUT_S"), DEFAULT_BOCHA_TIMEOUT_S, 5, 120),
            max_parallel=_clamp_int(env.get("BOCHA_MAX_PARALLEL"), DEFAULT_BOCHA_MAX_PARALLEL, 1, 8),
            max_rounds=_clamp_int(env.get("SEARCH_MAX_ROUNDS"), DEFAULT_SEARCH_ROUNDS, 1, 3),
            drop_low_tier=_as_flag(
                env.get("SEARCH_DROP_LOW_TIER"), DEFAULT_SEARCH_DROP_LOW_TIER
            ),
        )

    def with_overrides(self, **changes: Any) -> "SearchSettings":
        return replace(self, **changes)


# --------------------------------------------------------------------------- #
# 结构化行情/宏观数据（AKShare，免密钥）
# --------------------------------------------------------------------------- #
DEFAULT_MARKET_DATA_ENABLED = True
# AKShare 单次调用普遍较慢（多次 HTTP + pandas 处理），且同一份数据一天内几乎不变
DEFAULT_MARKET_DATA_TIMEOUT_S = 20
DEFAULT_MARKET_DATA_CACHE_TTL_S = 6 * 3600
# 市场级数据源（东方财富全球指数）很不稳定，失败时重试次数
DEFAULT_MARKET_DATA_RETRIES = 3
# 落盘缓存：源站故障时的兜底（legulegu 会整组 504）。给得比内存缓存长得多，
# 因为它承担的是"源挂了也能出简报"的职责；数据时点由各指标的 as_of 如实反映。
DEFAULT_MARKET_DATA_DISK_TTL_S = 7 * 24 * 3600
DEFAULT_MARKET_DATA_CACHE_DIR = ".cache/market_data"
# 分位计算至少需要这么多观测点，否则不输出该窗口
PERCENTILE_MIN_OBSERVATIONS = 12


@dataclass(frozen=True)
class MarketDataSettings:
    """AKShare 取数配置。``enabled=False`` 时整个数据层直接跳过。"""

    enabled: bool = DEFAULT_MARKET_DATA_ENABLED
    timeout_s: int = DEFAULT_MARKET_DATA_TIMEOUT_S
    cache_ttl_s: int = DEFAULT_MARKET_DATA_CACHE_TTL_S
    retries: int = DEFAULT_MARKET_DATA_RETRIES
    # 落盘缓存的生存时间与目录（``None`` / 空串 = 关闭落盘缓存）
    disk_cache_ttl_s: int = DEFAULT_MARKET_DATA_DISK_TTL_S
    cache_dir: str | None = DEFAULT_MARKET_DATA_CACHE_DIR
    # 只取这些指标（留空 = 全部）
    include: tuple[str, ...] = ()

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, load_dotenv: bool = True
    ) -> "MarketDataSettings":
        if load_dotenv:
            _load_dotenv()
        env = env if env is not None else os.environ
        raw_include = (env.get("MARKET_DATA_INCLUDE") or "").strip()
        include = tuple(part.strip() for part in raw_include.split(",") if part.strip())
        return cls(
            enabled=_as_bool(env.get("MARKET_DATA_ENABLED"), DEFAULT_MARKET_DATA_ENABLED),
            timeout_s=_clamp_int(
                env.get("MARKET_DATA_TIMEOUT_S"), DEFAULT_MARKET_DATA_TIMEOUT_S, 5, 120
            ),
            cache_ttl_s=_clamp_int(
                env.get("MARKET_DATA_CACHE_TTL_S"), DEFAULT_MARKET_DATA_CACHE_TTL_S, 0, 7 * 24 * 3600
            ),
            retries=_clamp_int(env.get("MARKET_DATA_RETRIES"), DEFAULT_MARKET_DATA_RETRIES, 0, 5),
            disk_cache_ttl_s=_clamp_int(
                env.get("MARKET_DATA_DISK_TTL_S"),
                DEFAULT_MARKET_DATA_DISK_TTL_S,
                0,
                90 * 24 * 3600,
            ),
            cache_dir=(env.get("MARKET_DATA_CACHE_DIR") or DEFAULT_MARKET_DATA_CACHE_DIR).strip()
            or None,
            include=include,
        )

    def with_overrides(self, **changes: Any) -> "MarketDataSettings":
        return replace(self, **changes)


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() not in _FALSE_WORDS


# --------------------------------------------------------------------------- #
# 本月实况：关注点探测阈值
# --------------------------------------------------------------------------- #
DEFAULT_MONTH_OVER_BASELINE_FACTOR = 1.3
DEFAULT_MONTH_ONE_OFF_MIN_CENTS = 100_000
DEFAULT_MONTH_ONE_OFF_INCOME_RATIO = 0.10
DEFAULT_MONTH_BIG_DELTA_PCT = 20.0
DEFAULT_MONTH_GOAL_OFF_TRACK_FACTOR = 0.8
# 单笔投资本月亏损超过该金额（分）才值得追问
DEFAULT_MONTH_INVEST_LOSS_MIN_CENTS = 100_000
# 历史留档目录（相对 AGENT_ROOT）；留空 = 关闭留档
DEFAULT_MONTH_HISTORY_DIR = ".data/months"
# 做基线（上月 / 近三月均值）时最多回看多少个月
DEFAULT_MONTH_HISTORY_LIMIT = 3


@dataclass(frozen=True)
class MonthSettings:
    """本月实况的探测阈值 + 历史留档位置；``thresholds`` 交给 ``domain.probe`` 使用。

    ``MONTH_*`` 环境变量可覆盖（风格与 ``SearchSettings`` 一致）。
    """

    over_baseline_factor: float = DEFAULT_MONTH_OVER_BASELINE_FACTOR
    one_off_min_cents: int = DEFAULT_MONTH_ONE_OFF_MIN_CENTS
    one_off_income_ratio: float = DEFAULT_MONTH_ONE_OFF_INCOME_RATIO
    big_delta_pct: float = DEFAULT_MONTH_BIG_DELTA_PCT
    goal_off_track_factor: float = DEFAULT_MONTH_GOAL_OFF_TRACK_FACTOR
    investment_loss_min_cents: int = DEFAULT_MONTH_INVEST_LOSS_MIN_CENTS
    history_dir: str | None = DEFAULT_MONTH_HISTORY_DIR
    history_limit: int = DEFAULT_MONTH_HISTORY_LIMIT

    @property
    def thresholds(self) -> ProbeThresholds:
        return ProbeThresholds(
            over_baseline_factor=self.over_baseline_factor,
            one_off_min_cents=self.one_off_min_cents,
            one_off_income_ratio=self.one_off_income_ratio,
            big_delta_pct=self.big_delta_pct,
            goal_off_track_factor=self.goal_off_track_factor,
            investment_loss_min_cents=self.investment_loss_min_cents,
        )

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, load_dotenv: bool = True
    ) -> "MonthSettings":
        if load_dotenv:
            _load_dotenv()
        env = env if env is not None else os.environ
        raw_history_dir = env.get("MONTH_HISTORY_DIR")
        history_dir = (
            DEFAULT_MONTH_HISTORY_DIR
            if raw_history_dir is None
            else (str(raw_history_dir).strip() or None)  # 显式留空 = 关闭留档
        )
        return cls(
            over_baseline_factor=_as_float(
                env.get("MONTH_OVER_BASELINE_FACTOR"), DEFAULT_MONTH_OVER_BASELINE_FACTOR
            ),
            one_off_min_cents=_clamp_int(
                env.get("MONTH_ONE_OFF_MIN_CENTS"), DEFAULT_MONTH_ONE_OFF_MIN_CENTS, 0, 100_000_000
            ),
            one_off_income_ratio=_as_float(
                env.get("MONTH_ONE_OFF_INCOME_RATIO"), DEFAULT_MONTH_ONE_OFF_INCOME_RATIO
            ),
            big_delta_pct=_as_float(env.get("MONTH_BIG_DELTA_PCT"), DEFAULT_MONTH_BIG_DELTA_PCT),
            goal_off_track_factor=_as_float(
                env.get("MONTH_GOAL_OFF_TRACK_FACTOR"), DEFAULT_MONTH_GOAL_OFF_TRACK_FACTOR
            ),
            investment_loss_min_cents=_clamp_int(
                env.get("MONTH_INVEST_LOSS_MIN_CENTS"),
                DEFAULT_MONTH_INVEST_LOSS_MIN_CENTS,
                0,
                100_000_000,
            ),
            history_dir=history_dir,
            history_limit=_clamp_int(
                env.get("MONTH_HISTORY_LIMIT"), DEFAULT_MONTH_HISTORY_LIMIT, 0, 36
            ),
        )

    def with_overrides(self, **changes: Any) -> "MonthSettings":
        return replace(self, **changes)


# --------------------------------------------------------------------------- #
# 方案生成：工具编排轮数上限与思考开关
# --------------------------------------------------------------------------- #
# AI 自主工具编排的最大轮数（防止在 1 核 VPS 上死循环）
DEFAULT_PLAN_MAX_TOOL_ROUNDS = 4
# 工具循环默认**关闭思考**：思考模式 + tools 时 reasoning_content 必须回传，而客户端不回传会 400
DEFAULT_PLAN_TOOLS_THINKING = False


@dataclass(frozen=True)
class PlanSettings:
    """方案生成的编排参数；``PLAN_*`` 环境变量可覆盖（风格与 ``MonthSettings`` 一致）。

    情景演算的成本假设与目标阈值是**算法常量**（见 ``domain.plan``），不在此处重复配置。
    """

    max_tool_rounds: int = DEFAULT_PLAN_MAX_TOOL_ROUNDS
    tools_thinking_enabled: bool = DEFAULT_PLAN_TOOLS_THINKING

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, load_dotenv: bool = True
    ) -> "PlanSettings":
        if load_dotenv:
            _load_dotenv()
        env = env if env is not None else os.environ
        return cls(
            max_tool_rounds=_clamp_int(
                env.get("PLAN_MAX_TOOL_ROUNDS"), DEFAULT_PLAN_MAX_TOOL_ROUNDS, 1, 12
            ),
            tools_thinking_enabled=_as_bool(
                env.get("PLAN_TOOLS_THINKING"), DEFAULT_PLAN_TOOLS_THINKING
            ),
        )

    def with_overrides(self, **changes: Any) -> "PlanSettings":
        return replace(self, **changes)


# --------------------------------------------------------------------------- #
# 总结（后馈）：经验包 / 画像事件 / 月度复盘的落档位置
# --------------------------------------------------------------------------- #
DEFAULT_SUMMARY_EXPERIENCE_DIR = ".data/experience"
DEFAULT_SUMMARY_EVENT_DIR = ".data/profile_events"
DEFAULT_SUMMARY_DIR = ".data/summaries"
DEFAULT_SUMMARY_USER = "default"


@dataclass(frozen=True)
class SummarySettings:
    """总结环节的落档位置与用户标识；``SUMMARY_*`` 环境变量可覆盖。

    任一目录留空 = 关闭对应的留档（该产物只留在内存里）。
    """

    experience_dir: str | None = DEFAULT_SUMMARY_EXPERIENCE_DIR
    event_dir: str | None = DEFAULT_SUMMARY_EVENT_DIR
    summary_dir: str | None = DEFAULT_SUMMARY_DIR
    user: str = DEFAULT_SUMMARY_USER

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, load_dotenv: bool = True
    ) -> "SummarySettings":
        if load_dotenv:
            _load_dotenv()
        env = env if env is not None else os.environ

        def _dir(name: str, default: str) -> str | None:
            raw = env.get(name)
            if raw is None:
                return default
            return str(raw).strip() or None  # 显式留空 = 关闭

        return cls(
            experience_dir=_dir("SUMMARY_EXPERIENCE_DIR", DEFAULT_SUMMARY_EXPERIENCE_DIR),
            event_dir=_dir("SUMMARY_EVENT_DIR", DEFAULT_SUMMARY_EVENT_DIR),
            summary_dir=_dir("SUMMARY_SUMMARY_DIR", DEFAULT_SUMMARY_DIR),
            user=(env.get("SUMMARY_USER") or DEFAULT_SUMMARY_USER).strip() or DEFAULT_SUMMARY_USER,
        )

    def with_overrides(self, **changes: Any) -> "SummarySettings":
        return replace(self, **changes)
