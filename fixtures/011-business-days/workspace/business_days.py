"""Business-day counting over ISO calendar dates."""

from datetime import date, timedelta


def business_days(start: str, end: str, holidays: list[str]) -> int:
    """Count non-holiday weekdays from ``start`` through ``end`` inclusively."""

    cursor = date.fromisoformat(start)
    finish = date.fromisoformat(end)
    blocked = {date.fromisoformat(item) for item in holidays}
    count = 0
    while cursor <= finish:
        if cursor.weekday() < 5 and cursor not in blocked:
            count += 1
        cursor += timedelta(days=1)
    return count
