import { useEffect, useRef, useState } from "react";
import { MapView } from "./MapView";
import { type Ev, type Snapshot, type State, type Van, apply, hhmm, initial } from "./state";

const STATIC = import.meta.env.VITE_STATIC === "1";
const DAY0 = 8 * 3600, DAY1 = 18 * 3600;
const pct = (t: number) => `${Math.max(0, Math.min(100, ((t - DAY0) / (DAY1 - DAY0)) * 100))}%`;

function Timeline({ van, now, stops }: { van: Van; now: number; stops: Record<string, { priority: boolean }> }) {
  const done = Object.keys(van.done).length, total = van.route.visits.length;
  const behind = Math.round(van.delay / 60);
  const level = van.delay > 900 ? "bad" : van.delay > 300 ? "warn" : "ok";
  return (
    <div className="van-row">
      <div className="van-head">
        <b className="badge" style={{ background: van.color }}>{van.id}</b>
        <span>{done}/{total} stops</span>
        {van.finished ? <span className="pill ok">finished</span> : behind > 0 ? <span className={`pill ${level}`}>{behind} min behind</span> : now >= van.route.start ? <span className="pill ok">on time</span> : <span className="pill">starts {hhmm(van.route.start)}</span>}
        {van.replans.length > 0 && <span className="pill warn">re-planned ×{van.replans.length}</span>}
      </div>
      <div className="meters">
        {([["kg", van.route.load_kg, van.vehicle.capacity_kg], ["litres", van.route.load_l, van.vehicle.capacity_l]] as [string, number, number][]).map(([unit, load, cap]) => (
          <label key={unit}>{Math.round(load)} / {Math.round(cap)} {unit}<meter min={0} max={cap} high={cap * 0.9} value={load} /></label>
        ))}
      </div>
      <div className="track" aria-label={`${van.id} schedule from 08:00 to 18:00`}>
        <i className="shift" style={{ left: pct(van.vehicle.shift_start), right: `calc(100% - ${pct(van.vehicle.shift_end)})` }} />
        <i className="span" style={{ left: pct(van.route.start), right: `calc(100% - ${pct(van.route.visits.at(-1)?.departure ?? van.route.end)})`, background: van.color }} />
        {van.delay > 0 && !van.finished && <i className={`behind ${level}`} style={{ left: pct(now - van.delay), right: `calc(100% - ${pct(now)})` }} title={`${behind} minutes behind plan`} />}
        {van.route.visits.map((x) => (
          <i key={x.stop_id} title={`${x.stop_id} ${hhmm(van.done[x.stop_id] ?? x.arrival)}${van.late[x.stop_id] ? " late" : ""}`}
             className={`tick ${van.done[x.stop_id] !== undefined ? "done" : ""} ${van.late[x.stop_id] !== undefined || x.late_s > 0 ? "late" : ""} ${stops[x.stop_id].priority ? "prio" : ""}`}
             style={{ left: pct(van.done[x.stop_id] ?? x.arrival), borderColor: van.color }} />
        ))}
        {van.replans.map((t) => <i key={t} className="replan" style={{ left: pct(t) }} title={`re-planned at ${hhmm(t)}`}>⟳</i>)}
        <i className="now" style={{ left: pct(now) }} />
      </div>
    </div>
  );
}

