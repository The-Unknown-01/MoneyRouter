"""本月实况的历史留档：把落定后的月度快照按期间存下来。

为什么要有这一层：采集环节产出的只是"这一个月"。可后馈真正要看的是**多月对比**——
环比上月、近三月均值、趋势图、目标进度的推进速度，都得靠历史。所以落定的那一版必须落到
一个比会话检查点更持久的地方（检查点重启即丢）。

设计：

- **落定才留档**：只有用户核对通过（``confirmed``）的那一版才写历史；没确认的不算数。
- **按期间幂等**：同一个月重复落定 = 覆盖，不会堆出多份。
- **读要皮实、写要出声**：批量读遇到坏文件**跳过并如实报告**（`warnings`），
  点名读（:meth:`JsonMonthHistoryStore.load`）失败则抛 :class:`HistoryError`——不把"读不出来"
  伪装成"没有记录"。
- **可插拔**：:class:`MonthHistoryStore` 协议；默认 JSON 文件实现——人可读、无依赖、方便迁移，
  将来换 SQLite / 数据库只需换一个实现，调用方不动。
- **只存事实，不存派生**：派生字段（基线、趋势、目标进度）在读取后由 ``recompute`` 重算，
  所以历史里留的是当时的原始快照，口径变了也能重算。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import AGENT_ROOT
from .domain.month import MonthSnapshot
from .domain.probe import Probe

PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


class HistoryError(RuntimeError):
    """历史留档的读写失败（目录不可写、文件损坏等）。"""


class MonthRecord(BaseModel):
    """一个月落定后的留档。"""

    model_config = ConfigDict(extra="ignore")

    period: str = Field(description="统计期间 YYYY-MM。")
    recorded_at: str = Field(default="", description="落定时间（ISO 8601，本地时区）。")
    snapshot: MonthSnapshot = Field(
        default_factory=MonthSnapshot, description="落定时的月度实况快照（含原始事实与派生字段）。"
    )
    articulation: str = Field(default="", description="落定时那段第三人称实况结论。")
    probes: list[Probe] = Field(
        default_factory=list,
        description="关注点与归因（open/answered/dismissed 一并保留），供后续分析与复盘。",
    )


def now_iso() -> str:
    """本地时区的 ISO 8601 时间戳（秒级）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def snapshots_from(records: list[MonthRecord]) -> list[MonthSnapshot]:
    """把留档还原成快照序列（**升序**，最近的在后）——正是 ``MonthAgent(history=...)`` 要的形状。"""
    return [record.snapshot for record in records]


def resolve_history_dir(directory: str | Path | None) -> Path | None:
    """把相对目录解析到 :data:`AGENT_ROOT` 下；空值表示**关闭**留档。"""
    if directory is None:
        return None
    text = str(directory).strip()
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else (AGENT_ROOT / path)


class MonthHistoryStore(Protocol):
    """历史留档的读写接口。"""

    def save(self, record: MonthRecord) -> str:
        """写入（同期间覆盖），返回可展示的位置标识。"""

    def load(self, period: str) -> MonthRecord | None:
        """点名读取某个月；没有返回 ``None``，读不出来抛 :class:`HistoryError`。"""

    def list_periods(self) -> list[str]:
        """已有历史的期间（升序）。"""

    def load_records(
        self,
        *,
        limit: int | None = None,
        before: str | None = None,
        warnings: list[str] | None = None,
    ) -> list[MonthRecord]:
        """按期间升序批量读取；``before`` 只取更早的月，``limit`` 取最近 N 个。"""


