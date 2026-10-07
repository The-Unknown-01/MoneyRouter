"""月度实况的历史存储：把这个月落定的实况记下来，供后续月份比对与累计。

**为什么需要**：本月实况里的"异常"要靠对比——比预算、比上月、比近三月均值。没有历史，
这些比对都只能靠外部注入；把每次落定的实况存下来，下个月就能**自己长出**这些线索。
历史同时还承载"累计已攒"，让目标达成进度也能量出来。

设计：

- 一个 ``subject``（用户身份）+ 一个 ``period``（``YYYY-MM``）→ **一个 JSON 文件**；
- 同一期间再落定一次就是**覆盖**，不会产生重复记录（重跑安全）；
- 写入是**原子**的（先写临时文件再 ``os.replace``），中途崩溃不会留下半个文件；
- 读取**永不抛**：某个月的记录坏掉就当它不存在，并把文件名记进 ``warnings``，
  不影响整条对话流程——与"任何外部失败都不该拖垮主流程"一致；
- ``subject`` / ``period`` 进文件名前做**字符消毒**（只留 ``[0-9A-Za-z._-]``），
  避免 ``../`` 之类的路径穿越。

注意：``subject`` 是**用户身份**，不是 ``thread_id``——后者标识一次会话。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .config import AGENT_ROOT
from .domain.month import MonthSnapshot
from .domain.probe import Probe

DEFAULT_MONTH_HISTORY_DIR = "data/month_history"
DEFAULT_SUBJECT = "default"
DEFAULT_MAX_MONTHS = 24

_SAFE_KEY = re.compile(r"[^0-9A-Za-z._-]+")


class MonthHistoryError(RuntimeError):
    """历史记录读写失败（调用方通常降级为"这次没记上"，不打断对话）。"""


def sanitize_key(value: str | None, *, fallback: str = DEFAULT_SUBJECT) -> str:
    """把 subject / period 消毒成安全文件名；非法字符替换为 ``-``，空则用 ``fallback``。

    同时挡掉纯点号（``.`` / ``..``）这类会指向上级目录的名字。
    """
    text = _SAFE_KEY.sub("-", str(value or "").strip()).strip("-.")
    return text or fallback


class MonthRecord(BaseModel):
    """一条已落定的月度实况记录。"""

    model_config = ConfigDict(extra="ignore")

    period: str = Field(description="统计期间 YYYY-MM。")
    snapshot: MonthSnapshot = Field(
        default_factory=MonthSnapshot, description="落定后的完整快照（派生字段已算好）。"
    )
    articulation: str = Field(default="", description="当时的实况结论。")
    probes: list[Probe] = Field(
        default_factory=list, description="关注点与用户归因，留下来供以后分析。"
    )
    source: str = Field(default="conversation", description="记录来源：conversation / import。")
    confirmed_at: str = Field(default="", description="落定时间（本机时区 ISO 8601）。")

    @property
    def balance_cents(self) -> int | None:
        return self.snapshot.balance_cents


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class MonthHistorySettings:
    """历史存储配置；``MONTH_HISTORY_*`` 环境变量可覆盖。"""

    enabled: bool = True
    root: str | None = DEFAULT_MONTH_HISTORY_DIR
    max_months: int = DEFAULT_MAX_MONTHS

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, load_dotenv: bool = True
    ) -> "MonthHistorySettings":
        from .config import _as_bool, _clamp_int, _load_dotenv

        if load_dotenv:
            _load_dotenv()
        env = env if env is not None else os.environ
        raw_root = env.get("MONTH_HISTORY_DIR")
        root = (
            (raw_root if raw_root is not None else DEFAULT_MONTH_HISTORY_DIR).strip()
            or None
        )
        return cls(
            enabled=_as_bool(env.get("MONTH_HISTORY_ENABLED"), True),
            root=root,
            max_months=_clamp_int(env.get("MONTH_HISTORY_MAX_MONTHS"), DEFAULT_MAX_MONTHS, 1, 120),
        )

    def with_overrides(self, **changes: Any) -> "MonthHistorySettings":
        return replace(self, **changes)


class MonthHistoryStore:
    """按 ``subject`` 分目录、按 ``period`` 分文件的历史存储。"""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        subject: str = DEFAULT_SUBJECT,
        enabled: bool = True,
        max_months: int = DEFAULT_MAX_MONTHS,
    ) -> None:
        self.enabled = bool(enabled)
        self.subject = sanitize_key(subject)
        self.max_months = max(1, int(max_months))
        self.root = Path(root) if root else Path(AGENT_ROOT) / DEFAULT_MONTH_HISTORY_DIR

    @classmethod
    def from_env(
        cls, subject: str = DEFAULT_SUBJECT, *, settings: MonthHistorySettings | None = None
    ) -> "MonthHistoryStore":
        settings = settings or MonthHistorySettings.from_env()
        return cls(
            root=settings.root,
            subject=subject,
            enabled=settings.enabled,
            max_months=settings.max_months,
        )

    # ------------------------------------------------------------------ #
    @property
    def directory(self) -> Path:
        return self.root / self.subject

    def path_for(self, period: str) -> Path:
        return self.directory / f"{sanitize_key(period, fallback='unknown')}.json"

    # ------------------------------------------------------------------ #
    def save(self, record: MonthRecord) -> Path | None:
        """写入（覆盖）某个月的记录；未启用时返回 ``None``。

        失败抛 :class:`MonthHistoryError`——由调用方决定是否降级，而不是静默丢数据。
        """
        if not self.enabled:
            return None
        period = sanitize_key(record.period, fallback="")
        if not period or period == "unknown":
            raise MonthHistoryError("记录缺少可用的统计期间，无法落盘。")

        if not record.confirmed_at:
            record = record.model_copy(update={"confirmed_at": _now_iso()})
        path = self.path_for(period)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = record.model_dump(mode="json")
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)  # 原子替换，避免读到半个文件
        except OSError as exc:
            raise MonthHistoryError(f"历史记录写入失败：{type(exc).__name__}: {exc}") from exc
        return path

    def load(self, period: str, *, warnings: list[str] | None = None) -> MonthRecord | None:
        """读某个月的记录；不存在或损坏都返回 ``None``（损坏会记进 ``warnings``）。"""
        path = self.path_for(period)
        if not path.is_file():
            return None
        return self._read(path, warnings)

    def records(self, *, warnings: list[str] | None = None) -> list[MonthRecord]:
        """全部记录，按期间升序（最近的在最后）。"""
        if not self.directory.is_dir():
            return []
        out: list[MonthRecord] = []
        for path in sorted(self.directory.glob("*.json")):
            record = self._read(path, warnings)
            if record is not None:
                out.append(record)
        out.sort(key=lambda item: item.period)
        return out

    def snapshots(
        self, *, before: str | None = None, limit: int | None = None
    ) -> list[MonthSnapshot]:
        """历史快照，升序；``before`` 只取它之前的月份（``YYYY-MM`` 字符串可直接比较）。

        默认最多取 ``max_months`` 个（保留最近的），避免历史越攒越长。
        """
        records = self.records()
        if before:
            records = [r for r in records if r.period and r.period < before]
        cap = self.max_months if limit is None else max(1, int(limit))
        return [r.snapshot for r in records[-cap:]]

    def periods(self) -> list[str]:
        """已有记录的期间列表（升序）。"""
        return [r.period for r in self.records()]

    def clear(self) -> int:
        """删除本 subject 下的全部记录，返回删除条数。"""
        if not self.directory.is_dir():
            return 0
        removed = 0
        for path in self.directory.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    # ------------------------------------------------------------------ #
    @staticmethod
    def _read(path: Path, warnings: list[str] | None) -> MonthRecord | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return MonthRecord.model_validate(payload)
        except (OSError, ValueError, TypeError) as exc:  # 含 json.JSONDecodeError
            if warnings is not None:
                warnings.append(f"历史记录 {path.name} 读取失败，已忽略：{type(exc).__name__}: {exc}")
            return None
