#!/usr/bin/env python3
"""Private Discord bot for a personal WebUntis timetable."""
import asyncio
import calendar
import hashlib
import http.cookiejar
import json
import os
import re
import shlex
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

import legacy_bot as untis

ROOT = Path(__file__).resolve().parent
PLAN_STATE = ROOT / "plan_state.json"
CHANNEL_STATE = ROOT / "discord_channel.json"
REMINDER_STATE = ROOT / "reminder_state.json"
POLL_SECONDS = 900
FAST_POLL_SECONDS = 300
LOOKAHEAD_DAYS = 21
TIMETABLE_LOOKAHEAD_DAYS = 35
NORMAL_CHECK_DAYS = 21
HW_COMMAND_DEFAULT_WEEKS = 4
HW_COMMAND_MAX_WEEKS = 52
BOT_OWNER_ID = 976444034266583041
SCHOOL_TZ = ZoneInfo("Europe/Vienna")
FAVICON_COLOR = 0xFF6033


async def log_event(title, description, *, error=False):
    """Send a best-effort event summary without interrupting the bot."""
    try:
        config = untis.load_config() or {}
        url = os.environ.get("WEBUNTIS_LOG_WEBHOOK_URL") or config.get("WEBUNTIS_LOG_WEBHOOK_URL", "")
        if not url:
            return
        embed = discord.Embed(title=title[:256], description=description[:4096],
                              color=discord.Color.red() if error else FAVICON_COLOR,
                              timestamp=datetime.now(timezone.utc))
        webhook = discord.Webhook.from_url(url, client=bot)
        await asyncio.wait_for(webhook.send(
            embed=embed, username="WebUntis Logs",
            allowed_mentions=discord.AllowedMentions.none(),
        ), timeout=10)
    except Exception as exc:
        print(f"Webhook logging failed: {type(exc).__name__}", file=sys.stderr)


def log_location(guild, channel, user):
    def label(value):
        return discord.utils.escape_markdown(str(value))[:200]
    return (f"Server: **{label(getattr(guild, 'name', 'Direct message'))}**\n"
            f"Channel: **{label(getattr(channel, 'name', 'Unknown'))}**\n"
            f"User: **{label(getattr(user, 'display_name', 'Unknown'))}** "
            f"(`{getattr(user, 'id', 'unknown')}`)")


def add_months(day, months):
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def save_json(path, value):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def webuntis_connection():
    config = untis.load_config() or {}
    prefix = config.get("WEBUNTIS_SUBDOMAIN", "").strip()
    school = config.get("WEBUNTIS_SCHOOL", "").strip()
    username = config.get("WEBUNTIS_USERNAME", "").strip()
    password = config.get("WEBUNTIS_PASSWORD", "")
    if not all((prefix, school, username, password)):
        raise RuntimeError("WebUntis settings are missing from .env. Run legacy_bot.py --setup first.")

    endpoint = f"https://{prefix}.webuntis.com/WebUntis/jsonrpc.do?{urllib.parse.urlencode({'school': school})}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    preauth_url = f"https://{prefix}.webuntis.com/WebUntis/?{urllib.parse.urlencode({'school': school})}"
    with opener.open(preauth_url, timeout=25) as response:
        response.read()
    auth = untis.rpc(opener, endpoint, "authenticate", {
        "user": username, "password": password, "client": "PersonalTimetableDiscordBot"
    })
    if not isinstance(auth, dict) or not auth.get("personId"):
        raise RuntimeError("WebUntis login did not return a person ID.")
    return prefix, endpoint, opener, auth


def fetch_snapshot(full=False):
    prefix, endpoint, opener, auth = webuntis_connection()
    today = datetime.now(SCHOOL_TZ).date()
    exam_end = add_months(today, 5)
    end = exam_end if full else today + timedelta(days=TIMETABLE_LOOKAHEAD_DAYS)
    plan_end = exam_end if full else today + timedelta(days=LOOKAHEAD_DAYS)
    week_start = today - timedelta(days=today.weekday())
    query_start = add_months(today, -1) if full else week_start
    options = {
        "id": auth["personId"],
        "element": {"id": auth["personId"], "type": auth.get("personType", 5)},
        "startDate": untis.normal_date(query_start),
        "endDate": untis.normal_date(end),
        "showLsText": True,
        "showStudentgroup": True,
        "showLsNumber": True,
        "showSubstText": True,
        "showInfo": True,
        "showBooking": True,
    }
    timetable = untis.records_from(untis.rpc(opener, endpoint, "getTimetable", {"options": options}))
    homework, exams = untis.fetch_plan_markers(prefix, query_start, exam_end, opener)
    homework = [item for item in homework
                if str(item.get("dueDate", "")) <= untis.normal_date(plan_end)]
    return {
        "today": today,
        "end": end,
        "plan_end": plan_end,
        "exam_end": exam_end,
        "timetable": sorted(timetable, key=untis.schedule_sort_key),
        "homework": homework,
        "exams": exams,
        "entity_names": untis.load_entity_names(opener, endpoint),
    }


