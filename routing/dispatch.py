"""Live dispatch: follows every van against its plan, re-plans a van that falls more than 15 minutes behind,
and texts customers when the driver is 3 stops or 10 minutes away.

All methods take the time `t` (seconds after midnight) from the driver's message, so the logic is the same for a
real day and for an accelerated simulation, and tests need no clock.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .matrix import MatrixProvider, haversine_m
from .models import Plan, Point, Problem, Route, Stop, Visit, hhmm
from .notify import Notifier
from .solver import solve
from .store import Store

DELAY_TRIGGER_S = 15 * 60
NEAR_S = 10 * 60
STOPS_AWAY = 3
Event = dict[str, Any]


@dataclass
class VanState:
    route: Route
    next: int = 0                       # index of the next visit not yet completed
    at_stop: bool = False               # arrived at visits[next] and serving
    origin: Point | None = None         # where the current leg started (depot, last stop, or re-plan position)
    pos: Point | None = None
    delay_s: int = 0
    eta: int | None = None              # projected arrival at the next stop
    reoptimizations: int = 0
    done: bool = False
    trail: list[tuple[int, int]] = field(default_factory=list)   # (t, delay) history of re-plans


class Dispatcher:
    def __init__(self, problem: Problem, plan: Plan, matrix: MatrixProvider, store: Store, notifier: Notifier) -> None:
        self.problem, self.plan, self.matrix, self.store, self.notifier = problem, plan, matrix, store, notifier
        self.stops: dict[str, Stop] = {s.id: s for s in problem.stops}
        self.vans = {r.vehicle_id: VanState(route=r.model_copy(deep=True), origin=problem.depot, pos=problem.depot) for r in plan.routes}
        self.plan_id = store.save_plan(problem, plan)
        self.texted: set[tuple[str, str]] = set()
        self.lock = threading.Lock()    # the server calls from worker threads: one message at a time

    # ---- messages from drivers ----------------------------------------------------------------------------

    def on_position(self, vehicle_id: str, lat: float, lon: float, t: int) -> list[Event]:
        with self.lock:
            van = self.vans[vehicle_id]
            van.pos = Point(lat=lat, lon=lon)
            events: list[Event] = []
            if not van.done:
                self._project(van, t)
                if van.delay_s > DELAY_TRIGGER_S:
                    events += self._replan(van, t)
                events += self._texts(van, t)
            events.insert(0, {"type": "position", "vehicle_id": vehicle_id, "lat": lat, "lon": lon, "t": t,
                              "delay_s": van.delay_s, "eta": van.eta})
            return events

    def on_arrived(self, vehicle_id: str, stop_id: str, t: int) -> list[Event]:
        with self.lock:
            van = self.vans[vehicle_id]
            if van.done or van.route.visits[van.next].stop_id != stop_id:
                return [{"type": "rejected", "vehicle_id": vehicle_id, "t": t, "reason": f"{stop_id} is not the next stop"}]
            van.at_stop = True
            self.store.arrived(self.plan_id, stop_id, t)
            late = max(0, t - self.stops[stop_id].window_end)
            return [{"type": "arrived", "vehicle_id": vehicle_id, "stop_id": stop_id, "t": t, "late_s": late}]

    def on_completed(self, vehicle_id: str, stop_id: str, t: int) -> list[Event]:
        with self.lock:
            van = self.vans[vehicle_id]
            if van.done or not van.at_stop or van.route.visits[van.next].stop_id != stop_id:
                return [{"type": "rejected", "vehicle_id": vehicle_id, "t": t, "reason": f"{stop_id} is not being served"}]
            self.store.departed(self.plan_id, stop_id, t)
            stop = self.stops[stop_id]
            van.origin, van.at_stop, van.next = Point(lat=stop.lat, lon=stop.lon), False, van.next + 1
            van.done = van.next >= len(van.route.visits)
            events: list[Event] = [{"type": "completed", "vehicle_id": vehicle_id, "stop_id": stop_id, "t": t}]
            if van.done:
                van.delay_s, van.eta = 0, None
            else:
                self._project(van, t)
                events += self._texts(van, t)
            return events

    # ---- logic --------------------------------------------------------------------------------------------

    def _project(self, van: VanState, t: int) -> None:
        """Estimate when the van reaches its next stop, and how far behind plan that is."""
        visit = van.route.visits[van.next]
        if van.at_stop:
            van.eta, van.delay_s = None, max(0, t - visit.departure)
            return
        target = self.stops[visit.stop_id]
        assert van.pos is not None and van.origin is not None
        whole = haversine_m(van.origin, target)
        share = min(1.0, haversine_m(van.pos, target) / whole) if whole > 1 else 0.0
        van.eta = round(t + visit.travel_s * share)          # the planned leg time, scaled by the distance still to go
        van.delay_s = max(0, van.eta - visit.arrival)

    def _replan(self, van: VanState, t: int) -> list[Event]:
        """Re-optimise the stops this van still has to make, starting from where and when it is now."""
        first = van.next + (1 if van.at_stop else 0)         # a stop being served stays where it is
        rest = [self.stops[x.stop_id] for x in van.route.visits[first:]]
        if not rest:
            return []
        assert van.pos is not None
        here = self.stops[van.route.visits[van.next].stop_id] if van.at_stop else van.pos
        start_t = max(t, van.route.visits[van.next].arrival + self.stops[van.route.visits[van.next].stop_id].service_s) if van.at_stop else t
        dur, dist, source = self.matrix.table([*rest, here], [self.problem.depot, *rest])
        n = len(rest)                                         # square matrix laid out as [depot, *rest, start]
        square_d = [[0] * (n + 2)] + [row + [0] for row in dur]
        square_m = [[0] * (n + 2)] + [row + [0] for row in dist]
        vehicle = next(v for v in self.problem.vehicles if v.id == van.route.vehicle_id)
        sub = Problem(depot=self.problem.depot, vehicles=[vehicle], stops=rest)
        new = solve(sub, square_d, square_m, time_limit_s=1.0, start_nodes=[n + 1], start_times=[start_t], soft=True).routes[0]
        before = [x.stop_id for x in van.route.visits[first:]]
        van.route.visits[first:] = new.visits
        van.route.end = new.end
        van.route.geometry = self.matrix.geometry([Point(lat=here.lat, lon=here.lon), *rest_in_order(new.visits, self.stops), self.problem.depot])
        if not van.at_stop:
            van.origin = van.pos
        van.reoptimizations += 1
        van.trail.append((t, van.delay_s))
        delay = van.delay_s
        self._project(van, t)
        event: Event = {"type": "route_update", "vehicle_id": van.route.vehicle_id, "t": t, "reason": f"{delay // 60} min behind plan",
                        "visits": [x.model_dump() for x in van.route.visits], "geometry": van.route.geometry, "from_index": first,
                        "reordered": before != [x.stop_id for x in new.visits], "matrix_source": source,
                        "late_stops": [x.stop_id for x in new.visits if x.late_s > 0]}
        self.store.event(self.plan_id, t, van.route.vehicle_id, "route_update",
                         {k: event[k] for k in ("reason", "reordered", "late_stops", "matrix_source")})
        return [event]

    def _texts(self, van: VanState, t: int) -> list[Event]:
        events: list[Event] = []
        visits = van.route.visits
        ahead = van.next + STOPS_AWAY - 1                    # the customer who is now exactly 3 stops away
        if not van.at_stop and ahead < len(visits):
            events += self._text(visits[ahead], "3_stops", t,
                                 f"Your delivery is 3 stops away. Estimated arrival {hhmm(visits[ahead].arrival + van.delay_s)}.")
        if not van.at_stop and van.eta is not None and van.eta - t <= NEAR_S:
            events += self._text(visits[van.next], "10_min", t, f"Your driver is about {max(1, round((van.eta - t) / 60))} minutes away.")
        return events

    def _text(self, visit: Visit, kind: str, t: int, body: str) -> list[Event]:
        stop = self.stops[visit.stop_id]
        if (stop.id, kind) in self.texted or not stop.phone:
            return []
        self.texted.add((stop.id, kind))                     # each customer gets each message once, even after a re-plan
        status = self.notifier.send(stop.phone, body)
        self.store.sms(self.plan_id, t, stop.id, kind, stop.phone, body, status)
        return [{"type": "sms", "stop_id": stop.id, "kind": kind, "to": stop.phone, "body": body, "status": status, "t": t}]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"plan_id": self.plan_id, "problem": self.problem.model_dump(), "dropped": self.plan.dropped,
                    "solver_ms": self.plan.solver_ms, "matrix_source": self.plan.matrix_source,
                    "vans": [{"vehicle_id": k, "route": v.route.model_dump(), "next": v.next, "at_stop": v.at_stop,
                              "pos": v.pos.model_dump() if v.pos else None, "delay_s": v.delay_s, "eta": v.eta,
                              "reoptimizations": v.reoptimizations, "done": v.done} for k, v in self.vans.items()]}


def rest_in_order(visits: list[Visit], stops: dict[str, Stop]) -> list[Point]:
    return [Point(lat=stops[x.stop_id].lat, lon=stops[x.stop_id].lon) for x in visits]
