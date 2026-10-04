import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useEffect, useRef } from "react";
import type { State } from "./state";

/** Leaflet map: depot, stops, one coloured road path per van, and a moving marker per van. */
export function MapView({ state, version }: { state: State; version: number }) {
  const el = useRef<HTMLDivElement>(null);
  const layers = useRef<{ map: L.Map; stops: Record<string, L.CircleMarker>; routes: Record<string, L.Polyline>; vans: Record<string, L.Marker>; geo: Record<string, unknown> } | null>(null);

  useEffect(() => {
    const { problem } = state.snapshot;
    const map = L.map(el.current!, { zoomSnap: 0.25 });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(map);
    map.fitBounds(L.latLngBounds(problem.stops.map((s) => [s.lat, s.lon] as [number, number])).pad(0.06));
    L.marker([problem.depot.lat, problem.depot.lon], { icon: L.divIcon({ className: "depot", html: "DEPOT", iconSize: [46, 18] }) }).addTo(map);
    const routes: Record<string, L.Polyline> = {}, vans: Record<string, L.Marker> = {}, stops: Record<string, L.CircleMarker> = {};
    for (const v of state.vans) routes[v.id] = L.polyline([], { color: v.color, weight: 4, opacity: 0.75 }).addTo(map);
    for (const s of problem.stops) stops[s.id] = L.circleMarker([s.lat, s.lon], { radius: 6, weight: 2 }).addTo(map);
    for (const v of state.vans) vans[v.id] = L.marker(v.pos, { zIndexOffset: 1000, icon: L.divIcon({ className: "van", html: `<b style="background:${v.color}">${v.id.replace(/\D+/, "")}</b>`, iconSize: [26, 26] }) }).addTo(map);
    layers.current = { map, stops, routes, vans, geo: {} };
    return () => { map.remove(); layers.current = null; };
  }, [state.snapshot]);            // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const ly = layers.current;
    if (!ly) return;
    const window = Object.fromEntries(state.snapshot.problem.stops.map((s) => [s.id, s]));
    for (const v of state.vans) {
      if (ly.geo[v.id] !== v.route.geometry) { ly.routes[v.id].setLatLngs(v.route.geometry); ly.geo[v.id] = v.route.geometry; }
      ly.vans[v.id].setLatLng(v.pos);
      for (const visit of v.route.visits) {
        const s = window[visit.stop_id], done = v.done[s.id] !== undefined, late = v.late[s.id] !== undefined;
        ly.stops[s.id].setStyle({ color: late ? "#c62828" : v.color, fillColor: done ? (late ? "#c62828" : v.color) : "#fff", fillOpacity: done ? 0.9 : 1, weight: s.priority ? 4 : 2, radius: s.priority ? 7 : 5 });
        ly.stops[s.id].bindTooltip(`${s.id}${s.priority ? " · priority" : ""} · ${v.id}${done ? " · delivered" : ""}${late ? ` · ${Math.round(v.late[s.id] / 60)} min late` : ""}`);
      }
    }
  }, [state, version]);

  return <div ref={el} className="map" role="img" aria-label="Map of delivery routes and vans" />;
}