def fetch_homework_range(weeks, past=True, future=True):
    prefix, endpoint, opener, _auth = webuntis_connection()
    today = datetime.now(SCHOOL_TZ).date()
    start = today - timedelta(weeks=weeks) if past else today
    end = today + timedelta(weeks=weeks) if future else today
    homework, _ = untis.fetch_plan_markers(prefix, start, end, opener)
    names = untis.load_entity_names(opener, endpoint)
    homework = [item for item in homework if item_in_window(item, "dueDate", start, end)]
    return today, start, end, homework, names


def parse_homework_options(tokens):
    options = {}
    for token in tokens:
        key, separator, value = token.partition(":")
        key = key.casefold()
        if not separator or key not in ("class", "time", "range", "past", "future") or not value or key in options:
            raise ValueError("Invalid option.")
        options[key] = value
    if "time" in options and "range" in options:
        raise ValueError("Use time only once.")
    try:
        weeks = int(options.get("time", options.get("range", str(HW_COMMAND_DEFAULT_WEEKS))))
    except ValueError:
        raise ValueError("Time must be a whole number of weeks.") from None
    if not 1 <= weeks <= HW_COMMAND_MAX_WEEKS:
        raise ValueError(f"Time must be 1–{HW_COMMAND_MAX_WEEKS} weeks.")
    flags = {}
    for key in ("past", "future"):
        value = options.get(key, "false" if key == "past" else "true").casefold()
        if value not in ("true", "false"):
            raise ValueError(f"{key} must be true or false.")
        flags[key] = value == "true"
    if not any(flags.values()):
        raise ValueError("Past and future cannot both be false.")
    return options.get("class", "all"), weeks, flags["past"], flags["future"]

def stable_items(items):
    result = {}
    for index, item in enumerate(items):
        ident = item.get("id")
        # WebUntis returns exam records with id=0, which is a placeholder
        # shared by every exam. Treat missing/zero IDs as non-identifiers so
        # separate records cannot overwrite each other during comparisons.
        if ident in (None, 0, "", "0"):
            raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            ident = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        result[str(ident)] = item
    return result


def item_in_window(item, field, start, end):
    value = str(item.get(field) or "")
    return untis.normal_date(start) <= value <= untis.normal_date(end)


def merge_window(old_items, fresh_items, field, start, end):
    """Replace records in the checked window while retaining the rest of baseline."""
    outside = [item for item in old_items if not item_in_window(item, field, start, end)]
    inside = [item for item in fresh_items if item_in_window(item, field, start, end)]
    return outside + inside


def homework_subject(item, names):
    code = str(item.get("_lesson_subject") or item.get("subject") or "")
    subject_id = names.get("_subject_label_ids", {}).get(code.strip().casefold())
    return names.get("su", {}).get(str(subject_id), code or "Homework")


def homework_matches_class(item, requested, names):
    target = re.sub(r"[^a-z0-9]+", "", requested.casefold())
    if target == "all":
        return True
    aliases = [homework_subject(item, names), item.get("_lesson_subject"), item.get("subject")]
    for alias in aliases:
        normalized = re.sub(r"[^a-z0-9]+", "", str(alias or "").casefold())
        if normalized and target == normalized:
            return True
    return False


def homework_description(item, names):
    due = untis.date_label(item.get("dueDate")) or "date unknown"
    assigned = untis.date_label(item.get("date"))
    subject = homework_subject(item, names)
    teacher_id = item.get("_teacher_id")
    teacher = names.get("te", {}).get(str(teacher_id), "") if teacher_id is not None else ""
    status = "completed" if item.get("completed") else "not completed"
    detail = str(item.get("text") or "No assignment text provided.").strip()
    return f"Homework · {subject} · due {due} ({status})\nAssigned {assigned}" + (f" · {teacher}" if teacher else "") + f"\n{detail}"


