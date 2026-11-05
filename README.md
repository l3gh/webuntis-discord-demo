# WebUntis Discord watcher

A proof of concept that watches a WebUntis timetable for changes and can post them to Discord. It includes a webhook-based watcher, a Discord bot, and an iCal demo. The bot displays the timetable, homework and exams for one configured WebUntis account.

## Try the demos

With Python 3 installed, run:

```sh
python3 bot.py --demo
python3 legacy_bot.py --demo
```

These use simulated data and need no credentials or network connection.

## Setup

Create and activate a Python virtual environment, then install the dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python legacy_bot.py --setup
```

On Windows, activate the environment with `.venv\Scripts\activate` instead. Setup asks for the WebUntis subdomain, school name, username and password, and an optional Discord webhook. It saves these in `.env`; keep that file private.

Run `python legacy_bot.py --once` for a single check, or `python legacy_bot.py` to keep polling. The first successful check establishes a baseline; later checks report changes. Use `--help` for options, including `--interval`, `--days`, `--status`, `--send-timetable` and `--send-homework`.

To use the Discord bot, add `DISCORD_BOT_TOKEN="..."` to `.env`, invite the bot with the `bot` and `applications.commands` scopes, and run `python discord_bot.py`. Enable the Message Content intent in the Discord developer settings for the `.hw` message command. The bot provides `/timetable`, `/homework`, `/exams`, `/status`, `/check`, and `/restart`. Replies and alerts can be visible to other server members, so use a server and channel appropriate for private school data. Don't run the bot and webhook watcher simultaneously: they share a timetable snapshot.

Optionally copy `display_names.example.json` to `display_names.json` and add subject, teacher or room labels. Keep mappings private if they identify people.

## Limitations and privacy

This is a proof of concept, not an official WebUntis integration. Login and API behavior vary by school; SSO and other authentication methods may not work. Ask your school administrator whether API use is permitted. Times are currently interpreted in the `Europe/Vienna` timezone. The watcher only detects changes within its configured lookahead window.

The `.env` file contains credentials. Local state files can contain timetable, homework and Discord information. These files and personal display-name mappings are excluded by `.gitignore`; don't share them or paste tokens into chat. Review files before committing them, especially if you change the ignore rules.
