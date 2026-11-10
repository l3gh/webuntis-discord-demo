# WebUntis Discord bot

A Python bot that brings your WebUntis timetable into Discord. If a lesson gets cancelled or changes rooms, it posts the change in your server. You can also pull up your timetable, homework and exams without opening WebUntis.

`/timetable` sends the week's timetable as an image. `/homework` shows what's due, and `/exams` lists upcoming exams. At 18:00, the bot posts homework and exams due tomorrow and pings the owner if there's anything to report.

It runs on your own computer or server and uses one WebUntis account. Everyone in the channel sees that account's school data, so use a private server or a channel you're happy to share it with. Separate accounts for individual Discord members are not supported.

## Try it without an account

With Python 3 installed, run this from the project directory:

```sh
python3 legacy_bot.py --demo
```

This prints an added lesson and a room change using made-up data. It runs in your terminal without a login or network connection. To get messages in Discord, follow the setup below.

## Run your own Discord bot

You'll need a WebUntis account with a supported username/password login, a Discord bot token, and a computer or server that stays running to check for changes.

### 1. Connect WebUntis

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python legacy_bot.py --setup
```

On Windows, activate the environment with `.venv\Scripts\activate` instead.

Setup asks for your WebUntis subdomain, school name, username and password, plus an optional Discord webhook. Skip the webhook if you're using the Discord bot. Setup saves your credentials in `.env`. Keep that file private.

### 2. Connect Discord

1. Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications) and add its token to `.env`:

   ```dotenv
   DISCORD_BOT_TOKEN="your-bot-token"
   ```

2. Enable Message Content Intent in the bot's developer settings. The `.hw` message command needs it.
3. Invite the bot to your server with the `bot` and `applications.commands` scopes. Give it permission to view the intended channel, send messages, embed links and attach files.
4. Set `BOT_OWNER_ID` in [discord_bot.py](discord_bot.py) to your own Discord user ID. This controls who can use `/restart` and who gets pinged by evening reminders.

### 3. Start it

```sh
python discord_bot.py
```

Run `/check` in the channel where you want alerts. The first check saves the current timetable so the bot can spot changes on later checks.

Using `/timetable`, `/homework`, `/exams`, `/status` or `/check` in another server channel moves alerts to that channel.

Leave the process running for automatic checks and reminders. Choose either the Discord bot or the standalone webhook watcher. They share the same saved timetable.

## Commands

| Command | What you get |
| --- | --- |
| `/timetable` | This week's timetable image; choose a later week or a light/dark theme. |
| `/homework` | A homework overview image, with a light/dark theme option. |
| `/exams` | Upcoming exams from the linked account. |
| `.hw` | Homework due from today through the next four weeks. |
| `.hw help` | Options for subject and date filters. |
| `/status` | Last successful check and latest error, visible only to you. |
| `/check` | Check now and post detected changes in this channel; the command acknowledgement is visible only to you. |
| `/restart` | Restart the bot; restricted to the configured owner. |

### Filter homework

```text
.hw class:maths time:2
```

Shows maths homework due from today through the next two weeks. `class:` filters by subject; `time:` sets the window to between 1 and 52 weeks.

```text
.hw class:maths time:2 past:true future:false
```

Shows maths homework with due dates in the past two weeks through today. `past:true` includes past dates; `future:false` excludes future dates. At least one direction must be enabled. The older `range:` option is also accepted, in weeks.

## Webhook watcher

You can run `legacy_bot.py` on its own and send changes through a Discord webhook. It uses WebUntis's legacy JSON-RPC API and doesn't need a Discord bot token.

```sh
python legacy_bot.py --once   # Check once
python legacy_bot.py          # Keep checking
```

Set a Discord webhook during setup to deliver its alerts to a channel. Use `--help` for options including `--interval`, `--days`, `--status`, `--send-timetable` and `--send-homework`.

To try the iCal watcher's simulated data, run `python3 bot.py --demo`. For a real feed, set `WEBUNTIS_ICAL_URL` in the process environment to your private iCal subscription URL; optionally set `DISCORD_WEBHOOK_URL` for delivery, then run `python3 bot.py`.

## Optional configuration

Copy [display_names.example.json](display_names.example.json) to `display_names.json` to customize subject, teacher and room labels. Keep mappings private if they identify people.

Set `WEBUNTIS_LOG_WEBHOOK_URL` in `.env` to send summaries to a separate Discord webhook. Logs record startup, detected changes, reminders and failures. They also record command usage, including the user, channel and `.hw` options. They omit timetable and homework details. The process environment variable takes precedence over `.env`. A failed log delivery is printed locally and does not stop the bot.

## Limitations and privacy

This is a proof of concept, not an official WebUntis integration. Login and API behavior vary by school; SSO and other authentication methods may not work. Ask your school administrator whether API use is permitted.

The bot uses the `Europe/Vienna` timezone. It checks for changes periodically, so alerts are not instant. It only catches changes within the date range it checks.

Your `.env` contains credentials. Local state files can contain timetable, homework and Discord information. These files and personal display-name mappings are excluded by `.gitignore`; review files before committing or sharing them, especially if you change the ignore rules.