def homework_embeds(items, names, label, start, end):
    """Build mobile-friendly pages without dropping long assignments."""
    sections = []
    for item in items:
        subject = discord.utils.escape_markdown(str(homework_subject(item, names)))
        due = untis.date_label(item.get("dueDate")) or "date unknown"
        assigned = untis.date_label(item.get("date"))
        teacher_id = item.get("_teacher_id")
        teacher = names.get("te", {}).get(str(teacher_id), "") if teacher_id is not None else ""
        metadata = " · ".join(part for part in (
            f"Assigned {assigned}" if assigned else "",
            discord.utils.escape_markdown(str(teacher)) if teacher else "",
            "Completed" if item.get("completed") else "Not completed",
        ) if part)
        detail = str(item.get("text") or "No assignment text provided.").strip()
        section = f"**{subject}** · due **{due}**\n"
        if metadata:
            section += f"-# {metadata}\n"
        sections.extend(split_message(section + detail, limit=3500))

    pages, current = [], ""
    for section in sections:
        if current and len(current) + len(section) + 2 > 3500:
            pages.append(current)
            current = ""
        current = f"{current}\n\n{section}" if current else section
    if current:
        pages.append(current)
    if not pages:
        pages = ["No matching homework in this range."]

    date_range = f"{untis.date_label(untis.normal_date(start))} – {untis.date_label(untis.normal_date(end))}"
    count = f"{len(items)} assignment{'s' if len(items) != 1 else ''}"
    embeds = []
    for index, page in enumerate(pages):
        embed = discord.Embed(title=f"{label[:230]} homework", description=page, color=FAVICON_COLOR)
        footer = f"{date_range} · {count}"
        if len(pages) > 1:
            footer += f" · {index + 1}/{len(pages)}"
        embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds


def exam_title(item, names=None):
    title = item.get("name") or item.get("title") or item.get("text") or item.get("subject") or "Exam"
    if isinstance(title, dict):
        title = title.get("name") or title.get("shortName") or "Exam"
    title = str(title)
    names = names or {}
    for code, subject_id in names.get("_subject_label_ids", {}).items():
        label = names.get("su", {}).get(str(subject_id))
        if label and code and label.casefold() != code.casefold():
            title = re.sub(r"(?<!\w)" + re.escape(code) + r"(?!\w)", label, title, flags=re.IGNORECASE)
    subject = item.get("subject")
    if subject:
        subject_id = names.get("_subject_label_ids", {}).get(str(subject).strip().casefold())
        subject = names.get("su", {}).get(str(subject_id), subject) if subject_id is not None else subject
        if str(subject).casefold() not in title.casefold():
            title = f"{subject} · {title}"
    return title


def exam_description(item, names=None):
    exam_day = untis.date_label(item.get("examDate") or item.get("date")) or "date unknown"
    start = untis.time_label(item.get("startTime"))
    end = untis.time_label(item.get("endTime"))
    time_text = f" · {start}–{end}" if start and end else ""
    return f"Exam · {exam_day}{time_text} · {exam_title(item, names)}"


def exam_display_title(item, names):
    """Prefer the WebUntis subject; use the exam name when it is unavailable."""
    subject = item.get("subject")
    if isinstance(subject, dict):
        subject = subject.get("name") or subject.get("shortName")
    if subject and str(subject).strip():
        subject = str(subject).strip()
        subject_id = names.get("_subject_label_ids", {}).get(subject.casefold())
        return str(names.get("su", {}).get(str(subject_id)) or subject)
    return exam_title(item, names)


def exam_embeds(items, names, start, end):
    """Build mobile-friendly exam pages in the same style as homework."""
    sections = []
    for item in items:
        title = discord.utils.escape_markdown(exam_display_title(item, names))
        day = untis.date_label(item.get("examDate") or item.get("date")) or "date unknown"
        start_time = untis.time_label(item.get("startTime"))
        end_time = untis.time_label(item.get("endTime"))
        time = f"{start_time}–{end_time}" if start_time and end_time else start_time or end_time
        metadata = f"-# {discord.utils.escape_markdown(time)}" if time else ""
        section = f"**{title}** · **{day}**" + (f"\n{metadata}" if metadata else "")
        sections.extend(split_message(section, limit=3500))

    pages, current = [], ""
    for section in sections:
        if current and len(current) + len(section) + 2 > 3500:
            pages.append(current)
            current = ""
        current = f"{current}\n\n{section}" if current else section
    if current:
        pages.append(current)
    if not pages:
        pages = ["No exams are listed from this week through the next five months."]

    date_range = f"{untis.date_label(untis.normal_date(start))} – {untis.date_label(untis.normal_date(end))}"
    count = f"{len(items)} exam{'s' if len(items) != 1 else ''}"
    embeds = []
    for index, page in enumerate(pages):
        embed = discord.Embed(title="Upcoming exams", description=page, color=FAVICON_COLOR)
        footer = f"{date_range} · {count}"
        if len(pages) > 1:
            footer += f" · {index + 1}/{len(pages)}"
        embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds


