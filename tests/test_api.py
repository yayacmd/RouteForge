"""API tests. Stubs the routing provider so no network is needed."""
import math
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

# Point data at a scratch dir BEFORE importing the app.
_tmp = tempfile.mkdtemp(prefix="routeforge-test-")
os.environ["ROUTEFORGE_DATA"] = _tmp

from fastapi.testclient import TestClient  # noqa: E402

from app.providers.base import Coord, MatrixResult, RoutingProvider  # noqa: E402


class StubProvider(RoutingProvider):
    """Straight-line distances; no network."""
    name = "stub"
    max_matrix_size = 100

    async def search(self, query, limit=8):
        from app.providers.base import GeocodeResult
        return [GeocodeResult(label=f"{query} Result", lat=39.5, lon=-75.0)]

    async def _matrix_chunk(self, client, sources, destinations):
        def hav(a, b):
            R = 6371000.0
            p1, p2 = math.radians(a.lat), math.radians(b.lat)
            dp = math.radians(b.lat - a.lat)
            dl = math.radians(b.lon - a.lon)
            h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
            return 2 * R * math.asin(math.sqrt(h)) * 1.3
        dur = [[hav(s, d) / 15.6 for d in destinations] for s in sources]
        dist = [[hav(s, d) for d in destinations] for s in sources]
        return MatrixResult(durations=dur, distances=dist)

    async def directions(self, client, coords):
        return [[c.lon, c.lat] for c in coords]

    # NOTE: deliberately does NOT override matrix(), so the base class's
    # dedupe + cache + chunking logic is what actually gets exercised.


import app.main as main  # noqa: E402

# Mirror what the real factory does: hand the provider the shared cache.
main._provider = lambda: StubProvider(cache=main._matrix_cache)

client = TestClient(main.app)

PASSWORD = "dispatch-2026"
failures = []


def check(label, condition, detail=""):
    mark = "ok " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


print("\n=== 1. Unconfigured instance is locked down ===")
r = client.get("/api/status")
check("status endpoint is public", r.status_code == 200)
check("reports not configured", r.json()["configured"] is False)

r = client.get("/api/plan")
check("plan is refused before setup", r.status_code == 503, f"got {r.status_code}")

r = client.post("/api/solve", json={"commodities": [], "locations": [],
                                    "vehicles": [], "stops": []})
check("solve is refused before setup", r.status_code in (422, 503), f"got {r.status_code}")

print("\n=== 2. Setup ===")
r = client.post("/api/setup", json={
    "password": PASSWORD,
    "organization_name": "Yayac Fuel Co",
    "routing_provider": "locationiq",
    "routing_api_key": "pk.test",
})
check("setup succeeds", r.status_code == 200, r.text[:200])
api_token = r.json().get("api_token", "")
check("api token issued", bool(api_token))

r = client.post("/api/setup", json={"password": "another-one", "routing_api_key": "x"})
check("setup cannot be re-run", r.status_code == 409, f"got {r.status_code}")

print("\n=== 3. Auth ===")
r = client.get("/api/plan")
check("session cookie from setup works", r.status_code == 200, f"got {r.status_code}")

bare = TestClient(main.app)
r = bare.get("/api/plan")
check("no credentials is rejected", r.status_code == 401, f"got {r.status_code}")

r = bare.get("/api/plan", headers={"X-API-Token": "wrong-token"})
check("bad token rejected", r.status_code == 401, f"got {r.status_code}")

r = bare.get("/api/plan", headers={"X-API-Token": api_token})
check("valid API token accepted", r.status_code == 200, f"got {r.status_code}")

r = bare.get("/api/plan", headers={"Authorization": f"Bearer {api_token}"})
check("bearer header accepted", r.status_code == 200, f"got {r.status_code}")

r = bare.post("/api/login", json={"password": "wrong"})
check("wrong password rejected", r.status_code == 401)

r = bare.post("/api/login", json={"password": PASSWORD})
check("correct password accepted", r.status_code == 200)

print("\n=== 4. Plan persistence ===")
plan = {
    "commodities": [{"id": "c1", "name": "Diesel", "unit": "gallons"}],
    "locations": [{"id": "L0", "name": "Yard", "latitude": 39.48, "longitude": -75.02}],
    "vehicles": [], "stops": [], "depots": [], "drivers": [], "settings": {},
}
r = client.put("/api/plan", json=plan)
check("plan saves", r.status_code == 200)
r = client.get("/api/plan")
check("plan round-trips", r.json()["commodities"][0]["name"] == "Diesel")

