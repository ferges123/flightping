from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from aiogram.enums import ParseMode
from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route

from .formatting import flightaware_url, format_check, local_time as _time
from .statuses import MonitorJobStatus, UserStatus


CSS = """
:root { color-scheme: light; font-family: system-ui, sans-serif; scrollbar-gutter: stable; }
body { margin: 0; overflow-y: scroll; background: #f4f6f8; color: #17212b; }
header { background: #17212b; color: white; padding: .65rem max(1rem, calc((100% - 1100px)/2)); }
.brand { display: inline-flex; align-items: center; gap: .55rem; margin-right: 1.25rem; }
.brand img { width: 42px; height: 42px; object-fit: cover; border-radius: 50%; background: white; vertical-align: middle; }
header a { color: white; text-decoration: none; margin-right: 1rem; }
main { max-width: 1100px; margin: 1.25rem auto; padding: 0 1rem; }
.muted { color: #687582; font-size: .9rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: .75rem; }
.card, table { background: white; border: 1px solid #dce2e7; border-radius: 8px; }
.card { padding: 1rem; } .metric { font-size: 1.7rem; font-weight: 700; }
table { width: 100%; border-collapse: collapse; margin: .75rem 0 1.5rem; overflow: hidden; }
th, td { text-align: left; padding: .65rem .75rem; border-bottom: 1px solid #edf0f2; vertical-align: top; }
th { background: #f8fafb; font-size: .85rem; } tr:last-child td { border-bottom: 0; }
.usage-details { display: none; }
button { border: 0; border-radius: 5px; padding: .4rem .65rem; cursor: pointer; background: #1769aa; color: white; }
button.danger { background: #b42318; } button.secondary { background: #687582; }
form { display: inline; } h1 { margin-top: 0; } h2 { margin-top: 1.6rem; }
.monitor-form { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(150px, .55fr) auto; gap: .85rem; align-items: end; margin: .75rem 0 1.5rem; padding: 1rem; background: white; border: 1px solid #dce2e7; border-radius: 8px; box-shadow: 0 1px 2px rgb(23 33 43 / 4%); }
.monitor-form__heading { grid-column: 1 / -1; margin: 0; font-size: 1rem; }
.monitor-form label { display: grid; gap: .35rem; color: #465360; font-size: .85rem; font-weight: 600; }
.monitor-form input, .monitor-form select { box-sizing: border-box; width: 100%; height: 2.55rem; padding: .45rem .65rem; color: #17212b; background: #fff; border: 1px solid #bcc6d0; border-radius: 5px; font: inherit; }
.monitor-form input:focus, .monitor-form select:focus { outline: 2px solid rgb(23 105 170 / 25%); border-color: #1769aa; }
.monitor-form button { height: 2.55rem; padding: .45rem 1rem; white-space: nowrap; }
.notice { margin: .75rem 0; padding: .65rem .8rem; color: #155724; background: #edf8ef; border: 1px solid #c8e6cc; border-radius: 6px; }
.pagination { display: flex; gap: .5rem; justify-content: flex-end; margin: -.75rem 0 1.5rem; }
.pagination a { padding: .4rem .65rem; border-radius: 5px; background: #1769aa; color: white; text-decoration: none; }
@media (max-width: 650px) { header nav { display: flex; justify-content: space-between; align-items: center; } header a { margin-right: 0; } table { display: block; overflow-x: auto; white-space: nowrap; } .settings-page table { display: table; overflow: visible; white-space: normal; table-layout: fixed; } .settings-page th, .settings-page td { overflow-wrap: anywhere; } .settings-page .usage-table { display: block; border: 0; background: transparent; margin-bottom: 1.5rem; } .settings-page .usage-table thead { display: none; } .settings-page .usage-table tbody { display: grid; gap: .65rem; } .settings-page .usage-table tr { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border: 1px solid #dce2e7; border-radius: 8px; overflow: hidden; background: white; } .settings-page .usage-table td { padding: .55rem .7rem; border-bottom: 1px solid #edf0f2; } .settings-page .usage-table td:first-child { grid-column: 1 / -1; font-weight: 700; background: #f8fafb; } .settings-page .usage-table td:nth-last-child(-n + 2) { border-bottom: 0; } .settings-page .usage-table td[data-label]::before { content: attr(data-label) ": "; color: #687582; font-size: .8rem; } .settings-page .usage-by-user-table { display: table; border: 1px solid #dce2e7; border-radius: 8px; background: white; overflow: hidden; } .settings-page .usage-by-user-table thead { display: table-header-group; } .settings-page .usage-by-user-table tbody { display: table-row-group; } .settings-page .usage-by-user-table tr { display: table-row; border: 0; background: transparent; } .settings-page .usage-by-user-table td { border-bottom: 1px solid #edf0f2; } .settings-page .usage-by-user-table td:first-child { background: transparent; } .settings-page .usage-by-user-table td:nth-child(2)::before { content: none; } .settings-page .usage-by-user-table th:nth-child(3), .settings-page .usage-by-user-table th:nth-child(4), .settings-page .usage-by-user-table th:nth-child(5), .settings-page .usage-by-user-table td:nth-child(3), .settings-page .usage-by-user-table td:nth-child(4), .settings-page .usage-by-user-table td:nth-child(5) { display: none; } .settings-page .usage-by-user-table .usage-details { display: table-cell; } .monitor-form { grid-template-columns: 1fr; } .monitor-form button { width: 100%; } }
"""

