"""Inputs and outputs of the optimizer. Times are seconds after midnight of the delivery day."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

DAY = 24 * 3600


def hhmm(seconds: float) -> str:
    return f"{int(seconds) // 3600:02d}:{int(seconds) % 3600 // 60:02d}"


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    def key(self) -> tuple[float, float]:
        """Rounded to 4 decimals (about 11 m): nearby requests share one cache entry."""
        return (round(self.lat, 4), round(self.lon, 4))


class Stop(Point):
    id: str = Field(min_length=1)
    weight_kg: float = Field(ge=0)
    volume_l: float = Field(ge=0)
    service_s: int = Field(ge=0, le=4 * 3600)
    window_start: int = Field(ge=0, le=DAY)
    window_end: int = Field(ge=0, le=DAY)
    priority: bool = False                      # a promised delivery window: never dropped while any plan can serve it
    phone: str | None = None

    @model_validator(mode="after")
    def _window(self) -> Stop:
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


class Vehicle(BaseModel):
    id: str = Field(min_length=1)
    capacity_kg: float = Field(gt=0)
    capacity_l: float = Field(gt=0)
    shift_start: int = Field(ge=0, le=DAY)
    shift_end: int = Field(ge=0, le=DAY)

    @model_validator(mode="after")
    def _shift(self) -> Vehicle:
        if self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be after shift_start")
        return self


class Problem(BaseModel):
    depot: Point
    vehicles: list[Vehicle] = Field(min_length=1, max_length=200)
    stops: list[Stop] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _unique(self) -> Problem:
        for name, ids in (("stop", [s.id for s in self.stops]), ("vehicle", [v.id for v in self.vehicles])):
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate {name} ids")
        return self


class Visit(BaseModel):
    stop_id: str
    arrival: int
    departure: int
    travel_s: int = 0                           # driving time from the previous point
    late_s: int = 0                             # only non-zero after a live re-optimization (windows become soft)


class Route(BaseModel):
    vehicle_id: str
    start: int
    end: int
    visits: list[Visit]
    distance_m: int
    load_kg: float
    load_l: float
    geometry: list[tuple[float, float]] = []    # (lat, lon) road path for the map, when the routing engine supplied one


class Plan(BaseModel):
    routes: list[Route]
    dropped: list[str]
    distance_m: int
    duration_s: int
    solver_ms: int
    matrix_source: str = "unknown"