def change_lines(old, fresh, label, formatter):
    before, after = stable_items(old), stable_items(fresh)
    lines = []
    for key in after.keys() - before.keys():
        lines.append((f"{label} added", formatter(after[key])))
    for key in before.keys() - after.keys():
        lines.append((f"{label} removed", formatter(before[key])))
    for key in before.keys() & after.keys():
        if before[key] != after[key]:
            previous, current = before[key], after[key]
            if label == "Homework" and previous.get("completed") != current.get("completed"):
                kind = "Homework completed" if current.get("completed") else "Homework reopened"
            else:
                kind = f"{label} changed"
            lines.append((kind, formatter(current)))
    return lines


def split_message(text, limit=1850):
    chunks, current = [], ""
    for line in text.splitlines() or [""]:
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class UntisDiscordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         allowed_mentions=discord.AllowedMentions.none(),
                         activity=discord.Game(name="Restarted"))
        self.alert_channel_id = None
        try:
            self.alert_channel_id = json.loads(CHANNEL_STATE.read_text("utf-8")).get("channel_id")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        self.snapshot = None
        self.last_checked = None
        self.last_error = None
        self.poll_task = None
        self.started = False

    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
        try:
            channel_state = json.loads(CHANNEL_STATE.read_text("utf-8"))
            guild_id = int(channel_state.get("guild_id", 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            guild_id = 0
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.clear_commands(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            print(f"Cleared server commands for guild {guild_id}; {len(guild_synced)} remain.")
            channel_state.pop("guild_id", None)
            save_json(CHANNEL_STATE, channel_state)

    async def on_ready(self):
        if not self.started:
            self.started = True
            print("Discord bot connected; slash commands and change alerts are public in the server.")
            print(f"Due-tomorrow homework/exam reminders scheduled daily at 18:00 {SCHOOL_TZ.key}.")
            self.poll_task = asyncio.create_task(self.poll_forever())
            self.reminder_task = asyncio.create_task(self.reminder_forever())
            asyncio.create_task(self.show_restart_presence())
            await log_event("WebUntis bot started", "Connected to Discord and started polling.")

    async def on_interaction(self, interaction):
        if interaction.type == discord.InteractionType.application_command:
            name = (interaction.data or {}).get("name", "unknown")
            await log_event("Command used", f"`/{name}`\n" + log_location(
                interaction.guild, interaction.channel, interaction.user))

    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        try:
            tokens = shlex.split(message.content)
        except ValueError:
            return
        if not tokens or tokens[0].casefold() != ".hw":
            return

        command_text = discord.utils.escape_markdown(message.content[:1500])
        asyncio.create_task(log_event("Command used", f"Command: {command_text}\n" + log_location(message.guild, message.channel, message.author)))

        if len(tokens) == 2 and tokens[1].casefold() == "help":
            embed = discord.Embed(description=(
                "Defaults:\n"
                f"`.hw class:all time:{HW_COMMAND_DEFAULT_WEEKS} past:false future:true`\n\n"
                "`class:` Filter by subject; `all` includes every subject.\n"
                f"`time:` Window in weeks (1–{HW_COMMAND_MAX_WEEKS}); `range:` also works.\n"
                "`past:` Include past dates; `future:` include future dates. "
                "Today is always included; at least one must be true."
            ), color=FAVICON_COLOR)
            await message.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return

        try:
            class_name, weeks, past, future = parse_homework_options(tokens[1:])
        except ValueError as exc:
            await message.channel.send(
                f"{exc} Use `.hw help` for available options.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            today, start, end, homework, names = await asyncio.to_thread(fetch_homework_range, weeks, past, future)
            matches = [item for item in homework if homework_matches_class(item, class_name, names)]
            matches.sort(key=lambda item: str(item.get("dueDate", "")))
            label = "All subjects" if class_name.casefold() == "all" else (homework_subject(matches[0], names) if matches else class_name.title())
            for embed in homework_embeds(matches, names, label, start, end):
                await message.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            await log_event("Homework command failed", f"Error: `{type(exc).__name__}`", error=True)
            await message.channel.send(
                f"Could not load WebUntis homework: {str(exc)[:1000]}",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def show_restart_presence(self):
        await asyncio.sleep(5)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="WebUntis"
        ))

    def remember_alert_channel(self, interaction):
        channel = interaction.channel
        guild = interaction.guild
        if guild is None or channel is None:
            return
        member = guild.me
        if member is None or not channel.permissions_for(member).send_messages:
            return
        self.alert_channel_id = channel.id
        save_json(CHANNEL_STATE, {"channel_id": channel.id})

    async def get_alert_channel(self):
        if self.alert_channel_id is not None:
            channel = self.get_channel(int(self.alert_channel_id))
            if channel is None:
                try:
                    channel = await self.fetch_channel(int(self.alert_channel_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                    channel = None
            if channel is not None and hasattr(channel, "send"):
                guild = getattr(channel, "guild", None)
                member = guild.me if guild is not None else None
                if member is not None and channel.permissions_for(member).send_messages:
                    return channel
                self.alert_channel_id = None

        # If nobody has used a command yet, select the first writable text
        # channel in the installed server so automatic alerts still have a home.
        for guild in self.guilds:
            member = guild.me
            if member is None:
                continue
            channels = ([guild.system_channel] if guild.system_channel else []) + list(guild.text_channels)
            for channel in channels:
                if channel is None or not channel.permissions_for(member).send_messages:
                    continue
                self.alert_channel_id = channel.id
                save_json(CHANNEL_STATE, {"channel_id": channel.id})
                return channel
        return None

    async def poll_once(self, notify=True, length="normal"):
        full = length == "full"
        fresh = await asyncio.to_thread(fetch_snapshot, full)
        old_timetable = json.loads(untis.STATE.read_text("utf-8")) if untis.STATE.exists() else None
        old_plan = json.loads(PLAN_STATE.read_text("utf-8")) if PLAN_STATE.exists() else None
        old_status = json.loads(untis.STATUS.read_text("utf-8")) if untis.STATUS.exists() else {}
        old_timetable_end = fresh["today"] + timedelta(days=int(old_status.get("timetable_days", old_status.get("days", LOOKAHEAD_DAYS))))
        old_plan_end = fresh["today"] + timedelta(days=int(old_status.get("plan_days", LOOKAHEAD_DAYS)))
        window_start = add_months(fresh["today"], -1) if full else fresh["today"]
        window_end = fresh["exam_end"] if full else fresh["today"] + timedelta(days=NORMAL_CHECK_DAYS)
        checked_days = (window_end - fresh["today"]).days
        lines = []
        initial = old_timetable is None
        if initial:
            lines.append(("Initial timetable snapshot", f"Saved {len(fresh['timetable'])} upcoming lessons."))
        else:
            old_timetable_window = [item for item in old_timetable
                                    if item_in_window(item, "date", window_start, window_end)]
            fresh_timetable_window = [item for item in fresh["timetable"]
                                      if item_in_window(item, "date", window_start, window_end)]
            known_timetable_start = str(old_status.get("timetable_start") or min(
                (str(item.get("date", "")) for item in old_timetable if item.get("date")),
                default=untis.normal_date(fresh["today"]),
            ))
            if full and untis.normal_date(window_start) < known_timetable_start:
                old_keys = {untis.record_key(item, index) for index, item in enumerate(old_timetable_window)}
                fresh_timetable_window = [item for index, item in enumerate(fresh_timetable_window)
                                          if str(item.get("date", "")) >= known_timetable_start
                                          or untis.record_key(item, index) in old_keys]
            for kind, item in untis.diff(old_timetable_window, fresh_timetable_window):
                item_day = None
                try:
                    item_day = datetime.strptime(str(item.get("date", "")), "%Y%m%d").date()
                except (ValueError, TypeError):
                    pass
                if kind == "Added" and item_day is not None and item_day > old_timetable_end:
                    # A first full scan widens the known baseline. Don't report
                    # newly visible lessons as changes until a full baseline exists.
                    continue
                lines.append((f"Timetable {kind.lower()}", untis.pretty(item, fresh["entity_names"])))

        if old_plan is None:
            initial = True
        else:
            # Let past homework age out silently. Otherwise its normal
            # disappearance after the due date looks like a removal.
            homework_before = [item for item in old_plan.get("homework", [])
                               if item_in_window(item, "dueDate", fresh["today"], window_end)]
            homework_for_compare = [item for item in fresh["homework"]
                                    if item_in_window(item, "dueDate", fresh["today"], window_end)]
            if full and old_plan_end < window_end:
                known_homework = set(stable_items(homework_before))
                homework_for_compare = [item for item in homework_for_compare
                                        if (str(item.get("dueDate", "")) <= untis.normal_date(old_plan_end)
                                            or next(iter(stable_items([item])), "") in known_homework)]
            lines.extend(change_lines(homework_before, homework_for_compare, "Homework",
                                     lambda item: homework_description(item, fresh["entity_names"])))
            if old_plan.get("exam_identity_version", 0) >= 2:
                exams_before = [item for item in old_plan.get("exams", [])
                                if item_in_window(item, "examDate" if item.get("examDate") else "date", window_start, window_end)]
                exams_for_compare = [item for item in fresh["exams"]
                                     if item_in_window(item, "examDate" if item.get("examDate") else "date", window_start, window_end)]
                known_exam_start = str(old_status.get("exam_start") or min(
                    (str(item.get("examDate") or item.get("date") or "") for item in old_plan.get("exams", [])),
                    default=untis.normal_date(fresh["today"]),
                ))
                if full and untis.normal_date(window_start) < known_exam_start:
                    old_exam_keys = set(stable_items(exams_before))
                    exams_for_compare = [item for item in exams_for_compare
                                         if str(item.get("examDate") or item.get("date") or "") >= known_exam_start
                                         or next(iter(stable_items([item])), "") in old_exam_keys]
                lines.extend(change_lines(exams_before, exams_for_compare, "Exam", exam_description))
            else:
                # The previous comparator collapsed all id=0 exams into one
                # row. Re-key once without reporting the migration as additions.
                print("Initialized collision-free exam baseline; suppressed migration alerts.")
        # Persist only after public channel notifications succeed, so a temporary
        # Discord failure does not silently discard changes before the next poll.
        if notify and not initial and lines:
            message = f"<@{BOT_OWNER_ID}> **WebUntis update**\n" + "\n\n".join(f"**{kind}**\n{detail}" for kind, detail in lines)
            channel = await self.get_alert_channel()
            if channel is None:
                raise RuntimeError("No writable text channel found. Give the bot Send Messages permission in its server.")
            for chunk in split_message(message):
                await channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=BOT_OWNER_ID)]),
                )

        if old_timetable is None:
            timetable_state = fresh["timetable"]
        else:
            timetable_state = merge_window(old_timetable, fresh["timetable"], "date", window_start, window_end)
        save_json(untis.STATE, sorted(timetable_state, key=untis.schedule_sort_key))
        if old_plan is None:
            homework_state = fresh["homework"]
            exam_state = fresh["exams"]
        else:
            exam_state = merge_window(old_plan.get("exams", []), fresh["exams"], "examDate", window_start, window_end)
        homework_state = (fresh["homework"] if old_plan is None else
                          merge_window(old_plan.get("homework", []), fresh["homework"], "dueDate", fresh["today"], window_end))
        save_json(PLAN_STATE, {
            "homework": homework_state, "exams": exam_state,
            "exam_identity_version": 2,
        })
        save_json(untis.STATUS, {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "entries": len(timetable_state), "days": max(checked_days, int(old_status.get("days", 0))),
            "timetable_days": max(checked_days, int(old_status.get("timetable_days", old_status.get("days", TIMETABLE_LOOKAHEAD_DAYS)))),
            "plan_days": max(checked_days, int(old_status.get("plan_days", LOOKAHEAD_DAYS))),
            "timetable_start": min(str(old_status.get("timetable_start") or untis.normal_date(window_start)), untis.normal_date(window_start)),
            "exam_start": min(str(old_status.get("exam_start") or untis.normal_date(window_start)), untis.normal_date(window_start)),
            "homework": len(fresh["homework"]), "exams": len(fresh["exams"]),
        })
        self.snapshot = fresh
        self.last_checked = datetime.now(timezone.utc)
        self.last_error = None
        if initial or lines:
            await log_event("WebUntis check", f"{'Initial snapshot saved' if initial else 'Changes detected'}: {len(lines)} change(s).")
        return {"initial": initial, "changes": len(lines), "snapshot": fresh}

    async def poll_forever(self):
        await self.wait_until_ready()
        print("WebUntis polling: every 15 minutes normally; every 5 minutes weekdays 06:00–15:00 Europe/Vienna.")
        while not self.is_closed():
            now = datetime.now(SCHOOL_TZ)
            fast_window = now.weekday() < 5 and 6 <= now.hour < 15
            try:
                result = await self.poll_once()
                s = result["snapshot"]
                print(f"WebUntis checked: {len(s['timetable'])} lessons, {len(s['homework'])} homework, {len(s['exams'])} exams; {result['changes']} change(s).")
            except Exception as exc:
                self.last_error = str(exc)
                print(f"WebUntis check failed: {exc}", file=sys.stderr)
                await log_event("WebUntis check failed", f"Error: `{type(exc).__name__}`", error=True)
            await asyncio.sleep(FAST_POLL_SECONDS if fast_window else POLL_SECONDS)

    async def reminder_forever(self):
        await self.wait_until_ready()
        while not self.is_closed():
            now = datetime.now(SCHOOL_TZ)
            next_run = now.replace(hour=18, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            await asyncio.sleep((next_run - now).total_seconds())
            reminder_day = datetime.now(SCHOOL_TZ).date()
            try:
                try:
                    reminder_state = json.loads(REMINDER_STATE.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    reminder_state = {}
                if reminder_state.get("last_sent_for") == reminder_day.isoformat():
                    continue
                result = await self.poll_once(notify=True)
                snapshot = result["snapshot"]
                tomorrow = reminder_day + timedelta(days=1)
                tomorrow_key = untis.normal_date(tomorrow)
                homework = [item for item in snapshot["homework"]
                            if str(item.get("dueDate", "")) == tomorrow_key and not item.get("completed")]
                exams = [item for item in snapshot["exams"]
                         if str(item.get("examDate") or item.get("date") or "") == tomorrow_key]
                if homework or exams:
                    sections = [f"<@{BOT_OWNER_ID}> ⏰ **Due tomorrow · {untis.date_label(tomorrow_key)}**"]
                    if homework:
                        sections.append("**Homework**\n" + "\n\n".join(
                            f"• {homework_description(item, snapshot['entity_names'])}" for item in homework
                        ))
                    if exams:
                        sections.append("**Exams**\n" + "\n".join(
                            f"• {exam_description(item, snapshot['entity_names'])}" for item in exams
                        ))
                    reminder = "\n\n".join(sections)
                    channel = await self.get_alert_channel()
                    if channel is None:
                        raise RuntimeError("No writable text channel found for due-tomorrow reminders.")
                    for chunk in split_message(reminder):
                        await channel.send(
                            chunk,
                            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=BOT_OWNER_ID)]),
                        )
                    print(f"Posted due-tomorrow reminders and pinged Discord user {BOT_OWNER_ID}.")
                    await log_event("Reminder sent", f"{len(homework)} homework item(s), {len(exams)} exam(s) due tomorrow.")
                else:
                    print(f"No homework or exams due tomorrow ({untis.normal_date(tomorrow)}).")
                save_json(REMINDER_STATE, {"last_sent_for": reminder_day.isoformat()})
            except Exception as exc:
                self.last_error = str(exc)
                print(f"Due-tomorrow reminder failed: {exc}", file=sys.stderr)
                await log_event("Reminder failed", f"Error: `{type(exc).__name__}`", error=True)


