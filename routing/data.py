"""Mock data set: one depot, 50 delivery stops and 5 vans around Tampa, Florida. Coordinates are random points in
a box over the city (seeded, so always the same 50); nothing here is a real customer or address."""
from __future__ import annotations

import json
import random
from pathlib import Path

from .models import Point, Problem, Stop, Vehicle

DEPOT = Point(lat=27.9659, lon=-82.4402)            # near Ybor City
MATRIX_FILE = Path(__file__).resolve().parent.parent / "data" / "tampa_matrix.json"
H = 3600


def mock_problem(n_stops: int = 50, seed: int = 11) -> Problem:
    rng = random.Random(seed)
    stops = []
    for i in range(1, n_stops + 1):
        priority = i % 4 == 0                       # every 4th stop has a promised 2-hour window
        start = rng.choice([9, 11, 13, 15]) * H if priority else 8 * H
        stops.append(Stop(id=f"S{i:02d}", lat=round(rng.uniform(27.93, 28.07), 5), lon=round(rng.uniform(-82.52, -82.38), 5),
                          weight_kg=rng.randint(5, 60), volume_l=rng.randint(40, 600), service_s=rng.choice([300, 420, 600]),
                          window_start=start, window_end=start + 2 * H if priority else 18 * H, priority=priority,
                          phone=f"+1555010{i:04d}"))  # 555-01xx numbers are reserved for fiction
    vehicles = [Vehicle(id=f"VAN-{k}", capacity_kg=450, capacity_l=4500, shift_start=(10 if k == 5 else 8) * H,
                        shift_end=(18 if k == 5 else 17) * H) for k in range(1, 6)]
    return Problem(depot=DEPOT, vehicles=vehicles, stops=stops)


def bundled_matrix() -> tuple[list[list[int]], list[list[int]]]:
    """The OSRM duration/distance matrix for mock_problem(), recorded once so tests and profiling run offline."""
    data = json.loads(MATRIX_FILE.read_text())
    return data["durations"], data["distances"]
