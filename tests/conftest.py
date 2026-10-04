from __future__ import annotations

import os
import sqlite3

import pytest

from routing.data import bundled_matrix, mock_problem
from routing.matrix import MatrixProvider
from routing.models import Point, Problem, Stop, Vehicle
from routing.solver import points_of

os.environ["ROUTES_API_TOKEN"] = "test-token"
H = 3600


@pytest.fixture(scope="session")
def tampa() -> tuple[Problem, list[list[int]], list[list[int]]]:
    return (mock_problem(), *bundled_matrix())


def offline_matrix() -> MatrixProvider:
    return MatrixProvider(sqlite3.connect(":memory:", check_same_thread=False), osrm_url=None)


def line_problem(n: int = 5, **stop: object) -> Problem:
    """n stops in a row heading east from the depot, about 2.2 km (5.6 min at fallback speed) apart."""
    stops = [Stop(**{"id": f"S{i}", "lat": 28.0, "lon": round(-82.5 + 0.022 * i, 4), "weight_kg": 10, "volume_l": 100,  # type: ignore[arg-type]
                     "service_s": 300, "window_start": 8 * H, "window_end": 18 * H, "phone": f"+1555010000{i}", **stop})
             for i in range(1, n + 1)]
    return Problem(depot=Point(lat=28.0, lon=-82.5), stops=stops,
                   vehicles=[Vehicle(id="VAN-1", capacity_kg=500, capacity_l=5000, shift_start=8 * H, shift_end=17 * H)])


def square(problem: Problem, matrix: MatrixProvider) -> tuple[list[list[int]], list[list[int]]]:
    pts = points_of(problem)
    dur, dist, _ = matrix.table(pts, pts)
    return dur, dist


def manual_plan(problem: Problem, dur: list[list[int]], dist: list[list[int]], order: list[str], start: int) -> "Plan":
    """A plan with the stops in a chosen order, timed from the matrix (so tests do not depend on solver tie-breaks)."""
    from routing.models import Plan, Route, Visit
    node = {s.id: n for n, s in enumerate(problem.stops, start=1)}
    stop = {s.id: s for s in problem.stops}
    visits, clock, at, metres = [], start, 0, 0
    for sid in order:
        arrival = max(clock + dur[at][node[sid]], stop[sid].window_start)
        visits.append(Visit(stop_id=sid, arrival=arrival, departure=arrival + stop[sid].service_s, travel_s=dur[at][node[sid]]))
        metres += dist[at][node[sid]]
        clock, at = arrival + stop[sid].service_s, node[sid]
    route = Route(vehicle_id=problem.vehicles[0].id, start=start, end=clock + dur[at][0], visits=visits, distance_m=metres + dist[at][0],
                  load_kg=sum(stop[i].weight_kg for i in order), load_l=sum(stop[i].volume_l for i in order))
    return Plan(routes=[route], dropped=[], distance_m=route.distance_m, duration_s=route.end - start, solver_ms=0, matrix_source="test")
