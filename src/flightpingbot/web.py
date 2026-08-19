from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route

from .formatting import flightaware_url


CSS = """
:root { color-scheme: light; font-family: system-ui, sans-serif; }
body { margin: 0; background: #f4f6f8; color: #17212b; }
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
button { border: 0; border-radius: 5px; padding: .4rem .65rem; cursor: pointer; background: #1769aa; color: white; }
button.danger { background: #b42318; } button.secondary { background: #687582; }
form { display: inline; } h1 { margin-top: 0; } h2 { margin-top: 1.6rem; }
@media (max-width: 650px) { table { display: block; overflow-x: auto; white-space: nowrap; } }
"""


def _e(value) -> str:
    return escape("" if value is None else str(value))


def _time(value, timezone_name: str = "Atlantic/Canary") -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ZoneInfo(timezone_name))
        return _e(f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({parsed:%H:%M} UTC)")
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return _e(str(value).replace("T", " ")[:19])


def _add_duration(value, duration_seconds: int):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed + timedelta(seconds=duration_seconds)).isoformat()
    except (ValueError, TypeError):
        return None


class WebPanel:
    """Small server-rendered admin panel sharing the bot's repository."""

    def __init__(self, repo, monitor, bot, admin_ids: frozenset[int], timezone_name: str = "Atlantic/Canary"):
        self.repo = repo
        self.monitor = monitor
        self.bot = bot
        self.admin_ids = admin_ids
        self.timezone_name = timezone_name

    def app(self) -> Starlette:
        routes = [
            Route("/", self.dashboard),
            Route("/logo.jpeg", self.logo),
            Route("/monitoring", self.monitoring),
            Route("/users", self.users),
            Route("/history", self.history),
            Route("/successes", self.successes),
            Route("/actions/monitor/{user_id:int}/{airport}/stop", self.stop_monitor, methods=["POST"]),
            Route("/actions/access/{request_id:int}/{decision}", self.access_decision, methods=["POST"]),
            Route("/actions/user/{user_id:int}/{status}", self.user_status, methods=["POST"]),
        ]
        return Starlette(routes=routes)

    async def logo(self, request):
        logo_path = Path(__file__).resolve().parents[2] / "logo.jpeg"
        return FileResponse(logo_path, media_type="image/jpeg")

    def page(self, title: str, body: str, refresh_path: str = "/") -> HTMLResponse:
        now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        html = f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{_e(title)} · FlightPing</title><style>{CSS}</style></head><body><header><span class='brand'><img src='/logo.jpeg?v=2' alt='FlightPing logo'><b>FlightPing</b></span><nav><a href='/'>Dashboard</a><a href='/monitoring'>Monitoring</a><a href='/users'>Users</a><a href='/history'>History</a><a href='/successes'>Delayed flights</a></nav></header><main><div class='muted'>Data as of: {now} · <a href='{_e(refresh_path)}' style='color:#1769aa'>Refresh</a></div>{body}</main></body></html>"
        return HTMLResponse(html)

    async def dashboard(self, request):
        counts = await self.repo.user_counts()
        jobs = await self.repo.active_monitor_jobs()
        checks = await self.repo.recent_checks(limit=8)
        usage = await self.repo.usage(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat())
        body = "<h1>Admin dashboard</h1><div class='grid'>"
        for label, value in (("Pending users", counts.get("pending", 0)), ("Approved users", counts.get("approved", 0)), ("Active monitors", len(jobs)), ("API requests today", usage["total"] or 0)):
            body += f"<div class='card'><div class='muted'>{label}</div><div class='metric'>{_e(value)}</div></div>"
        body += "</div><h2>Recent checks</h2><table><tr><th>ID</th><th>Airport</th><th>Status</th><th>Flights</th><th>Delayed</th><th>Time</th></tr>"
        for row in checks:
            body += f"<tr><td>#{row['id']}</td><td>{_e(row['airport'])}</td><td>{_e(row['status'])}</td><td>{row['flight_count']}</td><td>{row['delayed_count']}</td><td>{_time(row['started_at'], self.timezone_name)}</td></tr>"
        body += "</table>"
        return self.page("Start", body, request.url.path)

    async def monitoring(self, request):
        rows = await self.repo.active_monitor_jobs()
        body = "<h1>Active monitoring</h1><table><tr><th>Airport</th><th>User</th><th>Interval</th><th>Started</th><th>Valid until</th><th>Action</th></tr>"
        for row in rows:
            action = f"<form method='post' action='/actions/monitor/{row['actor_user_id']}/{_e(row['airport'])}/stop'><button class='danger'>Stop</button></form>"
            valid_until = _add_duration(row["started_at"], self.monitor.duration) if self.monitor else None
            body += f"<tr><td><b>{_e(row['airport'])}</b></td><td>{row['actor_user_id']}</td><td>{row['interval_minutes']} min</td><td>{_time(row['started_at'], self.timezone_name)}</td><td>{_time(valid_until, self.timezone_name)}</td><td>{action}</td></tr>"
        body += "</table>" if rows else "<p>No active monitors.</p>"
        return self.page("Monitoring", body, request.url.path)

    async def users(self, request):
        rows = await self.repo.list_users()
        body = "<h1>Users</h1><table><tr><th>User</th><th>Status</th><th>Created</th><th>Action</th></tr>"
        for row in rows:
            action = ""
            if row["status"] == "pending":
                pending = await self.repo.pending_request_for_user(row["telegram_user_id"])
                if pending:
                    action = f"<form method='post' action='/actions/access/{pending[0]}/approve'><button>Approve</button></form> <form method='post' action='/actions/access/{pending[0]}/deny'><button class='danger'>Deny</button></form>"
            elif not row["is_admin"] and row["status"] in {"approved", "blocked"}:
                target = "blocked" if row["status"] == "approved" else "approved"
                label = "Block" if target == "blocked" else "Unblock"
                action = f"<form method='post' action='/actions/user/{row['telegram_user_id']}/{target}'><button class='{'danger' if target == 'blocked' else 'secondary'}'>{label}</button></form>"
            body += f"<tr><td>{_e(row['display_name'])}<br><span class='muted'>{row['telegram_user_id']}</span></td><td>{_e(row['status'])}</td><td>{_time(row['created_at'], self.timezone_name)}</td><td>{action or '—'}</td></tr>"
        body += "</table>" if rows else "<p>No users found.</p>"
        return self.page("Users", body, request.url.path)

    async def history(self, request):
        checks = await self.repo.recent_checks(limit=50)
        alerts = await self.repo.list_alerts(limit=30)
        body = "<h1>History</h1><h2>Checks</h2><table><tr><th>ID</th><th>Airport</th><th>Status</th><th>Flights</th><th>Delayed</th><th>Time</th></tr>"
        for row in checks:
            body += f"<tr><td>#{row['id']}</td><td>{_e(row['airport'])}</td><td>{_e(row['status'])}</td><td>{row['flight_count']}</td><td>{row['delayed_count']}</td><td>{_time(row['started_at'], self.timezone_name)}</td></tr>"
        body += "</table><h2>Alerts</h2><table><tr><th>Airport</th><th>Flight</th><th>Delay</th><th>Status</th><th>Time</th></tr>"
        for row in alerts:
            body += f"<tr><td>{_e(row['airport'])}</td><td>{_e(row['flight_id'])}</td><td>{row['delay_minutes']} min</td><td>{_e(row['status'])}</td><td>{_time(row['created_at'], self.timezone_name)}</td></tr>"
        body += "</table>"
        return self.page("History", body, request.url.path)

    async def successes(self, request):
        rows = await self.repo.delayed_observations(limit=100)
        body = "<h1>Delayed flights found</h1><p class='muted'>Flights found above the configured delay threshold. Scheduled and estimated times use the origin airport's local time, with UTC in parentheses.</p><table><tr><th>Flight</th><th>Airport</th><th>Route</th><th>Scheduled (local / UTC)</th><th>Estimated (local / UTC)</th><th>Delay</th><th>Found</th></tr>"
        for row in rows:
            link = escape(flightaware_url(row['flightaware_id'] or row['flight_id']), quote=True)
            route = f"{_e(row['origin'] or '?')} → {_e(row['destination'] or '?')}"
            airport_timezone = row['origin_timezone'] or self.timezone_name
            body += f"<tr><td><a href='{link}' target='_blank' rel='noreferrer'><b>{_e(row['flight_id'])}</b></a></td><td>{_e(row['airport'])}</td><td>{route}</td><td>{_time(row['scheduled_departure'], airport_timezone)}</td><td>{_time(row['estimated_departure'], airport_timezone)}</td><td><b>+{row['delay_minutes'] or 0} min</b></td><td>{_time(row['observed_at'], self.timezone_name)}</td></tr>"
        body += "</table>" if rows else "<p>No delayed flights have been found yet.</p>"
        return self.page("Delayed flights", body, request.url.path)

    async def stop_monitor(self, request):
        user_id = int(request.path_params["user_id"])
        airport = request.path_params["airport"]
        if await self.monitor.stop_user_airport(user_id, airport):
            await self.repo.audit(next(iter(self.admin_ids)), "monitor_stop", "monitor", airport, {"user_id": user_id, "source": "web"})
        return RedirectResponse("/monitoring", status_code=303)

    async def access_decision(self, request):
        request_id = int(request.path_params["request_id"])
        decision = request.path_params["decision"]
        if decision not in {"approve", "deny"}:
            return RedirectResponse("/users", status_code=303)
        changed, user_id = await self.repo.decide_request(request_id, next(iter(self.admin_ids)), decision == "approve")
        if changed and user_id:
            await self.repo.audit(next(iter(self.admin_ids)), "access_decision", "access_request", str(request_id), {"decision": decision, "user_id": user_id, "source": "web"})
            try:
                await self.bot.send_message(user_id, "✅ Access granted." if decision == "approve" else "❌ Access request denied.")
            except Exception:
                pass
        return RedirectResponse("/users", status_code=303)

    async def user_status(self, request):
        user_id = int(request.path_params["user_id"])
        status = request.path_params["status"]
        if status in {"approved", "revoked", "blocked"} and await self.repo.set_user_status(user_id, status):
            if status in {"revoked", "blocked"}:
                await self.monitor.stop_user_all(user_id)
            admin_id = next(iter(self.admin_ids))
            await self.repo.audit(admin_id, f"user_{status}", "user", str(user_id), {"source": "web"})
        return RedirectResponse("/users", status_code=303)


async def serve(panel: WebPanel, host: str = "127.0.0.1", port: int = 8080):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(panel.app(), host=host, port=port, log_level="warning"))
    await server.serve()
