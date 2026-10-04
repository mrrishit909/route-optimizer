"""Simulated drivers. Each one follows its route along the road geometry and reports like a phone would:
a position every 30 seconds, "arrived" and "completed" at each stop. A traffic jam can hold one van still.

  python -m routing.simulate --record web/public/data/replay.json      offline: drive the dispatcher directly, save every event
  python -m routing.simulate --url ws://localhost:8000 --token ... --speed 120      against a running server, 120x real time
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .data import bundled_matrix, mock_problem
from .dispatch import Dispatcher, Event
from .matrix import MatrixProvider, haversine_m
from .models import Point, Problem, Route, Stop, Visit
from .notify import Notifier
from .solver import points_of, solve
from .store import Store

TICK_S = 30
Path = list[tuple[float, float]]


def split_legs(geometry: Path, waypoints: list[Point]) -> list[Path]:
    """Cut one road path into the pieces between consecutive waypoints (nearest path vertex, searched in order)."""
    if len(geometry) < 2:
        return [[(a.lat, a.lon), (b.lat, b.lon)] for a, b in zip(waypoints, waypoints[1:])]
    cuts, at = [0], 0
    for wp in waypoints[1:-1]:
        at = min(range(at, len(geometry)), key=lambda i: haversine_m(Point(lat=geometry[i][0], lon=geometry[i][1]), wp))
        cuts.append(at)
    cuts.append(len(geometry) - 1)
    return [geometry[a:b + 1] if b > a else [geometry[a], geometry[a]] for a, b in zip(cuts, cuts[1:])]


def along(path: Path, share: float) -> tuple[float, float]:
    """The point a given share (0..1) of the way along a path, by distance."""
    lengths = [haversine_m(Point(lat=a[0], lon=a[1]), Point(lat=b[0], lon=b[1])) for a, b in zip(path, path[1:])]
    goal = max(0.0, min(1.0, share)) * sum(lengths)
    for (a, b), length in zip(zip(path, path[1:]), lengths):
        if goal <= length and length > 0:
            f = goal / length
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        goal -= length
    return path[-1]


@dataclass
class Jam:
    vehicle_id: str
    start: int
    seconds: int


class SimDriver:
    def __init__(self, route: Route, problem: Problem, jam: Jam | None = None) -> None:
        self.id, self.problem, self.jam = route.vehicle_id, problem, jam
        self.stops: dict[str, Stop] = {s.id: s for s in problem.stops}
        self.pos = (problem.depot.lat, problem.depot.lon)
        self.start = route.start
        self.done_ids: list[str] = []
        self.progress = 0.0                    # share of the current leg already driven
        self.serving_until: int | None = None
        self.finished = False
        self.set_route(route.visits, route.geometry, problem.depot)

    def set_route(self, visits: list[Visit], geometry: Path, origin: Point) -> None:
        """Take a (new) list of remaining visits; called at the start and after every re-plan."""
        self.todo = [v for v in visits if v.stop_id not in self.done_ids]
        if self.serving_until is not None:     # the stop being served is not part of the new legs
            current, rest = self.todo[0], self.todo[1:]
            origin = self.stops[current.stop_id]
            self.legs = [[], *split_legs(geometry, [origin, *[self.stops[v.stop_id] for v in rest], self.problem.depot])]
        else:
            self.legs = split_legs(geometry, [origin, *[self.stops[v.stop_id] for v in self.todo], self.problem.depot])
            self.progress = 0.0

    def tick(self, t: int) -> list[dict[str, Any]]:
        if self.finished or t < self.start:
            return []
        out: list[dict[str, Any]] = []
        jammed = self.jam is not None and self.jam.vehicle_id == self.id and self.jam.start <= t < self.jam.start + self.jam.seconds
        if self.serving_until is not None:
            if t >= self.serving_until:
                done = self.todo.pop(0)
                self.legs.pop(0)
                self.done_ids.append(done.stop_id)
                self.serving_until, self.progress = None, 0.0
                out.append({"type": "completed", "stop_id": done.stop_id, "t": t})
        elif self.todo and not jammed:
            visit, stop = self.todo[0], self.stops[self.todo[0].stop_id]
            self.progress += TICK_S / max(visit.travel_s, TICK_S)
            self.pos = along(self.legs[0], self.progress)
            if self.progress >= 1.0:
                self.pos = (stop.lat, stop.lon)
                self.serving_until = max(t, stop.window_start) + stop.service_s
                out.append({"type": "arrived", "stop_id": stop.id, "t": t})
        elif not self.todo:                    # driving back to the depot
            self.progress += TICK_S / 600
            self.pos = along(self.legs[0], self.progress) if self.legs else self.pos
            self.finished = self.progress >= 1.0
        return [{"type": "position", "lat": round(self.pos[0], 6), "lon": round(self.pos[1], 6), "t": t}, *out]


def run_offline(dispatcher: Dispatcher, jam: Jam | None, start: int = 8 * 3600, end: int = 19 * 3600) -> list[Event]:
    """Drive every van through its day against the dispatcher, in simulated time. Returns every event produced."""
    drivers = [SimDriver(v.route, dispatcher.problem, jam) for v in dispatcher.vans.values()]
    events: list[Event] = []
    for t in range(start, end, TICK_S):
        for d in drivers:
            for m in d.tick(t):
                if m["type"] == "position":
                    got = dispatcher.on_position(d.id, m["lat"], m["lon"], t)
                elif m["type"] == "arrived":
                    got = dispatcher.on_arrived(d.id, m["stop_id"], t)
                else:
                    got = dispatcher.on_completed(d.id, m["stop_id"], t)
                for e in got:
                    if e["type"] == "route_update":
                        d.set_route([Visit(**v) for v in e["visits"]], [tuple(p) for p in e["geometry"]], Point(lat=d.pos[0], lon=d.pos[1]))
                events += got
        if all(d.finished for d in drivers):
            break
    return events


async def run_live(url: str, token: str, speed: float, jam: Jam | None) -> None:  # pragma: no cover - needs a running server
    import httpx
    import websockets
    async with httpx.AsyncClient(base_url=url.replace("ws", "http", 1), headers={"Authorization": f"Bearer {token}"}) as client:
        snap = (await client.get("/api/plans/current")).raise_for_status().json()
    problem = Problem(**snap["problem"])

    async def drive(route: Route) -> None:
        async with websockets.connect(f"{url}/ws/driver/{route.vehicle_id}?token={token}") as ws:
            await ws.recv()
            driver = SimDriver(route, problem, jam)
            for t in range(8 * 3600, 19 * 3600, TICK_S):
                for m in driver.tick(t):
                    await ws.send(json.dumps(m))
                try:
                    while True:
                        e = json.loads(await asyncio.wait_for(ws.recv(), timeout=TICK_S / speed))
                        if e["type"] == "route_update":
                            driver.set_route([Visit(**v) for v in e["visits"]], [tuple(p) for p in e["geometry"]], Point(lat=driver.pos[0], lon=driver.pos[1]))
                except TimeoutError:
                    pass
                if driver.finished:
                    return

    await asyncio.gather(*(drive(Route(**v["route"])) for v in snap["vans"]))


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--record")
    ap.add_argument("--url")
    ap.add_argument("--token", default="")
    ap.add_argument("--speed", type=float, default=120)
    ap.add_argument("--jam-van", help="default: the van with the most stops")
    ap.add_argument("--jam-at", type=int, help="seconds after midnight; default: a minute after that van's 4th stop")
    ap.add_argument("--jam-minutes", type=int, default=40)
    ap.add_argument("--no-network", action="store_true", help="skip OSRM (straight-line geometry and fallback times)")
    a = ap.parse_args()
    if a.url:
        asyncio.run(run_live(a.url, a.token, a.speed, Jam(a.jam_van, a.jam_at, a.jam_minutes * 60) if a.jam_van and a.jam_at else None))
        return
    from .server import Hub, make_plan
    hub = Hub(":memory:", None if a.no_network else "https://router.project-osrm.org")
    problem = mock_problem()
    hub.matrix.seed(points_of(problem), *bundled_matrix())
    plan = make_plan(hub, problem, 3.0)
    dispatcher = Dispatcher(problem, plan, hub.matrix, hub.store, Notifier())
    busiest = max(plan.routes, key=lambda r: len(r.visits))
    jam = Jam(a.jam_van or busiest.vehicle_id, a.jam_at or busiest.visits[3].departure + 60, a.jam_minutes * 60)
    first = dispatcher.snapshot()
    events = run_offline(dispatcher, jam)
    perf = hub.store.rows("SELECT * FROM fleet_performance ORDER BY vehicle_id")
    summary = {"events": len(events), "route_updates": sum(e["type"] == "route_update" for e in events),
               "sms": sum(e["type"] == "sms" for e in events), "osrm_calls": hub.matrix.calls,
               "cache_hits": hub.matrix.hits, "cache_misses": hub.matrix.misses, "performance": perf}
    print(json.dumps(summary, indent=1))
    if a.record:
        with open(a.record, "w") as f:
            json.dump({"snapshot": first, "events": events, "jam": {"vehicle_id": jam.vehicle_id, "start": jam.start, "seconds": jam.seconds},
                       "performance": perf}, f)


if __name__ == "__main__":  # pragma: no cover
    main()
