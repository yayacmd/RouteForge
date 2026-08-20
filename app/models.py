"""Domain models for RouteForge.

These Pydantic models are the contract for the HTTP API *and* the internal
solver input. Anything a client can express, an automation script can POST.

Key design decisions:
  - Commodities are a LIST, not a fixed pair. A single-product operation
    defines one commodity; a fuel hauler might define three.
  - Time windows can be REQUIRED (hard constraint) or PREFERRED (soft —
    lateness is penalised but allowed). This is what turns "No Solution
    Found" into a usable answer.
  - Stop priority maps to drop penalties internally so users never see a
    raw penalty integer.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def _new_id() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# Enums — user-facing vocabulary, not solver jargon
# ---------------------------------------------------------------------------
class WindowType(str, Enum):
    """Whether a delivery window must be met or is merely preferred."""
    REQUIRED = "required"   # hard constraint
    PREFERRED = "preferred"  # soft constraint; lateness penalised


class StopPriority(str, Enum):
    """How reluctant the solver should be to skip a stop."""
    MUST = "must"        # effectively unskippable
    SHOULD = "should"    # skip only if otherwise infeasible
    OPTIONAL = "optional"  # nice to have

    @property
    def drop_multiplier(self) -> int:
        """Penalty expressed as a multiple of the largest single arc cost.

        Absolute penalties don't work: arc costs are in meters (or seconds),
        so a fixed 100,000 is enormous for a city and trivial for a region.
        Scaling to the actual problem keeps 'must deliver' meaningful at any
        geography.
        """
        return {
            StopPriority.MUST: 2000,
            StopPriority.SHOULD: 200,
            StopPriority.OPTIONAL: 20,
        }[self]


class Objective(str, Enum):
    """What the optimiser should prioritise."""
    DISTANCE = "distance"    # least total mileage
    TIME = "time"            # least total drive time
    BALANCED = "balanced"    # even workload across drivers


class Effort(str, Enum):
    """How long to spend optimising, in user language."""
    QUICK = "quick"
    NORMAL = "normal"
    THOROUGH = "thorough"

    @property
    def seconds(self) -> int:
        return {Effort.QUICK: 3, Effort.NORMAL: 15, Effort.THOROUGH: 60}[self]


class DistanceUnit(str, Enum):
    MILES = "miles"
    KILOMETERS = "kilometers"

    @property
    def per_meter(self) -> float:
        return 0.000621371 if self is DistanceUnit.MILES else 0.001


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------
class Commodity(BaseModel):
    """A thing being delivered. Replaces the old hardcoded Product 1/2."""
    id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1, description="e.g. 'Diesel', 'Cases', 'Propane'")
    unit: str = Field(default="units", description="e.g. 'gallons', 'pallets'")
    # Minutes to load/unload one unit. Drives service time at stops.
    minutes_per_unit: float = Field(default=0.0, ge=0)


class Location(BaseModel):
    """A geocoded point. Used for customers, vehicle bases, and depots."""
    id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1)
    address: str = ""
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @property
    def coord_key(self) -> str:
        """Rounded key used to deduplicate identical points in the matrix."""
        return f"{self.latitude:.6f},{self.longitude:.6f}"


class Vehicle(BaseModel):
    """A truck, van, or driver-resource with its own shift and capacities."""
    id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1, description="e.g. 'Truck 7'")
    start_location_id: str
    # Where it must finish. Defaults to the start location (a closed tour).
    end_location_id: Optional[str] = None

    # commodity_id -> capacity, and how much is already aboard at shift start.
    capacities: Dict[str, float] = Field(default_factory=dict)
    starting_load: Dict[str, float] = Field(default_factory=dict)

    # Per-vehicle shift, in minutes from midnight. Overrides the plan default.
    shift_start_minutes: Optional[int] = Field(default=None, ge=0, le=24 * 60)
    max_shift_minutes: Optional[int] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_load_within_capacity(self) -> "Vehicle":
        for cid, amount in self.starting_load.items():
            cap = self.capacities.get(cid, 0)
            if amount > cap:
                raise ValueError(
                    f"Vehicle '{self.name}' starts with {amount} of commodity "
                    f"{cid} but its capacity is only {cap}."
                )
        return self


class Stop(BaseModel):
    """A delivery to make."""
    id: str = Field(default_factory=_new_id)
    location_id: str

    # commodity_id -> amount to deliver.
    demands: Dict[str, float] = Field(default_factory=dict)

    # Delivery window, minutes from midnight. Omit for "any time in shift".
    window_start_minutes: Optional[int] = Field(default=None, ge=0)
    window_end_minutes: Optional[int] = Field(default=None, ge=0)
    window_type: WindowType = WindowType.REQUIRED

    # Fixed overhead at this stop regardless of quantity: parking, paperwork.
    fixed_service_minutes: float = Field(default=0, ge=0)

    priority: StopPriority = StopPriority.SHOULD

    # Dispatcher overrides applied on a re-solve.
    locked_vehicle_id: Optional[str] = None
    excluded: bool = False

    @model_validator(mode="after")
    def _check_window_order(self) -> "Stop":
        s, e = self.window_start_minutes, self.window_end_minutes
        if s is not None and e is not None and e < s:
            # An overnight window (22:00–02:00) is legitimate; the solver
            # normalises it. Only reject if both are on the same day and
            # the span is implausible as an overnight.
            if s - e > 12 * 60:
                raise ValueError(
                    "Delivery window end is far earlier than its start. "
                    "For an overnight window, keep the span under 12 hours."
                )
        return self


class Depot(BaseModel):
    """A place a vehicle can return to mid-route to reload."""
    id: str = Field(default_factory=_new_id)
    location_id: str
    reload_minutes: float = Field(default=45, ge=0)
    # Depots are usable by every vehicle unless restricted.
    open_start_minutes: Optional[int] = Field(default=None, ge=0)
    open_end_minutes: Optional[int] = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# The solve request — everything needed for one optimisation run
# ---------------------------------------------------------------------------
class PlanSettings(BaseModel):
    day_start_minutes: int = Field(default=6 * 60, ge=0, le=24 * 60)
    default_max_shift_minutes: int = Field(default=10 * 60, gt=0)
    objective: Objective = Objective.DISTANCE
    effort: Effort = Effort.NORMAL
    distance_unit: DistanceUnit = DistanceUnit.MILES
    # How many minutes late a PREFERRED window may run before it's hopeless.
    max_soft_lateness_minutes: int = Field(default=120, ge=0)

    # --- Advanced: OR-Tools internals. Safe to ignore. ---
    first_solution_strategy: str = "AUTOMATIC"
    local_search_metaheuristic: str = "GUIDED_LOCAL_SEARCH"
    soft_lateness_penalty_per_minute: int = Field(default=100, ge=0)


class SolveRequest(BaseModel):
    """Complete, self-contained problem description.

    An automation client can POST this without any stored state on the server.
    """
    commodities: List[Commodity]
    locations: List[Location]
    vehicles: List[Vehicle]
    stops: List[Stop]
    depots: List[Depot] = Field(default_factory=list)
    settings: PlanSettings = Field(default_factory=PlanSettings)

    @field_validator("commodities")
    @classmethod
    def _at_least_one_commodity(cls, v: List[Commodity]) -> List[Commodity]:
        if not v:
            raise ValueError("Define at least one commodity (the thing you deliver).")
        return v

    @model_validator(mode="after")
    def _check_references(self) -> "SolveRequest":
        loc_ids = {l.id for l in self.locations}
        com_ids = {c.id for c in self.commodities}
        veh_ids = {v.id for v in self.vehicles}

        for v in self.vehicles:
            if v.start_location_id not in loc_ids:
                raise ValueError(f"Vehicle '{v.name}' references an unknown start location.")
            if v.end_location_id and v.end_location_id not in loc_ids:
                raise ValueError(f"Vehicle '{v.name}' references an unknown end location.")
            for cid in list(v.capacities) + list(v.starting_load):
                if cid not in com_ids:
                    raise ValueError(f"Vehicle '{v.name}' references an unknown commodity.")

        for s in self.stops:
            if s.location_id not in loc_ids:
                raise ValueError("A stop references an unknown location.")
            for cid in s.demands:
                if cid not in com_ids:
                    raise ValueError("A stop references an unknown commodity.")
            if s.locked_vehicle_id and s.locked_vehicle_id not in veh_ids:
                raise ValueError("A stop is locked to a vehicle that doesn't exist.")

        for d in self.depots:
            if d.location_id not in loc_ids:
                raise ValueError("A depot references an unknown location.")

        return self

    # -- convenience lookups used by the solver --
    def location_map(self) -> Dict[str, Location]:
        return {l.id: l for l in self.locations}

    def active_stops(self) -> List[Stop]:
        return [s for s in self.stops if not s.excluded]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
class RouteStopResult(BaseModel):
    stop_id: Optional[str] = None          # None for base/depot nodes
    location_id: str
    location_name: str
    address: str = ""
    latitude: float
    longitude: float
    kind: str = "delivery"                  # delivery | start | end | depot
    arrival_minutes: float
    departure_minutes: float
    # commodity_id -> amount delivered here
    delivered: Dict[str, float] = Field(default_factory=dict)
    # commodity_id -> load aboard on departure
    load_after: Dict[str, float] = Field(default_factory=dict)
    # Populated when a PREFERRED window was missed.
    late_by_minutes: float = 0.0
    window_warning: Optional[str] = None


class VehicleRoute(BaseModel):
    vehicle_id: str
    vehicle_name: str
    stops: List[RouteStopResult]
    total_distance: float = 0.0
    distance_unit: str = "miles"
    total_drive_minutes: float = 0.0
    total_duration_minutes: float = 0.0
    delivered_totals: Dict[str, float] = Field(default_factory=dict)
    # GeoJSON-style [[lon, lat], ...] polyline for the map.
    geometry: List[List[float]] = Field(default_factory=list)


class SkippedStop(BaseModel):
    stop_id: str
    location_name: str
    reason: str


class SolveResponse(BaseModel):
    status: str                       # solved | infeasible | error
    routes: List[VehicleRoute] = Field(default_factory=list)
    skipped: List[SkippedStop] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    diagnostics: List[str] = Field(default_factory=list)
    total_distance: float = 0.0
    distance_unit: str = "miles"
    total_duration_minutes: float = 0.0
    solve_seconds: float = 0.0