print("\n=== 5. Geocode proxy ===")
r = client.get("/api/geocode", params={"q": "Vineland NJ"})
check("geocode returns results", r.status_code == 200 and len(r.json()["results"]) == 1)
r = client.get("/api/geocode", params={"q": "ab"})
check("short query returns empty", r.json()["results"] == [])

print("\n=== 6. Solve over HTTP ===")
solve_payload = {
    "commodities": [{"id": "c1", "name": "Diesel", "unit": "gallons", "minutes_per_unit": 0.02}],
    "locations": [
        {"id": "L0", "name": "Main Yard", "latitude": 39.4864, "longitude": -75.0257},
        {"id": "L1", "name": "Acme Farm", "latitude": 39.5100, "longitude": -75.0800},
        {"id": "L2", "name": "Bridgeton", "latitude": 39.4276, "longitude": -75.2340},
        {"id": "L3", "name": "Millville", "latitude": 39.4020, "longitude": -75.0393},
    ],
    "vehicles": [{"id": "V1", "name": "Truck 1", "start_location_id": "L0",
                  "capacities": {"c1": 3000}, "starting_load": {"c1": 3000}}],
    "stops": [
        {"id": "S1", "location_id": "L1", "demands": {"c1": 500}, "fixed_service_minutes": 10},
        {"id": "S2", "location_id": "L2", "demands": {"c1": 700}, "fixed_service_minutes": 10},
        {"id": "S3", "location_id": "L3", "demands": {"c1": 400}, "fixed_service_minutes": 10},
    ],
    "depots": [],
    "settings": {"effort": "quick", "day_start_minutes": 360},
}
r = client.post("/api/solve", json=solve_payload)
check("solve returns 200", r.status_code == 200, r.text[:300])
body = r.json()
check("solve status is solved", body.get("status") == "solved", str(body)[:200])
check("one route returned", len(body.get("routes", [])) == 1)
check("no stops skipped", len(body.get("skipped", [])) == 0)
check("geometry attached for map", len(body["routes"][0].get("geometry", [])) > 0)
print(f"     -> {body['total_distance']} {body['distance_unit']}, "
      f"{body['total_duration_minutes']} min, solved in {body['solve_seconds']}s")

print("\n=== 7. Infeasible plan gives plain-language diagnostics ===")
bad = json.loads(json.dumps(solve_payload)) if (json := __import__("json")) else None
bad["stops"][0]["demands"]["c1"] = 99999
r = client.post("/api/solve", json=bad)
body = r.json()
check("returns infeasible not 500", r.status_code == 200 and body["status"] == "infeasible")
check("explains why in plain language", len(body.get("diagnostics", [])) > 0)
for d in body.get("diagnostics", []):
    print(f"     -> {d}")
jargon = ["dimension", "cumul", "disjunction", "callback", "CP-SAT"]
has_jargon = any(j.lower() in " ".join(body.get("diagnostics", [])).lower() for j in jargon)
check("diagnostics contain no solver jargon", not has_jargon)

print("\n=== 8. Validation errors are caught ===")
broken = json.loads(json.dumps(solve_payload))
broken["stops"][0]["location_id"] = "NOPE"
r = client.post("/api/solve", json=broken)
check("bad reference rejected", r.status_code == 422, f"got {r.status_code}")

print("\n=== 9. CSV export ===")
r = client.post("/api/solve", json=solve_payload)
r2 = client.post("/api/export/csv", json=r.json())
check("csv export works", r2.status_code == 200 and "Vehicle" in r2.text)
print("     -> first rows:")
for line in r2.text.splitlines()[:4]:
    print(f"        {line}")

print("\n=== 10. Matrix cache ===")
r = client.get("/api/settings")
entries = r.json().get("cache", {}).get("entries", 0)
check("cache has entries after solves", entries > 0, f"entries={entries}")
print(f"     -> cached pairs: {entries}")

# A repeat solve must hit the cache rather than re-querying the provider.
calls = {"n": 0}
_orig_chunk = StubProvider._matrix_chunk


async def counting_chunk(self, client_, sources, destinations):
    calls["n"] += 1
    return await _orig_chunk(self, client_, sources, destinations)


StubProvider._matrix_chunk = counting_chunk
r = client.post("/api/solve", json=solve_payload)
check("repeat solve made zero provider matrix calls", calls["n"] == 0,
      f"made {calls['n']} calls")
check("cached repeat solve still solves", r.json()["status"] == "solved")
StubProvider._matrix_chunk = _orig_chunk

shutil.rmtree(_tmp, ignore_errors=True)

print("\n" + "=" * 60)
if failures:
    print("FAILURES:")
    for f in failures:
        print("  X", f)
    sys.exit(1)
print("All API checks passed.")
