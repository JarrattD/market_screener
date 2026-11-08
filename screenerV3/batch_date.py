"""Stable scheduled batch identity, independent of execution and rerun time."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
# First approved Friday schedule. Preserve old run dates when old runs are rerun.
FRIDAY_ROLLOUT = date(2026, 11, 13)


def batch_window(run):
    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("Workflow creation timestamp must have a timezone")
    local = created.astimezone(LONDON)
    if run.get("event") != "schedule" or local.date() < FRIDAY_ROLLOUT:
        start = datetime.combine(local.date(), time.min, LONDON)
        return start, start + timedelta(days=1)
    # Friday 18:00 through Sunday 18:00 is one delayed-launch window.
    # Reject unexpectedly late/early runs rather than silently reuse last week.
    friday = local.date() - timedelta(days=(local.weekday() - 4) % 7)
    start = datetime.combine(friday, time(18), LONDON)
    end = start + timedelta(days=2)
    if not start <= local < end:
        raise ValueError("Scheduled run outside Friday batch launch window")
    return start, end


def run_batch_date(run):
    return batch_window(run)[0].date()
