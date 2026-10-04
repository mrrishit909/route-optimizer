"""The live rules, driven message by message with explicit times (no clock, no sockets)."""
from __future__ import annotations

from conftest import H, line_problem, manual_plan, offline_matrix, square
from routing.dispatch import Dispatcher
from routing.models import Problem, Stop
from routing.notify import Notifier
from routing.simulate import Jam, run_offline
from routing.solver import solve
from routing.store import Store


ORDER = ["S1", "S2", "S3", "S4", "S5"]
START = 9 * H


def make(problem: Problem | None = None) -> Dispatcher:
    problem = problem or line_problem(5)
    matrix = offline_matrix()
    dur, dist = square(problem, matrix)
    return Dispatcher(problem, manual_plan(problem, dur, dist, ORDER, START), matrix, Store(), Notifier(None, None, None))


def kinds(events: list[dict[str, object]]) -> list[str]:
    return [str(e["type"]) for e in events]


def test_on_time_van_texts_three_stops_away_and_ten_minutes_away_once() -> None:
    d = make()
    visits = d.vans["VAN-1"].route.visits
    first = d.on_position("VAN-1", 28.0, -82.5, START)
    texts = [(e["stop_id"], e["kind"]) for e in first if e["type"] == "sms"]
    assert texts == [("S3", "3_stops"), ("S1", "10_min")]                 # S3 is third in line; S1 is under 10 minutes away
    assert first[0]["type"] == "position" and first[0]["delay_s"] == 0
    assert "3 stops away. Estimated arrival 09:2" in first[1]["body"]
    assert [e for e in d.on_position("VAN-1", 28.0, -82.49, START + 60) if e["type"] == "sms"] == []    # not repeated
    assert kinds(d.on_arrived("VAN-1", "S1", visits[0].arrival)) == ["arrived"]
    done = d.on_completed("VAN-1", "S1", visits[0].departure)
    assert [(e["stop_id"], e["kind"]) for e in done if e["type"] == "sms"] == [("S4", "3_stops"), ("S2", "10_min")]
    outbox = d.store.rows("SELECT stop_id, kind, status FROM sms_outbox ORDER BY id")
    assert len(outbox) == 4 and {r["status"] for r in outbox} == {"not sent (no Twilio credentials)"}
    assert d.store.rows("SELECT actual_arrival, actual_departure FROM visits WHERE stop_id = 'S1'") == [
        {"actual_arrival": visits[0].arrival, "actual_departure": visits[0].departure}]


def test_wrong_stop_messages_are_rejected() -> None:
    d = make()
    assert kinds(d.on_arrived("VAN-1", "S4", START)) == ["rejected"]
    assert kinds(d.on_completed("VAN-1", "S1", START)) == ["rejected"]     # never arrived there


def test_more_than_fifteen_minutes_behind_triggers_a_replan_then_waits_for_the_next_fifteen() -> None:
    d = make()
    van = d.vans["VAN-1"]
    planned_end = van.route.end
    assert kinds(d.on_position("VAN-1", 28.0, -82.5, START + 14 * 60))[:1] == ["position"] and van.reoptimizations == 0
    events = d.on_position("VAN-1", 28.0, -82.5, START + 16 * 60)          # still at the depot 16 minutes after leaving time
    update = next(e for e in events if e["type"] == "route_update")
    assert update["reason"] == "16 min behind plan" and van.reoptimizations == 1 and update["late_stops"] == []
    assert sorted(x.stop_id for x in van.route.visits) == ORDER and van.route.end == planned_end + 16 * 60   # same work, 16 min later
    assert min(x.arrival for x in van.route.visits) >= START + 16 * 60
    assert van.delay_s == 0                                                 # the plan is rebased on reality
    assert "route_update" not in kinds(d.on_position("VAN-1", 28.0, -82.5, START + 20 * 60))
    assert "route_update" in kinds(d.on_position("VAN-1", 28.0, -82.5, START + 32 * 60)) and van.reoptimizations == 2
    assert d.store.rows("SELECT count(*) AS n FROM events WHERE type = 'route_update'") == [{"n": 2}]


