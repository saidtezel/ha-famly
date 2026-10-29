"""Pure parsing of Famly API payloads into simple state objects.

Kept free of Home Assistant imports so it can be tested against captured
responses directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .const import (
    EVENT_ACTIVITY,
    EVENT_CHECK_IN,
    EVENT_CHECK_OUT,
    EVENT_MEAL,
    EVENT_NAP_END,
    EVENT_NAP_START,
    EVENT_NAPPY,
    EVENT_ROOM_CHANGE,
    EVENT_SICK,
    STATUS_ABSENT,
    STATUS_AT_NURSERY,
    STATUS_HOLIDAY,
    STATUS_NOT_ARRIVED,
    STATUS_PICKED_UP,
    STATUS_SICK,
)

KIND_CHECK_IN = "check_in"
KIND_CHECK_OUT = "check_out"
KIND_NAP = "nap"
KIND_ACTION = "action"
KIND_MEAL = "meal"
KIND_LEAVE = "leave"
KIND_EVENT = "event"
KIND_OTHER = "other"

_ROOM_RE = re.compile(r"signed (?:into|in to)\s+(.+?)\s*$", re.IGNORECASE)


@dataclass(slots=True)
class Child:
    """A child the login is related to."""

    child_id: str
    name: str
    institution_name: str | None = None


@dataclass(slots=True)
class Activity:
    """One entry from the child's calendar/activity log."""

    uid: str
    kind: str
    start: datetime | None
    end: datetime | None
    title: str
    subtitle: str
    day: date | None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "title": self.title,
            "start": self.start.isoformat() if self.start else None,
        }
        if self.end:
            out["end"] = self.end.isoformat()
        out.update({k: v for k, v in self.data.items() if v not in (None, "")})
        return out


@dataclass(slots=True)
class ChildState:
    """Everything the entities need for one child."""

    child_id: str
    name: str
    first_name: str
    institution_id: str | None
    institution_name: str | None
    group_name: str | None
    timezone: str
    checked_in: bool
    is_sick: bool
    is_absent: bool
    has_vacation: bool
    pickup_time: str | None
    pickup_name: str | None
    key_workers: list[str]
    status: str
    room: str | None
    arrived_at: datetime | None
    left_at: datetime | None
    today: list[Activity]
    naps_today: list[Activity]
    sleeping: bool
    sleep_minutes_today: float
    last_nap_start: datetime | None
    last_nap_end: datetime | None
    nappies_today: list[Activity]
    meals_today: list[Activity]
    last_activity: Activity | None
    leave_today: Activity | None


# --------------------------------------------------------------------- utils


