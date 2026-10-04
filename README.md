# Route optimizer

Plans multi-stop delivery routes for a fleet (who goes where, in what order, arriving when), then follows the vans
live: a driver who falls more than 15 minutes behind gets the rest of the route re-planned and pushed to the phone,
and customers get a text when the driver is 3 stops or 10 minutes away.

Built from the "Omnichannel Logistics & Route-Optimization Engine" blueprint as an MVP.
**Live demo (a recorded simulated day, replayed in the browser):** https://mrrishit909.github.io/projects/route-optimizer/demo/

## Run it

```bash
printf 'ROUTES_API_TOKEN=%s\n' "$(openssl rand -hex 24)" > .env      # never committed
docker compose up --build -d                                         # gateway + dashboard on http://localhost:8000
docker compose run --rm test                                         # 20 tests (97% coverage), then the solver profile

# in the dashboard: sign in with the token, press "Plan the 50-stop sample", then send simulated drivers:
python3 -m venv venv && venv/bin/pip install -e .
venv/bin/python -m routing.simulate --url ws://localhost:8000 --token "$(cut -d= -f2 .env)" --speed 240 \
    --jam-van VAN-1 --jam-at 37397
```

`python profile_solver.py` profiles the solver; `python -m routing.simulate --record web/public/data/replay.json` re-records the demo.

## How it works

| Part | File | What it does |
|---|---|---|
| Solver | `routing/solver.py` | Vehicle routing with time windows and capacities on Google OR-Tools. Inputs: fleet size, weight and volume capacity per van, driver shifts, service time per stop, delivery windows, priority flags. Cost = seconds driven + seconds each van is out + a fixed cost per van used. A stop is dropped only at a large penalty (10x larger for priority stops). `check()` re-verifies any plan against every constraint with separate code. |
| Travel times | `routing/matrix.py` | OSRM table API (OpenStreetMap roads) for durations and distances, behind a SQLite cache keyed by coordinate pair (rounded to about 11 m). Only rows that are not cached are requested. Falls back to straight-line estimates, labelled as such, if the engine is unreachable. |
| Live dispatch | `routing/dispatch.py` | Per van: position, next stop, projected arrival, minutes behind plan. Over 15 minutes behind → the van's remaining stops are re-optimised from its current position and time (windows become soft: least-late order). Texts at "3 stops away" and "10 minutes away", once per customer. |
| Gateway | `routing/server.py` | FastAPI. `WS /ws/driver/{van}` takes position / arrived / completed messages (validated), `WS /ws/dispatch` streams every event to dashboards, REST for plans and fleet metrics. Re-planning runs off the event loop. Token on every route and socket. |
| Texts | `routing/notify.py` | Twilio REST API. With no credentials configured, messages are written to the outbox and marked "not sent". |
| History | `routing/store.py` | SQLite: plans, planned vs actual time at every stop, events, text outbox, and a `fleet_performance` view (stops completed, on-time %, average minutes behind plan, re-plans per van). |
| Simulator | `routing/simulate.py` | Drivers that follow the road geometry and report like a phone, with an optional traffic jam; offline (deterministic) or over WebSockets against a running server. |
| Dashboard | `web/` | React + TypeScript + Leaflet: live map with a coloured road path and moving marker per van; per-driver timeline with shift, stops, load meters, behind-schedule bar and re-plan markers; event log. |

## Results on the bundled data

50 invented stops (random points over Tampa, 12 with a promised 2-hour window), 5 vans (450 kg / 4,500 l, 8:00 to 17:00, one
10:00 to 18:00), real road travel times from OSRM (`data/tampa_matrix.json`, fetched 2026-10-04).

`python profile_solver.py`:

| Time limit | Wall time | Vans used | Distance | Van-hours | Dropped | Violations |
|---|---|---|---|---|---|---|
| 0.5 s | 0.50 s | 4 | 196.2 km | 11.13 | 0 | 0 |
| 1 s | 1.00 s | 4 | 196.2 km | 11.13 | 0 | 0 |
| **3 s (default)** | **3.00 s** | 4 | 193.5 km | 11.08 | 0 | 0 |
| 30 s | 30.00 s | 4 | 193.5 km | 11.08 | 0 | 0 |

The 3-second plan is the same as the 30-second one. That is "no better plan found in ten times the search", not a proof
of optimality: the solver is a metaheuristic (guided local search). The 0.5 s row varies a little between machines.
Passing the matrices to OR-Tools directly instead of through Python callbacks is what made this possible: before that
change the 1-second plan was 210.6 km.

Recorded simulated day (`routing.simulate`, van 1 held in a 40-minute jam from 10:23):

- All 50 stops delivered. Van 1 was re-planned 3 times; the second re-plan changed the stop order to bring a priority stop
  forward. That stop was still reached 8 minutes after its window closed: the jam outlasted the re-plan. 49 of 50 on time.
- 93 customer texts generated (none sent: no Twilio credentials).
- 9 calls to OSRM for the whole day (4 route shapes at planning, then re-plan rows and shapes); the 51 x 51 matrix came from the cache.
- The same scenario over real WebSockets against the running server (300x speed) gave the same outcome.

## Not done, and caveats

- **No live traffic.** OSRM's public server knows road speeds, not today's congestion. Delays are detected from the vans'
  own progress. Google Maps or a traffic-aware engine would slot in behind `MatrixProvider`.
- **Twilio was never called for real**: the client is tested against a fake HTTP transport only.
- The public OSRM demo server is for light use; production needs a self-hosted OSRM (add it to `docker-compose.yml`).
- A re-plan only reorders the delayed van's own remaining stops (its parcels are already on board). It never moves stops
  between vans, and it never drops a stop: a window that cannot be met is reported as late.
- The projected arrival between stops is the planned leg time scaled by straight-line distance remaining, not a new routing call.
- Live state (which van is where) is in memory in one process: a restart loses the day in progress, and there is no
  horizontal scaling. History is in SQLite. One shared token; no driver accounts.
- Simulation speeds above about 300x distort results: a re-plan takes about a second of real time.
- Map tiles come from openstreetmap.org (fine for a demo; production needs a tile provider).

Data: road network © OpenStreetMap contributors (ODbL), via the OSRM demo server.
