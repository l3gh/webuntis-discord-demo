#!/usr/bin/env python3
"""Personal WebUntis JSON-RPC timetable watcher; credentials are prompted locally."""
import argparse
import getpass
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
from io import BytesIO
import uuid
from pathlib import Path

STATE = Path(__file__).with_name("legacy_state.json")
ENV_FILE = Path(__file__).with_name(".env")
STATUS = Path(__file__).with_name("watcher_status.json")
DISPLAY_NAMES_FILE = Path(__file__).with_name("display_names.json")
SCHOOL_TZ = ZoneInfo("Europe/Vienna")


def school_today():
    return datetime.now(SCHOOL_TZ).date()


def load_display_names():
    """Load optional user-selected labels, keyed by WebUntis entity ID."""
    try:
        data = json.loads(DISPLAY_NAMES_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"subjects": {}, "teachers": {}, "rooms": {}}
    return {
        kind: {str(ident): str(label) for ident, label in data.get(kind, {}).items() if label}
        for kind in ("subjects", "teachers", "rooms")
    }


def load_config():
    if not ENV_FILE.exists():
        return None
    config = {}
    for raw in ENV_FILE.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith('"'):
            value = json.loads(value)
        config[key.strip()] = value
    return config


def save_config(config):
    keys = ("WEBUNTIS_SUBDOMAIN", "WEBUNTIS_SCHOOL", "WEBUNTIS_USERNAME", "WEBUNTIS_PASSWORD", "DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN")
    content = "# Private settings for legacy_bot.py\n" + "".join(f"{key}={json.dumps(config.get(key, ''), ensure_ascii=False)}\n" for key in keys)
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(ENV_FILE, 0o600)


def setup_config():
    existing = load_config() or {}
    prefix = input(f"WebUntis school subdomain [{existing.get('WEBUNTIS_SUBDOMAIN', '')}]: ").strip() or existing.get("WEBUNTIS_SUBDOMAIN", "")
    school = input(f"WebUntis school name [{existing.get('WEBUNTIS_SCHOOL', '')}]: ").strip() or existing.get("WEBUNTIS_SCHOOL", "")
    username = input(f"WebUntis username [{existing.get('WEBUNTIS_USERNAME', '')}]: ").strip() or existing.get("WEBUNTIS_USERNAME", "")
    password_prompt = "WebUntis password (hidden; Enter keeps saved password): " if existing.get("WEBUNTIS_PASSWORD") else "WebUntis password (hidden): "
    password = getpass.getpass(password_prompt) or existing.get("WEBUNTIS_PASSWORD", "")
    hook_prompt = "Discord webhook URL (hidden; Enter keeps saved webhook, type - to clear): " if existing.get("DISCORD_WEBHOOK_URL") else "Discord webhook URL (hidden; Enter for print-only): "
    hook_input = getpass.getpass(hook_prompt).strip()
    hook = ("" if hook_input == "-" else hook_input) if hook_input else existing.get("DISCORD_WEBHOOK_URL", "")
    if not prefix or not school or not username or not password:
        raise ValueError("school subdomain, school name, username, and password are required")
    save_config({"WEBUNTIS_SUBDOMAIN": prefix, "WEBUNTIS_SCHOOL": school, "WEBUNTIS_USERNAME": username,
                 "WEBUNTIS_PASSWORD": password, "DISCORD_WEBHOOK_URL": hook,
                 "DISCORD_BOT_TOKEN": existing.get("DISCORD_BOT_TOKEN", "")})
    print(f"Saved settings to {ENV_FILE} with owner-only permissions.")