def parse_dt(value: Any) -> datetime | None:
    """Parse a Famly timestamp into an aware datetime.

    The v2 endpoints send ISO 8601 with an offset. The older REST endpoints
    (feed, me) send "2026-05-06 19:02:44.000000": space-separated, no zone,
    and in UTC. Both are accepted; zone-less values are taken as UTC.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _full_name(name: Any) -> str:
    if isinstance(name, dict):
        return (name.get("fullName") or name.get("firstName") or "").strip()
    return ""


# -------------------------------------------------------------- the account


def parse_children(me: dict[str, Any]) -> list[Child]:
    """Children the login is a relation of, from /api/me/me/me."""
    children: dict[str, Child] = {}
    for role in me.get("roles2") or []:
        if role.get("targetType") != "Famly.Daycare:Child":
            continue
        child_id = role.get("targetId")
        if not child_id or child_id in children:
            continue
        children[child_id] = Child(
            child_id=child_id,
            name=role.get("title") or "Child",
            institution_name=role.get("subtitle") or None,
        )
    return list(children.values())


# ------------------------------------------------------------- the calendar


def _kind_for(originator_type: str, embed: dict[str, Any]) -> str:
    if originator_type.endswith(":ChildCheckin"):
        return KIND_CHECK_OUT if embed.get("type") == "CHECK_OUT" else KIND_CHECK_IN
    if originator_type.endswith(":Nap"):
        return KIND_NAP
    if originator_type.endswith(":Action"):
        return KIND_ACTION
    if originator_type.endswith(":MealRegistration"):
        return KIND_MEAL
    if originator_type.endswith(":ChildLeave"):
        return KIND_LEAVE
    if originator_type.endswith(":Event"):
        return KIND_EVENT
    return KIND_OTHER


def _after_colon(title: str) -> str:
    return title.split(": ", 1)[1].strip() if ": " in title else title.strip()


def parse_calendar(payload: Any) -> list[Activity]:
    """Flatten a /api/v2/calendar response into a sorted list of activities."""
    activities: list[Activity] = []
    for period in payload or []:
        for day_entry in period.get("days") or []:
            day = parse_date(day_entry.get("day_localdate") or day_entry.get("day"))
            for raw in day_entry.get("events") or []:
                originator = raw.get("originator") or {}
                otype = str(originator.get("type") or "")
                oid = str(originator.get("id") or raw.get("title") or "")
                embed = raw.get("embed") or {}
                kind = _kind_for(otype, embed)
                title = str(raw.get("title") or "")
                subtitle = str(raw.get("subtitle") or "")
                start = parse_dt(raw.get("from"))
                end = parse_dt(raw.get("to"))
                data: dict[str, Any] = {}

                if kind == KIND_CHECK_IN:
                    match = _ROOM_RE.search(title)
                    data["room"] = match.group(1) if match else None
                elif kind == KIND_ACTION:
                    start = parse_dt(embed.get("occuredOn")) or start
                    data["action_type"] = embed.get("actionType")
                    data["nappy_type"] = embed.get("diaperingType")
                    data["note"] = embed.get("note")
                    data["label"] = _after_colon(title)
                elif kind == KIND_MEAL:
                    data["meal"] = _after_colon(title)
                    data["meal_type_id"] = embed.get("mealTypeId")
                elif kind == KIND_NAP:
                    data["start_note"] = embed.get("startNotes")
                    data["end_note"] = embed.get("endNotes")
                elif kind == KIND_LEAVE:
                    data["leave_type"] = embed.get("leaveType")
                    data["reason"] = subtitle or None
                elif kind == KIND_EVENT:
                    data["attending"] = embed.get("attending")
                    data["rsvp_required"] = embed.get("requireRsvp")

                activities.append(
                    Activity(
                        uid=f"{otype}:{oid}:{day.isoformat() if day else ''}",
                        kind=kind,
                        start=start,
                        end=end,
                        title=title,
                        subtitle=subtitle,
                        day=day,
                        data=data,
                    )
                )
    activities.sort(key=_sort_key)
    return activities


def _sort_key(act: Activity) -> tuple[date, datetime]:
    floor = datetime.min.replace(tzinfo=ZoneInfo("UTC"))
    return (act.day or date.min, act.start or floor)


# ------------------------------------------------------------- child state


def build_child_state(
    summary: dict[str, Any], activities: list[Activity], today: date
) -> ChildState:
    """Combine the child summary and today's activity log."""
    child = summary.get("child") or {}
    institution = summary.get("institution") or {}
    group = summary.get("group") or {}
    tz_name = institution.get("timezone") or "UTC"

    todays = [a for a in activities if a.day == today]
    check_ins = [a for a in todays if a.kind == KIND_CHECK_IN and a.start]
    check_outs = [a for a in todays if a.kind == KIND_CHECK_OUT and a.start]
    move_times = {a.start for a in check_ins}
    real_check_outs = [a for a in check_outs if a.start not in move_times]

    checked_in = bool(child.get("checkedIn"))
    arrived_at = parse_dt(child.get("checkinTime")) or (
        check_ins[0].start if check_ins else None
    )
    left_at = None
    if not checked_in and real_check_outs:
        left_at = real_check_outs[-1].start

    room = None
    if checked_in:
        room = (check_ins[-1].data.get("room") if check_ins else None) or group.get(
            "title"
        )

    naps = [a for a in todays if a.kind == KIND_NAP and a.start]
    sleeping = any(a.end is None for a in naps)
    now = datetime.now(tz=ZoneInfo("UTC"))
    sleep_minutes = sum(
        ((a.end or now) - a.start).total_seconds() / 60 for a in naps if a.start
    )

    last_nap_start: datetime | None = None
    last_nap_end: datetime | None = None
    if naps:
        last_nap_start, last_nap_end = naps[-1].start, naps[-1].end
    else:
        history = [n for n in summary.get("naps") or [] if parse_dt(n.get("startTime"))]
        if history:
            latest = max(history, key=lambda n: parse_dt(n["startTime"]))  # type: ignore[arg-type,return-value]
            last_nap_start = parse_dt(latest["startTime"])
            duration = latest.get("duration")
            if last_nap_start and isinstance(duration, (int, float)):
                last_nap_end = last_nap_start + timedelta(hours=duration)

    nappies = [
        a
        for a in todays
        if a.kind == KIND_ACTION and a.data.get("action_type") == "DIAPERCHANGE"
    ]
    meals = [a for a in todays if a.kind == KIND_MEAL]
    leave = next((a for a in todays if a.kind == KIND_LEAVE), None)
    loggable = [
        a
        for a in todays
        if a.kind in (KIND_CHECK_IN, KIND_CHECK_OUT, KIND_NAP, KIND_ACTION, KIND_MEAL)
        and a.start
    ]
    last_activity = max(loggable, key=lambda a: a.start) if loggable else None  # type: ignore[arg-type,return-value]

    is_sick = bool(child.get("isSick"))
    is_absent = bool(child.get("isAbsent"))
    has_vacation = bool(child.get("hasVacation"))
    leave_type = (leave.data.get("leave_type") if leave else None) or ""

    if is_sick or leave_type == "SICK":
        status = STATUS_SICK
    elif checked_in:
        status = STATUS_AT_NURSERY
    elif has_vacation or leave_type == "VACATION":
        status = STATUS_HOLIDAY
    elif is_absent or leave:
        status = STATUS_ABSENT
    elif arrived_at:
        status = STATUS_PICKED_UP
    else:
        status = STATUS_NOT_ARRIVED

    name_block = child.get("name") or {}
    return ChildState(
        child_id=child.get("childId") or "",
        name=_full_name(name_block),
        first_name=(name_block.get("firstName") or _full_name(name_block)).strip(),
        institution_id=institution.get("institutionId") or child.get("institutionId"),
        institution_name=institution.get("title"),
        group_name=group.get("title") or child.get("groupTitle"),
        timezone=tz_name,
        checked_in=checked_in,
        is_sick=is_sick,
        is_absent=is_absent,
        has_vacation=has_vacation,
        pickup_time=child.get("pickupTime") or None,
        pickup_name=child.get("pickupName") or None,
        key_workers=[
            _full_name(k.get("name")) for k in child.get("keyWorkers") or [] if k
        ],
        status=status,
        room=room,
        arrived_at=arrived_at,
        left_at=left_at,
        today=todays,
        naps_today=naps,
        sleeping=sleeping,
        sleep_minutes_today=round(sleep_minutes, 1),
        last_nap_start=last_nap_start,
        last_nap_end=last_nap_end,
        nappies_today=nappies,
        meals_today=meals,
        last_activity=last_activity,
        leave_today=leave,
    )


