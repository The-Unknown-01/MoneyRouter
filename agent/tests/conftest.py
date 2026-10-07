"""测试级的全局隔离。

**为什么必须有这个文件**：取数层的落盘缓存（`.cache/market_data/`）是"源站故障时
的兜底数据"——真机在 legulegu 这类站点挂掉时，会直接用它出简报。而测试里的假数据源
（`_loader` 返回的预置 DataFrame）如果写进了这个真实目录，就会变成"假数据兜底"：
真机看起来取数成功，数值却是测试造的。这比单纯测试失败危险得多。

所以这里把 `AGENT_ROOT` 整体重定向到 `tmp_path`——落盘缓存自然落在临时目录里，
测试之间互不干扰，也绝不碰真实缓存。

**历史留档同理**（`.data/months/`）：它是真实用户的月度数据，测试造的假月份若写进去，
下个月做基线时就会把假数据当成真历史。所以 `history.AGENT_ROOT` 一并重定向。
总结环节的三份落档（经验包 / 画像事件 / 月度复盘）同理——`summary_store.AGENT_ROOT` 同样纳入。
"""

from __future__ import annotations

import pytest

from moneyrouter_agent import history as history_mod
from moneyrouter_agent import summary_store as summary_store_mod
from moneyrouter_agent.tools import market_data as md


@pytest.fixture(autouse=True)
def _isolate_agent_dirs(tmp_path, monkeypatch):
    """每个测试独占缓存目录、历史留档目录与总结落档目录，并清空进程内缓存。"""
    monkeypatch.setenv("MONEYROUTER_CHECKPOINT_DIR", ":memory:")
    monkeypatch.setattr(md, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(history_mod, "AGENT_ROOT", tmp_path)
    monkeypatch.setattr(summary_store_mod, "AGENT_ROOT", tmp_path)
    md.clear_cache()
    yield
    md.clear_cache()