def test_replan_reorders_to_protect_a_priority_window() -> None:
    problem = line_problem(5)
    matrix = offline_matrix()
    dur, _ = square(problem, matrix)
    planned_s5 = manual_plan(problem, dur, dur, ORDER, START).routes[0].visits[-1].arrival
    problem.stops[4] = Stop(**problem.stops[4].model_copy(update={"priority": True, "window_end": planned_s5 + 5 * 60}).model_dump())
    d = make(problem)
    van = d.vans["VAN-1"]
    events = d.on_position("VAN-1", 28.0, -82.5, START + 20 * 60)          # stuck at the depot for 20 minutes
    update = next(e for e in events if e["type"] == "route_update")
    order = [x.stop_id for x in van.route.visits]
    s5 = van.route.visits[order.index("S5")]
    assert update["reordered"] and order.index("S5") < 4                    # no longer last: it would have been 15 min late
    assert s5.late_s == 0 and s5.arrival <= planned_s5 + 5 * 60 and update["late_stops"] == []


def test_replan_reports_stops_that_can_no_longer_be_on_time() -> None:
    problem = line_problem(5)
    problem.stops[0] = Stop(**problem.stops[0].model_copy(update={"window_end": START + 10 * 60}).model_dump())
    d = make(problem)
    events = d.on_position("VAN-1", 28.0, -82.5, START + 30 * 60)
    update = next(e for e in events if e["type"] == "route_update")
    assert update["late_stops"] == ["S1"] and d.vans["VAN-1"].route.visits[0].late_s > 20 * 60


def test_delay_while_serving_keeps_the_current_stop_and_replans_the_rest() -> None:
    d = make()
    van = d.vans["VAN-1"]
    v0 = van.route.visits[0].model_copy()
    d.on_position("VAN-1", 28.0, -82.478, v0.arrival)
    d.on_arrived("VAN-1", "S1", v0.arrival)
    events = d.on_position("VAN-1", 28.0, -82.478, v0.departure + 17 * 60)   # service overran by 17 minutes
    update = next(e for e in events if e["type"] == "route_update")
    assert update["from_index"] == 1 and van.route.visits[0] == v0
    assert min(x.arrival for x in van.route.visits[1:]) >= v0.departure + 17 * 60
    assert kinds(d.on_completed("VAN-1", "S1", v0.departure + 17 * 60))[0] == "completed"
    for x in list(van.route.visits[1:]):
        d.on_arrived("VAN-1", x.stop_id, x.arrival)
        d.on_completed("VAN-1", x.stop_id, x.departure)
    assert van.done and kinds(d.on_position("VAN-1", 28.0, -82.5, 16 * H)) == ["position"]
    assert kinds(d.on_arrived("VAN-1", "S1", 16 * H)) == ["rejected"]


def test_full_simulated_day_with_a_traffic_jam(tampa: tuple[object, list[list[int]], list[list[int]]]) -> None:
    from routing.matrix import MatrixProvider
    from routing.models import Problem
    from routing.solver import points_of
    problem, dur, dist = tampa
    assert isinstance(problem, Problem)
    store = Store()
    matrix = MatrixProvider(store.db, None)
    matrix.seed(points_of(problem), dur, dist)
    plan = solve(problem, dur, dist, 2)
    d = Dispatcher(problem, plan, matrix, store, Notifier(None, None, None))
    busiest = max(plan.routes, key=lambda r: len(r.visits))
    jam = Jam(busiest.vehicle_id, busiest.visits[3].departure + 60, 40 * 60)
    events = run_offline(d, jam)
    completed = [e["stop_id"] for e in events if e["type"] == "completed"]
    assert sorted(completed) == sorted(s.id for s in problem.stops)           # every stop served exactly once
    updates = [e for e in events if e["type"] == "route_update"]
    assert updates and {e["vehicle_id"] for e in updates} == {busiest.vehicle_id}
    assert not [e for e in events if e["type"] == "rejected"]
    texts = [(e["stop_id"], e["kind"]) for e in events if e["type"] == "sms"]
    assert len(texts) == len(set(texts)) and {k for _, k in texts} == {"3_stops", "10_min"}
    assert sum(k == "10_min" for _, k in texts) == 50
    perf = {r["vehicle_id"]: r for r in store.rows("SELECT * FROM fleet_performance")}
    assert sum(r["stops_completed"] for r in perf.values()) == 50 and perf[busiest.vehicle_id]["reoptimizations"] == len(updates)
    assert perf[busiest.vehicle_id]["avg_behind_plan_min"] > max(r["avg_behind_plan_min"] for k, r in perf.items() if k != busiest.vehicle_id)
    snap = d.snapshot()
    assert all(v["done"] for v in snap["vans"]) and snap["plan_id"] == 1