MAX_PAGE = 100_000


def _e(value) -> str:
    return escape("" if value is None else str(value))


def _add_duration(value, duration_seconds: int):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed + timedelta(seconds=duration_seconds)).isoformat()
    except (ValueError, TypeError):
        return None


def _metric_card(label: str, value) -> str:
    return f"<div class='card'><div class='muted'>{label}</div><div class='metric'>{_e(value)}</div></div>"


def _table(headers: list[str], rows: list[str], empty_message: str) -> str:
    if not rows:
        return f"<p>{_e(empty_message)}</p>"
    head = "".join(f"<th>{header}</th>" for header in headers)
    return f"<table><tr>{head}</tr>{''.join(rows)}</table>"


def _check_row(row, timezone_name: str) -> str:
    return (
        f"<tr><td>#{row['id']}</td><td>{_e(row['airport'])}</td><td>{_e(row['status'])}</td>"
        f"<td>{row['flight_count']}</td><td>{row['delayed_count']}</td>"
        f"<td>{_time(row['started_at'], timezone_name)}</td></tr>"
    )


def _checks_table(checks, timezone_name: str, empty_message: str = "No checks found.") -> str:
    return _table(
        ["ID", "Airport", "Status", "Flights", "Delayed", "Time"],
        [_check_row(row, timezone_name) for row in checks],
        empty_message,
    )


def _alerts_table(alerts, timezone_name: str) -> str:
    rows = [
        (
            f"<tr><td>{_e(row['airport'])}</td><td>{_e(row['flight_id'])}</td>"
            f"<td>{row['delay_minutes']} min</td><td>{_e(row['status'])}</td>"
            f"<td>{_time(row['created_at'], timezone_name)}</td></tr>"
        )
        for row in alerts
    ]
    return _table(["Airport", "Flight", "Delay", "Status", "Time"], rows, "No alerts found.")


