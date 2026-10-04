"""Performance profile of the optimizer on the bundled 50-stop problem.

    python profile_solver.py            prints the table below and fails if a 3-second solve takes 5 seconds or more

The solver is a metaheuristic with a time limit: it reports the best plan found in that time, which is not proven
optimal. The table shows how the plan improves as the limit grows, with 30 s as a reference point.
"""
from __future__ import annotations

import cProfile
import io
import pstats
import time

from routing.data import bundled_matrix, mock_problem
from routing.solver import check, solve


def main() -> None:
    problem = mock_problem()
    dur, dist = bundled_matrix()
    print(f"{len(problem.stops)} stops, {len(problem.vehicles)} vans, OSRM road matrix {len(dur)}x{len(dur)}\n")
    print(f"{'limit':>6} {'wall s':>7} {'vans':>5} {'km':>7} {'van-hours':>10} {'dropped':>8} {'violations':>11}")
    rows = {}
    for limit in (0.5, 1, 3, 30):
        started = time.perf_counter()
        plan = solve(problem, dur, dist, limit)
        wall = time.perf_counter() - started
        rows[limit] = (wall, plan)
        print(f"{limit:>6} {wall:>7.2f} {len(plan.routes):>5} {plan.distance_m / 1000:>7.1f} {plan.duration_s / 3600:>10.2f} "
              f"{len(plan.dropped):>8} {len(check(problem, plan, dur)):>11}")
    wall3, plan3 = rows[3]
    assert wall3 < 5, f"3-second solve took {wall3:.2f} s"
    assert check(problem, plan3, dur) == [] and not plan3.dropped
    ref = rows[30][1]
    gap = (plan3.distance_m + 0.0) / ref.distance_m - 1
    print(f"\n3 s plan vs 30 s reference: distance {gap:+.1%}, van-hours {plan3.duration_s / ref.duration_s - 1:+.1%}")

    profiler = cProfile.Profile()
    profiler.enable()
    solve(problem, dur, dist, 1)
    profiler.disable()
    out = io.StringIO()
    pstats.Stats(profiler, stream=out).sort_stats("cumulative").print_stats(6)
    print("\nWhere a 1-second solve spends its time (cProfile, top entries):")
    print("\n".join(line for line in out.getvalue().splitlines() if line.strip())[:1500])
    print(f"\nOK: valid 50-stop plan in {wall3:.2f} s (limit 5 s)")


if __name__ == "__main__":
    main()
