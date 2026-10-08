"""China calendar months. Relative months always require an explicit anchor."""
from datetime import date, datetime, timedelta, timezone
import re

BUSINESS_ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


def business_today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(BUSINESS_ZONE).date()


def valid_period(period: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period or "")) and period[:4] != "0000"


def shift_period(period: str, months: int) -> str:
    if not valid_period(period):
        raise ValueError("月份必须为有效的 YYYY-MM")
    index = (int(period[:4]) - 1) * 12 + int(period[5:]) - 1 + months
    if not 0 <= index < 9999 * 12:
        raise ValueError("月份超出支持范围")
    year, month = divmod(index, 12)
    return f"{year + 1:04d}-{month + 1:02d}"


def period_bounds(period: str) -> tuple[date, date]:
    if not valid_period(period):
        raise ValueError("月份必须为有效的 YYYY-MM")
    start = date.fromisoformat(period + "-01")
    # Avoid overflowing date.max for December 9999.
    end = date.max if period == "9999-12" else date.fromisoformat(shift_period(period, 1) + "-01") - timedelta(days=1)
    return start, end


def history_window(period: str, count: int = 3) -> list[str]:
    return [shift_period(period, -offset) for offset in range(count, 0, -1)]


def period_context(period: str, today: date | None = None) -> dict:
    current = (today or business_today()).strftime("%Y-%m")
    start, end = period_bounds(period)
    return {"selected_period": period, "current_period": current,
            "previous_period": shift_period(period, -1) if period != "0001-01" else None,
            "next_period": shift_period(period, 1) if period != "9999-12" else None,
            "status": "past" if period < current else "current" if period == current else "future",
            "start": start.isoformat(), "end": end.isoformat(),
            "as_of": (today or business_today()).isoformat()}