bot = UntisDiscordBot()


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Slash command failed: {error}", file=sys.stderr)
    await log_event("Slash command failed", f"Error: `{type(error).__name__}`", error=True)
    message = f"That command failed: {str(error)[:1200]}"
    if interaction.command and interaction.command.name in {"check", "status"}:
        embed = discord.Embed(description=message, color=FAVICON_COLOR)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if interaction.response.is_done():
        await interaction.followup.send(message)
    else:
        await interaction.response.send_message(message)


async def get_snapshot(interaction):
    bot.remember_alert_channel(interaction)
    if bot.snapshot is not None:
        return bot.snapshot
    await interaction.response.defer(thinking=True)
    result = await bot.poll_once(notify=False)
    return result["snapshot"]


@bot.tree.command(name="timetable", description="Show the linked WebUntis account's timetable week")
@app_commands.choices(week=[
    app_commands.Choice(name="1 · Next week", value=1),
    app_commands.Choice(name="2 · The week after", value=2),
    app_commands.Choice(name="3 · In three weeks", value=3),
    app_commands.Choice(name="4 · In four weeks", value=4),
])
@app_commands.choices(mode=[
    app_commands.Choice(name="Light", value="light"),
    app_commands.Choice(name="Dark", value="dark"),
])
async def timetable_command(
    interaction: discord.Interaction,
    week: app_commands.Choice[int] | None = None,
    mode: app_commands.Choice[str] | None = None,
):
    snapshot = await get_snapshot(interaction)
    today = snapshot["today"]
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week.value if week else 0)
    image = await asyncio.to_thread(
        untis.timetable_png, snapshot["timetable"], monday, snapshot["entity_names"],
        today, snapshot["end"], snapshot["homework"], snapshot["exams"], None,
        mode.value if mode else "light",
    )
    file = discord.File(BytesIO(image), filename=f"webuntis-week-{mode.value if mode else 'light'}.png")
    if interaction.response.is_done():
        await interaction.followup.send(file=file)
    else:
        await interaction.response.send_message(file=file)