@dataclass
class InMemoryMonthHistoryStore:
    """内存实现（测试与一次性演示用）。"""

    _by_period: dict[str, MonthRecord] = field(default_factory=dict)

    def save(self, record: MonthRecord) -> str:
        self._by_period[record.period] = record
        return f"memory://{record.period}"

    def load(self, period: str) -> MonthRecord | None:
        return self._by_period.get(period)

    def list_periods(self) -> list[str]:
        return sorted(self._by_period)

    def load_records(
        self,
        *,
        limit: int | None = None,
        before: str | None = None,
        warnings: list[str] | None = None,
    ) -> list[MonthRecord]:
        periods = [p for p in self.list_periods() if before is None or p < before]
        if limit is not None:
            periods = periods[-limit:] if limit > 0 else []
        return [self._by_period[p] for p in periods]


@dataclass
class JsonMonthHistoryStore:
    """JSON 文件实现：一个期间一个文件（``<dir>/<YYYY-MM>.json``）。

    选文件而不是数据库，是因为这份数据量极小（每月一个）、但**价值密度极高**——
    出问题时人可以直接打开看，也方便手工迁移与备份。
    """

    directory: str | Path

    def __post_init__(self) -> None:
        resolved = resolve_history_dir(self.directory)
        if resolved is None:
            raise HistoryError("历史留档目录为空：请给出目录，或把 history_dir 设为空以关闭留档。")
        self.directory = resolved

    # ------------------------------------------------------------------ #
    def path_for(self, period: str) -> Path:
        if not PERIOD_RE.match(str(period or "")):
            raise HistoryError(f"期间格式应为 YYYY-MM，收到：{period!r}")
        return Path(self.directory) / f"{period}.json"

    def save(self, record: MonthRecord) -> str:
        """写入（同期间覆盖）。先写临时文件再替换，避免半个文件被读到。"""
        path = self.path_for(record.period)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = record.model_dump(mode="json")
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            raise HistoryError(f"{path}：写入失败（{exc}）") from exc
        return str(path)

    def load(self, period: str) -> MonthRecord | None:
        path = self.path_for(period)
        if not path.is_file():
            return None
        try:
            return self._read(path)
        except (OSError, json.JSONDecodeError, ValidationError, HistoryError) as exc:
            raise HistoryError(f"{path}：读取失败（{exc}）") from exc

    def list_periods(self) -> list[str]:
        directory = Path(self.directory)
        if not directory.is_dir():
            return []
        try:
            names = [p.name for p in directory.glob("*.json")]
        except OSError:
            return []
        return sorted(name[: -len(".json")] for name in names if PERIOD_RE.match(name[: -len(".json")]))

    def load_records(
        self,
        *,
        limit: int | None = None,
        before: str | None = None,
        warnings: list[str] | None = None,
    ) -> list[MonthRecord]:
        periods = [p for p in self.list_periods() if before is None or p < before]
        if limit is not None:
            periods = periods[-limit:] if limit > 0 else []

        records: list[MonthRecord] = []
        for period in periods:
            try:
                record = self._read(self.path_for(period))
            except (OSError, json.JSONDecodeError, ValidationError, HistoryError) as exc:
                if warnings is not None:
                    warnings.append(f"历史记录 {period} 读取失败，已跳过：{exc}")
                continue
            records.append(record)
        return records

    # ------------------------------------------------------------------ #
    @staticmethod
    def _read(path: Path) -> MonthRecord:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise HistoryError("文件内容不是 JSON 对象")
        record = MonthRecord.model_validate(raw)
        if not record.period:
            record.period = path.stem  # 兼容早期手工写的文件
        return record

    def load_snapshots(
        self,
        *,
        limit: int | None = None,
        before: str | None = None,
        warnings: list[str] | None = None,
    ) -> list[MonthSnapshot]:
        return snapshots_from(self.load_records(limit=limit, before=before, warnings=warnings))

    def delete(self, period: str) -> bool:
        """删掉某个月（误落定时用）。没有该文件返回 ``False``。"""
        path = self.path_for(period)
        if not path.is_file():
            return False
        try:
            path.unlink()
        except OSError as exc:
            raise HistoryError(f"{path}：删除失败（{exc}）") from exc
        return True