def rpc(opener, url, method, params):
    body = json.dumps({"id": "untis-watch", "method": method, "params": params, "jsonrpc": "2.0"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with opener.open(req, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Include a short server response for diagnosis (e.g. a school-specific
        # API gateway message), while never including request data or credentials.
        detail = exc.read(500).decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"WebUntis returned HTTP {exc.code}{suffix}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not reach WebUntis: {exc}") from None
    if "error" in result:
        err = result["error"]
        raise RuntimeError(f"WebUntis API error {err.get('code', '')}: {err.get('message', 'unknown error')}")
    return result.get("result")


def normal_date(d):
    return d.strftime("%Y%m%d")


def records_from(response):
    # Servers differ slightly in the response envelope across versions.
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in ("periodData", "periods", "timetable", "result"):
            value = response.get(key)
            if isinstance(value, list):
                return value
    raise RuntimeError("Unexpected timetable response shape. The school's API may use a different version.")


def load_entity_names(opener, endpoint):
    names = {}
    subject_label_ids = {}
    for key, method in (("su", "getSubjects"), ("ro", "getRooms"), ("te", "getTeachers")):
        try:
            records = rpc(opener, endpoint, method, {})
        except Exception:
            if key == "te":
                print("WebUntis does not allow this account to list teachers; teacher IDs will be shown.")
            continue
        if not isinstance(records, list):
            continue
        names[key] = {}
        for item in records:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            # Untis timetable cells usually use the compact abbreviation.
            label = item.get("shortName") or item.get("name") or item.get("longName") or item.get("displayName")
            if label:
                names[key][str(item["id"])] = str(label)
            if key == "su":
                for alias in (item.get("shortName"), item.get("name"), item.get("longName"), item.get("displayName")):
                    if alias:
                        subject_label_ids[str(alias).strip().casefold()] = str(item["id"])
    # Keep the API's original subject aliases for matching homework metadata,
    # even when custom display labels replace the visible timetable names.
    names["_subject_label_ids"] = subject_label_ids
    configured = load_display_names()
    for kind, key in (("subjects", "su"), ("teachers", "te"), ("rooms", "ro")):
        if configured[kind]:
            names.setdefault(key, {}).update(configured[kind])
    return names


def date_label(value):
    raw = str(value or "")
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%d.%m.%Y")
        except ValueError:
            pass
    return raw


def time_label(value):
    if value in (None, ""):
        return ""
    try:
        digits = int(value)
        return f"{digits // 100:02d}:{digits % 100:02d}"
    except (ValueError, TypeError):
        return str(value)


def schedule_sort_key(record):
    if not isinstance(record, dict):
        return ("", 0)
    raw_time = record.get("startTime", 0)
    try:
        raw_time = int(raw_time)
        minute = (raw_time // 100) * 60 + raw_time % 100
    except (ValueError, TypeError):
        minute = 0
    return (str(record.get("date", "")), minute)


def record_key(record, index):
    if not isinstance(record, dict):
        return f"row-{index}"
    identity = record.get("lsid") or record.get("id") or record.get("lessonId")
    return "|".join(str(x) for x in (record.get("date", ""), record.get("startTime", ""), identity or index))


def pretty(record, entity_names=None):
    if not isinstance(record, dict):
        return str(record)
    entity_names = entity_names or {}
    def names(key):
        value = record.get(key) or []
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            value = [value]
        if isinstance(value, list):
            labels = []
            for item in value:
                if not isinstance(item, dict):
                    labels.append(str(item))
                    continue
                ident = item.get("id")
                label = entity_names.get(key, {}).get(str(ident)) if ident is not None else None
                label = label or item.get("longName") or item.get("name") or item.get("shortName") or item.get("displayName")
                if not label and ident is not None:
                    label = "[teacher]" if key == "te" else f"#{ident}"
                labels.append(str(label) if label else "(unnamed)")
            return ", ".join(labels)
        return str(value)
    start, end = time_label(record.get("startTime")), time_label(record.get("endTime"))
    period = f"{start}-{end}" if start or end else ""
    bits = [date_label(record.get("date")), period, names("su"), names("ro"), names("te")]
    text = " | ".join(bit for bit in bits if bit and bit != "–")
    note = record.get("lstext") or record.get("substText") or record.get("info")
    if note:
        text += " | " + str(note)
    return text or json.dumps(record, ensure_ascii=False, sort_keys=True)


def diff(old, fresh):
    before, after = {}, {}
    for i, rec in enumerate(old):
        before[record_key(rec, i)] = rec
    for i, rec in enumerate(fresh):
        after[record_key(rec, i)] = rec
    out = [("Added", after[k]) for k in after.keys() - before.keys()]
    out += [("Removed", before[k]) for k in before.keys() - after.keys()]
    out += [("Changed", after[k]) for k in after.keys() & before.keys() if after[k] != before[k]]
    return sorted(out, key=lambda row: schedule_sort_key(row[1]))


def send_discord(message, hook):
    if not hook:
        print("(Discord not configured; print-only mode)")
        return 0
    chunks = []
    current = ""
    for line in message.splitlines() or [""]:
        while len(line) > 1900:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:1900])
            line = line[1900:]
        if current and len(current) + 1 + len(line) > 1900:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    for chunk in chunks:
        payload = json.dumps({"content": chunk}).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "DiscordBot (https://discord.com/developers/docs, 1.0)"}
        req = urllib.request.Request(hook, payload, headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status not in (200, 204):
                    raise RuntimeError(f"Discord returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Discord rejected the webhook (HTTP {exc.code}){suffix}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Discord: {exc.reason}") from None
    return len(chunks)


def lesson_line(record, entity_names=None):
    """A compact, readable lesson card line for Discord timetable fields."""
    entity_names = entity_names or {}
    start, end = time_label(record.get("startTime")), time_label(record.get("endTime"))
    def entity(key, fallback):
        value = record.get(key) or []
        if isinstance(value, dict):
            value = [value]
        if isinstance(value, str):
            return value
        labels = []
        for item in value if isinstance(value, list) else []:
            if isinstance(item, dict):
                ident = item.get("id")
                label = entity_names.get(key, {}).get(str(ident)) if ident is not None else None
                label = label or (item.get("shortName") or item.get("name") or item.get("longName")
                                  or item.get("displayName"))
                if not label and ident is not None:
                    label = "[teacher]" if key == "te" else fallback.format(id=ident)
                if label:
                    labels.append(str(label))
            elif item:
                labels.append(str(item))
        return ", ".join(labels)
    subject = entity("su", "Subject #{id}") or "Lesson"
    room = entity("ro", "#{id}")
    teacher = entity("te", "#{id}")
    period = f"{start}–{end}" if start or end else "Time TBD"
    line = f"**{period} · {subject}**"
    details = [x for x in (room, teacher) if x]
    if details:
        line += "\n" + " · ".join(details)
    note = record.get("lstext") or record.get("substText") or record.get("info")
    if note:
        line += "\n↳ " + str(note)
    return line


def weekly_embeds(entries, entity_names=None):
    """Build compact Untis-inspired weekly cards, one embed per calendar week."""
    grouped = defaultdict(list)
    for entry in entries:
        try:
            day = datetime.strptime(str(entry.get("date", "")), "%Y%m%d").date()
        except (ValueError, TypeError):
            continue
        monday = day - timedelta(days=day.weekday())
        grouped[monday].append((day, entry))
    embeds = []
    for monday in sorted(grouped):
        by_day = defaultdict(list)
        for day, entry in grouped[monday]:
            by_day[day].append(entry)
        fields = []
        for offset in range(7):
            day = monday + timedelta(days=offset)
            lessons = sorted(by_day[day], key=lambda x: schedule_sort_key(x))
            if not lessons:
                continue
            value = "\n\n".join(lesson_line(item, entity_names) for item in lessons)
            # Discord limits each field to 1024 characters; retain complete lessons.
            if len(value) > 1000:
                value = value[:980].rsplit("\n\n", 1)[0] + "\n…more lessons omitted"
            fields.append({"name": day.strftime("%a · %d %b"), "value": value or "—", "inline": True})
        if not fields:
            continue
        end = monday + timedelta(days=6)
        embeds.append({
            "title": f"📅 Timetable · {monday.strftime('%d %b')} – {end.strftime('%d %b %Y')}",
            "color": 0x18A66A,
            "fields": fields,
            "footer": {"text": f"{sum(len(items) for items in by_day.values())} lessons · WebUntis"},
        })
    return embeds


def send_discord_embeds(embeds, hook):
    if not hook:
        print("(Discord not configured; print-only mode)")
        return 0
    sent = 0
    for embed in embeds:
        payload = json.dumps({"embeds": [embed]}).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "DiscordBot (https://discord.com/developers/docs, 1.0)"}
        req = urllib.request.Request(hook, payload, headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status not in (200, 204):
                    raise RuntimeError(f"Discord returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"Discord rejected the webhook (HTTP {exc.code}){suffix}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Discord: {exc.reason}") from None
        sent += 1
    return sent


def _period_key(record):
    subjects = record.get("su") or []
    if isinstance(subjects, dict):
        subjects = [subjects]
    subject_ids = sorted(str(x.get("id")) for x in subjects if isinstance(x, dict) and x.get("id") is not None)
    return (str(record.get("date", "")), str(record.get("startTime", "")),
            str(record.get("endTime", "")), tuple(subject_ids))


def _is_cancelled(record):
    status = str(record.get("code") or record.get("status") or "").strip().casefold()
    return status in {"cancelled", "canceled", "cancel", "entfall", "ausfall"}


def timetable_png(entries, monday, entity_names=None, range_start=None, range_end=None,
                  homework_items=None, exam_items=None, homework_periods=None, mode="light"):
    """Render one Monday-Friday timetable as a clean, Untis-inspired PNG."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Image timetable support needs Pillow (python3-pil on Debian).") from exc
    entity_names = entity_names or {}
    dark = str(mode).casefold() == "dark"
    palette = {
        "background": "#10151c" if dark else "#edf1f4",
        "title": "#e5ebf2" if dark else "#263747",
        "muted": "#94a3b3" if dark else "#758493",
        "day": "#a1afbe" if dark else "#536575",
        "grid": "#26303a" if dark else "#cbd3da",
        "column": "#202a34" if dark else "#d7dde2",
        "break": "#19222c" if dark else "#e3e9ed",
        "break_text": "#8795a5" if dark else "#72818d",
        "lesson": "#19222c" if dark else "#ffffff",
        "lesson_edge": "#263442" if dark else "#d6dde2",
        "subject": "#e2e9f0" if dark else "#344554",
        "time": "#a2b0bf" if dark else "#72808c",
        "detail": "#9caaba" if dark else "#657482",
        "footer": "#94a3b3" if dark else "#82909c",
        "cancel_bg": "#24171d" if dark else "#fde5e5",
        "cancel_edge": "#71313d" if dark else "#e6a3a3",
        "cancel_text": "#ff7589" if dark else "#8f3838",
        "exam_bg": "#242218" if dark else "#fff4cb",
        "exam_edge": "#8a6d2e" if dark else "#e6c65f",
        "exam_accent": "#f0b83d" if dark else "#d5a923",
        "exam_text": "#f3ca68" if dark else "#8b6f22",
        "homework_bg": "#172433" if dark else "#eaf2fb",
        "homework_edge": "#31577b" if dark else "#b7cbe0",
        "homework_accent": "#53a8ff" if dark else "#5b8fc5",
        "exam_badge_bg": "#f0b83d" if dark else "#fff0bd",
        "exam_badge_text": "#171a1e" if dark else "#8b6f22",
    }
    by_day = defaultdict(list)
    exam_days = defaultdict(int)
    exams_by_day = defaultdict(list)
    homework_period_keys = set()
    all_starts, all_ends = [], []
    week_end = monday + timedelta(days=5)
    for item in homework_items or []:
        try:
            due = datetime.strptime(str(item.get("dueDate", "")), "%Y%m%d").date()
        except (ValueError, TypeError):
            continue
        subject = str(item.get("_lesson_subject") or "").strip().casefold()
        subject_id = entity_names.get("_subject_label_ids", {}).get(subject)
        candidates = []
        for period in homework_periods or entries:
            try:
                period_day = datetime.strptime(str(period.get("date", "")), "%Y%m%d").date()
            except (ValueError, TypeError):
                continue
            if period_day != due:
                continue
            period_subject_ids = []
            subjects = period.get("su") or []
            if isinstance(subjects, dict):
                subjects = [subjects]
            if isinstance(subjects, list):
                period_subject_ids = [str(value.get("id")) for value in subjects
                                      if isinstance(value, dict) and value.get("id") is not None]
            period_subject = _entity_label(period, "su", entity_names, "").strip().casefold()
            if subject and ((subject_id and subject_id in period_subject_ids)
                            or (not subject_id and period_subject == subject)):
                candidates.append(period)
        teacher_id = item.get("_teacher_id")
        if teacher_id is not None and candidates:
            teacher_matches = []
            for period in candidates:
                teachers = period.get("te") or []
                if isinstance(teachers, dict):
                    teachers = [teachers]
                if any(isinstance(t, dict) and str(t.get("id")) == str(teacher_id) for t in teachers):
                    teacher_matches.append(period)
            if teacher_matches:
                candidates = teacher_matches
        for period in candidates:
            homework_period_keys.add(_period_key(period))
    for item in exam_items or []:
        try:
            exam_day = datetime.strptime(str(item.get("examDate", "")), "%Y%m%d").date()
        except (ValueError, TypeError):
            continue
        if monday <= exam_day < week_end:
            exam_days[exam_day] += 1
            try:
                exam_start = int(item.get("startTime") or 0)
                exam_end = int(item.get("endTime") or 0)
                start_min = exam_start // 100 * 60 + exam_start % 100
                end_min = exam_end // 100 * 60 + exam_end % 100
                if exam_start and exam_end and end_min > start_min:
                    exams_by_day[exam_day].append((start_min, end_min, item))
                    all_starts.append(start_min)
                    all_ends.append(end_min)
            except (ValueError, TypeError):
                pass
    for item in entries:
        try:
            day = datetime.strptime(str(item.get("date", "")), "%Y%m%d").date()
        except (ValueError, TypeError):
            continue
        if monday <= day < monday + timedelta(days=5):
            start, end = int(item.get("startTime", 0)), int(item.get("endTime", 0))
            start_min = start // 100 * 60 + start % 100
            end_min = end // 100 * 60 + end % 100
            if end_min <= start_min:
                continue
            by_day[day].append((start_min, end_min, item))
            all_starts.append(start_min)
            all_ends.append(end_min)
    start_minute = max(7 * 60, min(all_starts) // 15 * 15 - 15) if all_starts else 7 * 60 + 30
    end_minute = max(all_ends) if all_ends else 17 * 60
    end_minute = ((end_minute + 14) // 15) * 15
    scale = 1.45
    left, top, header_h, bottom = 92, 184, 62, 42
    width, col_w = 1520, (1520 - left - 18) // 5
    timeline_h = int((end_minute - start_minute) * scale)
    height = top + timeline_h + bottom
    img = Image.new("RGB", (width, height), palette["background"])
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font_bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    title_font = ImageFont.truetype(font_bold_path, 31)
    day_font = ImageFont.truetype(font_bold_path, 18)
    time_font = ImageFont.truetype(font_path, 13)
    subject_font = ImageFont.truetype(font_bold_path, 16)
    detail_font = ImageFont.truetype(font_path, 14)
    small_font = ImageFont.truetype(font_path, 12)
    marker_font = ImageFont.truetype(font_bold_path, 11)
    def draw_lesson_text(text, xy, font, fill, strike=False):
        draw.text(xy, text, font=font, fill=fill)
        if strike and text:
            box = draw.textbbox(xy, text, font=font)
            mid_y = (box[1] + box[3]) // 2
            draw.line((box[0], mid_y, box[2], mid_y), fill="#b94747", width=2)
    draw.text((left, 26), "TIMETABLE", font=small_font, fill=palette["muted"])
    draw.text((left, 48), f"{monday.strftime('%d %B')} – {(monday + timedelta(days=4)).strftime('%d %B %Y')}", font=title_font, fill=palette["title"])
    colors = ["#3e80a8", "#5978a9", "#428c83", "#96735b", "#7466a0", "#668b52"]
    day_names = ["MON", "TUE", "WED", "THU", "FRI"]
    for idx, name in enumerate(day_names):
        x = left + 12 + idx * col_w
        day = monday + timedelta(days=idx)
        draw.text((x + 12, 116), f"{name}  {day.strftime('%d.%m.')}", font=day_font, fill=palette["day"])
        marker_x = x + 12
        label = f"EXAM · {exam_days[day]}" if exam_days[day] else None
        if label:
            box = draw.textbbox((0, 0), label, font=marker_font)
            marker_w = box[2] - box[0] + 18
            draw.rounded_rectangle((marker_x, 148, marker_x + marker_w, 172), radius=7, fill=palette["exam_badge_bg"])
            draw.text((marker_x + 9, 153), label, font=marker_font, fill=palette["exam_badge_text"])
    y_for = lambda minute: top + int((minute - start_minute) * scale)
    for minute in range((start_minute // 60) * 60, end_minute + 1, 60):
        y = y_for(minute)
        draw.text((18, y - 8), f"{minute // 60:02d}:{minute % 60:02d}", font=time_font, fill=palette["footer"])
        draw.line((left, y, width - 15, y), fill=palette["grid"], width=1)
    for idx in range(5):
        x0 = left + 12 + idx * col_w
        x1 = x0 + col_w - 10
        draw.line((x0, top, x0, top + timeline_h), fill=palette["column"], width=1)
        day = monday + timedelta(days=idx)
        items = sorted(by_day[day], key=lambda x: (x[0], x[1]))
        day_is_in_range = (range_start is None or day >= range_start) and (range_end is None or day <= range_end)
        if not items and day_is_in_range:
            badge_y = top + max(24, timeline_h // 2 - 34)
            draw.rounded_rectangle((x0 + 22, badge_y, x1 - 22, badge_y + 62), radius=10, fill=palette["break"])
            label = "BREAK"
            label_box = draw.textbbox((0, 0), label, font=day_font)
            draw.text((x0 + (x1 - x0 - (label_box[2] - label_box[0])) / 2, badge_y + 9),
                      label, font=day_font, fill=palette["break_text"])
            sublabel = "No lessons scheduled"
            sub_box = draw.textbbox((0, 0), sublabel, font=small_font)
            draw.text((x0 + (x1 - x0 - (sub_box[2] - sub_box[0])) / 2, badge_y + 36),
                      sublabel, font=small_font, fill=palette["muted"])
        # Allocate side-by-side lanes for overlapping lessons.
        for start, end, item in items:
            overlaps = [r for r in items if r[0] < end and r[1] > start]
            lane = overlaps.index((start, end, item))
            lanes = max(1, len(overlaps))
            pad = 5
            bx0 = x0 + pad + lane * ((x1 - x0 - 2 * pad) / lanes)
            bx1 = x0 + pad + (lane + 1) * ((x1 - x0 - 2 * pad) / lanes) - 3
            y0, y1 = y_for(start) + 3, max(y_for(end) - 3, y_for(start) + 38)
            matching_exams = [exam for ex_start, ex_end, exam in exams_by_day[day]
                              if ex_start < end and ex_end > start]
            is_exam_period = bool(matching_exams)
            has_homework = _period_key(item) in homework_period_keys
            is_cancelled = _is_cancelled(item)
            card_fill = palette["cancel_bg"] if is_cancelled else (palette["exam_bg"] if is_exam_period else (palette["homework_bg"] if has_homework else palette["lesson"]))
            card_edge = palette["cancel_edge"] if is_cancelled else (palette["exam_edge"] if is_exam_period else (palette["homework_edge"] if has_homework else palette["lesson_edge"]))
            accent = "#ff536f" if is_cancelled and dark else ("#cf4545" if is_cancelled else (palette["exam_accent"] if is_exam_period else (palette["homework_accent"] if has_homework else ("#263442" if dark else colors[idx]))))
            draw.rounded_rectangle((bx0, y0, bx1, y1), radius=8, fill=card_fill, outline=card_edge, width=1)
            draw.rounded_rectangle((bx0 + 1, y0 + 5, bx0 + 5, y1 - 5), radius=2, fill=accent)
            tx, ty = bx0 + 13, y0 + 7
            subject = lesson_line(item, entity_names).splitlines()[0]
            subject = subject.split(" · ", 1)[-1].rstrip("*")
            draw_lesson_text(subject[:25], (tx, ty), subject_font,
                             palette["cancel_text"] if is_cancelled else palette["subject"], is_cancelled)
            if is_cancelled and y1 - y0 >= 58 and bx1 - bx0 >= 180:
                tag = "AUSFALL"
                tag_color = "#ff7589" if dark else "#b94747"
                tag_box = draw.textbbox((0, 0), tag, font=marker_font)
                draw.text((bx1 - (tag_box[2] - tag_box[0]) - 10, ty + 2), tag, font=marker_font, fill=tag_color)
            elif (is_exam_period or has_homework) and y1 - y0 >= 58:
                tag = "EXAM + HW" if is_exam_period and has_homework else ("EXAM" if is_exam_period else "HW")
                tag_color = palette["exam_text"] if is_exam_period else ("#69b5ff" if dark else "#52799f")
                tag_box = draw.textbbox((0, 0), tag, font=marker_font)
                draw.text((bx1 - (tag_box[2] - tag_box[0]) - 10, ty + 2), tag, font=marker_font, fill=tag_color)
            ty += 22
            draw_lesson_text(f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}",
                             (tx, ty), small_font, "#ff7589" if is_cancelled and dark else ("#8f5555" if is_cancelled else palette["time"]), is_cancelled)
            ty += 18
            room = _entity_label(item, "ro", entity_names, "#{id}")
            teacher = _entity_label(item, "te", entity_names, "#{id}")
            details = " · ".join(v for v in (room, teacher) if v)
            if details and ty + 17 < y1:
                draw_lesson_text(details[:32], (tx, ty), detail_font,
                                 "#ff7589" if is_cancelled and dark else ("#8f5555" if is_cancelled else palette["detail"]), is_cancelled)
            note = item.get("lstext") or item.get("substText") or item.get("info")
            if note and ty + 35 < y1:
                draw_lesson_text(str(note)[:30], (tx, ty + 20), small_font,
                                 "#ff7589" if is_cancelled and dark else ("#8f5555" if is_cancelled else palette["muted"]), is_cancelled)
        # If an exam has no matching lesson at that time, place a separate timed block.
        for exam_start, exam_end, exam in exams_by_day[day]:
            if any(start < exam_end and end > exam_start for start, end, _ in items):
                continue
            ey0, ey1 = y_for(exam_start) + 3, max(y_for(exam_end) - 3, y_for(exam_start) + 48)
            draw.rounded_rectangle((x0 + 5, ey0, x1 - 5, ey1), radius=8,
                                   fill=palette["exam_badge_bg"] if dark else "#fff0bd",
                                   outline=palette["exam_accent"] if dark else "#dfb83e", width=2)
            draw.rounded_rectangle((x0 + 6, ey0 + 5, x0 + 11, ey1 - 5), radius=2, fill=palette["exam_accent"])
            title = str(exam.get("name") or exam.get("subject") or "Exam")
            draw.text((x0 + 17, ey0 + 7), ("EXAM · " + title)[:28], font=subject_font, fill=palette["exam_text"])
            draw.text((x0 + 17, ey0 + 30),
                      f"{exam_start // 60:02d}:{exam_start % 60:02d}–{exam_end // 60:02d}:{exam_end % 60:02d}",
                      font=small_font, fill=palette["exam_text"])
    draw.text((left, height - 26), f"{sum(len(v) for v in by_day.values())} lessons  ·  WebUntis", font=small_font, fill=palette["footer"])
    output = BytesIO()
    img.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _entity_label(record, key, entity_names, fallback):
    value = record.get(key) or []
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, str):
        return value
    labels = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            ident = item.get("id")
            label = entity_names.get(key, {}).get(str(ident)) if ident is not None else None
            label = label or item.get("shortName") or item.get("name") or item.get("longName") or item.get("displayName")
            if not label and ident is not None:
                label = "[teacher]" if key == "te" else fallback.format(id=ident)
            if label:
                labels.append(str(label))
        elif item:
            labels.append(str(item))
    return ", ".join(labels)


def send_discord_image(image_bytes, filename, hook, content):
    """Upload a PNG attachment to an incoming Discord webhook."""
    boundary = "----WebUntis" + uuid.uuid4().hex
    metadata = json.dumps({"content": content, "attachments": [{"id": 0, "filename": filename}]}).encode("utf-8")
    parts = [
        b"--" + boundary.encode() + b"\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n" + metadata + b"\r\n",
        b"--" + boundary.encode() + b"\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"" + filename.encode() + b"\"\r\nContent-Type: image/png\r\n\r\n" + image_bytes + b"\r\n",
        b"--" + boundary.encode() + b"--\r\n",
    ]
    url = hook + ("&" if "?" in hook else "?") + "wait=true"
    req = urllib.request.Request(url, b"".join(parts), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "DiscordBot (https://discord.com/developers/docs, 1.0)",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Discord returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Discord rejected the timetable image (HTTP {exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Discord: {exc.reason}") from None


def homework_png(homework_items, entity_names, range_start, range_end, mode="light"):
    """Render a readable homework overview, grouped by due date."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Homework image support needs Pillow (python3-pil on Debian).") from exc

    dark = str(mode).casefold() == "dark"
    palette = {
        "background": "#10151c" if dark else "#edf1f4",
        "title": "#e5ebf2" if dark else "#334451",
        "meta": "#94a3b3" if dark else "#71808d",
        "date": "#e2e9f0" if dark else "#334451",
        "rule": "#26303a" if dark else "#c7d0d7",
        "empty": "#19222c" if dark else "#e3e9ed",
        "card": "#19222c" if dark else "#eaf2fb",
        "edge": "#31577b" if dark else "#b7cbe0",
        "accent": "#53a8ff" if dark else "#5b8fc5",
        "subject": "#e2e9f0" if dark else "#344554",
        "body": "#c5ced8" if dark else "#465763",
        "status_bg": "#53a8ff" if dark else None,
        "status": "#10151c" if dark else "#52799f",
    }
    grouped = defaultdict(list)
    visible_items = []
    for item in homework_items:
        try:
            due = datetime.strptime(str(item.get("dueDate", "")), "%Y%m%d").date()
        except (ValueError, TypeError):
            continue
        if range_start <= due <= range_end:
            grouped[due].append(item)
            visible_items.append(item)

    width, margin = 1120, 42
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        title_font = ImageFont.truetype(bold_path, 30)
        date_font = ImageFont.truetype(bold_path, 20)
        subject_font = ImageFont.truetype(bold_path, 18)
        body_font = ImageFont.truetype(font_path, 17)
        small_font = ImageFont.truetype(font_path, 14)
    except OSError:
        title_font = ImageFont.load_default()
        date_font = subject_font = body_font = small_font = title_font

    def subject_label(item):
        code = str(item.get("_lesson_subject") or "")
        subject_id = entity_names.get("_subject_label_ids", {}).get(code.strip().casefold())
        return entity_names.get("su", {}).get(str(subject_id), code or "Homework")

    def teacher_label(item):
        ident = item.get("_teacher_id")
        return entity_names.get("te", {}).get(str(ident), "") if ident is not None else ""

    def wrap(text, font, max_width):
        lines = []
        for paragraph in str(text or "").replace("\r", "").split("\n"):
            words = paragraph.split()
            if not words:
                lines.append("")
                continue
            line = words[0]
            for word in words[1:]:
                candidate = line + " " + word
                if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
                    line = candidate
                else:
                    lines.append(line)
                    line = word
            lines.append(line)
        return lines

    # Use the same restrained blue accent as the homework lesson markers.
    rows = []
    for due in sorted(grouped):
        rows.append(("date", due))
        for item in sorted(grouped[due], key=lambda value: (str(value.get("_lesson_subject", "")), str(value.get("text", "")))):
            body = str(item.get("text") or "No description provided.").strip()
            source_date = date_label(item.get("date"))
            teacher = teacher_label(item)
            meta = " · ".join(part for part in (f"Assigned {source_date}" if source_date else "", teacher) if part)
            attachments = item.get("attachments") or []
            attachment_names = [str(a.get("name") or a.get("fileName") or "Attachment")
                                for a in attachments if isinstance(a, dict)]
            rows.append(("item", item, body, meta, attachment_names))

    image = Image.new("RGB", (width, 160), palette["background"])
    draw = ImageDraw.Draw(image)
    max_text_width = width - margin * 2 - 44
    layout = []
    y = 110
    for row in rows:
        if row[0] == "date":
            y += 46
            layout.append((y, row))
            y += 38
            continue
        _, item, body, meta, attachment_names = row
        body_lines = wrap(body, body_font, max_text_width)
        card_h = 52 + len(body_lines) * 24 + (22 if meta else 0) + (20 if attachment_names else 0)
        layout.append((y, ("card", item, body_lines, meta, attachment_names, card_h)))
        y += card_h + 14
    height = max(190, y + 40)
    image = Image.new("RGB", (width, height), palette["background"])
    draw = ImageDraw.Draw(image)
    draw.text((margin, 28), "HOMEWORK", font=title_font, fill=palette["title"])
    count = len(visible_items)
    range_text = f"{count} assignment{'s' if count != 1 else ''} · due {range_start.strftime('%d %b')} – {range_end.strftime('%d %b %Y')}"
    draw.text((margin, 69), range_text, font=small_font, fill=palette["meta"])
    if not visible_items:
        draw.rounded_rectangle((margin, 120, width - margin, 184), radius=10, fill=palette["empty"])
        draw.text((margin + 20, 139), "No homework due in this period.", font=body_font, fill=palette["meta"])
    for y, row in layout:
        if row[0] == "date":
            due = row[1]
            label = due.strftime("%A · %d %B")
            draw.text((margin, y), label, font=date_font, fill=palette["date"])
            draw.line((margin, y + 32, width - margin, y + 32), fill=palette["rule"], width=1)
            continue
        _, item, body_lines, meta, attachment_names, card_h = row
        draw.rounded_rectangle((margin, y, width - margin, y + card_h), radius=10,
                               fill=palette["card"], outline=palette["edge"], width=1)
        draw.rounded_rectangle((margin + 1, y + 8, margin + 6, y + card_h - 8), radius=2, fill=palette["accent"])
        tx = margin + 20
        draw.text((tx, y + 12), subject_label(item), font=subject_font, fill=palette["subject"])
        status = "DONE" if item.get("completed") else "HW"
        status_box = draw.textbbox((0, 0), status, font=small_font)
        status_x = width - margin - (status_box[2] - status_box[0]) - 18
        if dark:
            draw.rounded_rectangle((status_x - 8, y + 11, width - margin - 10, y + 34), radius=10, fill=palette["status_bg"])
            draw.text((status_x, y + 14), status, font=small_font, fill=palette["status"])
        else:
            draw.text((status_x, y + 15), status, font=small_font, fill=palette["status"])
        text_y = y + 42
        for line in body_lines:
            draw.text((tx, text_y), line, font=body_font, fill=palette["body"])
            text_y += 24
        if meta:
            draw.text((tx, text_y + 2), meta, font=small_font, fill=palette["meta"])
            text_y += 22
        if attachment_names:
            draw.text((tx, text_y + 1), "Attachments: " + ", ".join(attachment_names), font=small_font, fill=palette["meta"])

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def fetch_plan_markers(prefix, today, end, opener):
    """Read homework/exam metadata from the logged-in WebUntis web session."""
    query = urllib.parse.urlencode({"startDate": normal_date(today), "endDate": normal_date(end)})
    base = f"https://{prefix}.webuntis.com/WebUntis/api/"
    results = {}
    for name, path, key in (("homework", "homeworks/lessons", "homeworks"), ("exams", "exams", "exams")):
        try:
            req = urllib.request.Request(base + path + "?" + query, headers={"Accept": "application/json"})
            with opener.open(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            data = payload.get("data", {})
            results[name] = data.get(key, [])
            if name == "homework":
                results["homework_data"] = data
            print(f"Loaded {len(results[name])} {name} marker(s) from WebUntis.")
        except Exception as exc:
            results[name] = []
            code = getattr(exc, "code", None)
            print(f"WebUntis {name} markers unavailable" + (f" (HTTP {code})" if code else "."))
    homework_items = results.get("homework", [])
    homework_data = results.get("homework_data", {})
    lesson_by_id = {str(item.get("id")): item for item in homework_data.get("lessons", [])
                    if isinstance(item, dict) and item.get("id") is not None}
    teacher_by_homework = {str(item.get("homeworkId")): item.get("teacherId")
                           for item in homework_data.get("records", [])
                           if isinstance(item, dict) and item.get("homeworkId") is not None}
    for item in homework_items:
        lesson = lesson_by_id.get(str(item.get("lessonId")), {})
        if lesson.get("subject"):
            item["_lesson_subject"] = str(lesson["subject"])
        teacher_id = teacher_by_homework.get(str(item.get("id")))
        if teacher_id is not None:
            item["_teacher_id"] = teacher_id
    return homework_items, results.get("exams", [])


def demo(hook=""):
    old = [{"date": 20261023, "startTime": 900, "endTime": 950, "su": [{"name": "Math"}], "ro": [{"name": "12"}], "lsid": 1}]
    new = [{"date": 20261023, "startTime": 900, "endTime": 950, "su": [{"name": "Math"}], "ro": [{"name": "14"}], "lsid": 1},
           {"date": 20261023, "startTime": 1000, "endTime": 1050, "su": [{"name": "Physics"}], "ro": [{"name": "7"}], "lsid": 2}]
    lines = ["WebUntis watcher test (simulated)"]
    for kind, item in diff(old, new):
        lines.append(f"{kind}: {pretty(item)}")
    message = "\n".join(lines)
    print(message)
    if hook:
        send_discord(message, hook)
        print("Sent simulated alert to Discord.")


def main():
    parser = argparse.ArgumentParser(description="Watch a WebUntis timetable through the legacy JSON-RPC interface.")
    parser.add_argument("--demo", action="store_true", help="show simulated changes; no login or network")
    parser.add_argument("--demo-discord", action="store_true", help="send the simulated change to the saved Discord webhook")
    parser.add_argument("--setup", action="store_true", help="save login and optional webhook in a private .env file")
    parser.add_argument("--status", action="store_true", help="show saved config and the last successful timetable snapshot time")
    parser.add_argument("--send-timetable", action="store_true", help="send the current timetable window to the saved Discord webhook")
    parser.add_argument("--send-homework", action="store_true", help="send a readable homework overview to the saved Discord webhook")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--interval", type=int, default=900, help="poll interval in seconds (default 900)")
    parser.add_argument("--days", type=int, default=21, help="how many days ahead to compare (default 21)")
    args = parser.parse_args()
    if args.setup:
        try:
            setup_config()
        except Exception as exc:
            parser.error(str(exc))
        return
    if args.status:
        config = load_config()
        print(f"Saved config: {'yes' if config else 'no'}")
        if STATE.exists():
            try:
                snapshot = json.loads(STATE.read_text("utf-8"))
                timestamp = datetime.fromtimestamp(STATE.stat().st_mtime, tz=SCHOOL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
                print(f"Timetable snapshot: {len(snapshot)} entries; last updated {timestamp}")
            except Exception:
                print("Timetable snapshot exists but could not be read")
        else:
            print("Timetable snapshot: not created yet")
        if STATUS.exists():
            try:
                status = json.loads(STATUS.read_text("utf-8"))
                print(f"Last successful fetch: {status.get('checked_at', 'unknown')} ({status.get('entries', '?')} entries, next {status.get('days', '?')} days)")
            except Exception:
                print("Last successful fetch status could not be read")
        return
    if args.demo:
        config = load_config() or {}
        if args.demo_discord and not config.get("DISCORD_WEBHOOK_URL"):
            parser.error("no Discord webhook is saved; run --setup first")
        try:
            demo(config.get("DISCORD_WEBHOOK_URL", "") if args.demo_discord else "")
        except Exception as exc:
            print(f"Discord test failed: {exc}", file=sys.stderr)
            return 1
        return
    if args.demo_discord:
        parser.error("--demo-discord requires --demo")
    if args.interval < 60 or args.days < 1 or args.days > 60:
        parser.error("use an interval of at least 60 seconds and a range of 1–60 days")

    config = load_config()
    if config:
        prefix = config.get("WEBUNTIS_SUBDOMAIN", "").strip()
        school = config.get("WEBUNTIS_SCHOOL", "").strip()
        username = config.get("WEBUNTIS_USERNAME", "").strip()
        password = config.get("WEBUNTIS_PASSWORD", "")
        hook = config.get("DISCORD_WEBHOOK_URL", "").strip()
        print(f"Loaded saved settings from {ENV_FILE}.")
    else:
        prefix = input("WebUntis school subdomain (from https://<school>.webuntis.com): ").strip()
        school = input("School name used by WebUntis (often the same as the subdomain): ").strip()
        username = input("WebUntis username: ").strip()
        password = getpass.getpass("WebUntis password (input hidden): ")
        hook = getpass.getpass("Optional Discord webhook URL (hidden; Enter for print-only): ").strip()
    if not prefix or not school or not username or not password:
        parser.error("school, subdomain, username, and password are required")

    endpoint = f"https://{prefix}.webuntis.com/WebUntis/jsonrpc.do?{urllib.parse.urlencode({'school': school})}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        # WebUntis needs a pre-auth session carrying the selected school name
        # before JSON-RPC authenticate will accept the login request.
        preauth_url = f"https://{prefix}.webuntis.com/WebUntis/?{urllib.parse.urlencode({'school': school})}"
        with opener.open(preauth_url, timeout=25) as response:
            response.read()
        auth = rpc(opener, endpoint, "authenticate", {"user": username, "password": password, "client": "PersonalTimetableWatcher"})
        if not isinstance(auth, dict) or not auth.get("personId"):
            raise RuntimeError("Login response did not include a person ID; the school may require SSO or another login method.")
        person_id = auth["personId"]
        person_type = auth.get("personType", 5)
        print("Signed in. Credentials were loaded from the private config file and are held in memory for this run.")
        entity_names = load_entity_names(opener, endpoint)
    except Exception as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    if args.send_timetable:
        if not hook:
            print("No Discord webhook is saved; run with --setup first.", file=sys.stderr)
            return 1
        try:
            today = school_today()
            end = today + timedelta(days=args.days)
            first_monday = today - timedelta(days=today.weekday())
            options = {"id": person_id, "element": {"id": person_id, "type": person_type},
                       # Include earlier days from this week so homework can be
                       # attached to the period where it was assigned.
                       "startDate": normal_date(first_monday), "endDate": normal_date(end),
                       "showLsText": True, "showStudentgroup": True, "showLsNumber": True,
                       "showSubstText": True, "showInfo": True, "showBooking": True}
            fresh = records_from(rpc(opener, endpoint, "getTimetable", {"options": options}))
            homework_items, exam_items = fetch_plan_markers(prefix, today, end, opener)
            entries = sorted(fresh, key=schedule_sort_key)
            mondays = []
            monday = first_monday
            while monday <= end:
                mondays.append(monday)
                monday += timedelta(days=7)
            count = 0
            for monday in mondays:
                image = timetable_png(entries, monday, entity_names, today, end, homework_items, exam_items)
                if image:
                    filename = f"timetable-{monday.isoformat()}.png"
                    send_discord_image(image, filename, hook, f"Your WebUntis timetable · week of {monday.strftime('%d %b %Y')}")
                    count += 1
            print(f"Sent {len(entries)} timetable entries as {count} weekly timetable image(s), including the rest of this week and the next {args.days} days. The saved baseline was not changed.")
        except Exception as exc:
            print(f"Timetable send failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.send_homework:
        if not hook:
            print("No Discord webhook is saved; run with --setup first.", file=sys.stderr)
            return 1
        try:
            today = school_today()
            end = today + timedelta(days=args.days)
            homework_items, _ = fetch_plan_markers(prefix, today, end, opener)
            image = homework_png(homework_items, entity_names, today, end)
            filename = f"homework-{today.isoformat()}.png"
            send_discord_image(image, filename, hook,
                               f"Your WebUntis homework · {today.strftime('%d %b')}–{end.strftime('%d %b %Y')}")
            print(f"Sent a homework overview with {len(homework_items)} assignment(s) due in the next {args.days} days.")
        except Exception as exc:
            print(f"Homework send failed: {exc}", file=sys.stderr)
            return 1
        return 0

    while True:
        try:
            today = school_today()
            end = today + timedelta(days=args.days)
            options = {"id": person_id, "element": {"id": person_id, "type": person_type},
                       "startDate": normal_date(today), "endDate": normal_date(end),
                       "showLsText": True, "showStudentgroup": True, "showLsNumber": True,
                       "showSubstText": True, "showInfo": True, "showBooking": True}
            fresh = records_from(rpc(opener, endpoint, "getTimetable", {"options": options}))
            STATUS.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "entries": len(fresh), "days": args.days}, indent=2), encoding="utf-8")
            old = json.loads(STATE.read_text("utf-8")) if STATE.exists() else None
            if old is None:
                STATE.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"Initial snapshot saved ({len(fresh)} timetable entries, next {args.days} days). Future differences will alert.")
            else:
                found = diff(old, fresh)
                if found:
                    lines = [f"WebUntis timetable update: {len(found)} change(s)"]
                    for kind, item in found[:20]:
                        lines.append(f"{kind}: {pretty(item, entity_names)}")
                    if len(found) > 20:
                        lines.append(f"... and {len(found) - 20} more")
                    message = "\n".join(lines)
                    print(message)
                    send_discord(message, hook)
                else:
                    print(f"No changes ({len(fresh)} entries).")
                STATE.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"Check failed: {exc}", file=sys.stderr)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main() or 0)
