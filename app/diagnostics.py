"""Plain-language feasibility checks.

The point of this module: a dispatcher should never see "No Solution Found"
with no explanation. Most infeasible plans are infeasible for a boring,
detectable reason — not enough capacity, a window outside the shift, a stop
too big for any truck. We catch those *before* spending rate-limited API
calls on a distance matrix, and we say what to do about it.

Vocabulary rule: no solver jargon. Say "truck", "capacity", "delivery
window" — never "dimension", "disjunction", or "cumul var".
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .models import SolveRequest, StopPriority, WindowType


def _fmt_minutes(m: float) -> str:
    """Render minutes-from-midnight as a readable clock time."""
    m = int(round(m)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def _fmt_amount(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def check_plan(req: SolveRequest) -> Tuple[List[str], List[str]]:
    """Return (blocking_errors, warnings).

    Blocking errors mean the plan cannot possibly succeed; we stop and
    explain rather than running the solver. Warnings mean it will probably
    solve but the user should know something.
    """
    errors: List[str] = []
    warnings: List[str] = []

    locations = req.location_map()
    commodities = {c.id: c for c in req.commodities}
    stops = req.active_stops()
    settings = req.settings

    # ---- Nothing to do ------------------------------------------------
    if not stops:
        errors.append(
            "There are no stops to deliver to. Add at least one stop, or "
            "un-exclude a stop you've turned off."
        )
    if not req.vehicles:
        errors.append(
            "There are no vehicles available. Add at least one vehicle "
            "before building routes."
        )
    if errors:
        return errors, warnings

    # ---- Capacity: total demand vs total fleet capacity ----------------
    # With depots the fleet can reload, so capacity is only a hard ceiling
    # when there is nowhere to restock.
    has_depot = bool(req.depots)

    for cid, com in commodities.items():
        demand = sum(s.demands.get(cid, 0) for s in stops)
        if demand <= 0:
            continue

        fleet_capacity = sum(v.capacities.get(cid, 0) for v in req.vehicles)
        available = sum(
            v.starting_load.get(cid, 0) if not has_depot else v.capacities.get(cid, 0)
            for v in req.vehicles
        )

        if fleet_capacity <= 0:
            errors.append(
                f"You have {_fmt_amount(demand)} {com.unit} of {com.name} to "
                f"deliver, but no vehicle has any capacity for {com.name}. "
                f"Set a {com.name} capacity on at least one vehicle."
            )
            continue

        if not has_depot and demand > available:
            errors.append(
                f"{com.name}: you need {_fmt_amount(demand)} {com.unit} but the "
                f"vehicles only start with {_fmt_amount(available)} {com.unit} "
                f"aboard, and there are no restock depots. Add a depot so "
                f"trucks can reload, load more before departure, or add a vehicle."
            )
        elif has_depot and demand > fleet_capacity:
            # Not fatal — they can reload — but worth flagging the trip count.
            trips = demand / fleet_capacity
            warnings.append(
                f"{com.name}: total demand ({_fmt_amount(demand)} {com.unit}) is "
                f"about {trips:.1f}x the fleet's combined capacity, so trucks "
                f"will need to reload at a depot several times. Expect long routes."
            )

    # ---- Single stop bigger than any single vehicle --------------------
    for s in stops:
        name = locations[s.location_id].name
        for cid, amount in s.demands.items():
            if amount <= 0:
                continue
            com = commodities[cid]
            biggest = max((v.capacities.get(cid, 0) for v in req.vehicles), default=0)
            if amount > biggest:
                errors.append(
                    f"'{name}' needs {_fmt_amount(amount)} {com.unit} of "
                    f"{com.name} in one delivery, but the largest vehicle only "
                    f"holds {_fmt_amount(biggest)}. Split this into two stops "
                    f"or use a bigger vehicle."
                )

    # ---- Delivery windows vs the working day ---------------------------
    for s in stops:
        name = locations[s.location_id].name
        start = s.window_start_minutes
        end = s.window_end_minutes
        if start is None or end is None:
            continue

        # Shift span across the whole fleet (widest possible working period).
        earliest = min(
            (v.shift_start_minutes if v.shift_start_minutes is not None
             else settings.day_start_minutes)
            for v in req.vehicles
        )
        latest = max(
            (v.shift_start_minutes if v.shift_start_minutes is not None
             else settings.day_start_minutes)
            + (v.max_shift_minutes or settings.default_max_shift_minutes)
            for v in req.vehicles
        )

        # Normalise an overnight window forward a day for comparison.
        norm_end = end if end >= start else end + 24 * 60

        if norm_end < earliest:
            msg = (
                f"'{name}' must be delivered by {_fmt_minutes(end)}, but the "
                f"earliest any vehicle starts is {_fmt_minutes(earliest)}. "
                f"Move the window later or start a shift earlier."
            )
            errors.append(msg) if s.window_type is WindowType.REQUIRED else warnings.append(msg)
        elif start > latest:
            msg = (
                f"'{name}' can't be delivered before {_fmt_minutes(start)}, but "
                f"all shifts end by {_fmt_minutes(latest)}. Extend the maximum "
                f"shift length or move the window earlier."
            )
            errors.append(msg) if s.window_type is WindowType.REQUIRED else warnings.append(msg)

        if norm_end - start < 15:
            warnings.append(
                f"'{name}' has a very tight delivery window "
                f"({_fmt_minutes(start)}–{_fmt_minutes(end)}). Widening it, or "
                f"marking it 'Preferred' instead of 'Required', makes a "
                f"workable plan much more likely."
            )

    # ---- Locked stops that can't work ----------------------------------
    by_vehicle: Dict[str, List] = {}
    for s in stops:
        if s.locked_vehicle_id:
            by_vehicle.setdefault(s.locked_vehicle_id, []).append(s)

    vehicles = {v.id: v for v in req.vehicles}
    for vid, locked in by_vehicle.items():
        veh = vehicles[vid]
        for cid, com in commodities.items():
            need = sum(s.demands.get(cid, 0) for s in locked)
            cap = veh.capacities.get(cid, 0)
            if need > cap and not has_depot:
                errors.append(
                    f"You've pinned {len(locked)} stops to '{veh.name}' needing "
                    f"{_fmt_amount(need)} {com.unit} of {com.name}, but it only "
                    f"holds {_fmt_amount(cap)} and there's no depot to reload at. "
                    f"Unpin a stop or add a depot."
                )

    # ---- Soft-window sanity --------------------------------------------
    soft = [s for s in stops if s.window_type is WindowType.PREFERRED]
    if soft:
        warnings.append(
            f"{len(soft)} stop(s) have 'Preferred' windows. The planner will try "
            f"to hit them but may deliver late if that's the only way to fit "
            f"everything in — any late arrivals are flagged in the results."
        )

    optional = [s for s in stops if s.priority is StopPriority.OPTIONAL]
    if optional:
        warnings.append(
            f"{len(optional)} stop(s) are marked 'Optional' and may be left "
            f"out of the plan if they don't fit."
        )

    return errors, warnings


def explain_no_solution(req: SolveRequest) -> List[str]:
    """Suggestions when the solver itself returns nothing.

    Pre-checks passed, so the cause is an interaction the simple checks can't
    see — usually travel time between stops with tight windows.
    """
    tips = [
        "The planner couldn't fit every required stop into the available "
        "shifts. The usual causes, most common first:",
        "• Delivery windows are too tight once driving time between stops is "
        "counted. Try marking some windows 'Preferred' instead of 'Required'.",
        "• The working day is too short. Increase the maximum shift length.",
        "• There aren't enough vehicles for the number of stops. Add a vehicle.",
        "• Stops are too far apart to serve in one day. Consider splitting "
        "them across two days.",
    ]
    if not req.depots:
        tips.append(
            "• There are no restock depots, so each truck is limited to the "
            "load it starts with. Adding a depot often fixes this."
        )
    tips.append(
        "You can also set 'Optimising effort' to Thorough — a longer search "
        "sometimes finds a plan a quick search misses."
    )
    return tips
