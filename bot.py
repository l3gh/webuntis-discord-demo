#!/usr/bin/env python3
"""Small WebUntis iCal change watcher. Uses only the Python standard library."""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

STATE = Path(__file__).with_name("state.json")


def unescape(value):
    return value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_ics(data):
    lines = []
    for line in data.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    events, current = {}, None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            uid = current.get("UID") or hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
            events[uid] = current
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key.split(";", 1)[0].upper()] = unescape(value)
    return events


def fetch_calendar(url):
    req = urllib.request.Request(url, headers={"User-Agent": "WebUntisDiscordDemo/1.0"})
    with urllib.request.urlopen(req, timeout=25) as response:
        return parse_ics(response.read().decode("utf-8-sig", errors="replace"))


def label(event):
    return event.get("SUMMARY", "Untitled event")


def changes(old, new):
    result = []
    for uid in sorted(new.keys() - old.keys()):
        result.append(("New", new[uid]))
    for uid in sorted(old.keys() - new.keys()):
        result.append(("Removed", old[uid]))
    for uid in sorted(old.keys() & new.keys()):
        if old[uid] != new[uid]:
            result.append(("Changed", new[uid]))
    return result


def send_discord(message):
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        print("[dry run; set DISCORD_WEBHOOK_URL to enable Discord delivery]")
        return
    payload = json.dumps({"content": message[:1900]}).encode()
    req = urllib.request.Request(hook, payload, {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def demo():
    before = {"math": {"UID": "math", "SUMMARY": "Math - Room 12", "DTSTART": "20261023T090000"},
              "english": {"UID": "english", "SUMMARY": "English - Room 8", "DTSTART": "20261023T100000"}}
    after = {"math": {"UID": "math", "SUMMARY": "Math - Room 14", "DTSTART": "20261023T090000"},
             "english": {"UID": "english", "SUMMARY": "English - Room 8", "DTSTART": "20261023T100000"},
             "physics": {"UID": "physics", "SUMMARY": "Physics - added", "DTSTART": "20261023T110000"}}
    found = changes(before, after)
    print("Demo: comparing two sample timetable snapshots\n")
    for kind, event in found:
        print(f"{kind}: {label(event)} at {event.get('DTSTART', 'time unknown')}")
    send_discord("WebUntis demo detected changes:\n" + "\n".join(f"{kind}: {label(event)}" for kind, event in found))


def main():
    parser = argparse.ArgumentParser(description="Poll a WebUntis iCal feed and alert on timetable changes.")
    parser.add_argument("--demo", action="store_true", help="show a simulated change without WebUntis credentials")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--interval", type=int, default=900, help="poll interval in seconds (default: 900)")
    args = parser.parse_args()
    if args.demo:
        demo()
        return
    url = os.environ.get("WEBUNTIS_ICAL_URL")
    if not url:
        parser.error("set WEBUNTIS_ICAL_URL to your private WebUntis iCal subscription URL")
    if args.interval < 60:
        parser.error("poll interval must be at least 60 seconds")

    while True:
        try:
            current = fetch_calendar(url)
            old = json.loads(STATE.read_text("utf-8")) if STATE.exists() else None
            snapshot = {uid: event for uid, event in current.items()}
            if old is None:
                STATE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), "utf-8")
                print(f"Saved initial baseline: {len(snapshot)} events. Future changes will alert.")
            else:
                found = changes(old, snapshot)
                if found:
                    lines = [f"**WebUntis timetable update ({len(found)})**"]
                    for kind, event in found[:20]:
                        when = event.get("DTSTART", "")
                        lines.append(f"{kind}: {label(event)}" + (f" — {when}" if when else ""))
                    if len(found) > 20:
                        lines.append(f"…and {len(found) - 20} more")
                    message = "\n".join(lines)
                    print(message)
                    send_discord(message)
                else:
                    print(f"No changes ({len(snapshot)} events).")
                STATE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), "utf-8")
        except Exception as exc:
            print(f"Check failed: {exc}", file=sys.stderr)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
