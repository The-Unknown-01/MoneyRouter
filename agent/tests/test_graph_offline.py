"""离线降级路径：没有模型也能跑通整条图。"""

from moneyrouter_agent.agent import ProfileAgent
from moneyrouter_agent.config import Settings


def _offline_agent() -> ProfileAgent:
    # 显式不给密钥、也不读 .env，强制降级
    return ProfileAgent(settings=Settings(api_key=None, key_file=None))


def test_first_turn_degrades_to_rule_guide():
    agent = _offline_agent()
    result = agent.turn("offline-1", "你好")

    assert result.degraded is True
    assert result.error is not None
    assert result.ready_to_finalize is False
    assert result.awaiting_confirmation is False
    # 降级路径应是规则引导文案，而不是空回复
    assert result.reply
    assert "方便先说说您" in result.reply


def test_offline_turns_keep_working_and_never_finalize():
    agent = _offline_agent()
    for index, text in enumerate(["你好", "我是学生", "每月生活费大概两千"], start=1):
        result = agent.turn("offline-2", text)
        assert result.degraded is True
        assert result.turn_count == index
        assert result.ready_to_finalize is False
        assert result.confirmed is False
        assert result.reply


def test_offline_snapshot_is_read_only():
    agent = _offline_agent()
    agent.turn("offline-3", "你好")
    snap = agent.snapshot("offline-3")
    assert snap.thread_id == "offline-3"
    assert snap.turn_count == 1
