from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import line_problem
from routing.server import Hub, app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.state.hub = Hub(":memory:", None)          # no network: straight-line geometry, fallback travel times
    with TestClient(app) as c:
        yield c


def test_rest_endpoints(client: TestClient) -> None:
    assert client.get("/api/plans/current").status_code == 401
    assert client.get("/api/plans/current", headers=AUTH).status_code == 404
    snap = client.post("/api/plans", headers=AUTH, json={"time_limit_s": 1}).json()        # the bundled 50 stops
    assert snap["matrix_source"] == "cache" and snap["dropped"] == [] and sum(len(v["route"]["visits"]) for v in snap["vans"]) == 50
    assert client.get("/api/plans/current", headers=AUTH).json()["plan_id"] == snap["plan_id"]
    assert client.post("/api/plans", headers=AUTH, json={"problem": {"depot": {"lat": 99, "lon": 0}, "vehicles": [], "stops": []}}).status_code == 422
    perf = client.get("/api/fleet/performance", headers=AUTH).json()
    assert len(perf["vehicles"]) == len(snap["vans"]) and perf["plans"][0]["stops"] == 50 and perf["sms"] == []


def test_driver_and_dispatch_sockets(client: TestClient) -> None:
    problem = line_problem(1).model_dump()                                # one stop, 5.6 minutes from the depot
    snap = client.post("/api/plans", headers=AUTH, json={"problem": problem, "time_limit_s": 1}).json()
    route = snap["vans"][0]["route"]
    t0 = route["start"]
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/dispatch?token=wrong") as ws:
        ws.receive_json()
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/driver/VAN-99?token=test-token") as ws:
        ws.receive_json()
    with client.websocket_connect("/ws/dispatch?token=test-token") as board, \
            client.websocket_connect("/ws/driver/VAN-1?token=test-token") as driver:
        assert board.receive_json()["type"] == "plan"
        assert driver.receive_json()["route"]["visits"] == route["visits"]
        driver.send_json({"type": "position", "lat": 28.0, "lon": -82.5, "t": t0})
        got = [board.receive_json(), board.receive_json()]
        assert [e["type"] for e in got] == ["position", "sms"] and got[1]["kind"] == "10_min" and got[0]["vehicle_id"] == "VAN-1"
        driver.send_json({"type": "teleport", "t": t0})                                   # not a known message type
        assert driver.receive_json()["type"] == "rejected"
        driver.send_json({"type": "arrived", "t": t0})                                     # stop_id missing
        assert board.receive_json()["type"] == "rejected"
        assert driver.receive_json()["type"] == "rejected"
        driver.send_json({"type": "position", "lat": 28.0, "lon": -82.5, "t": t0 + 16 * 60})   # 16 minutes late: re-plan
        events = [board.receive_json(), board.receive_json()]
        assert [e["type"] for e in events] == ["position", "route_update"]
        pushed = driver.receive_json()                                                    # the driver gets the new route too
        assert pushed["type"] == "route_update" and pushed["visits"] == events[1]["visits"]
        driver.send_json({"type": "arrived", "stop_id": "S1", "t": t0 + 22 * 60})
        assert board.receive_json()["type"] == "arrived"
        driver.send_json({"type": "completed", "stop_id": "S1", "t": t0 + 27 * 60})
        assert board.receive_json()["type"] == "completed"
    perf = client.get("/api/fleet/performance", headers=AUTH).json()
    assert perf["vehicles"][0]["stops_completed"] == 1 and perf["vehicles"][0]["reoptimizations"] == 1 and len(perf["sms"]) == 1
