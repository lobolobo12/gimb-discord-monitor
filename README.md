# GIMB → Discord

A daily monitor for the [Gimnazija Bežigrad intranet](https://www.gimb.org/intranet/). It checks announcements, events, linked detail pages, and student documents, then posts changes to a Discord channel in Slovenian.

The default schedule is **18:00 Europe/Ljubljana**. Substitutions, weekly room changes (`Selitve`), and lunch menus (`Jedilnik`) are excluded. This is an independent project, not an official school service.

## Features

- Normal school login, using the current login form and nonce.
- Discord incoming webhook or bot token with a channel ID.
- Relevant school links followed up to two levels deep, capped at 25 HTML pages.
- Document fingerprints detect replacements even when the URL stays the same.
- First check creates a silent baseline; unchanged sources stay silent.
- Persistent snapshots and a delivery queue survive restarts.
- A local preview, manual checks, foreground operation, and a macOS background job.

## Requirements

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- Your own authorized GIMB intranet login.
- A Discord text channel with an incoming webhook or a bot you control.
- A computer that is awake and online when checks run.

The double-click setup and automatic background installation are for macOS. The Python CLI can run in a terminal on other systems; it has not been validated on those systems.

## Setup on macOS

Install `uv` if needed (`brew install uv` when using Homebrew), then:

```sh
git clone https://github.com/lobolobo12/gimb-discord-monitor.git
cd gimb-discord-monitor
./Setup.command
```

You can also double-click `Setup.command` in Finder. Setup installs the locked dependencies and prompts locally for your school credentials, Discord connection, and daily check time. It previews the content before offering a Discord test message and background installation.

For manual setup:

```sh
uv sync --locked --no-dev
cp .env.example .env
chmod 600 .env
# Fill in .env using your editor, then preview the sources:
.venv/bin/python bot.py preview
.venv/bin/python bot.py run
```

### Connect Discord

**Webhook:** in the target text channel, open **Edit Channel → Integrations → Webhooks → New Webhook**, then copy its URL into setup. You need permission to manage webhooks. Messages appear as “GIMB obvestila.”

**Bot:** create or use an application in the [Discord Developer Portal](https://discord.com/developers/applications), invite its bot with **View Channel** and **Send Messages** permissions, and configure the bot token and channel ID. Enable Developer Mode in Discord to copy channel IDs. Administrator permission and privileged intents are not needed.

Bot-token mode uses Discord's HTTP API: it does not show an online presence or provide chat commands. Both modes post excerpts to the configured channel; choose a channel appropriate for school intranet information.

## What gets tracked

The monitor reads the intranet dashboard and content links to notices, news, events, and student documents. Changes can include deadlines, instructions, event details, and newly linked documents.

Discovery uses the school's article-content container. It excludes navigation, breadcrumbs, account/password/logout links, and unrelated **Sorodno** recommendations. Links and labels for substitutions, weekly room changes, and lunch menus are removed before comparison, so new weekly files do not trigger dashboard alerts.

HTML pages are followed up to two links deep. PDF, Word, and Excel downloads are limited to files linked on the starting pages or their direct detail pages, with a cap of 50 files and 20 MB per file. The deeper general forms/agreements library is watched for HTML text/link changes without downloading its additional files. The monitor pauses between requests.

Documents are compared by SHA-256 fingerprint. An alert links to the changed file; it does not summarize the document. A metadata-only rewrite can count as a change. External sites, sign-up forms, and text embedded in images are not crawled, and HTML pages do not execute JavaScript.

Sources are rediscovered each day. Pages that are no longer linked keep their old snapshots but are no longer fetched. Newly discovered school pages and files get a silent baseline; their newly added links can trigger alerts on parent pages.

## Configuration

Copy `.env.example` or use setup. Main settings:

| Setting | Default / purpose |
| --- | --- |
| `GIMB_EMAIL`, `GIMB_PASSWORD` | Your school login; no credentials are included |
| `DISCORD_WEBHOOK_URL` | Incoming webhook URL, or use the bot settings below |
| `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` | Alternative Discord connection |
| `CHECK_TIME` | `18:00` |
| `TIMEZONE` | `Europe/Ljubljana` |
| `WATCH_URLS` | Comma-separated starting school pages; defaults to the intranet |
| `FOLLOW_LINKS`, `LINK_DEPTH` | `true`, `2` |
| `MAX_PAGES` | `25` |
| `TRACK_FILES`, `MAX_FILES` | `true`, `50` |
| `CONTENT_SELECTOR` | Optional CSS selector for starting pages only |
| `IGNORE_SELECTORS` | CSS exclusions applied to every school page |

Keep the default exclusions to omit substitutions, room changes, and menus:

```dotenv
TRACK_SUBSTITUTIONS=false
IGNORE_SELECTORS='a[href*="/nadomescanja/"], a[href*="selitve" i], a[href*="jedilnik" i]'
```

An optional substitutions parser remains in the code but is disabled. It requires an explicit `SCHOOL_CLASS` and changes to the exclusions before use; no timetable access link or class credentials are distributed.

## Commands

Run these from the project directory:

```sh
# Preview sources without sending messages or updating snapshots
.venv/bin/python bot.py preview

# Check immediately, ignoring the daily schedule
.venv/bin/python bot.py once

# Run in a terminal; Ctrl+C stops it
.venv/bin/python bot.py run

# Show last fetch time, source counts, and queued messages
.venv/bin/python bot.py status

# Send one explicit Discord test message
.venv/bin/python bot.py test-discord

# Install/reinstall the macOS background job after setup
.venv/bin/python bot.py install
```

## Scheduling and delivery

The first successful scan saves a baseline immediately without posting old notices. Later automatic checks run at or after the configured local time, at most once per date after a successful fetch. Ljubljana daylight saving changes are handled automatically.

The macOS job wakes every five minutes to check whether work is due; it does not fetch the site every five minutes. Failed checks retry after at least 15 minutes. The computer must be awake, online, and logged in. After sleep or downtime, the next scheduled check compares against the last saved snapshot. Changes that appeared and disappeared while offline cannot be recovered.

Failed sends remain queued; confirmed sends are removed individually. A connection loss after Discord accepts a webhook may produce a duplicate on retry, so webhook delivery is not exactly-once. Failed retrievals preserve the previous snapshot. A failed starting page stops the scan; failed linked sources are logged and skipped while other sources continue. There is no Discord outage alert.

Background job: `~/Library/LaunchAgents/org.gimb.discord-monitor.plist`.

```sh
# Inspect the background job
launchctl print "gui/$(id -u)/org.gimb.discord-monitor"

# Read recent logs from the project directory
tail -n 40 logs/monitor.log

# Disable automatic checks, preserving settings and snapshots
launchctl bootout "gui/$(id -u)/org.gimb.discord-monitor"
rm ~/Library/LaunchAgents/org.gimb.discord-monitor.plist
```

The job reads `.env` on its next run after settings change. Restart a foreground `run` process to reload settings. Reinstall the job after moving the project folder. Do not run setup during an active background check.

## Run on a server with Docker

On a Linux server with Docker and the Compose plugin:

```sh
git clone https://github.com/lobolobo12/gimb-discord-monitor.git
cd gimb-discord-monitor
# Copy your filled-in .env here (for example with scp), then:
chmod 600 .env
docker compose up -d --build
docker compose logs -f
```

The container runs `bot.py run`, restarts automatically after crashes and server reboots, and keeps its snapshots in the `data` Docker volume. It is limited to 256 MB of RAM and half a CPU. The first check saves a silent baseline. Update with `git pull && docker compose up -d --build`. Run other commands inside it, for example `docker compose exec monitor /app/.venv/bin/python bot.py status`.

With rootless Docker, run `loginctl enable-linger` once so Docker keeps running after you log out and starts after a reboot.

Disable the macOS background job when the server takes over, or both will post the same notices.

## Local data

`.env` holds credentials in **plaintext**, with owner-only permissions when created by setup. The bot submits school credentials only to the school's login form. `.env`, downloaded inspection data, snapshots, logs, and local environments are ignored by Git and are not included in this repository.

State is stored in `data/state.json`. Keep it to preserve the baseline and pending messages. Do not include `.env`, `data/`, or `logs/` when sharing a bug report. The MIT license applies to this software, not to school documents or other third-party content.

## Development

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
```

Tests use synthetic fixtures and require no school or Discord credentials. Authenticated school layouts have been checked locally, but may change. Verify your own setup with `preview` and `test-discord`; live Discord delivery is not exercised by the test suite.

## License

[MIT](LICENSE).
