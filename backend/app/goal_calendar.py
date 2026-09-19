from __future__ import annotations

from datetime import date, timedelta

MAX_PROGRAM_DAYS = 30


def schedule_constraints(value: dict | None) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"available_weekdays", "excluded_dates"}:
        raise ValueError("schedule_constraints must contain available_weekdays and excluded_dates only")
    weekdays = value.get("available_weekdays", list(range(7)))
    excluded = value.get("excluded_dates", [])
    if not isinstance(weekdays, list) or not weekdays or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays):
        raise ValueError("available_weekdays must contain weekdays from 0 (Monday) to 6 (Sunday)")
    if not isinstance(excluded, list) or len(excluded) > MAX_PROGRAM_DAYS:
        raise ValueError("excluded_dates must be a list of at most 30 dates")
    try:
        dates = [date.fromisoformat(day).isoformat() for day in excluded]
    except (TypeError, ValueError) as exc:
        raise ValueError("excluded_dates must use YYYY-MM-DD") from exc
    return {"available_weekdays": sorted(set(weekdays)), "excluded_dates": sorted(set(dates))}


def calendar_days(start: str, end: str, constraints: dict | None = None) -> dict:
    constraints = schedule_constraints(constraints)
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    count = (last - first).days + 1
    if not 1 <= count <= MAX_PROGRAM_DAYS:
        raise ValueError(f"program duration must be between 1 and {MAX_PROGRAM_DAYS} days")
    days = [first + timedelta(days=offset) for offset in range(count)]
    available = [day.isoformat() for day in days if day.weekday() in constraints["available_weekdays"] and day.isoformat() not in constraints["excluded_dates"]]
    return {"natural_days": count, "study_days": len(available), "available_dates": available,
            "rest_dates": [day.isoformat() for day in days if day.isoformat() not in available]}