def _job_row(row, default_duration_seconds: int | None, timezone_name: str) -> str:
    is_active = row["status"] == MonitorJobStatus.ACTIVE
    action = (
        f"<form method='post' action='/actions/monitor/{row['actor_user_id']}/{_e(row['airport'])}/stop'><button class='danger'>Stop</button></form>"
        if is_active
        else f"<form method='post' action='/actions/monitor/{row['id']}/remonitor'><button>Remonitor</button></form>"
    )
    duration_seconds = row["duration_hours"] * 3600 if row["duration_hours"] else default_duration_seconds
    planned_expiry = _add_duration(row["started_at"], duration_seconds) if is_active and duration_seconds else None
    status = "Active" if is_active else "Finished"
    ended_at = "—" if is_active else _time(row["stopped_at"], timezone_name)
    return (
        f"<tr><td><b>{_e(row['airport'])}</b></td><td>{row['actor_user_id']}</td><td>{status}</td>"
        f"<td>{row['interval_minutes']} min</td><td>{_time(row['started_at'], timezone_name)}</td>"
        f"<td>{ended_at}</td><td>{_time(planned_expiry, timezone_name)}</td><td>{action}</td></tr>"
    )


def _jobs_table(rows, duration_seconds: int | None, timezone_name: str) -> str:
    return _table(
        ["Airport", "User", "Status", "Interval", "Started", "Ended", "Planned expiry", "Action"],
        [_job_row(row, duration_seconds, timezone_name) for row in rows],
        "No monitoring jobs found.",
    )


def _user_action(row, pending_request) -> str:
    if row["status"] == UserStatus.PENDING:
        if not pending_request:
            return ""
        request_id = pending_request[0]
        return (
            f"<form method='post' action='/actions/access/{request_id}/approve'><button>Approve</button></form> "
            f"<form method='post' action='/actions/access/{request_id}/deny'><button class='danger'>Deny</button></form>"
        )
    if not row["is_admin"] and row["status"] in {UserStatus.APPROVED, UserStatus.BLOCKED}:
        target = UserStatus.BLOCKED if row["status"] == UserStatus.APPROVED else UserStatus.APPROVED
        label = "Block" if target == UserStatus.BLOCKED else "Unblock"
        css_class = "danger" if target == UserStatus.BLOCKED else "secondary"
        return f"<form method='post' action='/actions/user/{row['telegram_user_id']}/{target}'><button class='{css_class}'>{label}</button></form>"
    return ""


def _users_table(rows, pending_requests: dict[int, int], timezone_name: str) -> str:
    rendered = []
    for row in rows:
        request_id = pending_requests.get(row["telegram_user_id"]) if row["status"] == UserStatus.PENDING else None
        action = _user_action(row, [request_id] if request_id else None)
        rendered.append(
            f"<tr><td>{_e(row['display_name'])}<br><span class='muted'>{row['telegram_user_id']}</span></td>"
            f"<td>{_e(row['status'])}</td><td>{_time(row['created_at'], timezone_name)}</td>"
            f"<td>{action or '—'}</td></tr>"
        )
    return _table(["User", "Status", "Created", "Action"], rendered, "No users found.")


def _delayed_table(rows, default_timezone_name: str) -> str:
    table_rows = []
    for row in rows:
        link = escape(flightaware_url(row["flightaware_id"] or row["flight_id"]), quote=True)
        route = f"{_e(row['origin'] or '?')} → {_e(row['destination'] or '?')}"
        airport_timezone = row["origin_timezone"] or default_timezone_name
        table_rows.append(
            f"<tr><td><a href='{link}' target='_blank' rel='noreferrer'><b>{_e(row['flight_id'])}</b></a></td>"
            f"<td>{_e(row['airport'])}</td><td>{route}</td>"
            f"<td>{_time(row['scheduled_departure'], airport_timezone)}</td>"
            f"<td>{_time(row['estimated_departure'], airport_timezone)}</td>"
            f"<td><b>+{row['delay_minutes'] or 0} min</b></td>"
            f"<td>{_time(row['observed_at'], default_timezone_name)}</td></tr>"
        )
    return _table(
        ["Flight", "Airport", "Route", "Scheduled (local / UTC)", "Estimated (local / UTC)", "Delay", "Found"],
        table_rows,
        "No delayed flights have been found yet.",
    )


