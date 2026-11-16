"""Compare lesson occurrences and describe every changed timetable field."""
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime


ENTITY_FIELDS = {"su": "Subject", "te": "Teacher", "ro": "Room", "kl": "Class"}
NOTE_FIELDS = {"lstext": "Lesson note", "substText": "Substitution note", "info": "Information"}
IDENTITY_FIELDS = ("id", "lsid", "lessonId")
CANCELLED_CODES = {"cancelled", "canceled", "cancel", "entfall", "ausfall"}


def lesson_status(record):
    status = str(record.get("code") or record.get("status") or "").strip().casefold()
    if status in CANCELLED_CODES:
        return "cancelled"
    return "" if status in {"normal", "regular"} else status


def is_cancelled(record):
    return lesson_status(record) == "cancelled"


def _identity(record):
    for key in IDENTITY_FIELDS:
        if record.get(key):
            return key, str(record[key])
    return None


def _sort_key(change):
    try:
        start = int(change.record.get("startTime") or 0)
    except (TypeError, ValueError):
        start = 0
    return str(change.record.get("date", "")), start, str(_identity(change.record)), change.kind


def _normalise(key, value):
    if key in ENTITY_FIELDS:
        if isinstance(value, dict):
            value = [value]
        if isinstance(value, list):
            # Labels can be hydrated independently of the actual assignment.
            value = [{k: str(item[k]) for k in ("id", "orgid") if k in item}
                     if isinstance(item, dict) and "id" in item else item for item in value]
            return sorted(value, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _content(record):
    content = {key: _normalise(key, value) for key, value in record.items()
               if key not in (*IDENTITY_FIELDS, "code", "status") and value not in (None, "", [], {})}
    if lesson_status(record):
        content["status"] = lesson_status(record)
    return content


@dataclass(frozen=True)
class TimetableChange:
    kind: str
    before: dict | None
    after: dict | None

    @property
    def record(self):
        return self.after if self.after is not None else self.before


def diff(old, fresh):
    """Match occurrences without losing duplicates or guessing between recurrences."""
    remaining_old, remaining_new = set(range(len(old))), set(range(len(fresh)))
    pairs = []

    def match(key_for, *, identical=False):
        before, after = defaultdict(list), defaultdict(list)
        for index in sorted(remaining_old):
            key = key_for(old[index])
            if key is not None:
                before[key].append(index)
        for index in sorted(remaining_new):
            key = key_for(fresh[index])
            if key is not None:
                after[key].append(index)
        for key in before.keys() & after.keys():
            if not identical and (len(before[key]) != 1 or len(after[key]) != 1):
                continue
            for left, right in zip(before[key], after[key]):
                pairs.append((left, right))
                remaining_old.remove(left)
                remaining_new.remove(right)

    # Preserve unchanged duplicates first; never collapse occurrences into a dict.
    match(lambda record: json.dumps([_identity(record), _content(record)], sort_keys=True), identical=True)
    match(lambda record: (_identity(record), str(record.get("date", "")),
                          str(record.get("startTime", ""))))
    match(lambda record: (_identity(record), str(record.get("date", ""))) if _identity(record) else None)
    old_counts = Counter(_identity(record) for record in old)
    new_counts = Counter(_identity(record) for record in fresh)
    # Only a globally unique ID can safely identify a move to another date.
    match(lambda record: _identity(record) if _identity(record)
          and old_counts[_identity(record)] == new_counts[_identity(record)] == 1 else None)

    changes = [TimetableChange("Changed", old[left], fresh[right]) for left, right in pairs
               if _content(old[left]) != _content(fresh[right])]
    changes.extend(TimetableChange("Added", None, fresh[index]) for index in sorted(remaining_new))
    changes.extend(TimetableChange("Removed", old[index], None) for index in sorted(remaining_old))
    return sorted(changes, key=_sort_key)


def _date(value):
    try:
        return datetime.strptime(str(value), "%Y%m%d").strftime("%d.%m.%Y")
    except ValueError:
        return str(value or "Date unavailable")


def _time(value):
    try:
        value = int(value)
        return f"{value // 100:02d}:{value % 100:02d}"
    except (ValueError, TypeError):
        return str(value or "Time unavailable")


def _schedule(record):
    return f"{_date(record.get('date'))} · {_time(record.get('startTime'))}–{_time(record.get('endTime'))}"


def _entity(record, key, names):
    value = record.get(key) or []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return str(value)

    def label(ident, item):
        return str(names.get(key, {}).get(str(ident)) or item.get("shortName") or item.get("name")
                   or item.get("longName") or item.get("displayName")
                   or (f"{ENTITY_FIELDS[key]} #{ident}" if ident is not None else "Not assigned"))

    labels = []
    for item in value:
        if isinstance(item, dict):
            text = label(item.get("id"), item)
            original = item.get("orgid")
            if original is not None and str(original) != str(item.get("id")):
                text += f" [originally {label(original, {})}]"
            labels.append(text)
        else:
            labels.append(str(item))
    return ", ".join(labels) or "Not assigned"


def _field_label(key):
    return {**ENTITY_FIELDS, **NOTE_FIELDS, "sg": "Student group", "activityType": "Activity type",
            "statflags": "Status flags", "lsnumber": "Lesson number"}.get(
                key, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", key).replace("_", " ").capitalize())


def _value(record, key, names):
    if key in ENTITY_FIELDS:
        return _entity(record, key, names)
    if key == "status":
        status = lesson_status(record)
        return {"": "Scheduled", "cancelled": "Cancelled", "irregular": "Irregular"}.get(status, status)
    value = record.get(key)
    if value in (None, "", [], {}):
        return "None"
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else str(value)


def format_change(change, entity_names=None, *, escape=str):
    """Return a title and body. All labels and changed fields remain visible."""
    names = entity_names or {}
    before, after, record = change.before, change.after, change.record
    subject = _entity(record, "su", names)
    lines = [f"{escape(subject)} · {escape(_schedule(record))}"]
    if change.kind != "Changed":
        tags = ["Lesson added" if change.kind == "Added" else "Removed from timetable"]
        if is_cancelled(record):
            tags.insert(0, "Lesson cancelled" if after is not None else "Cancelled lesson")
        elif lesson_status(record):
            tags.append(f"{escape(_value(record, 'status', names))} lesson")
        for key in ("te", "ro"):
            lines.append(f"{ENTITY_FIELDS[key]}: {escape(_entity(record, key, names))}")
        # Include other lesson details, including every note rather than only the first.
        for key in _content(record):
            if key not in {"su", "te", "ro", "date", "startTime", "endTime", "status"}:
                lines.append(f"{_field_label(key)}: {escape(_value(record, key, names))}")
        return " · ".join(tags), "\n".join(lines)

    old_content, new_content = _content(before), _content(after)
    changed = {key for key in old_content.keys() | new_content.keys()
               if old_content.get(key) != new_content.get(key)}
    tags = []
    if "status" in changed:
        tags.append("Lesson cancelled" if is_cancelled(after) else
                    "Lesson reinstated" if is_cancelled(before) else "Status change")
    for key, tag in (("te", "Teacher substitution"), ("ro", "Room change"), ("su", "Subject change")):
        if key in changed:
            if key in {"te", "ro"} and not after.get(key):
                tag = f"{ENTITY_FIELDS[key]} removed"
            elif key in {"te", "ro"} and not before.get(key):
                tag = f"{ENTITY_FIELDS[key]} assigned"
            tags.append(tag)
    schedule_changed = changed & {"date", "startTime", "endTime"}
    if schedule_changed:
        tags.append("Lesson moved" if changed & {"date", "startTime"} else "Lesson duration changed")
    if changed & NOTE_FIELDS.keys():
        tags.append("Note change")
    for key in sorted(changed - {"status", "te", "ro", "su", "date", "startTime", "endTime", *NOTE_FIELDS}):
        tags.append(f"{_field_label(key)} change")

    for key in ("status", "su", "te", "ro"):
        if key in changed:
            lines.append(f"{_field_label(key)}: {escape(_value(before, key, names))} → {escape(_value(after, key, names))}")
    # Keep room/teacher context even when those assignments did not change.
    for key in ("te", "ro"):
        if key not in changed:
            lines.append(f"{ENTITY_FIELDS[key]}: {escape(_entity(record, key, names))}")
    if schedule_changed:
        lines.append(f"When: {escape(_schedule(before))} → {escape(_schedule(after))}")
    for key in (*NOTE_FIELDS, *sorted(changed - {"status", "su", "te", "ro", "date", "startTime", "endTime", *NOTE_FIELDS})):
        if key in changed:
            lines.append(f"{_field_label(key)}: {escape(_value(before, key, names))} → {escape(_value(after, key, names))}")
        elif key in NOTE_FIELDS and record.get(key):
            lines.append(f"{NOTE_FIELDS[key]}: {escape(_value(record, key, names))}")
    return " · ".join(tags) or "Lesson changed", "\n".join(lines)
