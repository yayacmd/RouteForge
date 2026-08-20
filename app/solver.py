"""The optimiser.

Takes a SolveRequest plus a travel matrix and produces routes. This is the
part of the original desktop tool that was worth keeping; what's changed:

  - Commodities are a list, so one capacity dimension is registered per
    commodity in a loop instead of two hardcoded ones.
  - Time windows may be soft (lateness penalised) or hard (enforced).
  - Each vehicle has its own shift start and length.
  - Depot reload nodes are added once per vehicle only when depots exist,
    and identical coordinates are deduplicated before the matrix is built.
  - Stops can be pinned to a vehicle or excluded by the dispatcher.

Node layout (index -> meaning):
    [0 .. V)                     vehicle start nodes
    [V .. 2V)                    vehicle end nodes
    [2V .. 2V + D*V)             depot reload copies (D depots x V vehicles)
    [2V + D*V .. end)            delivery stops
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .models import (Objective, RouteStopResult, SkippedStop, SolveRequest,
                     SolveResponse, StopPriority, VehicleRoute, WindowType)
from .providers.base import Coord

# Travel/service times are handled in seconds internally for OR-Tools
# integer arithmetic, then reported back to the user in minutes.
SEC_PER_MIN = 60


@dataclass
class NodeInfo:
    """One node in the routing model."""
    kind: str                      # start | end | depot | delivery
    location_id: str
    coord: Coord
    stop_id: Optional[str] = None
    vehicle_id: Optional[str] = None      # for start/end/depot copies
    demands: Dict[str, float] = None      # commodity_id -> signed amount
    service_seconds: int = 0
    window: Optional[Tuple[int, int]] = None   # seconds from day start
    soft_window: bool = False
    drop_multiplier: int = 0
    locked_vehicle_id: Optional[str] = None

    def __post_init__(self):
        if self.demands is None:
            self.demands = {}


def _minutes_from_day_start(minute_of_day: int, day_start: int) -> int:
    """Normalise a clock time to seconds elapsed since the shift began.

    Windows earlier than the shift start are assumed to belong to the next
    day, which is how overnight windows (22:00-02:00) stay coherent.
    """
    delta = minute_of_day - day_start
    if delta < 0:
        delta += 24 * 60
    return delta * SEC_PER_MIN


def build_nodes(req: SolveRequest) -> List[NodeInfo]:
    """Flatten the request into the node list the solver indexes into."""
    locations = req.location_map()
    settings = req.settings
    day_start = settings.day_start_minutes
    nodes: List[NodeInfo] = []

    # --- vehicle start nodes ---
    for v in req.vehicles:
        loc = locations[v.start_location_id]
        nodes.append(NodeInfo(
            kind="start", location_id=loc.id, vehicle_id=v.id,
            coord=Coord(loc.latitude, loc.longitude)))

    # --- vehicle end nodes ---
    for v in req.vehicles:
        loc = locations[v.end_location_id or v.start_location_id]
        nodes.append(NodeInfo(
            kind="end", location_id=loc.id, vehicle_id=v.id,
            coord=Coord(loc.latitude, loc.longitude)))

    # --- depot reload copies: one per depot per vehicle ---
    # A vehicle can only visit a reload node once, so N copies allow N reloads.
    for v in req.vehicles:
        for d in req.depots:
            loc = locations[d.location_id]
            window = None
            if d.open_start_minutes is not None and d.open_end_minutes is not None:
                window = (
                    _minutes_from_day_start(d.open_start_minutes, day_start),
                    _minutes_from_day_start(d.open_end_minutes, day_start),
                )
            # A reload sets the load back to full: negative demand equal to
            # the largest capacity across the fleet for each commodity.
            demands = {
                c.id: -max((veh.capacities.get(c.id, 0) for veh in req.vehicles), default=0)
                for c in req.commodities
            }
            nodes.append(NodeInfo(
                kind="depot", location_id=loc.id, vehicle_id=v.id,
                coord=Coord(loc.latitude, loc.longitude),
                demands=demands,
                service_seconds=int(d.reload_minutes * SEC_PER_MIN),
                window=window))

    # --- delivery stops ---
    for s in req.active_stops():
        loc = locations[s.location_id]
        service = s.fixed_service_minutes
        for c in req.commodities:
            service += s.demands.get(c.id, 0) * c.minutes_per_unit
        window = None
        if s.window_start_minutes is not None and s.window_end_minutes is not None:
            start = _minutes_from_day_start(s.window_start_minutes, day_start)
            end = _minutes_from_day_start(s.window_end_minutes, day_start)
            if end < start:
                end += 24 * 60 * SEC_PER_MIN
            window = (start, end)
        nodes.append(NodeInfo(
            kind="delivery", location_id=loc.id, stop_id=s.id,
            coord=Coord(loc.latitude, loc.longitude),
            demands={c.id: s.demands.get(c.id, 0) for c in req.commodities},
            service_seconds=int(service * SEC_PER_MIN),
            window=window,
            soft_window=(s.window_type is WindowType.PREFERRED),
            drop_multiplier=s.priority.drop_multiplier,
            locked_vehicle_id=s.locked_vehicle_id))

    return nodes


def solve(
    req: SolveRequest,
    nodes: List[NodeInfo],
    durations: Sequence[Sequence[float]],
    distances: Sequence[Sequence[float]],
    index_map: Sequence[int],
) -> SolveResponse:
    """Run the optimisation.

    `durations`/`distances` are matrices over *unique* coordinates;
    `index_map[node_i]` gives the matrix row for node i.
    """
    started = time.monotonic()
    settings = req.settings
    vehicles = req.vehicles
    V = len(vehicles)
    n_nodes = len(nodes)
    commodities = req.commodities
    stops_by_id = {s.id: s for s in req.stops}
    locations = req.location_map()

    starts = list(range(V))
    ends = list(range(V, 2 * V))
    depot_nodes = [i for i, nd in enumerate(nodes) if nd.kind == "depot"]
    delivery_nodes = [i for i, nd in enumerate(nodes) if nd.kind == "delivery"]

    manager = pywrapcp.RoutingIndexManager(n_nodes, V, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    def travel(from_node: int, to_node: int, matrix) -> float:
        if from_node == to_node:
            return 0.0
        return matrix[index_map[from_node]][index_map[to_node]]

    # ---- arc cost: what we're actually minimising ----------------------
    def distance_cb(from_index, to_index):
        f, t = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        # Discourage hopping directly between two reload nodes.
        if nodes[f].kind == "depot" and nodes[t].kind == "depot":
            return 1_000_000
        return int(travel(f, t, distances))

    def time_cost_cb(from_index, to_index):
        f, t = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        if nodes[f].kind == "depot" and nodes[t].kind == "depot":
            return 1_000_000
        return int(travel(f, t, durations))

    if settings.objective is Objective.TIME:
        cost_cb = routing.RegisterTransitCallback(time_cost_cb)
        cost_matrix = durations
    else:
        cost_cb = routing.RegisterTransitCallback(distance_cb)
        cost_matrix = distances
    routing.SetArcCostEvaluatorOfAllVehicles(cost_cb)

    # Everything that competes with arc cost — drop penalties, the balance
    # coefficient — has to be expressed in the same magnitude, or the solver
    # makes nonsense trade-offs. `cost_scale` is the longest single hop.
    cost_scale = 1
    for row in cost_matrix:
        for value in row:
            if value and value > cost_scale:
                cost_scale = int(value)

    # ---- capacity dimension per commodity ------------------------------
    for c in commodities:
        def make_demand_cb(commodity_id: str):
            def cb(from_index):
                node = manager.IndexToNode(from_index)
                return int(round(nodes[node].demands.get(commodity_id, 0)))
            return cb

        cb_index = routing.RegisterUnaryTransitCallback(make_demand_cb(c.id))
        caps = [int(round(v.capacities.get(c.id, 0))) for v in vehicles]
        dim_name = f"cap_{c.id}"
        routing.AddDimensionWithVehicleCapacity(
            cb_index,
            int(max(caps) if caps else 0),  # slack: allows reload to reset load
            caps,
            True,   # start cumul at zero
            dim_name,
        )
        dim = routing.GetDimensionOrDie(dim_name)

        # A vehicle begins the day already partly loaded: the amount it can
        # still deliver before needing a reload is capacity minus what it has.
        for vi, v in enumerate(vehicles):
            aboard = int(round(v.starting_load.get(c.id, 0)))
            cap = int(round(v.capacities.get(c.id, 0)))
            dim.CumulVar(routing.Start(vi)).SetRange(0, max(0, cap - aboard))

        # Deliveries consume capacity strictly; only reload nodes use slack.
        for node in delivery_nodes:
            dim.SlackVar(manager.NodeToIndex(node)).SetValue(0)

    # ---- time dimension -------------------------------------------------
    horizon = max(
        (v.max_shift_minutes or settings.default_max_shift_minutes) for v in vehicles
    ) * SEC_PER_MIN
    # Room for waiting plus any allowed soft lateness.
    horizon += settings.max_soft_lateness_minutes * SEC_PER_MIN

    def time_cb(from_index, to_index):
        f, t = manager.IndexToNode(from_index), manager.IndexToNode(to_index)
        return int(nodes[f].service_seconds + travel(f, t, durations))

    time_cb_index = routing.RegisterTransitCallback(time_cb)
    routing.AddDimension(time_cb_index, horizon, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    day_start = settings.day_start_minutes
    for vi, v in enumerate(vehicles):
        shift_start = v.shift_start_minutes if v.shift_start_minutes is not None else day_start
        offset = (shift_start - day_start) * SEC_PER_MIN
        if offset < 0:
            offset += 24 * 60 * SEC_PER_MIN
        max_shift = (v.max_shift_minutes or settings.default_max_shift_minutes) * SEC_PER_MIN
        start_index = routing.Start(vi)
        end_index = routing.End(vi)
        time_dim.CumulVar(start_index).SetRange(offset, offset)
        time_dim.CumulVar(end_index).SetRange(offset, offset + max_shift)
        routing.AddToAssignment(time_dim.SlackVar(start_index))

    # Window constraints on stops and depots.
    soft_penalty = settings.soft_lateness_penalty_per_minute
    max_late = settings.max_soft_lateness_minutes * SEC_PER_MIN
    for i, nd in enumerate(nodes):
        if nd.window is None or nd.kind in ("start", "end"):
            continue
        index = manager.NodeToIndex(i)
        w_start, w_end = nd.window
        if nd.soft_window:
            # Allow arriving late, but pay for every minute of it.
            time_dim.CumulVar(index).SetRange(w_start, min(w_end + max_late, horizon))
            time_dim.SetCumulVarSoftUpperBound(
                index, w_end, max(1, soft_penalty // SEC_PER_MIN))
        else:
            time_dim.CumulVar(index).SetRange(w_start, min(w_end, horizon))
        routing.AddToAssignment(time_dim.SlackVar(index))

    # ---- droppable nodes -------------------------------------------------
    # Reload nodes are free to skip; deliveries cost their priority penalty.
    for node in depot_nodes:
        routing.AddDisjunction([manager.NodeToIndex(node)], 0)
    max_penalty = 2_000_000_000  # keep well inside int64 arithmetic
    for node in delivery_nodes:
        penalty = min(nodes[node].drop_multiplier * cost_scale, max_penalty)
        routing.AddDisjunction([manager.NodeToIndex(node)], int(penalty))

    # ---- vehicle pinning --------------------------------------------------
    vehicle_index = {v.id: i for i, v in enumerate(vehicles)}
    for i, nd in enumerate(nodes):
        index = manager.NodeToIndex(i)
        if nd.kind == "depot":
            # Each depot copy belongs to exactly one vehicle.
            routing.VehicleVar(index).SetValues([-1, vehicle_index[nd.vehicle_id]])
        elif nd.kind == "delivery" and nd.locked_vehicle_id:
            routing.VehicleVar(index).SetValues([-1, vehicle_index[nd.locked_vehicle_id]])

    # ---- balanced workload -------------------------------------------------
    if settings.objective is Objective.BALANCED:
        # Penalising the longest route pushes work onto idle vehicles. The
        # coefficient multiplies a span measured in seconds, so it must stay
        # far below the drop penalties — otherwise abandoning every stop
        # becomes the cheapest way to have no span at all.
        span_coefficient = max(1, cost_scale // 2000)
        time_dim.SetGlobalSpanCostCoefficient(span_coefficient)

    # ---- search parameters --------------------------------------------------
    params = pywrapcp.DefaultRoutingSearchParameters()
    try:
        params.first_solution_strategy = getattr(
            routing_enums_pb2.FirstSolutionStrategy, settings.first_solution_strategy)
    except AttributeError:
        params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.AUTOMATIC
    try:
        params.local_search_metaheuristic = getattr(
            routing_enums_pb2.LocalSearchMetaheuristic, settings.local_search_metaheuristic)
    except AttributeError:
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(settings.effort.seconds)

    solution = routing.SolveWithParameters(params)
    elapsed = time.monotonic() - started

    if not solution:
        return SolveResponse(
            status="infeasible",
            solve_seconds=round(elapsed, 2),
            distance_unit=settings.distance_unit.value,
        )

    # ---- read the solution back -------------------------------------------
    unit_factor = settings.distance_unit.per_meter
    routes: List[VehicleRoute] = []
    visited_stops: set[str] = set()
    warnings: List[str] = []
    grand_distance = 0.0
    grand_duration = 0.0

    for vi, v in enumerate(vehicles):
        index = routing.Start(vi)
        stop_results: List[RouteStopResult] = []
        route_distance = 0.0
        drive_seconds = 0.0
        delivered_totals: Dict[str, float] = {c.id: 0.0 for c in commodities}
        # Track what's aboard so the dispatcher can see load at each step.
        load = {c.id: float(v.starting_load.get(c.id, 0)) for c in commodities}

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            nd = nodes[node]
            arrival = solution.Min(time_dim.CumulVar(index))
            loc = locations[nd.location_id]

            delivered: Dict[str, float] = {}
            late_by = 0.0
            warning = None

            if nd.kind == "delivery":
                visited_stops.add(nd.stop_id)
                for c in commodities:
                    amount = nd.demands.get(c.id, 0)
                    if amount:
                        delivered[c.id] = amount
                        delivered_totals[c.id] += amount
                        load[c.id] = max(0.0, load[c.id] - amount)
                stop = stops_by_id[nd.stop_id]
                if nd.window and nd.soft_window and arrival > nd.window[1]:
                    late_by = (arrival - nd.window[1]) / SEC_PER_MIN
                    warning = (
                        f"Arrives about {late_by:.0f} min after the preferred "
                        f"window closes."
                    )
                    warnings.append(f"'{loc.name}' — {warning}")
            elif nd.kind == "depot":
                for c in commodities:
                    load[c.id] = float(v.capacities.get(c.id, 0))
                warning = "Reload"

            departure = arrival + nd.service_seconds
            stop_results.append(RouteStopResult(
                stop_id=nd.stop_id,
                location_id=loc.id,
                location_name=loc.name,
                address=loc.address,
                latitude=loc.latitude,
                longitude=loc.longitude,
                kind=nd.kind,
                arrival_minutes=round(arrival / SEC_PER_MIN + day_start, 1),
                departure_minutes=round(departure / SEC_PER_MIN + day_start, 1),
                delivered=delivered,
                load_after={k: round(val, 2) for k, val in load.items()},
                late_by_minutes=round(late_by, 1),
                window_warning=warning,
            ))

            prev = index
            index = solution.Value(routing.NextVar(index))
            pf, pt = manager.IndexToNode(prev), manager.IndexToNode(index)
            route_distance += travel(pf, pt, distances)
            drive_seconds += travel(pf, pt, durations)

        # Final (end) node.
        node = manager.IndexToNode(index)
        nd = nodes[node]
        loc = locations[nd.location_id]
        arrival = solution.Min(time_dim.CumulVar(index))
        stop_results.append(RouteStopResult(
            stop_id=None, location_id=loc.id, location_name=loc.name,
            address=loc.address, latitude=loc.latitude, longitude=loc.longitude,
            kind="end",
            arrival_minutes=round(arrival / SEC_PER_MIN + day_start, 1),
            departure_minutes=round(arrival / SEC_PER_MIN + day_start, 1),
            load_after={k: round(val, 2) for k, val in load.items()},
        ))

        # A route with only a start and end never left the yard.
        real_stops = [s for s in stop_results if s.kind in ("delivery", "depot")]
        if not real_stops:
            continue

        start_min = stop_results[0].arrival_minutes
        total_minutes = stop_results[-1].arrival_minutes - start_min
        grand_distance += route_distance
        grand_duration += total_minutes

        routes.append(VehicleRoute(
            vehicle_id=v.id,
            vehicle_name=v.name,
            stops=stop_results,
            total_distance=round(route_distance * unit_factor, 2),
            distance_unit=settings.distance_unit.value,
            total_drive_minutes=round(drive_seconds / SEC_PER_MIN, 1),
            total_duration_minutes=round(total_minutes, 1),
            delivered_totals={k: round(val, 2) for k, val in delivered_totals.items()},
        ))

    # ---- stops that didn't make the plan ----------------------------------
    skipped: List[SkippedStop] = []
    for s in req.active_stops():
        if s.id in visited_stops:
            continue
        loc = locations[s.location_id]
        if s.priority is StopPriority.OPTIONAL:
            reason = "Marked optional and didn't fit in the available shifts."
        elif s.priority is StopPriority.SHOULD:
            reason = ("Couldn't be fitted in. Try extending the shift, adding a "
                      "vehicle, or widening its delivery window.")
        else:
            reason = ("Marked must-deliver but couldn't be scheduled — its "
                      "delivery window or size may be impossible to meet.")
        skipped.append(SkippedStop(stop_id=s.id, location_name=loc.name, reason=reason))

    return SolveResponse(
        status="solved",
        routes=routes,
        skipped=skipped,
        warnings=warnings,
        total_distance=round(grand_distance * unit_factor, 2),
        distance_unit=settings.distance_unit.value,
        total_duration_minutes=round(grand_duration, 1),
        solve_seconds=round(elapsed, 2),
    )
