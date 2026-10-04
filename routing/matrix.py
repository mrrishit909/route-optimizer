"""Travel-time matrix from OSRM (OpenStreetMap roads), behind a SQLite cache.

Every (from, to) pair is cached by coordinates rounded to 4 decimals, so overlapping requests (the same customers
tomorrow, a van re-planning among stops already seen) cost no external call. If OSRM cannot be reached the
matrix falls back to straight-line distance x 1.35 at 35 km/h, and says so in `source`.
"""
from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Sequence

import httpx

from .models import Point

Matrix = list[list[int]]
DETOUR, SPEED_MS = 1.35, 35 / 3.6
MAX_AGE_S = 7 * 24 * 3600
SCHEMA = """CREATE TABLE IF NOT EXISTS matrix_cache (
    a_lat REAL, a_lon REAL, b_lat REAL, b_lon REAL, duration_s INTEGER NOT NULL, distance_m INTEGER NOT NULL,
    fetched_at REAL NOT NULL, PRIMARY KEY (a_lat, a_lon, b_lat, b_lon)) WITHOUT ROWID"""


def haversine_m(a: Point, b: Point) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b.lon - a.lon) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


class MatrixProvider:
    def __init__(self, db: sqlite3.Connection, osrm_url: str | None = "https://router.project-osrm.org",
                 transport: httpx.BaseTransport | None = None) -> None:
        self.db, self.osrm_url, self.transport = db, osrm_url, transport
        self.calls = self.hits = self.misses = 0
        db.execute(SCHEMA)

    def _cached(self, a: Point, b: Point) -> tuple[int, int] | None:
        row = self.db.execute("SELECT duration_s, distance_m FROM matrix_cache WHERE a_lat=? AND a_lon=? AND b_lat=? AND b_lon=? "
                              "AND fetched_at > ?", (*a.key(), *b.key(), time.time() - MAX_AGE_S)).fetchone()
        return (row[0], row[1]) if row else None

    def seed(self, points: Sequence[Point], durations: Matrix, distances: Matrix) -> None:
        """Load a known matrix into the cache (used for the bundled 50-stop data set and by tests)."""
        now = time.time()
        self.db.executemany("INSERT OR REPLACE INTO matrix_cache VALUES (?,?,?,?,?,?,?)", [
            (*a.key(), *b.key(), durations[i][j], distances[i][j], now) for i, a in enumerate(points) for j, b in enumerate(points)])
        self.db.commit()

    def _fetch(self, sources: Sequence[Point], destinations: Sequence[Point]) -> tuple[Matrix, Matrix]:
        coords = ";".join(f"{p.lon},{p.lat}" for p in [*sources, *destinations])
        params = {"annotations": "duration,distance", "sources": ";".join(map(str, range(len(sources)))),
                  "destinations": ";".join(str(len(sources) + j) for j in range(len(destinations)))}
        with httpx.Client(timeout=20, transport=self.transport) as client:
            data = client.get(f"{self.osrm_url}/table/v1/driving/{coords}", params=params).raise_for_status().json()
        if data.get("code") != "Ok":
            raise httpx.HTTPError(f"OSRM said {data.get('code')}")
        self.calls += 1
        return ([[round(x) for x in row] for row in data["durations"]], [[round(x) for x in row] for row in data["distances"]])

    def table(self, sources: Sequence[Point], destinations: Sequence[Point]) -> tuple[Matrix, Matrix, str]:
        """(durations in seconds, distances in metres, where the numbers came from)."""
        dur: Matrix = [[0] * len(destinations) for _ in sources]
        dist: Matrix = [[0] * len(destinations) for _ in sources]
        missing_rows: list[int] = []
        for i, a in enumerate(sources):
            for j, b in enumerate(destinations):
                hit = (0, 0) if a.key() == b.key() else self._cached(a, b)
                if hit is None:
                    missing_rows.append(i)
                    break
                dur[i][j], dist[i][j] = hit
        self.hits += len(sources) - len(missing_rows)
        self.misses += len(missing_rows)
        if not missing_rows:
            return dur, dist, "cache"
        rows = [sources[i] for i in missing_rows]
        source = "osrm"
        try:
            if self.osrm_url is None:
                raise httpx.HTTPError("no routing engine configured")
            got_dur, got_dist = self._fetch(rows, destinations)
        except httpx.HTTPError:
            source = "haversine"        # not cached: a later request should try the road network again
            got_dist = [[round(haversine_m(a, b) * DETOUR) for b in destinations] for a in rows]
            got_dur = [[round(d / SPEED_MS) for d in row] for row in got_dist]
        for k, i in enumerate(missing_rows):
            dur[i], dist[i] = got_dur[k], got_dist[k]
        if source == "osrm":
            now = time.time()
            self.db.executemany("INSERT OR REPLACE INTO matrix_cache VALUES (?,?,?,?,?,?,?)", [
                (*sources[i].key(), *b.key(), got_dur[k][j], got_dist[k][j], now)
                for k, i in enumerate(missing_rows) for j, b in enumerate(destinations)])
            self.db.commit()
        return dur, dist, source

    def geometry(self, points: Sequence[Point]) -> list[tuple[float, float]]:
        """Road path through the points for drawing on the map; straight lines if the engine is unavailable."""
        straight = [(p.lat, p.lon) for p in points]
        if self.osrm_url is None or len(points) < 2:
            return straight
        coords = ";".join(f"{p.lon},{p.lat}" for p in points)
        try:
            with httpx.Client(timeout=20, transport=self.transport) as client:
                data = client.get(f"{self.osrm_url}/route/v1/driving/{coords}",
                                  params={"overview": "full", "geometries": "geojson"}).raise_for_status().json()
            self.calls += 1
            return [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]
        except (httpx.HTTPError, KeyError, IndexError):
            return straight
