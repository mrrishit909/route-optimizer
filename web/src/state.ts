// The dashboard's state is the plan snapshot plus every event applied in order: the same reducer serves the live
// WebSocket feed and the recorded replay.
export interface Stop { id: string; lat: number; lon: number; weight_kg: number; volume_l: number; service_s: number; window_start: number; window_end: number; priority: boolean }
export interface Vehicle { id: string; capacity_kg: number; capacity_l: number; shift_start: number; shift_end: number }
export interface Visit { stop_id: string; arrival: number; departure: number; travel_s: number; late_s: number }
export interface Route { vehicle_id: string; start: number; end: number; visits: Visit[]; distance_m: number; load_kg: number; load_l: number; geometry: [number, number][] }
export interface Snapshot {
  problem: { depot: { lat: number; lon: number }; vehicles: Vehicle[]; stops: Stop[] }; dropped: string[]; solver_ms: number; matrix_source: string;
  vans: { vehicle_id: string; route: Route; next: number; pos: { lat: number; lon: number } | null; delay_s: number; reoptimizations: number; done: boolean }[];
}
export interface Ev { type: string; t: number; vehicle_id?: string; lat?: number; lon?: number; delay_s?: number; stop_id?: string; late_s?: number;
  visits?: Visit[]; geometry?: [number, number][]; reason?: string; reordered?: boolean; late_stops?: string[]; kind?: string; to?: string; body?: string; status?: string }
export interface Van { id: string; color: string; vehicle: Vehicle; route: Route; pos: [number, number]; delay: number; done: Record<string, number>; late: Record<string, number>; replans: number[]; finished: boolean }
export interface LogLine { t: number; kind: "replan" | "late" | "sms"; van?: string; text: string }
export interface State { snapshot: Snapshot; vans: Van[]; log: LogLine[]; now: number }

export const COLORS = ["#3987e5", "#d95926", "#3aa876", "#b05cc8", "#c9a227", "#d1477a"];
export const hhmm = (s: number) => `${String(Math.floor(s / 3600)).padStart(2, "0")}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;

export function initial(snapshot: Snapshot): State {
  const vehicles = Object.fromEntries(snapshot.problem.vehicles.map((v) => [v.id, v]));
  return {
    snapshot, log: [], now: Math.min(...snapshot.vans.map((v) => v.route.start)),
    vans: snapshot.vans.map((v, i) => ({
      id: v.vehicle_id, color: COLORS[i % COLORS.length], vehicle: vehicles[v.vehicle_id], route: structuredClone(v.route),
      pos: v.pos ? [v.pos.lat, v.pos.lon] : [snapshot.problem.depot.lat, snapshot.problem.depot.lon], delay: v.delay_s, done: {}, late: {}, replans: [], finished: v.done,
    })),
  };
}

/** Apply one event in place (the caller owns the state object and re-renders after a batch). */
export function apply(s: State, e: Ev): void {
  s.now = Math.max(s.now, e.t);
  const van = s.vans.find((v) => v.id === e.vehicle_id);
  if (e.type === "position" && van) { van.pos = [e.lat!, e.lon!]; van.delay = e.delay_s ?? 0; }
  else if (e.type === "arrived" && van && e.late_s) {
    van.late[e.stop_id!] = e.late_s;
    s.log.unshift({ t: e.t, kind: "late", van: van.id, text: `${van.id} reached ${e.stop_id} ${Math.round(e.late_s / 60)} min after its window closed` });
  } else if (e.type === "completed" && van) {
    van.done[e.stop_id!] = e.t;
    van.finished = Object.keys(van.done).length >= van.route.visits.length;
    if (van.finished) van.delay = 0;
  } else if (e.type === "route_update" && van) {
    van.route = { ...van.route, visits: e.visits!, geometry: e.geometry!, end: e.visits!.length ? van.route.end : van.route.end };
    van.replans.push(e.t);
    s.log.unshift({ t: e.t, kind: "replan", van: van.id, text: `${van.id} re-planned (${e.reason})${e.reordered ? ": stop order changed" : ": same order, new times"}${e.late_stops?.length ? `; expected late: ${e.late_stops.join(", ")}` : ""}` });
  } else if (e.type === "sms") {
    s.log.unshift({ t: e.t, kind: "sms", text: `Text to ${e.stop_id}: “${e.body}” (${e.status})` });
  }
}
