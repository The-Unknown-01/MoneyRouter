"""AKShare 结构化取数层。

## 为什么必须有这一层

搜索引擎返回的是**网页文本**：口径不明、时点不明、无法复算。而方案里的金额要能被
独立校验，就必须有一批"带单位、带时点、带口径"的数字。这一层负责把它们取回来——
**结果只进指标表，模型没有写入通路**，所以指标不可能被编造。

## 实测踩到的坑（写代码时逐条对齐，勿凭文档臆断）

1. ``bond_zh_us_rate`` 的**中国收益率序列尾部是 NaN**（只有美债更新到最新交易日）。
   必须取"最后一个非空值"并记录它自己的日期；直接取最后一行会拿到空值。
2. 中国 CPI/PPI **不能**用 ``macro_china_cpi_yearly``（实测陈旧到 2025-09），
   要用 ``macro_china_cpi()`` / ``macro_china_ppi()``（2026-08 当期值）。
3. ``currency_boc_safe`` 的美元报价是**每 100 美元**，必须除以 100 才是汇率。
4. ``futures_foreign_hist("OIL")`` 可用，但名义是"外盘原油连续合约"，
   不能写成"布伦特"——口径写错比没有数据更糟。
5. **不同数据源的排序方向不一致**（``macro_china_cpi`` 降序、``bond_zh_us_rate`` 升序），
   不统一排序就取"最后一行"，会把 2008 年的 CPI 当成当期值报出去。

## 已放弃的数据源（2026-10-07 决策：宁缺毋滥）

实测后确认"难以稳定获取"，直接从指标表移除，而不是留着当 noise：

- **legulegu**（``stock_index_pe_lg`` / ``stock_index_pb_lg``）：曾用于 4 个指数的
  PE-TTM/PB 与历史分位。实测**连打必挂**：第 1 轮 4/4 成功、第 2 轮 2/4、第 3 轮 0/4，
  是站点限流而非偶发抖动。一次要取 8 个指标必然触发，于是：
  - 权益 PE 改用**中证指数公司官方接口** ``stock_zh_index_value_csindex``（实测 3/3 全稳）；
  - **放弃市净率 PB 与历史分位**——官方接口不提供 PB，且只返回约 20 个交易日，
    用 20 天算"近 5 年分位"是误导。
- **货币基金 7 日年化**：``fund_money_fund_daily_em`` 全为 ``---``，
  ``fund_money_rank_em`` 数值明显异常 → 短端收益直接用 Shibor 代理。
- **VIX / 美元指数**（``index_global_spot_em``）：该表里根本没有 VIX，且接口极不稳定
  （实测 3 次挂 2 次）→ 境内波动率改用中国波指（50ETF 期权 QVIX）。

## 降级纪律

- **每个指标独立降级**：一个取不到不影响其它；失败的进 ``MetricGap`` 并写明原因。
- **不静默补值**：绝不返回 "0" 或"大概值"冒充数据。
- 所有失败都是可预期的，不向上抛异常。
"""

from __future__ import annotations

import pickle
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import pandas as pd

from ..config import AGENT_ROOT, PERCENTILE_MIN_OBSERVATIONS, MarketDataSettings
from ..domain.finance import (
    METRIC_SPECS,
    PERCENTILE_WINDOWS,
    MetricGap,
    MetricPoint,
    MetricSpec,
    Percentile,
)

