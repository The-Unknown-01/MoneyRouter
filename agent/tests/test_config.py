"""配置层单测：密钥读取顺序、思考模式开关与力度、base URL 兼容、降级判定。"""

from moneyrouter_agent.config import Settings, resolve_api_key, resolve_base_url


def test_api_key_from_env_wins():
    key = resolve_api_key({"DEEPSEEK_API_KEY": "  abc123  ", "DEEPSEEK_API_KEY_FILE": "/nope"})
    assert key == "abc123"


def test_api_key_falls_back_to_file(tmp_path):
    key_file = tmp_path / "deepseek_api.key"
    key_file.write_text("file-key\n", encoding="utf-8")
    key = resolve_api_key({"DEEPSEEK_API_KEY_FILE": str(key_file)})
    assert key == "file-key"


def test_api_key_missing_returns_none(tmp_path):
    assert resolve_api_key({"DEEPSEEK_API_KEY_FILE": str(tmp_path / "absent")}) is None


def test_degraded_when_no_key():
    assert Settings(api_key=None).degraded is True
    assert Settings(api_key="k").degraded is False


# --------------------------------------------------------------------------- #
# base URL：兼容 Go 侧 agent.go 用的 DEEPSEEK_BASE_URL
# --------------------------------------------------------------------------- #
def test_base_url_accepts_go_side_env_name():
    assert (
        resolve_base_url({"DEEPSEEK_BASE_URL": "https://api.deepseek.com/"})
        == "https://api.deepseek.com"
    )


def test_base_url_prefers_langchain_standard_name():
    assert (
        resolve_base_url(
            {"DEEPSEEK_API_BASE": "https://a.example.com", "DEEPSEEK_BASE_URL": "https://b.example.com"}
        )
        == "https://a.example.com"
    )


def test_base_url_defaults():
    assert resolve_base_url({}) == "https://api.deepseek.com"


# --------------------------------------------------------------------------- #
# 思考模式（官方默认开启）与推理力度
# --------------------------------------------------------------------------- #
def test_thinking_enabled_by_default():
    settings = Settings.from_env(env={}, load_dotenv=False)
    assert settings.thinking_enabled is True


def test_thinking_toggle_words():
    for word in ("disabled", "off", "FALSE", "0", "no"):
        assert Settings.from_env(env={"DEEPSEEK_THINKING": word}, load_dotenv=False).thinking_enabled is False
    for word in ("enabled", "on", "true", "1", "yes"):
        assert Settings.from_env(env={"DEEPSEEK_THINKING": word}, load_dotenv=False).thinking_enabled is True


def test_reasoning_effort_defaults_to_low():
    assert Settings.from_env(env={}, load_dotenv=False).reasoning_effort == "low"


def test_reasoning_effort_normalised_to_official_values():
    cases = {
        "LOW": "low",
        "minimal": "low",
        "medium": "high",
        "high": "high",
        "xhigh": "max",
        "ultra": "max",
        "max": "max",
        "莫名其妙的取值": "low",
    }
    for raw, expected in cases.items():
        settings = Settings.from_env(env={"DEEPSEEK_REASONING_EFFORT": raw}, load_dotenv=False)
        assert settings.reasoning_effort == expected, raw


# --------------------------------------------------------------------------- #
# 生成与重试
# --------------------------------------------------------------------------- #
def test_max_tokens_leaves_room_for_thinking():
    """思考模式的推理过程计入 completion token，上限必须留足。"""
    assert Settings.from_env(env={}, load_dotenv=False).max_tokens >= 4096


def test_retry_defaults_and_total_attempts():
    settings = Settings.from_env(env={}, load_dotenv=False)
    assert settings.max_retries == 2  # SDK 层：网络/5xx
    assert settings.structured_retries == 2  # 模型层：空内容/解析失败
    assert settings.structured_max_attempts == 3


def test_top_p_optional():
    assert Settings.from_env(env={}, load_dotenv=False).top_p is None
    assert Settings.from_env(env={"DEEPSEEK_TOP_P": "0.97"}, load_dotenv=False).top_p == 0.97


def test_defaults_match_go_side():
    settings = Settings.from_env(env={}, load_dotenv=False)
    assert settings.model == "deepseek-flash"
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.timeout_s == 120  # 思考模式更慢，比 Go 侧的 18s 宽松
