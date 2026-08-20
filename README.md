# FlightPingBot

Private, asynchronous Telegram bot for checking scheduled departures and flight delays through [FlightAware AeroAPI](https://www.flightaware.com/commercial/aeroapi/).

The bot works only in private Telegram chats. Access is granted by an administrator, and each approved user provides their own AeroAPI key. Keys are encrypted before they are stored in SQLite.

## Features

- administrator-approved access requests;
- one-off airport checks using three-letter IATA codes;
- recurring airport monitoring;
- alerts for flights exceeding the configured delay threshold;
- separate AeroAPI credentials for each user;
- Fernet encryption for stored keys and deletion of key messages;
- API request limits and per-user cooldowns;
- audit log, usage statistics, SQLite backups, and data retention cleanup;
- hardened systemd service configuration.

## Lightweight web panel

The bot includes a small server-rendered administrator panel. It has no
background polling, WebSockets, frontend build step, or JSON API. Pages are
refreshed only when opened, navigated, or explicitly refreshed.

By default it listens on the local network address `192.168.2.111:8080`:

```text
http://192.168.2.111:8080/
```

To change the bind address or port, set `FPB_WEB_HOST` and `FPB_WEB_PORT` in
the environment file. Keep the panel on a trusted local network or protect it
with an external access layer.

## Requirements

- Python 3.11 or newer;
- a Telegram account and bot token from BotFather;
- a FlightAware account with AeroAPI enabled;
- the Telegram user ID of at least one administrator;
- Linux with systemd for production deployment.

## Development installation

```bash
cd /opt/flightping
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
mkdir -p config state
cp .env.example config/flightpingbot.env
```

Edit `config/flightpingbot.env` before starting the bot. Never commit this file or any other secret to the repository.

Run locally:

```bash
.venv/bin/python -m flightpingbot
```

Run the test suite:

```bash
.venv/bin/pytest -q
```

## Configuration

The example configuration is available in [.env.example](/opt/flightping/.env.example).

| Variable | Description |
|---|---|
| `FPB_BOT_TOKEN` | Telegram bot token from BotFather. |
| `FPB_CREDENTIALS_KEY` | Stable Fernet key used to encrypt AeroAPI keys. |
| `FPB_ADMIN_USER_IDS` | Comma-separated Telegram administrator user IDs. |
| `FPB_STATE_DIR` | Directory for the database, backups, and state; defaults to `/opt/flightping/state`. |
| `FPB_MONITOR_INTERVAL_MINUTES` | Interval between monitoring checks. |
| `FPB_MONITOR_WINDOW_HOURS` | Departure window requested from AeroAPI. |
| `FPB_MONITOR_DURATION_HOURS` | Maximum monitoring duration. |
| `FPB_MIN_DELAY_MINUTES` | Minimum delay considered an alert. |
| `FPB_TIMEZONE` | Time zone used in user-facing messages. |
| `FPB_MAX_ACTIVE_AIRPORTS` | Maximum active monitored airports per user. |
| `FPB_DAILY_API_REQUEST_LIMIT` | Daily request limit; `0` disables the limit. |
| `FPB_MONTHLY_API_REQUEST_LIMIT` | Monthly request limit; `0` disables the limit. |
| `FPB_USAGE_WARNING_PERCENT` | Usage percentage at which a warning is shown. |
| `FPB_USER_REQUEST_COOLDOWN_SECONDS` | Minimum interval between manual user checks. |
| `FPB_TELEGRAM_MESSAGES_PER_MINUTE` | Maximum inbound messages per user per minute; defaults to `30`. Administrators are exempt. |
| `FPB_FSM_STATE_TTL_SECONDS` | Inactivity timeout for interactive input states; defaults to `900` seconds. |
| `FPB_*_RETENTION_DAYS` | Retention periods for observations, audit events, and API request logs. Flight observations (the “Delayed flights found” view) are retained for 7 days by default. |

### Generate the Fernet key

Generate this key once and keep it unchanged across restarts. Changing it makes existing encrypted AeroAPI keys unreadable.

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the generated value as `FPB_CREDENTIALS_KEY` in the configuration file.

## User commands

All commands work in a private chat with the bot.

1. Send `/start` and wait for approval.
2. After approval, send `/aeroapi` and provide your personal AeroAPI key.
3. Verify the setup with `/aeroapi status` or `/aeroapi test`.

| Command | Description |
|---|---|
| `/start` | Request access or show the current access status. |
| `/check TFS` | Run a one-off scheduled departure check. |
| `/monitor TFS` | Start monitoring an airport. |
| `/status` | Show active monitoring subscriptions. |
| `/stop` | Stop your monitoring subscriptions. |
| `/aeroapi` | Set or replace your AeroAPI key. |
| `/aeroapi status` | Show whether an AeroAPI key is configured. |
| `/aeroapi test` | Test the key through the AeroAPI account endpoint. |
| `/aeroapi remove` | Remove the key and stop monitoring. |
| `/cancel` | Cancel the current input flow. |
| `/help` | Show help and current settings. |
| `/hide` | Hide the Telegram keyboard. |

Airport values must be three-letter ASCII IATA codes, for example `WAW`, `TFS`, or `LHR`.

## Administrator commands

Administrators are configured through `FPB_ADMIN_USER_IDS`.

| Command | Description |
|---|---|
| `/requests` | List pending access requests. |
| `/approve <user_id>` | Approve an access request. |
| `/deny <user_id>` | Deny an access request. |
| `/users` | List users and their statuses. |
| `/revoke <user_id>` | Revoke a user's access. |
| `/block <user_id>` | Block a user. |
| `/unblock <user_id>` | Restore a user's access. |
| `/checks [IATA]` | Show recent checks. |
| `/checklog <check_id>` | Show observations for a check. |
| `/usage [user_id]` | Show AeroAPI usage statistics. |
| `/alerts [IATA]` | Show alert history. |
| `/audit [days]` | Show recent audit events. |
| `/admin_status` | Show users, monitoring, and database status. |
| `/db_status` | Show database, WAL, and backup information. |
| `/stopall` | Emergency stop for all monitors. |

After `revoke` or `block`, the user's active monitors are stopped and any in-progress input flow can no longer be completed.

## systemd deployment

The [flightpingbot.service](/opt/flightping/flightpingbot.service) unit expects the application at `/opt/flightping` and configuration at `config/flightpingbot.env`.

Example installation as a user service:

```bash
mkdir -p ~/.config/systemd/user
cp flightpingbot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now flightpingbot.service
systemctl --user status flightpingbot.service
```

View logs:

```bash
journalctl --user -u flightpingbot.service -f
```

To keep the service running after logging out of the system account, enable linger:

```bash
loginctl enable-linger "$USER"
```

The optional [flightpingbot-healthcheck.service](/opt/flightping/flightpingbot-healthcheck.service) unit lets systemd check whether the main service is active.

## Data and backups

By default, application data is stored in:

```text
state/flightpingbot.sqlite3
state/backups/
```

SQLite runs in WAL mode. On startup, the database and its `-wal` and `-shm` files are secured with `0600` permissions, while the state directory uses `0700`. The maintenance task creates up to three backups and removes old records according to the retention settings.

A local backup is not a substitute for an off-host copy. In production, periodically copy `state/backups/` to a secure separate location.

## Security notes

- `config/flightpingbot.env` contains secrets and must remain outside Git;
- keep `FPB_CREDENTIALS_KEY` separately from the repository and database backups;
- do not change `FPB_CREDENTIALS_KEY` without a migration plan for encrypted data;
- the bot should not be used in groups or channels;
- users are identified by numeric Telegram user IDs, not `@username` values;
- the message containing an API key is deleted after receipt, but Telegram permissions or a temporary API error may prevent deletion; the bot warns the user in that case;
- after code changes, run the tests and inspect the service logs.

## Pre-deployment verification

```bash
.venv/bin/pytest -q
systemd-analyze verify flightpingbot.service flightpingbot-healthcheck.service
```

The `.github/workflows/security-audit.yml` workflow runs `pip-audit` and the automated test suite in CI.