@dataclass(frozen=True)
class FetchOutcome:
    """一次取数的结果。

    - ``gaps``：确实没取到的指标（含技术原因）；
    - ``stale``：**走了落盘缓存兜底**的指标名称——源站故障时数据仍然可用，
      但必须如实说明这不是刚取回来的。

    实现了 ``__iter__``，所以 ``metrics, gaps = fetcher()`` 的老写法照常可用。
    """

    metrics: list[MetricPoint]
    gaps: list[MetricGap]
    stale: list[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[Any]:
        return iter((self.metrics, self.gaps))


# 数据源加载器：名字 → DataFrame
Loader = Callable[[str], Any]
# 指标构建器：加载器 → 指标
Builder = Callable[[Loader], MetricPoint]


# --------------------------------------------------------------------------- #
# 失败分类：源站级故障不值得反复重试
#
# legulegu 这类站点会整组返回 504 网关超时。这不是"这次没请求到"，而是
# "站点整体不可用"——几秒内不会自愈，按满额重试只会把 8 个估值指标从
# 5 秒拖到 3 分钟。所以只额外给一次机会。
# --------------------------------------------------------------------------- #
_SOURCE_OUTAGE_HINTS = (
    "504",
    "502",
    "503",
    "gateway time-out",
    "gateway timeout",
    "bad gateway",
    "service unavailable",
    "remotedisconnected",
    "connection aborted",
    "timed out",
    "timeout",
)


def is_source_outage(exc: BaseException) -> bool:
    """是否为"源站整体不可用"类错误（网关超时 / 连接被断）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in _SOURCE_OUTAGE_HINTS)


# --------------------------------------------------------------------------- #
# 缓存（AKShare 单次调用普遍 1–3 秒，同一份数据在 TTL 内不会变）
#
# 两级：
#   1. 进程内 TTL 缓存——快，重启即丢；
#   2. 落盘缓存——**只在实时取数失败时兜底**，源站故障不至于让整组指标消失。
# --------------------------------------------------------------------------- #
_CACHE: dict[str, tuple[float, Any]] = {}


def clear_cache() -> None:
    """清空进程内缓存（测试与强制刷新用）。"""
    _CACHE.clear()


def _cached(key: str, ttl_s: int, loader: Callable[[], Any]) -> Any:
    now = time.time()
    hit = _CACHE.get(key)
    if hit is not None and ttl_s > 0 and now - hit[0] < ttl_s:
        return hit[1]
    value = loader()
    _CACHE[key] = (now, value)
    return value


def _cache_path(cache_dir: str | Path, name: str) -> Path:
    """数据源名字 → 缓存文件路径（名字里的非法字符统一替换）。"""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return Path(cache_dir) / f"{safe}.pkl"


def write_disk_cache(cache_dir: str | Path | None, name: str, value: Any) -> None:
    """写落盘缓存。任何失败都静默忽略——缓存只是优化，不该拖垮主流程。"""
    if not cache_dir:
        return
    try:
        path = _cache_path(cache_dir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            pickle.dump({"saved_at": time.time(), "value": value}, handle)
    except Exception:  # noqa: BLE001 - 磁盘只读/满了等，直接放弃缓存
        return


def read_disk_cache(cache_dir: str | Path | None, name: str, ttl_s: int) -> Any | None:
    """读落盘缓存；不存在 / 过期 / 损坏都返回 ``None``。"""
    if not cache_dir or ttl_s <= 0:
        return None
    try:
        with _cache_path(cache_dir, name).open("rb") as handle:
            blob = pickle.load(handle)
    except Exception:  # noqa: BLE001 - 文件缺失或损坏
        return None
    if not isinstance(blob, dict) or "value" not in blob:
        return None
    if time.time() - float(blob.get("saved_at") or 0) > ttl_s:
        return None
    return blob["value"]


# --------------------------------------------------------------------------- #
# 默认数据源（akshare 延迟导入：mock 与测试路径完全不需要它）
# --------------------------------------------------------------------------- #
def default_loader(name: str) -> Any:
    """按数据源名字调用 AKShare。名字格式见各指标构建器。"""
    import akshare as ak  # 延迟导入：只在真正取数时付出这个开销

    if name == "bond_zh_us_rate":
        return ak.bond_zh_us_rate()
    if name == "macro_china_lpr":
        return ak.macro_china_lpr()
    if name == "macro_china_shibor_all":
        return ak.macro_china_shibor_all()
    if name == "macro_china_cpi":
        return ak.macro_china_cpi()
    if name == "macro_china_ppi":
        return ak.macro_china_ppi()
    if name == "macro_usa_cpi_yoy":
        return ak.macro_usa_cpi_yoy()
    if name == "index_option_50etf_qvix":
        return ak.index_option_50etf_qvix()
    if name == "spot_hist_sge":
        return ak.spot_hist_sge()
    if name == "currency_boc_safe":
        return ak.currency_boc_safe()
    if name == "futures_foreign_hist:OIL":
        return ak.futures_foreign_hist(symbol="OIL")
    if name.startswith("csindex:"):
        return ak.stock_zh_index_value_csindex(symbol=name[len("csindex:") :])
    raise KeyError(f"未知数据源：{name}")


# --------------------------------------------------------------------------- #
# 数值与日期工具
# --------------------------------------------------------------------------- #
def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result  # NaN 自我保护


def _date_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        return text[:10]
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return str(value)[:10]


def _month_str(value: Any) -> str:
    """``'2026年08月份'`` → ``'2026-08'``（月度数据的时点用月份表示）。"""
    text = str(value or "")
    match = re.search(r"(\d{4})\D+(\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return _date_str(value)


def _numeric(df: Any, column: str) -> pd.Series:
    return pd.to_numeric(df[column], errors="coerce")


def _sort_key(series: pd.Series) -> pd.Series:
    """把日期列转成可排序的键。

    支持 ``datetime`` / ``YYYY-MM-DD`` / 中文月份（``2026年08月份``）。
    之所以需要它：**不同数据源的排序方向不一致**——``macro_china_cpi`` 是降序
    （首行最新），而 ``bond_zh_us_rate`` 是升序。不统一排序就取"最后一行"，
    会把 2008 年的 CPI 当成当期值报出去。

    先用正则自己拆，而不是交给 ``pd.to_datetime`` 猜——既避免 pandas 的
    "无法推断格式"告警，也避免它对中文月份直接失败。
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce")

    text = series.astype(str).str.strip()
    full = text.str.extract(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})")
    month = text.str.extract(r"(\d{4})\D+(\d{1,2})")

    if full[1].notna().sum() == 0 and month[1].notna().sum() == 0:
        return pd.to_datetime(series, errors="coerce")

    if full[1].notna().sum() >= month[1].notna().sum():
        parts = {
            "year": pd.to_numeric(full[0], errors="coerce"),
            "month": pd.to_numeric(full[1], errors="coerce"),
            "day": pd.to_numeric(full[2], errors="coerce"),
        }
    else:
        parts = {
            "year": pd.to_numeric(month[0], errors="coerce"),
            "month": pd.to_numeric(month[1], errors="coerce"),
            "day": 1,
        }
    return pd.to_datetime(parts, errors="coerce")


