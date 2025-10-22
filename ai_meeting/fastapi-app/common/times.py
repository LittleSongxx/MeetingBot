from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(LOCAL_TZ)


def to_local(value: datetime) -> datetime:
    return value.replace(tzinfo=LOCAL_TZ) if value.tzinfo is None else value.astimezone(LOCAL_TZ)


def format_datetime(value: datetime | None) -> str | None:
    return to_local(value).strftime("%Y-%m-%d %H:%M:%S") if value else None
