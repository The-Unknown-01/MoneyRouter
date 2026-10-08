"""AKShare 取数层：全部离线——用一个假 loader 顶替真实网络。

这里最重要的是几条**回归测试**：它们锁住实测踩到的坑，
任何一条被破坏都意味着输出会变成错误的数字。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from moneyrouter_agent.config import MarketDataSettings
from moneyrouter_agent.tools import market_data as md


@pytest.fixture(autouse=True)
def _clear_cache():
    md.clear_cache()
    yield
    md.clear_cache()


def _loader(frames: dict[str, Any], *, fail: set[str] | None = None):
    """假数据源：按名字返回预置 DataFrame，记录调用次数。"""
    calls: list[str] = []

    def load(name: str) -> Any:
        calls.append(name)
        if fail and name in fail:
            raise RuntimeError(f"数据源 {name} 故障")
        if name not in frames:
            raise KeyError(f"测试未准备数据源：{name}")
        return frames[name]

    load.calls = calls  # type: ignore[attr-defined]
    return load


def _client(frames: dict[str, Any], *, include: tuple[str, ...], **overrides: Any):
    settings = MarketDataSettings(include=include, cache_ttl_s=0, **overrides)
    return md.MarketDataClient(settings, loader=_loader(frames))


# --------------------------------------------------------------------------- #
# 回归 1：降序数据源不能把最老的当最新
# --------------------------------------------------------------------------- #
def test_descending_source_takes_newest_not_oldest():
    """``macro_china_cpi`` 是**降序**的（首行最新）。

    曾经的 bug：直接取 ``.iloc[-1]``，把 2008 年的 CPI 当成当期值报了出去。
    """
    frame = pd.DataFrame(
        {
            "月份": ["2026年08月份", "2026年07月份", "2008年01月份"],
            "全国-同比增长": [0.8, 0.5, 7.07],
        }
    )
    metrics, gaps = _client({"macro_china_cpi": frame}, include=("cn_cpi_yoy",)).fetch()

    assert gaps == []
    assert metrics[0].value == 0.8
    assert metrics[0].as_of == "2026-08"
    assert metrics[0].change == "+0.30 个百分点"


def test_ascending_source_still_works():
    """升序与降序都要得到同一个结果——排序是统一处理的。"""
    ascending = pd.DataFrame({"月份": ["2008年01月份", "2026年07月份", "2026年08月份"], "全国-同比增长": [7.07, 0.5, 0.8]})
    metrics, _ = _client({"macro_china_cpi": ascending}, include=("cn_cpi_yoy",)).fetch()
    assert metrics[0].value == 0.8


# --------------------------------------------------------------------------- #
# 回归 2：债券收益率尾部是 NaN，要取最后一个非空值
# --------------------------------------------------------------------------- #
def test_bond_yield_skips_trailing_nan():
    frame = pd.DataFrame(
        {
            "日期": [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 6)],
            "中国国债收益率10年": [1.66, 1.68, float("nan"), float("nan")],
        }
    )
    metrics, _ = _client({"bond_zh_us_rate": frame}, include=("cn_10y_yield",)).fetch()

    assert metrics[0].value == 1.68
    assert metrics[0].as_of == "2026-09-30"  # 用的是这个值自己的日期，不是最后一行


def test_term_spread_uses_the_same_row_for_both_yields():
    """期限利差必须同一天相减——两个利率的最新日期可能不同。"""
    frame = pd.DataFrame(
        {
            "日期": [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)],
            "中国国债收益率10年": [1.70, 1.68, float("nan")],
            "中国国债收益率2年": [1.30, 1.27, float("nan")],
        }
    )
    metrics, _ = _client({"bond_zh_us_rate": frame}, include=("cn_term_spread",)).fetch()

    assert metrics[0].value == pytest.approx(0.41)
    assert metrics[0].as_of == "2026-09-30"


# --------------------------------------------------------------------------- #
# 回归 3：中行中间价原始报价是"每 100 美元"
# --------------------------------------------------------------------------- #
def test_usdcny_is_divided_by_100():
    frame = pd.DataFrame({"日期": [date(2026, 9, 29), date(2026, 9, 30)], "美元": [673.0, 673.51]})
    metrics, _ = _client({"currency_boc_safe": frame}, include=("usdcny_mid",)).fetch()

    assert metrics[0].value == pytest.approx(6.7351)
    assert metrics[0].unit == "元"


# --------------------------------------------------------------------------- #
# 分位：多窗口、带起点与样本数、样本不足则跳过
# --------------------------------------------------------------------------- #
def _gold_frame(periods: int, *, last_value: float | None = None) -> pd.DataFrame:
    """带分位的指标（金价）用的序列；这一组仍是长历史、仍给分位。"""
    dates = pd.date_range("2015-10-31", periods=periods, freq="ME")
    values = [float(i) for i in range(1, periods + 1)]
    if last_value is not None:
        values[-1] = last_value
    return pd.DataFrame({"date": dates, "close": values})


def test_percentiles_cover_four_windows_with_provenance():
    frame = _gold_frame(130, last_value=65.0)
    metrics, _ = _client({"spot_hist_sge": frame}, include=("gold_cny_gram",)).fetch()

    windows = {p.window: p for p in metrics[0].percentiles}
    assert set(windows) == {"3y", "5y", "10y", "all"}
    # 序列是 1..129 加一个 65，共 130 个观测，其中 66 个不超过 65
    assert windows["all"].value == pytest.approx(66 / 130 * 100, abs=0.5)
    # 近 3 年窗口里几乎全在它之上
    assert windows["3y"].value < 10
    # 每个窗口都带起点与样本数，否则不可复现
    for window in windows.values():
        assert window.start and window.observations >= 12


def test_percentile_skipped_when_history_too_short():
    frame = _gold_frame(6)
    metrics, _ = _client({"spot_hist_sge": frame}, include=("gold_cny_gram",)).fetch()

    assert metrics[0].percentiles == []


def test_index_valuation_has_no_percentile_by_design():
    """中证官方接口只给约 20 个交易日 —— 刻意不给分位。

    若哪天有人给它接回分位，就会看到"近 5 年分位"实际只有 20 天样本的荒谬结果，
    所以这条守着它。
    """
    days = pd.date_range("2026-09-01", periods=20, freq="B")
    frame = pd.DataFrame({"日期": days, "市盈率2": [12.0 + i * 0.1 for i in range(20)]})
    metrics, _ = _client({"csindex:000300": frame}, include=("hs300_pe_ttm",)).fetch()

    assert metrics[0].percentiles == []
    assert metrics[0].value == pytest.approx(13.9)
    assert metrics[0].source == "中证指数公司官方"


# --------------------------------------------------------------------------- #
# 降级：单指标失败不影响其它；整体关闭时全部进缺口
# --------------------------------------------------------------------------- #
def test_one_failed_metric_does_not_block_others():
    good = pd.DataFrame({"日期": [date(2026, 9, 30)], "中国国债收益率10年": [1.68]})
    settings = MarketDataSettings(include=("cn_10y_yield", "cn_cpi_yoy"), cache_ttl_s=0)
    client = md.MarketDataClient(settings, loader=_loader({"bond_zh_us_rate": good}))
    metrics, gaps = client.fetch()

    assert [m.key for m in metrics] == ["cn_10y_yield"]
    assert [g.key for g in gaps] == ["cn_cpi_yoy"]
    assert gaps[0].reason and "KeyError" in gaps[0].reason


def test_disabled_layer_reports_every_metric_as_gap():
    settings = MarketDataSettings(enabled=False, cache_ttl_s=0)
    metrics, gaps = md.MarketDataClient(settings, loader=_loader({})).fetch()

    assert metrics == []
    assert len(gaps) == len(md.METRIC_SPECS)
    assert all("已关闭" in gap.reason for gap in gaps)


def test_loader_error_message_is_trimmed_for_display():
    settings = MarketDataSettings(include=("cn_10y_yield",), cache_ttl_s=0)
    client = md.MarketDataClient(
        settings, loader=_loader({}, fail={"bond_zh_us_rate"})
    )
    _, gaps = client.fetch()

    assert len(gaps) == 1
    assert len(gaps[0].reason) < 120


def test_retries_before_giving_up():
    attempts: list[str] = []

    def flaky(name: str) -> Any:
        attempts.append(name)
        if len(attempts) < 3:
            raise RuntimeError("临时的网络抖动")
        return pd.DataFrame({"日期": [date(2026, 9, 30)], "中国国债收益率10年": [1.68]})

    settings = MarketDataSettings(include=("cn_10y_yield",), cache_ttl_s=0, retries=3)
    metrics, gaps = md.MarketDataClient(settings, loader=flaky).fetch()

    assert gaps == []
    assert len(attempts) == 3


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
def test_same_source_is_loaded_once_per_fetch():
    frame = pd.DataFrame(
        {
            "日期": [date(2026, 9, 30)],
            "中国国债收益率10年": [1.68],
            "中国国债收益率2年": [1.27],
        }
    )
    load = _loader({"bond_zh_us_rate": frame})
    settings = MarketDataSettings(
        include=("cn_10y_yield", "cn_2y_yield", "cn_term_spread"), cache_ttl_s=3600
    )
    md.MarketDataClient(settings, loader=load).fetch()

    assert load.calls.count("bond_zh_us_rate") == 1  # 三个指标共用一个数据源


def test_cache_can_be_disabled():
    frame = pd.DataFrame({"日期": [date(2026, 9, 30)], "中国国债收益率10年": [1.68]})
    load = _loader({"bond_zh_us_rate": frame})
    settings = MarketDataSettings(include=("cn_10y_yield",), cache_ttl_s=0)
    client = md.MarketDataClient(settings, loader=load)
    client.fetch()
    client.fetch()

    assert load.calls.count("bond_zh_us_rate") == 2


# --------------------------------------------------------------------------- #
# 落盘缓存：源站故障时的兜底
# --------------------------------------------------------------------------- #
_TEN_Y = pd.DataFrame({"日期": [date(2026, 9, 30)], "中国国债收益率10年": [1.68]})


def test_source_outage_is_retried_only_once():
    """504 这类源站故障几秒内不会自愈，不该按满额重试把取数拖成 3 分钟。"""
    calls: list[str] = []

    def down(name: str) -> Any:
        calls.append(name)
        raise RuntimeError(
            "API Error: legulegu 请求失败，请确认该站点在当前网络下可正常访问："
            "504 Server Error: Gateway Time-out"
        )

    settings = MarketDataSettings(include=("cn_10y_yield",), cache_ttl_s=0, retries=3)
    _, gaps = md.MarketDataClient(settings, loader=down).fetch()

    assert len(gaps) == 1
    assert len(calls) == 2, "源站级故障只额外给一次机会（首次 + 1 次重试）"


def test_transient_error_still_uses_full_retries():
    """对照组：普通抖动仍按配置重试，别把恢复能力一并削掉。"""
    attempts: list[int] = []

    def flaky(name: str) -> Any:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 4:
            raise RuntimeError("临时的网络抖动")
        return _TEN_Y

    settings = MarketDataSettings(include=("cn_10y_yield",), cache_ttl_s=0, retries=3)
    metrics, gaps = md.MarketDataClient(settings, loader=flaky).fetch()

    assert gaps == []
    assert len(attempts) == 4


def test_disk_cache_carries_over_a_source_outage():
    """源站挂掉时用上一次成功取回的数据兜底，并把它如实回报为 stale。"""
    settings = MarketDataSettings(include=("cn_10y_yield",), cache_ttl_s=0, retries=0)

    first = md.MarketDataClient(settings, loader=_loader({"bond_zh_us_rate": _TEN_Y})).fetch()
    assert [m.key for m in first.metrics] == ["cn_10y_yield"]
    assert first.stale == []

    def down(_name: str) -> Any:
        raise RuntimeError("504 Server Error: Gateway Time-out")

    second = md.MarketDataClient(settings, loader=down).fetch()

    assert [m.key for m in second.metrics] == ["cn_10y_yield"]
    assert second.gaps == []
    assert second.stale == [md.METRIC_SPECS["cn_10y_yield"].label]
    # 数据时点仍是原序列的日期，不会被伪装成"刚取回来的"
    assert second.metrics[0].as_of == "2026-09-30"


def test_corrupt_disk_cache_is_ignored_not_trusted():
    """缓存文件损坏时必须如实进缺口，不能拿坏数据充数。"""
    settings = MarketDataSettings(include=("cn_10y_yield",), cache_ttl_s=0, retries=0)
    md.MarketDataClient(settings, loader=_loader({"bond_zh_us_rate": _TEN_Y})).fetch()

    cache_dir = md.resolve_cache_dir(settings.cache_dir)
    assert cache_dir is not None
    md._cache_path(cache_dir, "bond_zh_us_rate").write_bytes(b"not a pickle at all")

    def down(_name: str) -> Any:
        raise RuntimeError("504 Server Error: Gateway Time-out")

    result = md.MarketDataClient(settings, loader=down).fetch()

    assert result.metrics == []
    assert len(result.gaps) == 1
    assert result.stale == []


def test_disk_cache_can_be_turned_off():
    settings = MarketDataSettings(
        include=("cn_10y_yield",), cache_ttl_s=0, retries=0, cache_dir=None
    )
    md.MarketDataClient(settings, loader=_loader({"bond_zh_us_rate": _TEN_Y})).fetch()

    assert md.resolve_cache_dir(None) is None
    assert not (md.AGENT_ROOT / ".cache").exists()


def test_tests_never_write_the_real_cache_dir():
    """守卫：真实缓存目录里的数据会被真机在源站故障时兜底使用，
    所以测试绝不能往里写假数据（由 ``tests/conftest.py`` 重定向保证）。"""
    real_agent_root = Path(md.__file__).resolve().parents[3]
    assert md.AGENT_ROOT != real_agent_root


# --------------------------------------------------------------------------- #
# 派生指标
# --------------------------------------------------------------------------- #
def test_erp_is_derived_from_pe_and_bond_yield():
    """ERP = 100/市盈率 − 10 年期收益率，按**同一日期**内连接对齐。

    两个序列交易日并不重合（中证估值只覆盖最近 20 个交易日），
    所以必须内连接，不能各自取最后一个值硬凑。
    """
    days = pd.date_range("2026-09-01", periods=20, freq="B")
    pe = pd.DataFrame({"日期": days, "市盈率2": [10.0] * 20})
    bond = pd.DataFrame({"日期": days, "中国国债收益率10年": [2.0] * 20})
    metrics, gaps = _client(
        {"csindex:000300": pe, "bond_zh_us_rate": bond}, include=("hs300_erp",)
    ).fetch()

    assert gaps == []
    # 100/10 − 2.0 = 8.0
    assert metrics[0].value == pytest.approx(8.0)
    assert metrics[0].percentiles == []  # 20 天样本，刻意不给分位
    assert metrics[0].as_of == days[-1].strftime("%Y-%m-%d")


def test_erp_reports_gap_when_dates_do_not_overlap():
    """两份序列没有重合日期时必须如实报缺口，而不是拿两个不同日期的值相减。"""
    pe = pd.DataFrame({"日期": pd.date_range("2026-09-01", periods=5, freq="B"), "市盈率2": [10.0] * 5})
    bond = pd.DataFrame(
        {"日期": pd.date_range("2026-01-05", periods=5, freq="B"), "中国国债收益率10年": [2.0] * 5}
    )
    metrics, gaps = _client(
        {"csindex:000300": pe, "bond_zh_us_rate": bond}, include=("hs300_erp",)
    ).fetch()

    assert metrics == []
    assert len(gaps) == 1 and "没有重合的日期" in gaps[0].reason