def _pagination(base_path: str, page: int, has_next: bool) -> str:
    if page <= 1 and not has_next:
        return ""
    links = ""
    if page > 1:
        links += f"<a href='{base_path}?page={page - 1}'>&lt;&lt;</a>"
    if has_next:
        links += f"<a href='{base_path}?page={page + 1}'>&gt;&gt;</a>"
    return f"<nav class='pagination'>{links}</nav>"


class WebPanel:
    """Small server-rendered admin panel sharing the bot's repository."""

    def __init__(self, repo, monitor, bot, admin_ids: frozenset[int], timezone_name: str = "Atlantic/Canary", app_settings=None):
        self.repo = repo
        self.monitor = monitor
        self.bot = bot
        self.admin_ids = admin_ids
        self.timezone_name = timezone_name
        self.app_settings = app_settings

    def app(self) -> Starlette:
        routes = [
            Route("/", self.dashboard),
            Route("/logo.jpeg", self.logo),
            Route("/monitoring", self.monitoring),
            Route("/users", self.users),
            Route("/history", self.history),
            Route("/successes", self.successes),
            Route("/settings", self.settings),
            Route("/actions/monitor/start", self.start_monitor, methods=["POST"]),
            Route("/actions/monitor/{user_id:int}/{airport}/stop", self.stop_monitor, methods=["POST"]),
            Route("/actions/monitor/{job_id:int}/remonitor", self.remonitor, methods=["POST"]),
            Route("/actions/access/{request_id:int}/{decision}", self.access_decision, methods=["POST"]),
            Route("/actions/user/{user_id:int}/{status}", self.user_status, methods=["POST"]),
        ]
        return Starlette(routes=routes)

    async def logo(self, request):
        logo_path = Path(__file__).resolve().parents[2] / "logo.jpeg"
        return FileResponse(logo_path, media_type="image/jpeg")

    def page(self, title: str, body: str, refresh_path: str = "/") -> HTMLResponse:
        now = datetime.now(timezone.utc).astimezone(ZoneInfo(self.timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
        html = f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{_e(title)} · FlightPing</title><style>{CSS}</style></head><body><header><span class='brand'><img src='/logo.jpeg?v=2' alt='FlightPing logo'><b>FlightPing</b></span><nav><a href='/'>Start</a><a href='/monitoring'>Monitor</a><a href='/users'>Users</a><a href='/history'>History</a><a href='/successes'>Delays</a><a href='/settings' aria-label='Settings' title='Settings'>⚙</a></nav></header><main><div class='muted'>Data as of: {now} · <a href='{_e(refresh_path)}' style='color:#1769aa'>Refresh</a></div>{body}</main></body></html>"
        return HTMLResponse(html)

    async def dashboard(self, request):
        # Four aggregates should tell one coherent story; group the reads so a
        # concurrent write transaction cannot be half-visible between them.
        now = datetime.now(timezone.utc)
        async with self.repo.db.consistent_reads():
            counts = await self.repo.user_counts()
            jobs = await self.repo.active_monitor_jobs()
            checks = await self.repo.recent_checks(limit=8)
            daily_usage = await self.repo.usage(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat())
            monthly_usage = await self.repo.usage(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat())
        cards = "".join(
            _metric_card(label, value)
            for label, value in (
                ("Pending users", counts.get(UserStatus.PENDING, 0)),
                ("Approved users", counts.get(UserStatus.APPROVED, 0)),
                ("Active monitors", len(jobs)),
                ("API requests today", daily_usage["total"] or 0),
                ("API requests this month", monthly_usage["total"] or 0),
            )
        )
        body = f"<h1>Admin dashboard</h1><div class='grid'>{cards}</div><h2>Recent checks</h2>{_checks_table(checks, self.timezone_name)}"
        return self.page("Start", body, request.url.path)

    async def monitoring(self, request):
        page_size = 10
        try:
            page = min(MAX_PAGE, max(1, int(request.query_params.get("page", "1"))))
        except ValueError:
            page = 1
        results = await self.repo.monitor_jobs(limit=page_size + 1, offset=(page - 1) * page_size)
        rows, has_next = results[:page_size], len(results) > page_size
        users = await self.repo.list_users(status=UserStatus.APPROVED)
        notice = request.query_params.get("notice")
        body = "<h1>Active monitoring</h1>"
        if notice:
            body += f"<p class='notice'>{_e(notice)}</p>"
        options = ""
        for user in users:
            label = user["username"] and f"@{user['username']}" or user["display_name"]
            options += f"<option value='{user['telegram_user_id']}'>{_e(label)} ({user['telegram_user_id']})</option>"
        body += (
            "<form class='monitor-form' method='post' action='/actions/monitor/start'>"
            "<h2 class='monitor-form__heading'>Add monitoring</h2>"
            f"<label>User<select name='user_id' required>{options}</select></label>"
            "<label>Airport (IATA)<input name='airport' type='text' minlength='3' maxlength='3' pattern='[A-Za-z]{3}' placeholder='WAW' required></label>"
            "<button type='submit'>Add monitoring</button></form>"
        )
        duration_seconds = self.monitor.duration if self.monitor else None
        body += _jobs_table(rows, duration_seconds, self.timezone_name)
        if page > 1 or has_next:
            body += _pagination("/monitoring", page, has_next)
        return self.page("Monitoring", body, str(request.url.path) + (f"?page={page}" if page > 1 else ""))

    async def users(self, request):
        rows = await self.repo.list_users()
        pending_requests = await self.repo.pending_request_ids()
        body = f"<h1>Users</h1>{_users_table(rows, pending_requests, self.timezone_name)}"
        return self.page("Users", body, request.url.path)

    async def history(self, request):
        page_size = 10
        try:
            page = min(MAX_PAGE, max(1, int(request.query_params.get("page", "1"))))
        except ValueError:
            page = 1
        rows = await self.repo.recent_checks(limit=page_size + 1, offset=(page - 1) * page_size)
        checks, has_next = rows[:page_size], len(rows) > page_size
        alerts = await self.repo.list_alerts(limit=30)
        body = f"<h1>History</h1><h2>Checks</h2>{_checks_table(checks, self.timezone_name)}"
        if page > 1 or has_next:
            body += _pagination("/history", page, has_next)
        body += f"<h2>Alerts</h2>{_alerts_table(alerts, self.timezone_name)}"
        return self.page("History", body, str(request.url.path) + (f"?page={page}" if page > 1 else ""))

    async def successes(self, request):
        rows = await self.repo.delayed_observations(limit=100)
        body = (
            "<h1>Delayed flights found</h1>"
            "<p class='muted'>Latest observation for each delayed flight. Scheduled and estimated times use the origin airport's local time, with UTC in parentheses.</p>"
            f"{_delayed_table(rows, self.timezone_name)}"
        )
        return self.page("Delayed flights", body, request.url.path)

    async def settings(self, request):
        settings = self.app_settings
        if settings is None:
            body = "<h1>Settings</h1><p class='muted'>Configuration details are unavailable in this panel.</p>"
            return self.page("Settings", body, request.url.path)

        now = datetime.now(timezone.utc)
        monthly_since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with self.repo.db.consistent_reads():
            daily_usage = await self.repo.usage(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat())
            monthly_usage = await self.repo.usage(monthly_since)
            monthly_usage_by_user = await self.repo.usage_by_user(monthly_since)

        def value(name: str, suffix: str = "") -> str:
            return f"{_e(getattr(settings, name))}{suffix}"

        def limit(name: str) -> str:
            configured = getattr(settings, name)
            return "Unlimited" if configured == 0 else str(configured)

        def settings_table(rows: list[tuple[str, str]]) -> str:
            return _table(["Setting", "Current value"], [f"<tr><td>{_e(label)}</td><td>{current}</td></tr>" for label, current in rows], "")

        def usage_table(first_column: str, rows: list[tuple[str, object]], empty_message: str = "", css_class: str = "usage-table", mobile_details: bool = False) -> str:
            if not rows:
                return f"<p>{_e(empty_message)}</p>"
            details_header = "<th class='usage-details'>Details</th>" if mobile_details else ""
            def usage_row(label: str, row) -> str:
                details = (
                    f"<td class='usage-details'>✓ {row['success'] or 0} · errors {row['errors'] or 0} · retries {row['retries'] or 0}</td>"
                    if mobile_details else ""
                )
                return (
                    f"<tr><td>{_e(label)}</td><td data-label='Requests'>{row['total'] or 0}</td>"
                    f"<td data-label='Successes'>{row['success'] or 0}</td><td data-label='Errors'>{row['errors'] or 0}</td>"
                    f"<td data-label='Retries'>{row['retries'] or 0}</td>{details}</tr>"
                )
            table_rows = "".join(usage_row(label, row) for label, row in rows)
            return (
                f"<table class='{_e(css_class)}'><thead><tr>"
                f"<th>{_e(first_column)}</th><th>Requests</th><th>Successes</th><th>Errors</th><th>Retries</th>"
                f"{details_header}</tr></thead><tbody>{table_rows}</tbody></table>"
            )

        api = settings_table([
            ("Daily API request limit", limit("daily_api_request_limit")),
            ("Monthly API request limit", limit("monthly_api_request_limit")),
            ("Usage warning threshold", value("usage_warning_percent", "%")),
            ("Request cooldown", value("user_request_cooldown_seconds", " s")),
        ])
        usage = usage_table("Period", [("Today", daily_usage), ("This month", monthly_usage)])
        usage_by_user = usage_table(
            "User",
            [(row["user_name"], row) for row in monthly_usage_by_user],
            "No API requests have been recorded this month.",
            "usage-table usage-by-user-table",
            True,
        )
        monitoring = settings_table([
            ("Default check window", value("monitor_window_hours", " h")),
            ("Default monitor interval", value("monitor_interval_minutes", " min")),
            ("Default monitoring duration", value("monitor_duration_hours", " h")),
            ("Default delay threshold", value("min_delay_minutes", " min")),
            ("Maximum active airports per user", value("max_active_airports")),
        ])
        retention = settings_table([
            ("Flight observations", value("observation_retention_days", " days")),
            ("API request records", value("api_request_retention_days", " days")),
            ("Checks", value("check_retention_days", " days")),
            ("Alerts", value("alert_retention_days", " days")),
            ("Monitoring history", value("monitor_job_retention_days", " days")),
            ("Audit log", value("audit_retention_days", " days")),
        ])
        body = (
            "<section class='settings-page'><h1>Settings</h1>"
            "<p class='muted'>Read-only application configuration. Update environment variables and restart FlightPing to apply changes. Tokens and API keys are intentionally never shown here.</p>"
            f"<h2>Current API usage</h2>{usage}"
            f"<h2>API usage by user — this month</h2>{usage_by_user}"
            f"<h2>API controls</h2>{api}"
            f"<h2>Monitoring defaults</h2>{monitoring}"
            f"<h2>Data retention</h2>{retention}"
            f"<h2>Regional settings</h2>{settings_table([('Timezone', _e(settings.timezone_name))])}</section>"
        )
        return self.page("Settings", body, request.url.path)

    async def stop_monitor(self, request):
        user_id = int(request.path_params["user_id"])
        airport = request.path_params["airport"]
        if await self.monitor.stop_user_airport(user_id, airport):
            await self.repo.audit(None, "monitor_stop", "monitor", airport, {"user_id": user_id, "source": "web"})
        return RedirectResponse("/monitoring", status_code=303)

    async def start_monitor(self, request):
        form = parse_qs((await request.body()).decode(), keep_blank_values=True)
        try:
            user_id = int(form.get("user_id", [""])[0])
            airport = form.get("airport", [""])[0]
            user = await self.repo.user(user_id)
            if not user or user["status"] != UserStatus.APPROVED:
                raise ValueError("Choose an approved user.")

            async def notify(result):
                saved = await self.repo.user_settings(user_id)
                language = saved["language"] if saved else "en"
                await self.bot.send_message(user["chat_id"], format_check(result, self.timezone_name, language), parse_mode=ParseMode.HTML)

            status = await self.monitor.start(user_id, user["chat_id"], airport, notify)
            notice = {
                "started": f"Monitoring for {airport.strip().upper()} was added.",
                "subscribed": f"The user was subscribed to {airport.strip().upper()}.",
                "already_subscribed": f"Monitoring for {airport.strip().upper()} already exists.",
            }[status]
        except (TypeError, ValueError, RuntimeError) as exc:
            notice = str(exc) or "Could not add monitoring."
        return RedirectResponse(f"/monitoring?{urlencode({'notice': notice})}", status_code=303)

    async def remonitor(self, request):
        job_id = int(request.path_params["job_id"])
        try:
            previous = await self.repo.monitor_job(job_id)
            if not previous or previous["status"] != MonitorJobStatus.STOPPED:
                raise ValueError("This monitoring job is no longer available to restart.")
            user = await self.repo.user(previous["actor_user_id"])
            if not user or user["status"] != UserStatus.APPROVED:
                raise ValueError("The monitoring owner is no longer an approved user.")

            async def notify(result):
                saved = await self.repo.user_settings(user["telegram_user_id"])
                language = saved["language"] if saved else "en"
                await self.bot.send_message(user["chat_id"], format_check(result, self.timezone_name, language), parse_mode=ParseMode.HTML)

            airport = previous["airport"]
            status = await self.monitor.start(user["telegram_user_id"], user["chat_id"], airport, notify)
            if status == "started":
                await self.repo.audit(None, "monitor_remonitor", "monitor", str(job_id), {"user_id": user["telegram_user_id"], "airport": airport, "source": "web"})
            notice = {
                "started": f"Monitoring for {airport} was restarted.",
                "subscribed": f"The user was subscribed to {airport}.",
                "already_subscribed": f"Monitoring for {airport} is already active.",
            }[status]
        except (TypeError, ValueError, RuntimeError) as exc:
            notice = str(exc) or "Could not restart monitoring."
        return RedirectResponse(f"/monitoring?{urlencode({'notice': notice})}", status_code=303)

    async def access_decision(self, request):
        request_id = int(request.path_params["request_id"])
        decision = request.path_params["decision"]
        if decision not in {"approve", "deny"}:
            return RedirectResponse("/users", status_code=303)
        changed, user_id = await self.repo.decide_request(request_id, None, decision == "approve")
        if changed and user_id:
            await self.repo.audit(None, "access_decision", "access_request", str(request_id), {"decision": decision, "user_id": user_id, "source": "web"})
            try:
                await self.bot.send_message(user_id, "✅ Access granted." if decision == "approve" else "❌ Access request denied.")
            except Exception:
                pass
        return RedirectResponse("/users", status_code=303)

    async def user_status(self, request):
        user_id = int(request.path_params["user_id"])
        status = request.path_params["status"]
        if status in {UserStatus.APPROVED, UserStatus.REVOKED, UserStatus.BLOCKED} and await self.repo.set_user_status(user_id, status):
            if status in {UserStatus.REVOKED, UserStatus.BLOCKED}:
                await self.monitor.stop_user_all(user_id)
            await self.repo.audit(None, f"user_{status}", "user", str(user_id), {"source": "web"})
        return RedirectResponse("/users", status_code=303)


async def serve(panel: WebPanel, host: str = "127.0.0.1", port: int = 8080):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(panel.app(), host=host, port=port, log_level="warning"))
    await server.serve()