@bot.tree.command(name="homework", description="Show homework due from the linked WebUntis account")
@app_commands.choices(mode=[
    app_commands.Choice(name="Light", value="light"),
    app_commands.Choice(name="Dark", value="dark"),
])
async def homework_command(interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None):
    snapshot = await get_snapshot(interaction)
    image = await asyncio.to_thread(
        untis.homework_png, snapshot["homework"], snapshot["entity_names"],
        snapshot["today"], snapshot["plan_end"], mode.value if mode else "light",
    )
    file = discord.File(BytesIO(image), filename=f"webuntis-homework-{mode.value if mode else 'light'}.png")
    if interaction.response.is_done():
        await interaction.followup.send(file=file)
    else:
        await interaction.response.send_message(file=file)


@bot.tree.command(name="exams", description="List upcoming exams from the linked WebUntis account")
async def exams_command(interaction: discord.Interaction):
    snapshot = await get_snapshot(interaction)
    week_start = snapshot["today"] - timedelta(days=snapshot["today"].weekday())
    items = [item for item in snapshot["exams"]
             if untis.normal_date(week_start) <= str(item.get("examDate") or item.get("date") or "")
             <= untis.normal_date(snapshot["exam_end"])]
    items.sort(key=lambda item: str(item.get("examDate") or item.get("date") or ""))
    for index, embed in enumerate(exam_embeds(items, snapshot["entity_names"], week_start, snapshot["exam_end"])):
        if index == 0 and not interaction.response.is_done():
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.followup.send(embed=embed)


