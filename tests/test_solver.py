"""Solver tests using a synthetic straight-line matrix (no network calls).

Run: python3 -m tests.test_solver
"""
import math
import sys

sys.path.insert(0, ".")

from app.models import (Commodity, Depot, DistanceUnit, Effort, Location,
                        Objective, PlanSettings, SolveRequest, Stop,
                        StopPriority, Vehicle, WindowType)
from app.solver import build_nodes, solve
from app.providers.base import Coord, dedupe
from app.diagnostics import check_plan


def haversine_m(a: Coord, b: Coord) -> float:
    R = 6371000.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = math.radians(b.lat - a.lat)
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def fake_matrices(coords):
    """Straight-line distance; duration assumes ~35 mph with road factor."""
    n = len(coords)
    dist = [[0.0] * n for _ in range(n)]
    dur = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            d = haversine_m(coords[i], coords[j]) * 1.3
            dist[i][j] = d
            dur[i][j] = d / 15.6  # ~35 mph in m/s
    return dur, dist


def run(req: SolveRequest):
    errors, warnings = check_plan(req)
    if errors:
        return None, errors, warnings
    nodes = build_nodes(req)
    coords = [n.coord for n in nodes]
    unique, index_map = dedupe(coords)
    dur, dist = fake_matrices(unique)
    resp = solve(req, nodes, dur, dist, index_map)
    return resp, errors, warnings


def base_scenario(**overrides):
    """A small South Jersey delivery day."""
    fuel = Commodity(id="c1", name="Diesel", unit="gallons", minutes_per_unit=0.02)
    depot_loc = Location(id="L0", name="Main Yard", address="Vineland NJ",
                         latitude=39.4864, longitude=-75.0257)
    l1 = Location(id="L1", name="Acme Farm", latitude=39.5100, longitude=-75.0800)
    l2 = Location(id="L2", name="Bridgeton Depot", latitude=39.4276, longitude=-75.2340)
    l3 = Location(id="L3", name="Millville Shop", latitude=39.4020, longitude=-75.0393)
    l4 = Location(id="L4", name="Hammonton Co-op", latitude=39.6362, longitude=-74.8024)

    v1 = Vehicle(id="V1", name="Truck 1", start_location_id="L0",
                 capacities={"c1": 3000}, starting_load={"c1": 3000})

    stops = [
        Stop(id="S1", location_id="L1", demands={"c1": 500}, fixed_service_minutes=10),
        Stop(id="S2", location_id="L2", demands={"c1": 700}, fixed_service_minutes=10),
        Stop(id="S3", location_id="L3", demands={"c1": 400}, fixed_service_minutes=10),
        Stop(id="S4", location_id="L4", demands={"c1": 600}, fixed_service_minutes=10),
    ]

    data = dict(
        commodities=[fuel],
        locations=[depot_loc, l1, l2, l3, l4],
        vehicles=[v1],
        stops=stops,
        depots=[],
        settings=PlanSettings(day_start_minutes=6 * 60, default_max_shift_minutes=10 * 60,
                              effort=Effort.QUICK),
    )
    data.update(overrides)
    return SolveRequest(**data)


def show(title, resp, errors, warnings):
    print(f"\n{'='*66}\n{title}\n{'='*66}")
    if errors:
        print("BLOCKED:")
        for e in errors:
            print("  !", e)
        return
    for w in warnings:
        print("  warn:", w)
    print(f"status={resp.status} solve={resp.solve_seconds}s "
          f"dist={resp.total_distance}{resp.distance_unit} "
          f"dur={resp.total_duration_minutes}min")
    for r in resp.routes:
        print(f"  {r.vehicle_name}: {r.total_distance} {r.distance_unit}, "
              f"{r.total_duration_minutes} min, delivered={r.delivered_totals}")
        for s in r.stops:
            t = f"{int(s.arrival_minutes)//60:02d}:{int(s.arrival_minutes)%60:02d}"
            extra = f"  <-- {s.window_warning}" if s.window_warning else ""
            print(f"     {t} [{s.kind:8s}] {s.location_name}{extra}")
    for sk in resp.skipped:
        print(f"  SKIPPED {sk.location_name}: {sk.reason}")