# --------------------------------------------------------------- new events


def diff_activities(
    previous: dict[str, Activity] | None, current: list[Activity]
) -> list[tuple[str, Activity]]:
    """Return (event_type, activity) pairs for things that appeared since last poll.

    Returns nothing on the first poll so a restart does not replay the day.
    """
    if previous is None:
        return []

    new_events: list[tuple[str, Activity]] = []
    check_in_times = {a.start for a in current if a.kind == KIND_CHECK_IN and a.start}
    check_out_times = {a.start for a in current if a.kind == KIND_CHECK_OUT and a.start}

    for act in current:
        before = previous.get(act.uid)
        if before is not None:
            if act.kind == KIND_NAP and before.end is None and act.end is not None:
                new_events.append((EVENT_NAP_END, act))
            continue

        if act.kind == KIND_CHECK_IN:
            moved = act.start in check_out_times
            new_events.append((EVENT_ROOM_CHANGE if moved else EVENT_CHECK_IN, act))
        elif act.kind == KIND_CHECK_OUT:
            if act.start not in check_in_times:
                new_events.append((EVENT_CHECK_OUT, act))
        elif act.kind == KIND_NAP:
            new_events.append((EVENT_NAP_START, act))
            if act.end is not None:
                new_events.append((EVENT_NAP_END, act))
        elif act.kind == KIND_ACTION:
            is_nappy = act.data.get("action_type") == "DIAPERCHANGE"
            new_events.append((EVENT_NAPPY if is_nappy else EVENT_ACTIVITY, act))
        elif act.kind == KIND_MEAL:
            new_events.append((EVENT_MEAL, act))
        elif act.kind == KIND_LEAVE and act.data.get("leave_type") == "SICK":
            new_events.append((EVENT_SICK, act))

    return new_events


# ----------------------------------------------------------- opening hours


def is_within_opening_hours(
    opening_hours: list[dict[str, Any]],
    now: datetime,
    margin: timedelta,
) -> bool:
    """True if `now` (aware, in the nursery's zone) is inside opening hours ± margin.

    Famly numbers days ISO-style: 1 = Monday ... 7 = Sunday.
    """
    if not opening_hours:
        return True  # unknown, poll actively to be safe
    for entry in opening_hours:
        if entry.get("day") != now.isoweekday() or not entry.get("open"):
            continue
        opens = datetime.combine(
            now.date(),
            time(int(entry.get("fromHour") or 0), int(entry.get("fromMinute") or 0)),
            tzinfo=now.tzinfo,
        )
        closes = datetime.combine(
            now.date(),
            time(int(entry.get("toHour") or 0), int(entry.get("toMinute") or 0)),
            tzinfo=now.tzinfo,
        )
        if opens - margin <= now <= closes + margin:
            return True
    return False