@bot.tree.command(name="status", description="Check when the bot last reached WebUntis")
async def status_command(interaction: discord.Interaction):
    bot.remember_alert_channel(interaction)
    checked = bot.last_checked.astimezone(SCHOOL_TZ).strftime("%d %b %Y %H:%M %Z") if bot.last_checked else "not yet"
    if bot.snapshot:
        counts = f"{len(bot.snapshot['timetable'])} lessons · {len(bot.snapshot['homework'])} homework · {len(bot.snapshot['exams'])} exams"
    else:
        counts = "No WebUntis snapshot yet"
    error = f"\nLatest error: {bot.last_error[:800]}" if bot.last_error else ""
    embed = discord.Embed(description=f"Last successful check: {checked}\n{counts}{error}", color=FAVICON_COLOR)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="restart", description="Restart the WebUntis bot")
async def restart_command(interaction: discord.Interaction):
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message("You can't use this command because you don't own this bot.", ephemeral=True)
        return

    await interaction.response.send_message("Restarting the bot now.", ephemeral=True)
    await bot.change_presence(activity=discord.Game(name="Restarting.."))
    await asyncio.sleep(1)
    await bot.close()
    os.execv(sys.executable, [sys.executable, *sys.argv])


@bot.tree.command(name="check", description="Check WebUntis now and post any changes in this channel")
@app_commands.choices(length=[
    app_commands.Choice(name="Normal · 3 weeks", value="normal"),
    app_commands.Choice(name="Full · 1 month back, 5 months ahead", value="full"),
])
async def check_command(interaction: discord.Interaction, length: app_commands.Choice[str] | None = None):
    bot.remember_alert_channel(interaction)
    await interaction.response.defer(thinking=True, ephemeral=True)
    selected_length = length.value if length else "normal"
    range_label = "last month through the next 5 months" if selected_length == "full" else "the next 3 weeks"
    try:
        result = await bot.poll_once(notify=True, length=selected_length)
        if result["initial"]:
            message = f"Saved the first {range_label} WebUntis snapshot. Changes will be posted in the server channel."
        elif result["changes"]:
            message = f"Found {result['changes']} change(s) within {range_label}; posted them in this channel."
        else:
            message = f"Checked the {range_label} window; nothing has changed."
    except Exception as exc:
        bot.last_error = str(exc)
        await log_event("Manual check failed", f"Error: `{type(exc).__name__}`", error=True)
        message = f"Check failed: {str(exc)[:1500]}"
    await interaction.followup.send(embed=discord.Embed(description=message, color=FAVICON_COLOR), ephemeral=True)


def main():
    config = untis.load_config() or {}
    token = str(config.get("DISCORD_BOT_TOKEN", "")).strip()
    if not token:
        print("Add DISCORD_BOT_TOKEN to the private .env file before starting the bot.", file=sys.stderr)
        return 2
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected the bot token. Replace it in .env and restart the service.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
