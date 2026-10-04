"""HTTP + WebSocket gateway.

  POST /api/plans                 solve a problem (or the bundled 50-stop one) and make it the live plan
  GET  /api/plans/current         the live plan and every van's state
  GET  /api/fleet/performance     per-vehicle history metrics
  WS   /ws/driver/{vehicle_id}    a driver's phone: sends position / arrived / completed, receives route updates
  WS   /ws/dispatch               a dispatcher's dashboard: receives every event
"""
from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .data import bundled_matrix, mock_problem
from .dispatch import Dispatcher, Event
from .matrix import MatrixProvider
from .models import DAY, Plan, Point, Problem
from .notify import Notifier
from .solver import points_of, solve
from .store import Store

app = FastAPI(title="Route optimizer")


class Hub:
    """Everything the running server shares: storage, the matrix cache, the live plan and the open sockets."""

    def __init__(self, db_path: str = ":memory:", osrm_url: str | None = "https://router.project-osrm.org") -> None:
        self.store = Store(db_path)
        self.matrix = MatrixProvider(self.store.db, osrm_url)
        self.notifier = Notifier()
        self.dispatcher: Dispatcher | None = None
        self.dashboards: set[WebSocket] = set()
        self.drivers: dict[str, WebSocket] = {}

    async def broadcast(self, events: list[Event]) -> None:
        for event in events:
            driver = self.drivers.get(event.get("vehicle_id", ""))
            targets = [*self.dashboards, *([driver] if driver and event["type"] in ("route_update", "rejected") else [])]
            for ws in targets:
                try:
                    await ws.send_json(event)
                except Exception:                 # a closed socket must not stop the others
                    self.dashboards.discard(ws)


def hub() -> Hub:
    if not hasattr(app.state, "hub"):
        app.state.hub = Hub(os.environ.get("ROUTES_DB", "routes.sqlite"), os.environ.get("OSRM_URL", "https://router.project-osrm.org") or None)
    hub_: Hub = app.state.hub
    return hub_


def _token_ok(given: str) -> bool:
    return hmac.compare_digest(given.encode(), os.environ["ROUTES_API_TOKEN"].encode())


def auth(authorization: Annotated[str, Header()] = "") -> None:
    if not _token_ok(authorization.removeprefix("Bearer ")):
        raise HTTPException(401, "missing or wrong API token")


Auth = Annotated[None, Depends(auth)]


def make_plan(h: Hub, problem: Problem, time_limit_s: float) -> Plan:
    pts = points_of(problem)
    dur, dist, source = h.matrix.table(pts, pts)
    plan = solve(problem, dur, dist, time_limit_s)
    plan.matrix_source = source
    stop = {s.id: s for s in problem.stops}
    for r in plan.routes:
        r.geometry = h.matrix.geometry([problem.depot, *[Point(lat=stop[x.stop_id].lat, lon=stop[x.stop_id].lon) for x in r.visits], problem.depot])
    return plan


class PlanRequest(BaseModel):
    problem: Problem | None = None                 # omitted: the bundled 50-stop Tampa data set
    time_limit_s: float = Field(default=3.0, gt=0, le=30)


@app.post("/api/plans")
async def create_plan(body: PlanRequest, _: Auth) -> dict[str, Any]:
    h = hub()
    problem = body.problem
    if problem is None:
        problem = mock_problem()
        pts = points_of(problem)
        h.matrix.seed(pts, *bundled_matrix())
    try:
        plan = await asyncio.to_thread(make_plan, h, problem, body.time_limit_s)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
    h.dispatcher = Dispatcher(problem, plan, h.matrix, h.store, h.notifier)
    snapshot = h.dispatcher.snapshot()
    await h.broadcast([{"type": "plan", **snapshot}])
    return snapshot


@app.get("/api/plans/current")
async def current_plan(_: Auth) -> dict[str, Any]:
    d = hub().dispatcher
    if d is None:
        raise HTTPException(404, "no live plan yet")
    return d.snapshot()


@app.get("/api/fleet/performance")
async def fleet_performance(_: Auth) -> dict[str, Any]:
    s = hub().store
    return {"vehicles": s.rows("SELECT * FROM fleet_performance ORDER BY vehicle_id"),
            "plans": s.rows("SELECT id, created_at, stops, dropped, distance_m, solver_ms, matrix_source FROM plans ORDER BY id DESC LIMIT 50"),
            "sms": s.rows("SELECT t, stop_id, kind, to_number, body, status FROM sms_outbox ORDER BY id DESC LIMIT 200")}


class DriverMessage(BaseModel):
    type: Literal["position", "arrived", "completed"]
    t: int = Field(ge=0, le=2 * DAY)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    stop_id: str | None = None


@app.websocket("/ws/dispatch")
async def dispatch_socket(ws: WebSocket, token: str = "") -> None:
    if not _token_ok(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    h = hub()
    h.dashboards.add(ws)
    try:
        if h.dispatcher:
            await ws.send_json({"type": "plan", **h.dispatcher.snapshot()})
        while True:
            await ws.receive_text()               # dashboards only listen; this keeps the socket open
    except WebSocketDisconnect:
        h.dashboards.discard(ws)


@app.websocket("/ws/driver/{vehicle_id}")
async def driver_socket(ws: WebSocket, vehicle_id: str, token: str = "") -> None:
    h = hub()
    d = h.dispatcher
    if not _token_ok(token) or d is None or vehicle_id not in d.vans:
        await ws.close(code=4401 if not _token_ok(token) else 4404)
        return
    await ws.accept()
    h.drivers[vehicle_id] = ws
    await ws.send_json({"type": "route", "vehicle_id": vehicle_id, "route": d.vans[vehicle_id].route.model_dump()})
    try:
        while True:
            try:
                m = DriverMessage.model_validate(await ws.receive_json())
            except ValidationError as exc:
                await ws.send_json({"type": "rejected", "reason": exc.errors()[0]["msg"]})
                continue
            # re-planning calls the solver and the routing engine: run it off the event loop
            if m.type == "position" and m.lat is not None and m.lon is not None:
                events = await asyncio.to_thread(d.on_position, vehicle_id, m.lat, m.lon, m.t)
            elif m.type == "arrived" and m.stop_id:
                events = await asyncio.to_thread(d.on_arrived, vehicle_id, m.stop_id, m.t)
            elif m.type == "completed" and m.stop_id:
                events = await asyncio.to_thread(d.on_completed, vehicle_id, m.stop_id, m.t)
            else:
                events = [{"type": "rejected", "vehicle_id": vehicle_id, "reason": "message is missing lat/lon or stop_id"}]
            await h.broadcast(events)
    except WebSocketDisconnect:
        if h.drivers.get(vehicle_id) is ws:
            del h.drivers[vehicle_id]


_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
if _dist.exists():  # pragma: no cover - only in the built image
    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
