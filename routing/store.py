"""SQLite persistence: plans, what actually happened at every stop, events, text messages, and the
fleet_performance view that turns the history into per-vehicle metrics."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .models import Plan, Problem

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT (datetime('now')), problem TEXT NOT NULL, plan TEXT NOT NULL,
    stops INTEGER NOT NULL, dropped INTEGER NOT NULL, distance_m INTEGER NOT NULL, solver_ms INTEGER NOT NULL, matrix_source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visits (
    plan_id INTEGER NOT NULL REFERENCES plans(id), vehicle_id TEXT NOT NULL, stop_id TEXT NOT NULL,
    planned_arrival INTEGER NOT NULL, window_end INTEGER NOT NULL, actual_arrival INTEGER, actual_departure INTEGER,
    PRIMARY KEY (plan_id, stop_id));
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL, t INTEGER NOT NULL, vehicle_id TEXT, type TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sms_outbox (
    id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL, t INTEGER NOT NULL, stop_id TEXT NOT NULL, kind TEXT NOT NULL,
    to_number TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, UNIQUE (plan_id, stop_id, kind));
CREATE VIEW IF NOT EXISTS fleet_performance AS
SELECT v.vehicle_id,
       count(DISTINCT v.plan_id)                                              AS days,
       count(*)                                                               AS stops_planned,
       count(v.actual_arrival)                                                AS stops_completed,
       sum(v.actual_arrival <= v.window_end)                                  AS on_time,
       round(100.0 * sum(v.actual_arrival <= v.window_end) / max(count(v.actual_arrival), 1), 1) AS on_time_pct,
       round(avg(max(v.actual_arrival - v.planned_arrival, 0)) / 60.0, 1)     AS avg_behind_plan_min,
       (SELECT count(*) FROM events e WHERE e.vehicle_id = v.vehicle_id AND e.type = 'route_update') AS reoptimizations
FROM visits v GROUP BY v.vehicle_id;
"""


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)   # one writer at a time: the dispatcher holds a lock
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def save_plan(self, problem: Problem, plan: Plan) -> int:
        cur = self.db.execute(
            "INSERT INTO plans (problem, plan, stops, dropped, distance_m, solver_ms, matrix_source) VALUES (?,?,?,?,?,?,?)",
            (problem.model_dump_json(), plan.model_dump_json(), len(problem.stops), len(plan.dropped), plan.distance_m,
             plan.solver_ms, plan.matrix_source))
        plan_id = int(cur.lastrowid or 0)
        window = {s.id: s.window_end for s in problem.stops}
        self.db.executemany("INSERT INTO visits (plan_id, vehicle_id, stop_id, planned_arrival, window_end) VALUES (?,?,?,?,?)",
                            [(plan_id, r.vehicle_id, x.stop_id, x.arrival, window[x.stop_id]) for r in plan.routes for x in r.visits])
        self.db.commit()
        return plan_id

    def event(self, plan_id: int, t: int, vehicle_id: str | None, kind: str, detail: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO events (plan_id, t, vehicle_id, type, detail) VALUES (?,?,?,?,?)",
                        (plan_id, t, vehicle_id, kind, json.dumps(detail)))
        self.db.commit()

    def arrived(self, plan_id: int, stop_id: str, t: int) -> None:
        self.db.execute("UPDATE visits SET actual_arrival = ? WHERE plan_id = ? AND stop_id = ?", (t, plan_id, stop_id))
        self.db.commit()

    def departed(self, plan_id: int, stop_id: str, t: int) -> None:
        self.db.execute("UPDATE visits SET actual_departure = ? WHERE plan_id = ? AND stop_id = ?", (t, plan_id, stop_id))
        self.db.commit()

    def sms(self, plan_id: int, t: int, stop_id: str, kind: str, to: str, body: str, status: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO sms_outbox (plan_id, t, stop_id, kind, to_number, body, status) VALUES (?,?,?,?,?,?,?)",
                        (plan_id, t, stop_id, kind, to, body, status))
        self.db.commit()

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, params)]
