from __future__ import annotations

import time

import pytest

from conftest import H, line_problem, offline_matrix, square
from routing.models import Problem, Stop
from routing.solver import check, solve

Tampa = tuple[Problem, list[list[int]], list[list[int]]]


def test_fifty_stops_valid_plan_under_five_seconds(tampa: Tampa) -> None:
    problem, dur, dist = tampa
    started = time.perf_counter()
    plan = solve(problem, dur, dist, time_limit_s=3)
    assert time.perf_counter() - started < 5
    assert check(problem, plan, dur) == [] and plan.dropped == []
    assert sum(len(r.visits) for r in plan.routes) == 50 and len(plan.routes) <= 5
    stop = {s.id: s for s in problem.stops}
    for r in plan.routes:
        for x in r.visits:
            assert stop[x.stop_id].window_start <= x.arrival <= stop[x.stop_id].window_end and x.late_s == 0
    assert plan.distance_m == sum(r.distance_m for r in plan.routes) and 150_000 < plan.distance_m < 260_000


def test_check_catches_a_tampered_plan(tampa: Tampa) -> None:
    problem, dur, dist = tampa
    plan = solve(problem, dur, dist, time_limit_s=1)
    plan.routes[0].visits[1].arrival = 7 * H                             # before the window and faster than the road
    errors = check(problem, plan, dur)
    assert any("faster than the road" in e for e in errors) and any("outside its window" in e for e in errors)
    plan = solve(problem, dur, dist, time_limit_s=1)
    plan.routes[0].visits.append(plan.routes[1].visits.pop())            # one van takes another's stop without re-timing
    assert check(problem, plan, dur) != []
    plan.dropped.append("S01")
    assert "every stop must be visited exactly once or listed as dropped" in check(problem, plan, dur)
    small = problem.model_copy(deep=True)
    for v in small.vehicles:
        v.capacity_kg, v.shift_end = 100, 9 * H
    errors = check(small, solve(problem, dur, dist, time_limit_s=1), dur)
    assert any("over weight capacity" in e for e in errors) and any("outside its shift" in e for e in errors)


def test_capacity_limits_what_one_van_carries() -> None:
    problem = line_problem(6, weight_kg=100)
    problem.vehicles[0].capacity_kg = 350                                # room for three 100 kg stops
    dur, dist = square(problem, offline_matrix())
    plan = solve(problem, dur, dist, 1)
    assert len(plan.routes[0].visits) == 3 and len(plan.dropped) == 3 and check(problem, plan, dur) == []
    problem.vehicles.append(problem.vehicles[0].model_copy(update={"id": "VAN-2"}))
    plan = solve(problem, dur, dist, 1)
    assert plan.dropped == [] and sorted(len(r.visits) for r in plan.routes) == [3, 3]
    assert max(r.load_kg for r in plan.routes) <= 350


def test_volume_capacity_and_shift_length_also_bind() -> None:
    problem = line_problem(4, volume_l=2000)                             # 5000 l van: two stops by volume
    dur, dist = square(problem, offline_matrix())
    assert len(solve(problem, dur, dist, 1).dropped) == 2
    problem = line_problem(5, service_s=3600)                            # an hour each, but a 3-hour shift
    problem.vehicles[0].shift_end = 11 * H
    plan = solve(problem, square(problem, offline_matrix())[0], dist, 1)
    assert 1 <= len(plan.routes[0].visits) <= 2 and plan.routes[0].end <= 11 * H


def test_priority_window_is_kept_and_priority_stops_are_dropped_last() -> None:
    problem = line_problem(5)
    late = problem.stops[4].model_copy(update={"window_start": 14 * H, "window_end": 15 * H, "priority": True})
    problem.stops[4] = Stop(**late.model_dump())
    dur, dist = square(problem, offline_matrix())
    plan = solve(problem, dur, dist, 1)
    arrival = {x.stop_id: x.arrival for x in plan.routes[0].visits}
    assert 14 * H <= arrival["S5"] <= 15 * H and plan.dropped == []
    problem.vehicles[0].capacity_kg = 10                                 # room for one stop only: it must be the priority one
    plan = solve(problem, dur, dist, 1)
    assert [x.stop_id for x in plan.routes[0].visits] == ["S5"] and len(plan.dropped) == 4


def test_soft_mode_serves_everything_and_reports_lateness() -> None:
    problem = line_problem(4)
    problem.stops[3] = Stop(**problem.stops[3].model_copy(update={"window_end": 8 * H + 60}).model_dump())   # cannot be met
    dur, dist = square(problem, offline_matrix())
    plan = solve(problem, dur, dist, 1, start_times=[9 * H], soft=True)
    visits = plan.routes[0].visits
    assert len(visits) == 4 and plan.dropped == [] and visits[0].stop_id == "S4"      # least-late: goes there first
    assert visits[0].late_s == visits[0].arrival - (8 * H + 60) > 0 and all(x.late_s == 0 for x in visits[1:])


def test_impossible_fleet_raises() -> None:
    problem = line_problem(2)
    dur, dist = square(problem, offline_matrix())
    with pytest.raises(RuntimeError, match="no feasible plan"):
        solve(problem, dur, dist, 1, start_times=[30 * H])               # the shift is over before it starts
