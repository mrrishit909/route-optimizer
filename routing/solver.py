"""Vehicle routing with time windows and capacities (VRPTW + CVRP) on Google OR-Tools.

Node 0 is the depot, nodes 1..N the stops. For a live re-plan a vehicle starts from its current position, which is
appended as an extra node. Cost = seconds driven + seconds each van is out (so waiting is not free) + a fixed amount
per van used, so the solver prefers fewer vans and compact shifts. A stop may be left out only at a large penalty (ten times larger for priority stops).
"""
from __future__ import annotations

import time
from collections.abc import Sequence

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .models import DAY, Plan, Point, Problem, Route, Stop, Vehicle, Visit

Matrix = list[list[int]]
DROP_PENALTY, PRIORITY_DROP_PENALTY, VEHICLE_FIXED_COST, LATE_PENALTY_PER_S = 1_000_000, 10_000_000, 1_800, 20


def points_of(problem: Problem) -> list[Point]:
    return [problem.depot, *problem.stops]


def solve(problem: Problem, durations: Matrix, distances: Matrix, time_limit_s: float = 3.0, *,
          start_nodes: Sequence[int] | None = None, start_times: Sequence[int] | None = None, soft: bool = False) -> Plan:
    """durations/distances are indexed like [depot, *stops, *extra start nodes].

    soft=True is the live re-planning mode: every stop must be served, and a missed window or shift end is
    allowed but penalised per second, so the answer is "the least-late order", never "no answer".
    """
    try:
        return _solve(problem, durations, distances, time_limit_s, start_nodes, start_times, soft)
    except RuntimeError:
        raise
    except Exception as exc:        # OR-Tools raises a bare Exception when a constraint is impossible while building
        raise RuntimeError("no feasible plan: check shifts, capacities and time windows") from exc


def _solve(problem: Problem, durations: Matrix, distances: Matrix, time_limit_s: float, start_nodes: Sequence[int] | None,
           start_times: Sequence[int] | None, soft: bool) -> Plan:
    began = time.perf_counter()
    stops, vehicles = problem.stops, problem.vehicles
    starts = list(start_nodes) if start_nodes is not None else [0] * len(vehicles)
    manager = pywrapcp.RoutingIndexManager(len(durations), len(vehicles), starts, [0] * len(vehicles))
    routing = pywrapcp.RoutingModel(manager)
    service = [0] + [s.service_s for s in stops] + [0] * (len(durations) - len(stops) - 1)

    # matrices are handed to OR-Tools whole: a Python callback per arc evaluation was 80% of the solve time
    with_service = [[d + service[a] for d in row] for a, row in enumerate(durations)]
    routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitMatrix(durations))
    routing.SetFixedCostOfAllVehicles(0 if soft else VEHICLE_FIXED_COST)
    routing.AddDimension(routing.RegisterTransitMatrix(with_service), DAY, 2 * DAY, False, "Time")
    clock = routing.GetDimensionOrDie("Time")

    for n, stop in enumerate(stops, start=1):
        index = manager.NodeToIndex(n)
        if soft:
            clock.CumulVar(index).SetRange(stop.window_start, 2 * DAY)
            clock.SetCumulVarSoftUpperBound(index, stop.window_end, LATE_PENALTY_PER_S)
        else:
            clock.CumulVar(index).SetRange(stop.window_start, stop.window_end)
            routing.AddDisjunction([index], PRIORITY_DROP_PENALTY if stop.priority else DROP_PENALTY)
    for v, vehicle in enumerate(vehicles):
        earliest = start_times[v] if start_times is not None else vehicle.shift_start
        clock.CumulVar(routing.Start(v)).SetRange(earliest, 2 * DAY if soft else max(earliest, vehicle.shift_end))
        if soft:
            clock.SetCumulVarSoftUpperBound(routing.End(v), vehicle.shift_end, LATE_PENALTY_PER_S)
        else:
            clock.CumulVar(routing.End(v)).SetRange(earliest, vehicle.shift_end)
        # finish as early as possible, then leave the depot as late as that finish allows: no idle waiting on the road
        routing.AddVariableMinimizedByFinalizer(clock.CumulVar(routing.End(v)))
        routing.AddVariableMaximizedByFinalizer(clock.CumulVar(routing.Start(v)))
    clock.SetSpanCostCoefficientForAllVehicles(1)      # every second a van is out costs, driving or waiting

    pad = [0] * (len(durations) - len(stops) - 1)
    for name, loads, capacities in (      # integer units: 100 g and litres
            ("Weight", [0, *[round(s.weight_kg * 10) for s in stops], *pad], [round(v.capacity_kg * 10) for v in vehicles]),
            ("Volume", [0, *[round(s.volume_l) for s in stops], *pad], [round(v.capacity_l) for v in vehicles])):
        routing.AddDimensionWithVehicleCapacity(routing.RegisterUnaryTransitVector(loads), 0, capacities, True, name)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(int(time_limit_s * 1000))
    solution = routing.SolveWithParameters(params)
    if solution is None:
        raise RuntimeError("no feasible plan: check shifts, capacities and time windows")

    routes: list[Route] = []
    served: set[str] = set()
    for v, vehicle in enumerate(vehicles):
        index = routing.Start(v)
        start = solution.Min(clock.CumulVar(index))
        visits: list[Visit] = []
        metres, prev = 0, manager.IndexToNode(index)
        index = solution.Value(routing.NextVar(index))
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            stop = stops[node - 1]
            arrival = solution.Min(clock.CumulVar(index))
            visits.append(Visit(stop_id=stop.id, arrival=arrival, departure=arrival + stop.service_s,
                                travel_s=durations[prev][node], late_s=max(0, arrival - stop.window_end)))
            served.add(stop.id)
            metres += distances[prev][node]
            prev, index = node, solution.Value(routing.NextVar(index))
        if not visits:
            continue
        metres += distances[prev][0]
        by_id = {s.id: s for s in stops}
        routes.append(Route(vehicle_id=vehicle.id, start=start, end=solution.Min(clock.CumulVar(index)), visits=visits,
                            distance_m=metres, load_kg=round(sum(by_id[x.stop_id].weight_kg for x in visits), 1),
                            load_l=round(sum(by_id[x.stop_id].volume_l for x in visits), 1)))
    return Plan(routes=routes, dropped=[s.id for s in stops if s.id not in served],
                distance_m=sum(r.distance_m for r in routes), duration_s=sum(r.end - r.start for r in routes),
                solver_ms=round((time.perf_counter() - began) * 1000))