export function App() {
  const state = useRef<State | null>(null);
  const [version, setVersion] = useState(0);                 // bumped after each batch of events
  const [error, setError] = useState("");
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState(240);
  const [signedIn, setSignedIn] = useState(STATIC || !!sessionStorage.getItem("token"));
  const replay = useRef<{ snapshot: Snapshot; events: Ev[]; cursor: number; clock: number } | null>(null);
  const token = () => sessionStorage.getItem("token") ?? "";

  // recorded replay: advance a simulated clock and apply the events that are due
  useEffect(() => {
    if (!STATIC) return;
    fetch("./data/replay.json").then((r) => r.json()).then((d: { snapshot: Snapshot; events: Ev[] }) => {
      state.current = initial(d.snapshot);
      replay.current = { ...d, cursor: 0, clock: state.current.now - 120 };
      setVersion((v) => v + 1);
    }).catch((e) => setError(String(e)));
  }, []);
  useEffect(() => {
    if (!STATIC || !playing) return;
    const id = setInterval(() => {
      const r = replay.current, s = state.current;
      if (!r || !s) return;
      r.clock = Math.min(r.clock + speed * 0.25, DAY1);
      while (r.cursor < r.events.length && r.events[r.cursor].t <= r.clock) apply(s, r.events[r.cursor++]);
      s.now = Math.max(s.now, r.clock);
      if (r.cursor >= r.events.length) setPlaying(false);
      setVersion((v) => v + 1);
    }, 250);
    return () => clearInterval(id);
  }, [playing, speed]);
  const seek = (t: number) => {
    const r = replay.current;
    if (!r) return;
    const s = initial(r.snapshot);
    r.cursor = 0;
    r.clock = t;
    while (r.cursor < r.events.length && r.events[r.cursor].t <= t) apply(s, r.events[r.cursor++]);
    s.now = t;
    state.current = s;
    setVersion((v) => v + 1);
  };

  // live: snapshot over REST, then every event over the dispatch WebSocket
  useEffect(() => {
    if (STATIC || !signedIn) return;
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dispatch?token=${encodeURIComponent(token())}`);
    ws.onmessage = (m) => {
      const e = JSON.parse(m.data);
      if (e.type === "plan") state.current = initial(e as Snapshot);
      else if (state.current) apply(state.current, e as Ev);
      setVersion((v) => v + 1);
    };
    ws.onclose = (c) => { if (c.code === 4401) { sessionStorage.removeItem("token"); setSignedIn(false); setError("Wrong API token"); } else setError("Connection to the server closed. Reload to reconnect."); };
    return () => { ws.onclose = null; ws.close(); };
  }, [signedIn]);
  const newPlan = async () => {
    setError("");
    const r = await fetch("/api/plans", { method: "POST", headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" }, body: "{}" });
    if (!r.ok) setError(`Could not create a plan (${r.status})`);
  };

  if (!signedIn)
    return (
      <form className="signin" onSubmit={(e) => { e.preventDefault(); sessionStorage.setItem("token", String(new FormData(e.currentTarget).get("token"))); setError(""); setSignedIn(true); }}>
        <h1>Dispatch Control</h1>
        <label>API token<input name="token" type="password" required autoComplete="off" /></label>
        <button>Connect</button>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
    );

  const s = state.current;
  const stops = s ? Object.fromEntries(s.snapshot.problem.stops.map((x) => [x.id, x])) : {};
  const delivered = s ? s.vans.reduce((n, v) => n + Object.keys(v.done).length, 0) : 0;
  return (
    <div className="app">
      <header>
        <h1>Dispatch Control</h1>
        {s && <span className="clock">{hhmm(s.now)}</span>}
        {s && <span className="dim">{delivered}/{s.snapshot.problem.stops.length} delivered · {s.vans.length} vans · {Math.round(s.vans.reduce((n, v) => n + v.route.distance_m, 0) / 1000)} km planned in {(s.snapshot.solver_ms / 1000).toFixed(1)} s</span>}
        <span className="controls">
          {STATIC ? <>
            <button onClick={() => { if (replay.current && replay.current.cursor >= replay.current.events.length) seek(DAY0 + 1.9 * 3600); setPlaying((p) => !p); }}>{playing ? "Pause" : "Play"}</button>
            <label>Speed <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>{[60, 240, 600, 1800].map((x) => <option key={x} value={x}>{x}×</option>)}</select></label>
            <input type="range" min={DAY0} max={DAY1} step={60} value={s?.now ?? DAY0} onChange={(e) => seek(Number(e.target.value))} aria-label="Time of day" />
          </> : <button onClick={newPlan}>Plan the 50-stop sample</button>}
        </span>
      </header>
      {STATIC && <p className="banner">Demo: a recorded simulation. 50 invented stops on real Tampa roads; van 1 is held in a 40-minute traffic jam from 10:23. No real customers, and no texts were sent.</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {!s ? <p className="empty">{STATIC ? "Loading…" : "No live plan yet. Create one, then start the simulator (see README)."}</p> : (
        <div className="body">
          <MapView state={s} version={version} />
          <div className="side">
            <section aria-label="Driver timelines">
              <h2>Drivers <small>08:00 to 18:00 · ring = priority window · red = late</small></h2>
              {s.vans.map((v) => <Timeline key={v.id} van={v} now={s.now} stops={stops} />)}
            </section>
            <section aria-label="Event log" className="log">
              <h2>Events <small>{s.log.filter((l) => l.kind === "sms").length} texts · {s.log.filter((l) => l.kind === "replan").length} re-plans</small></h2>
              <ol>{s.log.slice(0, 60).map((l, i) => <li key={`${l.t}-${i}`} className={l.kind}><time>{hhmm(l.t)}</time> {l.text}</li>)}</ol>
            </section>
          </div>
        </div>
      )}
    </div>
  );
}
