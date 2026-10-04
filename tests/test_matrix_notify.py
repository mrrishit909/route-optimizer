from __future__ import annotations

import sqlite3

import httpx

from routing.matrix import MatrixProvider, haversine_m
from routing.models import Point
from routing.notify import Notifier

A, B, C = Point(lat=27.95, lon=-82.46), Point(lat=28.00, lon=-82.40), Point(lat=27.90, lon=-82.50)


def osrm(log: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if "/route/" in request.url.path:
            return httpx.Response(200, json={"code": "Ok", "routes": [{"geometry": {"coordinates": [[-82.46, 27.95], [-82.43, 27.97], [-82.40, 28.0]]}}]})
        q = request.url.params
        n_src, n_dst = len(q["sources"].split(";")), len(q["destinations"].split(";"))
        return httpx.Response(200, json={"code": "Ok", "durations": [[100.4 + 10 * i + j for j in range(n_dst)] for i in range(n_src)],
                                         "distances": [[1000.6 + i + j for j in range(n_dst)] for i in range(n_src)]})
    return httpx.MockTransport(handler)


def test_table_is_fetched_once_then_served_from_cache() -> None:
    log: list[httpx.Request] = []
    m = MatrixProvider(sqlite3.connect(":memory:"), "https://osrm.test", osrm(log))
    dur, dist, source = m.table([A, B], [A, B])
    assert source == "osrm" and dur == [[100, 101], [110, 111]] and dist[0][1] == 1002 and len(log) == 1
    assert log[0].url.params["sources"] == "0;1" and log[0].url.params["destinations"] == "2;3"
    again = m.table([A, B], [A, B])
    assert again[2] == "cache" and again[0][0][1] == 101 and again[0][0][0] == 0 and len(log) == 1       # no second call
    # a point 3 m away rounds to the same key: still the cache
    assert m.table([Point(lat=27.95002, lon=-82.46001)], [B])[2] == "cache" and len(log) == 1
    # a new origin among known destinations: only its row is requested
    dur, _, source = m.table([A, C], [A, B])
    assert source == "osrm" and len(log) == 2 and log[1].url.params["sources"] == "0" and dur[0][1] == 101
    assert (m.calls, m.hits, m.misses) == (2, 4, 3)


def test_falls_back_to_straight_line_when_the_engine_is_down_and_does_not_cache_that() -> None:
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    m = MatrixProvider(sqlite3.connect(":memory:"), "https://osrm.test", down)
    dur, dist, source = m.table([A], [B])
    assert source == "haversine" and dist[0][0] == round(haversine_m(A, B) * 1.35) and dur[0][0] == round(dist[0][0] / (35 / 3.6))
    assert m.db.execute("SELECT count(*) FROM matrix_cache").fetchone()[0] == 0
    bad = httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "NoSegment"}))
    assert MatrixProvider(sqlite3.connect(":memory:"), "https://osrm.test", bad).table([A], [B])[2] == "haversine"
    assert 7_000 < haversine_m(A, B) < 9_000


def test_geometry_from_engine_or_straight_lines() -> None:
    m = MatrixProvider(sqlite3.connect(":memory:"), "https://osrm.test", osrm([]))
    assert m.geometry([A, B]) == [(27.95, -82.46), (27.97, -82.43), (28.0, -82.40)]
    off = MatrixProvider(sqlite3.connect(":memory:"), None)
    assert off.geometry([A, B]) == [(A.lat, A.lon), (B.lat, B.lon)]
    broken = MatrixProvider(sqlite3.connect(":memory:"), "https://osrm.test", httpx.MockTransport(lambda r: httpx.Response(200, json={"routes": []})))
    assert broken.geometry([A, B]) == [(A.lat, A.lon), (B.lat, B.lon)]


def test_twilio_notifier_statuses() -> None:
    seen: list[httpx.Request] = []

    def twilio(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201 if b"To=%2B15550100001" in request.content else 400, json={})

    n = Notifier("AC123", "secret", "+15550109999", httpx.MockTransport(twilio))
    assert n.send("+15550100001", "Your driver is about 10 minutes away.") == "sent"
    assert seen[0].url.path == "/2010-04-01/Accounts/AC123/Messages.json" and seen[0].headers["Authorization"].startswith("Basic ")
    assert b"From=%2B15550109999" in seen[0].content and b"Body=Your+driver" in seen[0].content
    assert n.send("+15550100002", "x") == "failed (HTTP 400)"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    assert Notifier("AC123", "secret", "+1", httpx.MockTransport(boom)).send("+1", "x") == "failed (ConnectError)"
    assert Notifier(None, None, None).send("+1", "x") == "not sent (no Twilio credentials)"