def main():
    failures = []

    # 1. Basic feasible run
    resp, e, w = run(base_scenario())
    show("1. Basic single-vehicle run", resp, e, w)
    if not resp or resp.status != "solved" or len(resp.routes) != 1:
        failures.append("basic run did not solve")
    elif len(resp.skipped) != 0:
        failures.append("basic run skipped stops unexpectedly")

    # 2. Demand exceeds capacity, no depot -> should be caught pre-solve
    req = base_scenario()
    req.stops[0].demands["c1"] = 5000
    resp, e, w = run(req)
    show("2. Oversized stop (expect a blocking, plain-language error)", resp, e, w)
    if not e:
        failures.append("oversized stop was not caught")

    # 3. Same overall demand, but a depot lets the truck reload
    req = base_scenario()
    for s in req.stops:
        s.demands["c1"] = 1200          # 4800 total vs 3000 capacity
    req.depots = [Depot(id="D1", location_id="L0", reload_minutes=30)]
    resp, e, w = run(req)
    show("3. Reload at depot (4800 gal demand, 3000 gal truck)", resp, e, w)
    if not resp or resp.status != "solved":
        failures.append("depot reload scenario failed to solve")
    else:
        reloads = sum(1 for r in resp.routes for s in r.stops if s.kind == "depot")
        print(f"  -> reload visits: {reloads}")
        if reloads < 1:
            failures.append("depot reload never used despite over-capacity demand")

    # 4. Hard window that cannot be met -> infeasible or skipped
    req = base_scenario()
    req.stops[3].window_start_minutes = 6 * 60
    req.stops[3].window_end_minutes = 6 * 60 + 20   # 20 min after shift start
    req.stops[3].window_type = WindowType.REQUIRED
    resp, e, w = run(req)
    show("4. Impossible REQUIRED window", resp, e, w)

    # 5. Same window but PREFERRED -> should solve with a lateness warning
    req = base_scenario()
    req.stops[3].window_start_minutes = 6 * 60
    req.stops[3].window_end_minutes = 6 * 60 + 20
    req.stops[3].window_type = WindowType.PREFERRED
    resp, e, w = run(req)
    show("5. Same window as PREFERRED (soft) -> late but delivered", resp, e, w)
    if resp and resp.status == "solved":
        late = [s for r in resp.routes for s in r.stops if s.late_by_minutes > 0]
        print(f"  -> late arrivals reported: {len(late)}")
        if not late:
            failures.append("soft window produced no lateness report")
    else:
        failures.append("soft-window scenario failed to solve")

    # 6. Multi-commodity, two vehicles
    req = base_scenario()
    req.commodities.append(Commodity(id="c2", name="Gasoline", unit="gallons",
                                     minutes_per_unit=0.02))
    req.vehicles[0].capacities = {"c1": 2000, "c2": 1500}
    req.vehicles[0].starting_load = {"c1": 2000, "c2": 1500}
    req.vehicles.append(Vehicle(id="V2", name="Truck 2", start_location_id="L0",
                                capacities={"c1": 2000, "c2": 1500},
                                starting_load={"c1": 2000, "c2": 1500},
                                shift_start_minutes=8 * 60, max_shift_minutes=8 * 60))
    for i, s in enumerate(req.stops):
        s.demands = {"c1": 400 + i * 100, "c2": 300}
    resp, e, w = run(req)
    show("6. Two commodities, two vehicles, staggered shifts", resp, e, w)
    if not resp or resp.status != "solved":
        failures.append("multi-commodity scenario failed")

    # 7. Locked stop assignment
    req = base_scenario()
    req.vehicles.append(Vehicle(id="V2", name="Truck 2", start_location_id="L0",
                                capacities={"c1": 3000}, starting_load={"c1": 3000}))
    req.stops[0].locked_vehicle_id = "V2"
    req.stops[1].locked_vehicle_id = "V2"
    resp, e, w = run(req)
    show("7. Stops pinned to Truck 2", resp, e, w)
    if resp and resp.status == "solved":
        for r in resp.routes:
            names = [s.location_name for s in r.stops if s.kind == "delivery"]
            if r.vehicle_id == "V1" and ("Acme Farm" in names or "Bridgeton Depot" in names):
                failures.append("pinned stop was served by the wrong vehicle")
        print("  -> pinning respected")

    # 8. Excluded stop
    req = base_scenario()
    req.stops[2].excluded = True
    resp, e, w = run(req)
    show("8. Excluded stop is left out entirely", resp, e, w)
    if resp and resp.status == "solved":
        served = {s.location_name for r in resp.routes for s in r.stops}
        if "Millville Shop" in served:
            failures.append("excluded stop was still routed")
        else:
            print("  -> excluded stop correctly omitted")

    # 9. Balanced objective spreads work
    req = base_scenario(settings=PlanSettings(effort=Effort.QUICK,
                                              objective=Objective.BALANCED))
    req.vehicles.append(Vehicle(id="V2", name="Truck 2", start_location_id="L0",
                                capacities={"c1": 3000}, starting_load={"c1": 3000}))
    resp, e, w = run(req)
    show("9. Balanced objective with two trucks", resp, e, w)
    if not resp or resp.status != "solved":
        failures.append("balanced scenario failed to solve")
    else:
        counts = [len([s for s in r.stops if s.kind == "delivery"]) for r in resp.routes]
        print(f"  -> stops per vehicle: {counts}")
        if resp.skipped:
            failures.append(
                f"balanced objective dropped {len(resp.skipped)} stop(s) that "
                f"should have been served")
        if len(counts) < 2:
            failures.append("balanced objective left a vehicle idle")

    # 9b. Distance objective on the same fleet, for comparison
    req = base_scenario(settings=PlanSettings(effort=Effort.QUICK,
                                              objective=Objective.DISTANCE))
    req.vehicles.append(Vehicle(id="V2", name="Truck 2", start_location_id="L0",
                                capacities={"c1": 3000}, starting_load={"c1": 3000}))
    resp, e, w = run(req)
    show("9b. Distance objective, same fleet", resp, e, w)
    if not resp or resp.status != "solved":
        failures.append("distance objective with two trucks failed")
    elif resp.skipped:
        failures.append("distance objective dropped stops unexpectedly")

    # 10. No vehicles at all
    req = base_scenario()
    req.vehicles = []
    try:
        resp, e, w = run(req)
        show("10. No vehicles (expect blocking error)", resp, e, w)
        if not e:
            failures.append("empty fleet not caught")
    except Exception as exc:
        print(f"\n10. No vehicles -> validation raised: {exc}")

    # 11. Kilometers
    req = base_scenario(settings=PlanSettings(effort=Effort.QUICK,
                                              distance_unit=DistanceUnit.KILOMETERS))
    resp, e, w = run(req)
    show("11. Kilometer output", resp, e, w)
    if resp and resp.routes and resp.distance_unit != "kilometers":
        failures.append("distance unit not applied")

    print(f"\n{'='*66}")
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  X", f)
        return 1
    print("All solver checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
