"""总结环节的落档层：经验包、画像事件日志、月度复盘。

三份东西的**写语义不同**，别混为一谈：

| 内容 | 目录 | 语义 |
|---|---|---|
| 经验包 `ExperiencePack` | `.data/experience/<用户>.json` | **版本化覆盖**（每次汇入 version+1、按 kind 归并） |
| 画像事件 `ProfileEvent` | `.data/profile_events/<用户>.json` | **append-only**（已存在的 id 直接 no-op，原文永不改写） |
| 月度复盘 `MonthlySummary` | `.data/summaries/<用户>/<YYYY-MM>.json` | **按期间覆盖**（可重算的派生件） |

统一纪律（照 `history.py`）：

- 原子写：先写 ``.tmp`` 再 ``replace``，避免读到半个文件；
- **读要皮实、写要出声**——点名读失败抛 :class:`SummaryStoreError`（不把"读不出来"伪装成"没有记录"），
  批量读跳过坏文件并把原因记进 ``warnings``；
- 路径消毒：用户标识只允许 ``[0-9A-Za-z._-]``，防路径穿越。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from .config import AGENT_ROOT
from .domain.experience import ExperiencePack
from .domain.profile_delta import ProfileEvent, append_events, validate_events
from .domain.summary import MonthlySummary

PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
_UNSAFE = re.compile(r"[^0-9A-Za-z._-]")
DEFAULT_USER = "default"


class SummaryStoreError(RuntimeError):
    """总结落档的读写失败（目录不可用、文件损坏等）。"""


def sanitize_key(value: str | None) -> str:
    """把用户标识消毒成安全文件名：只留 ``[0-9A-Za-z._-]``，去掉可能导致穿越的开头点。"""
    text = _UNSAFE.sub("_", str(value or "").strip())
    text = text.lstrip("._")
    return text[:64] or DEFAULT_USER


def resolve_dir(directory: str | Path | None) -> Path:
    """把相对目录解析到 :data:`AGENT_ROOT` 下；空值抛错（调用方应提前关闭该 store）。"""
    text = str(directory if directory is not None else "").strip()
    if not text:
        raise SummaryStoreError("落档目录为空：请给出目录，或把对应配置设为空以关闭该留档。")
    path = Path(text)
    return path if path.is_absolute() else (AGENT_ROOT / path)


def _write_json(path: Path, payload: Any) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        raise SummaryStoreError(f"{path}：写入失败（{exc}）") from exc
    return str(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SummaryStoreError(f"{path}：读取失败（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise SummaryStoreError(f"{path}：不是合法 JSON（{exc}）") from exc


# --------------------------------------------------------------------------- #
# 协议
# --------------------------------------------------------------------------- #
class ExperiencePackStore(Protocol):
    """经验包留档：一份累积文件，版本化覆盖。"""

    def load(self) -> ExperiencePack | None: ...

    def save(self, pack: ExperiencePack) -> str: ...


class ProfileEventStore(Protocol):
    """画像事件日志：append-only。"""

    def load(self) -> list[ProfileEvent]: ...

    def append(self, events: list[ProfileEvent]) -> list[ProfileEvent]: ...


class MonthlySummaryStore(Protocol):
    """月度复盘留档：按期间覆盖。"""

    def save(self, summary: MonthlySummary) -> str: ...

    def load(self, period: str) -> MonthlySummary | None: ...

    def list_periods(self) -> list[str]: ...


# --------------------------------------------------------------------------- #
# 内存实现（测试与一次性演示）
# --------------------------------------------------------------------------- #
@dataclass
class InMemoryExperiencePackStore:
    pack: ExperiencePack | None = None

    def load(self) -> ExperiencePack | None:
        return self.pack

    def save(self, pack: ExperiencePack) -> str:
        self.pack = pack
        return f"memory://experience/{pack.version}"


@dataclass
class InMemoryProfileEventStore:
    _events: list[ProfileEvent] = field(default_factory=list)

    def load(self) -> list[ProfileEvent]:
        return list(self._events)

    def append(self, events: list[ProfileEvent]) -> list[ProfileEvent]:
        self._events = append_events(self._events, events)
        return list(self._events)


@dataclass
class InMemoryMonthlySummaryStore:
    _by_period: dict[str, MonthlySummary] = field(default_factory=dict)

    def save(self, summary: MonthlySummary) -> str:
        self._by_period[summary.period] = summary
        return f"memory://summaries/{summary.period}"

    def load(self, period: str) -> MonthlySummary | None:
        return self._by_period.get(period)

    def list_periods(self) -> list[str]:
        return sorted(self._by_period)


# --------------------------------------------------------------------------- #
# JSON 实现
# --------------------------------------------------------------------------- #
@dataclass
class JsonExperiencePackStore:
    directory: str | Path
    user: str = DEFAULT_USER

    def __post_init__(self) -> None:
        self.directory = resolve_dir(self.directory)

    @property
    def path(self) -> Path:
        return Path(self.directory) / f"{sanitize_key(self.user)}.json"

    def load(self) -> ExperiencePack | None:
        path = self.path
        if not path.is_file():
            return None
        try:
            return ExperiencePack.model_validate(_read_json(path))
        except ValidationError as exc:
            raise SummaryStoreError(f"{path}：内容不符合经验包结构（{exc}）") from exc

    def save(self, pack: ExperiencePack) -> str:
        return _write_json(self.path, pack.model_dump(mode="json"))


@dataclass
class JsonProfileEventStore:
    directory: str | Path
    user: str = DEFAULT_USER

    def __post_init__(self) -> None:
        self.directory = resolve_dir(self.directory)

    @property
    def path(self) -> Path:
        return Path(self.directory) / f"{sanitize_key(self.user)}.json"

    def load(self) -> list[ProfileEvent]:
        """读整个事件日志。文件不存在 = 还没记过，返回空；文件坏了**抛错**。"""
        path = self.path
        if not path.is_file():
            return []
        raw = _read_json(path)
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("events") or []
        else:
            raise SummaryStoreError(f"{path}：内容既不是数组也不是带 events 的对象。")
        try:
            return [ProfileEvent.model_validate(item) for item in items]
        except ValidationError as exc:
            raise SummaryStoreError(f"{path}：事件结构不合法（{exc}）") from exc

    def append(self, events: list[ProfileEvent]) -> list[ProfileEvent]:
        """**只追加**：已存在的 id 直接 no-op（既有条目一字不改）。返回追加后的完整日志。"""
        existing = self.load()
        merged = append_events(existing, events)
        if len(merged) == len(existing):
            return existing  # 全是重复 id，不产生写盘
        problems = validate_events(merged)
        if problems:
            raise SummaryStoreError("事件日志不一致：" + "；".join(problems))
        _write_json(self.path, {"user": self.user, "events": [e.model_dump(mode="json") for e in merged]})
        return merged


@dataclass
class JsonMonthlySummaryStore:
    directory: str | Path
    user: str = DEFAULT_USER

    def __post_init__(self) -> None:
        self.directory = resolve_dir(self.directory)

    @property
    def root(self) -> Path:
        return Path(self.directory) / sanitize_key(self.user)

    def path_for(self, period: str) -> Path:
        if not PERIOD_RE.match(str(period or "")):
            raise SummaryStoreError(f"期间格式应为 YYYY-MM，收到：{period!r}")
        return self.root / f"{period}.json"

    def save(self, summary: MonthlySummary) -> str:
        return _write_json(self.path_for(summary.period), summary.model_dump(mode="json"))

    def load(self, period: str) -> MonthlySummary | None:
        path = self.path_for(period)
        if not path.is_file():
            return None
        try:
            return MonthlySummary.model_validate(_read_json(path))
        except ValidationError as exc:
            raise SummaryStoreError(f"{path}：内容不符合复盘结构（{exc}）") from exc

    def list_periods(self) -> list[str]:
        root = self.root
        if not root.is_dir():
            return []
        try:
            names = [item.name for item in root.glob("*.json")]
        except OSError:
            return []
        return sorted(name[: -len(".json")] for name in names if PERIOD_RE.match(name[: -len(".json")]))

    def load_all(self, *, limit: int | None = None, warnings: list[str] | None = None) -> list[MonthlySummary]:
        """批量读（升序）；坏文件**跳过并如实报告**，不让一个坏档拖垮整批。"""
        periods = self.list_periods()
        if limit is not None:
            periods = periods[-limit:] if limit > 0 else []
        out: list[MonthlySummary] = []
        for period in periods:
            try:
                found = self.load(period)
            except SummaryStoreError as exc:
                if warnings is not None:
                    warnings.append(f"复盘 {period} 读取失败，已跳过：{exc}")
                continue
            if found is not None:
                out.append(found)
        return out