def check(problem: Problem, plan: Plan, durations: Matrix) -> list[str]:
    """Independent re-check of a plan against every constraint. Returns the violations (empty = valid)."""
    node = {s.id: n for n, s in enumerate(problem.stops, start=1)}
    stop: dict[str, Stop] = {s.id: s for s in problem.stops}
    vehicle: dict[str, Vehicle] = {v.id: v for v in problem.vehicles}
    errors: list[str] = []
    seen = [x.stop_id for r in plan.routes for x in r.visits]
    if sorted(seen + plan.dropped) != sorted(stop):
        errors.append("every stop must be visited exactly once or listed as dropped")
    if len({r.vehicle_id for r in plan.routes}) != len(plan.routes):
        errors.append("a vehicle has two routes")
    for r in plan.routes:
        v = vehicle[r.vehicle_id]
        if sum(stop[x.stop_id].weight_kg for x in r.visits) > v.capacity_kg + 1e-6:
            errors.append(f"{v.id}: over weight capacity")
        if sum(stop[x.stop_id].volume_l for x in r.visits) > v.capacity_l + 1e-6:
            errors.append(f"{v.id}: over volume capacity")
        if r.start < v.shift_start or r.end > v.shift_end:
            errors.append(f"{v.id}: outside its shift")
        clock, at = r.start, 0
        for x in r.visits:
            s = stop[x.stop_id]
            if x.arrival < clock + durations[at][node[s.id]]:
                errors.append(f"{v.id}: reaches {s.id} faster than the road allows")
            if not s.window_start <= x.arrival <= s.window_end:
                errors.append(f"{v.id}: {s.id} outside its window")
            if x.departure != x.arrival + s.service_s:
                errors.append(f"{v.id}: wrong service time at {s.id}")
            clock, at = x.departure, node[s.id]
        if r.end < clock + durations[at][0]:
            errors.append(f"{v.id}: returns to the depot faster than the road allows")
    return errors