def _frame(df: Any, date_column: str, value_columns: list[str]) -> pd.DataFrame:
    """抽出需要的列、转数值、丢缺失，并**统一按时间升序排列**。"""
    columns = [date_column, *value_columns]
    missing = [c for c in columns if c not in getattr(df, "columns", [])]
    if missing:
        raise ValueError(f"数据源缺少列：{'、'.join(missing)}")
    sub = df[columns].copy()
    for column in value_columns:
        sub[column] = pd.to_numeric(sub[column], errors="coerce")
    sub = sub.dropna(subset=value_columns)
    sub["_sort"] = _sort_key(sub[date_column])
    sub = sub.dropna(subset=["_sort"]).sort_values("_sort").reset_index(drop=True)
    return sub.drop(columns="_sort")


def _fmt_change(delta: float | None, unit: str) -> str:
    if delta is None:
        return ""
    if unit == "%":
        return f"{delta:+.2f} 个百分点"
    return f"{delta:+.2f}{unit}"


# --------------------------------------------------------------------------- #
# 历史分位：四个窗口都算，每个都带窗口起点与样本数
# --------------------------------------------------------------------------- #
def percentile_windows(
    sub: pd.DataFrame,
    value_column: str,
    date_column: str,
    *,
    current: float | None = None,
) -> list[Percentile]:
    """按 3 年 / 5 年 / 10 年 / 全历史算分位；样本不足的窗口直接跳过。

    窗口起点与样本数一并输出——同一个 PE 用不同窗口能算出完全不同的分位，
    不写清窗口等于不可复现。
    """
    if sub is None or len(sub) == 0:
        return []
    dated = sub.copy()
    dated["_d"] = pd.to_datetime(dated[date_column], errors="coerce")
    dated = dated.dropna(subset=["_d"])
    if len(dated) == 0:
        return []

    values = pd.to_numeric(dated[value_column], errors="coerce")
    latest = dated["_d"].iloc[-1]
    target = float(current) if current is not None else float(values.iloc[-1])

    out: list[Percentile] = []
    for window in PERCENTILE_WINDOWS:
        if window == "all":
            windowed = dated
        else:
            years = int(window[:-1])
            windowed = dated[dated["_d"] >= (latest - pd.DateOffset(years=years))]
        window_values = pd.to_numeric(windowed[value_column], errors="coerce").dropna()
        if len(window_values) < PERCENTILE_MIN_OBSERVATIONS:
            continue
        rank = float((window_values <= target).sum()) / len(window_values) * 100
        out.append(
            Percentile(
                window=window,
                value=round(rank, 1),
                start=_date_str(windowed["_d"].iloc[0]),
                observations=int(len(window_values)),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 指标构建器
# --------------------------------------------------------------------------- #
def _make(
    spec: MetricSpec,
    *,
    value: float,
    as_of: str,
    source: str,
    change: str = "",
    percentiles: list[Percentile] | None = None,
) -> MetricPoint:
    return MetricPoint(
        key=spec.key,
        label=spec.label,
        category=spec.category,
        value=round(float(value), 4),
        unit=spec.unit,
        as_of=as_of,
        source=source,
        method=spec.method,
        change=change,
        percentiles=percentiles or [],
    )


def _flat_builder(key: str, source_name: str, date_column: str, value_column: str, provider: str) -> Builder:
    """单列指标：取最后一个非空观测，并给出与上一期的变化。"""

    def build(load: Loader) -> MetricPoint:
        spec = METRIC_SPECS[key]
        sub = _frame(load(source_name), date_column, [value_column])
        if len(sub) == 0:
            raise ValueError("序列全为空")
        value = float(sub[value_column].iloc[-1])
        previous = float(sub[value_column].iloc[-2]) if len(sub) > 1 else None
        return _make(
            spec,
            value=value,
            as_of=_date_str(sub[date_column].iloc[-1]),
            source=provider,
            change=_fmt_change(value - previous if previous is not None else None, spec.unit),
        )

    return build


def _month_builder(key: str, source_name: str, month_column: str, value_column: str, provider: str) -> Builder:
    """月度同比指标：时点用月份表示。"""

    def build(load: Loader) -> MetricPoint:
        spec = METRIC_SPECS[key]
        sub = _frame(load(source_name), month_column, [value_column])
        if len(sub) == 0:
            raise ValueError("序列全为空")
        value = float(sub[value_column].iloc[-1])
        previous = float(sub[value_column].iloc[-2]) if len(sub) > 1 else None
        return _make(
            spec,
            value=value,
            as_of=_month_str(sub[month_column].iloc[-1]),
            source=provider,
            change=_fmt_change(value - previous if previous is not None else None, spec.unit),
        )

    return build


# 中证指数公司官方估值接口：指数编制方自己发布，稳定且口径权威，
# 但它**只返回最近约 20 个交易日**——所以这一组不提供历史分位。
CSINDEX_PE_COLUMN = "市盈率2"
CSINDEX_PROVIDER = "中证指数公司官方"
CSINDEX_CODES: dict[str, str] = {
    "hs300_pe_ttm": "000300",
    "sz50_pe_ttm": "000016",
    "csi500_pe_ttm": "000905",
    "csi1000_pe_ttm": "000852",
}


def _with_date_key(frame: pd.DataFrame, date_column: str) -> pd.DataFrame:
    """加一列规范化的日期键（``YYYY-MM-DD``），用于跨数据源对齐。

    不同数据源的日期列类型不一致（``datetime.date`` / ``Timestamp`` / ``str``），
    直接 merge 会静默对不上，所以统一先归一化。
    """
    keys = pd.to_datetime(frame[date_column], errors="coerce").dt.strftime("%Y-%m-%d")
    return frame.assign(_dk=keys).dropna(subset=["_dk"])


def _csindex_pe_builder(key: str, code: str) -> Builder:
    """指数市盈率：中证指数公司官方接口。

    **刻意不给历史分位**：接口只返回最近约 20 个交易日，用这么短的样本算出的
    "近 5 年分位"是误导（会显示成"近 5 年 3% 分位"而实际只有 20 天数据）。
    宁可只给当期值与日环比，并在口径里写清来源。
    """
    source_name = f"csindex:{code}"

    def build(load: Loader) -> MetricPoint:
        spec = METRIC_SPECS[key]
        sub = _frame(load(source_name), "日期", [CSINDEX_PE_COLUMN])
        if len(sub) == 0:
            raise ValueError("序列全为空")
        value = float(sub[CSINDEX_PE_COLUMN].iloc[-1])
        previous = float(sub[CSINDEX_PE_COLUMN].iloc[-2]) if len(sub) > 1 else None
        return _make(
            spec,
            value=value,
            as_of=_date_str(sub["日期"].iloc[-1]),
            source=CSINDEX_PROVIDER,
            change=_fmt_change(value - previous if previous is not None else None, spec.unit),
        )

    return build


def _percentile_builder(key: str, source_name: str, date_column: str, value_column: str, provider: str) -> Builder:
    """带分位的高频序列（波指、金价、汇率）。"""

    def build(load: Loader) -> MetricPoint:
        spec = METRIC_SPECS[key]
        sub = _frame(load(source_name), date_column, [value_column])
        if len(sub) == 0:
            raise ValueError("序列全为空")
        value = float(sub[value_column].iloc[-1])
        previous = float(sub[value_column].iloc[-2]) if len(sub) > 1 else None
        return _make(
            spec,
            value=value,
            as_of=_date_str(sub[date_column].iloc[-1]),
            source=provider,
            change=_fmt_change(value - previous if previous is not None else None, spec.unit),
            percentiles=percentile_windows(sub, value_column, date_column, current=value),
        )

    return build


def _term_spread_builder(load: Loader) -> MetricPoint:
    """10Y−2Y 期限利差：同一行相减，保证两个利率是同一天的。"""
    spec = METRIC_SPECS["cn_term_spread"]
    sub = _frame(load("bond_zh_us_rate"), "日期", ["中国国债收益率10年", "中国国债收益率2年"])
    if len(sub) == 0:
        raise ValueError("序列全为空")
    spread = sub["中国国债收益率10年"] - sub["中国国债收益率2年"]
    value = float(spread.iloc[-1])
    previous = float(spread.iloc[-2]) if len(spread) > 1 else None
    return _make(
        spec,
        value=value,
        as_of=_date_str(sub["日期"].iloc[-1]),
        source="中债/东方财富",
        change=_fmt_change(value - previous if previous is not None else None, spec.unit),
    )


def _usdcny_builder(load: Loader) -> MetricPoint:
    """中行中间价：原始报价是"每 100 美元"，必须除以 100。"""
    spec = METRIC_SPECS["usdcny_mid"]
    sub = _frame(load("currency_boc_safe"), "日期", ["美元"])
    if len(sub) == 0:
        raise ValueError("序列全为空")
    sub["美元"] = sub["美元"] / 100.0
    value = float(sub["美元"].iloc[-1])
    previous = float(sub["美元"].iloc[-2]) if len(sub) > 1 else None
    return _make(
        spec,
        value=value,
        as_of=_date_str(sub["日期"].iloc[-1]),
        source="中国银行/国家外汇管理局",
        change=_fmt_change(value - previous if previous is not None else None, spec.unit),
        percentiles=percentile_windows(sub, "美元", "日期", current=value),
    )


def _erp_builder(load: Loader) -> MetricPoint:
    """沪深300 股权风险溢价 = 100/市盈率 − 10 年期国债收益率。

    两个序列都是日频，但**交易日并不重合**（中证估值只覆盖最近 20 个交易日，
    中债收益率尾部还有 NaN），所以按**同一日期内连接**再取最后一个可用点——
    绝不把不同日期的两个数硬凑成一对。
    """
    spec = METRIC_SPECS["hs300_erp"]
    pe = _frame(load(f"csindex:{CSINDEX_CODES['hs300_pe_ttm']}"), "日期", [CSINDEX_PE_COLUMN])
    bond = _frame(load("bond_zh_us_rate"), "日期", ["中国国债收益率10年"])
    if len(pe) == 0 or len(bond) == 0:
        raise ValueError("构成 ERP 的序列为空")

    merged = _with_date_key(pe, "日期").merge(
        _with_date_key(bond, "日期"), on="_dk", how="inner", suffixes=("_pe", "_bond")
    ).dropna(subset=[CSINDEX_PE_COLUMN, "中国国债收益率10年"])
    if len(merged) == 0:
        raise ValueError("市盈率与国债收益率没有重合的日期")
    merged = merged.assign(erp=100.0 / merged[CSINDEX_PE_COLUMN] - merged["中国国债收益率10年"])

    value = float(merged["erp"].iloc[-1])
    previous = float(merged["erp"].iloc[-2]) if len(merged) > 1 else None
    return _make(
        spec,
        value=value,
        as_of=str(merged["_dk"].iloc[-1]),
        source="由沪深300 市盈率（中证指数公司）与 10 年期国债收益率推导",
        change=_fmt_change(value - previous if previous is not None else None, spec.unit),
    )


_METRIC_BUILDERS: dict[str, Builder] = {
    # 无风险基准
    "cn_10y_yield": _flat_builder("cn_10y_yield", "bond_zh_us_rate", "日期", "中国国债收益率10年", "中债/东方财富"),
    "cn_2y_yield": _flat_builder("cn_2y_yield", "bond_zh_us_rate", "日期", "中国国债收益率2年", "中债/东方财富"),
    "cn_term_spread": _term_spread_builder,
    "us_10y_yield": _flat_builder("us_10y_yield", "bond_zh_us_rate", "日期", "美国国债收益率10年", "美国财政部/东方财富"),
    "cn_lpr_1y": _flat_builder("cn_lpr_1y", "macro_china_lpr", "TRADE_DATE", "LPR1Y", "中国人民银行"),
    "cn_lpr_5y": _flat_builder("cn_lpr_5y", "macro_china_lpr", "TRADE_DATE", "LPR5Y", "中国人民银行"),
    # 现金类收益
    "cn_shibor_on": _flat_builder("cn_shibor_on", "macro_china_shibor_all", "日期", "O/N-定价", "全国银行间同业拆借中心"),
    "cn_shibor_3m": _flat_builder("cn_shibor_3m", "macro_china_shibor_all", "日期", "3M-定价", "全国银行间同业拆借中心"),
    # 通胀
    "cn_cpi_yoy": _month_builder("cn_cpi_yoy", "macro_china_cpi", "月份", "全国-同比增长", "国家统计局"),
    "cn_ppi_yoy": _month_builder("cn_ppi_yoy", "macro_china_ppi", "月份", "当月同比增长", "国家统计局"),
    "us_cpi_yoy": _flat_builder("us_cpi_yoy", "macro_usa_cpi_yoy", "时间", "现值", "美国劳工统计局"),
    # 权益估值（中证指数公司官方口径；接口只给约 20 个交易日，故无历史分位）
    "hs300_pe_ttm": _csindex_pe_builder("hs300_pe_ttm", CSINDEX_CODES["hs300_pe_ttm"]),
    "sz50_pe_ttm": _csindex_pe_builder("sz50_pe_ttm", CSINDEX_CODES["sz50_pe_ttm"]),
    "csi500_pe_ttm": _csindex_pe_builder("csi500_pe_ttm", CSINDEX_CODES["csi500_pe_ttm"]),
    "csi1000_pe_ttm": _csindex_pe_builder("csi1000_pe_ttm", CSINDEX_CODES["csi1000_pe_ttm"]),
    # 波动与风险
    "hs300_erp": _erp_builder,
    "cn_qvix_50etf": _percentile_builder(
        "cn_qvix_50etf", "index_option_50etf_qvix", "date", "close", "上证50ETF期权隐含波动率"
    ),
    # 避险资产
    "gold_cny_gram": _percentile_builder("gold_cny_gram", "spot_hist_sge", "date", "close", "上海黄金交易所"),
    "usdcny_mid": _usdcny_builder,
    "crude_oil": _percentile_builder("crude_oil", "futures_foreign_hist:OIL", "date", "close", "外盘原油连续合约"),
}


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
def resolve_cache_dir(raw: str | None) -> Path | None:
    """把配置里的缓存目录解析成绝对路径（相对路径以 ``agent/`` 为基准）。"""
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else AGENT_ROOT / path


class MarketDataClient:
    """按注册表逐个取数；每个指标独立降级，绝不整体失败。"""

    def __init__(self, settings: MarketDataSettings, *, loader: Loader | None = None) -> None:
        self.settings = settings
        self._loader: Loader = loader or default_loader
        self._cache_dir = resolve_cache_dir(settings.cache_dir)
        # 本次取数中"走了落盘兜底"的数据源名字（fetch 据此回报给上层）
        self._stale_names: set[str] = set()

    def _load(self, name: str) -> Any:
        """取一份原始数据：内存缓存 → 实时取数 → 落盘兜底。

        落盘兜底**只在实时取数失败时**启用：源站（如 legulegu）整组 504 时，
        仍能给出上一次成功取到的序列。此时数据时点由各指标的 ``as_of`` 如实反映，
        并通过 ``FetchOutcome.stale`` 回报给上层——绝不假装是刚取的。
        """
        now = time.time()
        ttl = self.settings.cache_ttl_s
        hit = _CACHE.get(name)
        if hit is not None and ttl > 0 and now - hit[0] < ttl:
            return hit[1]

        try:
            value = self._fetch_with_retry(name)
        except Exception:  # noqa: BLE001 - 实时取数失败才考虑兜底
            fallback = read_disk_cache(self._cache_dir, name, self.settings.disk_cache_ttl_s)
            if fallback is None:
                raise
            self._stale_names.add(name)
            _CACHE[name] = (now, fallback)  # 兜底值也进内存，避免同一轮反复读盘
            return fallback

        _CACHE[name] = (now, value)
        write_disk_cache(self._cache_dir, name, value)
        return value

    def _fetch_with_retry(self, name: str) -> Any:
        """实时取数 + 有界重试。

        **源站级故障只额外给一次机会**：网关超时这类错误几秒内不会自愈，
        按满额重试只会把一个指标从 5 秒拖到 20 秒（8 个估值指标就是 3 分钟）。
        """
        last_error: Exception | None = None
        allowed = self.settings.retries
        attempt = 0
        while attempt <= allowed:
            try:
                return self._loader(name)
            except Exception as exc:  # noqa: BLE001 - 重试用尽后交给指标级降级
                last_error = exc
                if attempt < allowed and is_source_outage(exc):
                    allowed = attempt + 1
                if attempt < allowed:
                    time.sleep(0.5 * (attempt + 1))
                attempt += 1
        assert last_error is not None
        raise last_error

    def fetch(self) -> FetchOutcome:
        """取回全部（或 ``include`` 指定的）指标。"""
        if not self.settings.enabled:
            return FetchOutcome(
                metrics=[],
                gaps=[
                    MetricGap(key=spec.key, label=spec.label, reason="数据层已关闭")
                    for spec in METRIC_SPECS.values()
                ],
            )

        wanted = set(self.settings.include) if self.settings.include else None
        metrics: list[MetricPoint] = []
        gaps: list[MetricGap] = []
        stale: list[str] = []
        self._stale_names = set()
        for key, spec in METRIC_SPECS.items():
            if wanted is not None and key not in wanted:
                continue
            builder = _METRIC_BUILDERS.get(key)
            if builder is None:
                gaps.append(MetricGap(key=key, label=spec.label, reason="未实现该指标的取数"))
                continue
            before = set(self._stale_names)
            try:
                metrics.append(builder(self._load))
            except Exception as exc:  # noqa: BLE001 - 单个指标失败不影响其它
                gaps.append(
                    MetricGap(
                        key=key,
                        label=spec.label,
                        reason=f"{type(exc).__name__}: {' '.join(str(exc).split())[:80]}",
                    )
                )
                continue
            if self._stale_names - before:
                stale.append(spec.label)
        return FetchOutcome(metrics=metrics, gaps=gaps, stale=stale)
