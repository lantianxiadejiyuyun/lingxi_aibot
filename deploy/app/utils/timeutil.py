"""时间工具：全库统一 UTC 存储（naive），展示层按用户时区转换。

约定：
- 数据库中 datetime 一律为 naive UTC。
- 与用户交互的字符串使用用户时区。
- 函数名带 _naive 返回 naive UTC datetime。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from dateutil import rrule as dateutil_rrule

UTC = timezone.utc


def utcnow() -> datetime:
    """当前 naive UTC 时间。"""
    return datetime.now(UTC).replace(tzinfo=None)


def to_naive_utc(dt: datetime) -> datetime:
    """任意 aware datetime → naive UTC。naive 输入视为已 UTC。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def get_tz(tz_name: Optional[str] = None) -> ZoneInfo:
    """时区名 → ZoneInfo，非法则回退 Asia/Shanghai。"""
    try:
        return ZoneInfo(tz_name or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def user_tz(user) -> ZoneInfo:
    """当前用户时区（user 需有 timezone 属性）。"""
    name = getattr(user, "timezone", None) if user is not None else None
    return get_tz(name)


def to_user(dt_naive_utc: Optional[datetime], tz: ZoneInfo) -> Optional[datetime]:
    """naive UTC → 用户时区 aware datetime。"""
    if dt_naive_utc is None:
        return None
    return dt_naive_utc.replace(tzinfo=UTC).astimezone(tz)


def fmt_dt(dt_naive_utc: Optional[datetime], tz: ZoneInfo, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """naive UTC → 用户时区格式化字符串。"""
    local = to_user(dt_naive_utc, tz)
    return local.strftime(fmt) if local else ""


def fmt_date(d: Optional[date], fmt: str = "%Y-%m-%d") -> str:
    return d.strftime(fmt) if d else ""


def parse_local(text: str, tz: ZoneInfo) -> Optional[datetime]:
    """把用户时区的字符串解析为 naive UTC。支持多种格式，失败返回 None。

    支持：ISO（含时区）、'YYYY-MM-DD HH:MM[:SS]'、'YYYY-MM-DD'、'HH:MM'（今天）。
    """
    if not text:
        return None
    text = str(text).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            # 无时区信息 → 视为用户时区（如 "2026-08-17 21:35"）
            dt = dt.replace(tzinfo=tz)
        return to_naive_utc(dt)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(tzinfo=tz)
            else:
                dt = dt.replace(tzinfo=tz)
            return to_naive_utc(dt)
        except ValueError:
            continue
    try:
        hh, mm = text.split(":")
        today = datetime.now(tz).date()
        dt = datetime.combine(today, time(int(hh), int(mm)), tzinfo=tz)
        return to_naive_utc(dt)
    except ValueError:
        return None


def expand_rrule(rrule_str: Optional[str], dtstart_naive_utc: datetime, range_start: datetime,
                 range_end: datetime, tz: ZoneInfo) -> list[datetime]:
    """展开重复规则，返回 [range_start, range_end] 内的所有发生时间（naive UTC）。

    range 参数为 naive UTC。dtstart 为事件首次开始时间（naive UTC）。
    """
    if not rrule_str:
        return []
    try:
        dtstart = dtstart_naive_utc.replace(tzinfo=UTC).astimezone(tz)
        rule = dateutil_rrule.rrulestr(rrule_str, dtstart=dtstart)
        start = range_start.replace(tzinfo=UTC).astimezone(tz)
        end = range_end.replace(tzinfo=UTC).astimezone(tz)
        occurrences = list(rule.between(start, end, inc=True))
        return [to_naive_utc(o) for o in occurrences]
    except Exception:
        return []


def month_bounds(year: int, month: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """某月 [1 号 00:00, 下月 1 号 00:00) 的 naive UTC 边界。"""
    start = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tz)
    return to_naive_utc(start), to_naive_utc(end)


def day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """某天 00:00 到次日 00:00 的 naive UTC 边界。"""
    start = datetime.combine(d, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return to_naive_utc(start), to_naive_utc(end)


def weekday_cn(dt_local: datetime) -> str:
    return "一二三四五六日"[dt_local.weekday()]


def humanize_relative(dt_naive_utc: Optional[datetime], tz: ZoneInfo) -> str:
    """相对时间描述：今天/明天/后天/昨天 或 日期。"""
    if dt_naive_utc is None:
        return ""
    local = to_user(dt_naive_utc, tz)
    today = datetime.now(tz).date()
    delta = (local.date() - today).days
    prefix = {0: "今天", 1: "明天", 2: "后天", -1: "昨天"}.get(delta, None)
    if prefix:
        return f"{prefix} {local.strftime('%H:%M')}"
    return local.strftime("%Y-%m-%d %H:%M")
