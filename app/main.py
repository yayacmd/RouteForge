"""RouteForge HTTP API.

Auth model (deliberately simple, appropriate for a small dispatch office):
  - One shared password for people, held in a signed session cookie.
  - One API token for machines, sent as `X-API-Token` or a Bearer header.
  - Until a password is set, ONLY the setup endpoints respond. That closes
    the window where a fresh cloud deploy would otherwise be public.

The solve endpoint is deliberately payload-based: an automation client can
POST a complete problem and get routes back without touching stored state.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from . import config as cfg_module
from .config import (CACHE_PATH, InstanceConfig, load_config, load_plan,
                     save_config, save_plan, set_password, verify_password)
from .diagnostics import check_plan, explain_no_solution
from .models import SolveRequest, SolveResponse
from .providers.backends import build_provider
from .providers.base import Coord, PairCache, ProviderError, dedupe
from .solver import build_nodes, solve

app = FastAPI(
    title="RouteForge API",
    description=(
        "Delivery route optimisation. POST a complete plan to /api/solve and "
        "get back optimised routes — no stored state required, so this works "
        "equally well from the web UI or an automation script."
    ),
    version="1.0.0",
)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "routeforge_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # two weeks

# OR-Tools is CPU-bound and would otherwise block the event loop.
_solver_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="solver")
_matrix_cache = PairCache(CACHE_PATH)

_config: InstanceConfig = load_config()


def get_config() -> InstanceConfig:
    return _config


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_config.session_secret, salt="routeforge-session")


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------
def _has_valid_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=SESSION_MAX_AGE)
        return True
    except BadSignature:
        return False


def _has_valid_api_token(request: Request) -> bool:
    supplied = request.headers.get("x-api-token", "")
    if not supplied:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    import secrets as _s
    return bool(supplied) and _s.compare_digest(supplied, _config.api_token)


async def require_auth(request: Request) -> None:
    """Allow either a logged-in browser session or a valid API token."""
    if not _config.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This RouteForge instance hasn't been set up yet. Open it in a browser to finish setup.",
        )
    if _has_valid_session(request) or _has_valid_api_token(request):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not signed in. Sign in with the shared password, or send a valid API token.",
    )


# ---------------------------------------------------------------------------
# Setup & auth endpoints
# ---------------------------------------------------------------------------
class SetupPayload(BaseModel):
    password: str = Field(min_length=8)
    organization_name: str = "RouteForge"
    routing_provider: str = "locationiq"
    routing_api_key: str = ""
    routing_base_url: str = ""
    nominatim_url: str = ""


class LoginPayload(BaseModel):
    password: str


@app.get("/api/status")
async def api_status() -> Dict[str, Any]:
    """Unauthenticated: tells the frontend whether to show setup or login."""
    return {
        "configured": _config.configured,
        "organization_name": _config.organization_name,
        "version": app.version,
    }


@app.post("/api/setup")
async def api_setup(payload: SetupPayload, response: Response) -> Dict[str, Any]:
    if _config.configured:
        raise HTTPException(
            status_code=409,
            detail="This instance is already set up. Sign in instead, or change settings after signing in.",
        )
    if payload.routing_provider != "osrm" and not payload.routing_api_key:
        raise HTTPException(
            status_code=400,
            detail=f"An API key is required for {payload.routing_provider}. "
                   f"Sign up free at the provider, or choose self-hosted OSRM.",
        )
    set_password(_config, payload.password)
    _config.organization_name = payload.organization_name or "RouteForge"
    _config.routing_provider = payload.routing_provider
    _config.routing_api_key = payload.routing_api_key
    _config.routing_base_url = payload.routing_base_url
    _config.nominatim_url = payload.nominatim_url
    _config.configured = True
    save_config(_config)

    token = _serializer().dumps({"t": time.time()})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_MAX_AGE)
    return {"ok": True, "api_token": _config.api_token}


@app.post("/api/login")
async def api_login(payload: LoginPayload, response: Response) -> Dict[str, Any]:
    if not _config.configured:
        raise HTTPException(status_code=409, detail="This instance hasn't been set up yet.")
    # Constant-ish delay to blunt brute-force attempts.
    await asyncio.sleep(0.4)
    if not verify_password(_config, payload.password):
        raise HTTPException(status_code=401, detail="Incorrect password.")
    token = _serializer().dumps({"t": time.time()})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_MAX_AGE)
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/settings", dependencies=[Depends(require_auth)])
async def api_get_settings() -> Dict[str, Any]:
    data = _config.public_dict()
    data["api_token"] = _config.api_token
    data["routing_base_url"] = _config.routing_base_url
    data["nominatim_url"] = _config.nominatim_url
    data["cache"] = _matrix_cache.stats()
    return data


class SettingsPatch(BaseModel):
    organization_name: Optional[str] = None
    routing_provider: Optional[str] = None
    routing_api_key: Optional[str] = None
    routing_base_url: Optional[str] = None
    nominatim_url: Optional[str] = None
    new_password: Optional[str] = None
    rotate_api_token: bool = False


@app.patch("/api/settings", dependencies=[Depends(require_auth)])
async def api_patch_settings(patch: SettingsPatch) -> Dict[str, Any]:
    if patch.organization_name is not None:
        _config.organization_name = patch.organization_name
    if patch.routing_provider is not None:
        _config.routing_provider = patch.routing_provider
    if patch.routing_api_key is not None:
        _config.routing_api_key = patch.routing_api_key
    if patch.routing_base_url is not None:
        _config.routing_base_url = patch.routing_base_url
    if patch.nominatim_url is not None:
        _config.nominatim_url = patch.nominatim_url
    if patch.new_password:
        if len(patch.new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
        set_password(_config, patch.new_password)
    if patch.rotate_api_token:
        import secrets as _s
        _config.api_token = _s.token_urlsafe(24)
    save_config(_config)
    data = _config.public_dict()
    data["api_token"] = _config.api_token
    return data


# ---------------------------------------------------------------------------
# Plan storage
# ---------------------------------------------------------------------------
@app.get("/api/plan", dependencies=[Depends(require_auth)])
async def api_get_plan() -> Dict[str, Any]:
    return load_plan()


@app.put("/api/plan", dependencies=[Depends(require_auth)])
async def api_put_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    save_plan(plan)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Address search (proxied so the API key never reaches the browser)
# ---------------------------------------------------------------------------
def _provider():
    return build_provider(
        _config.routing_provider,
        api_key=_config.routing_api_key,
        base_url=_config.routing_base_url,
        nominatim_url=_config.nominatim_url,
        cache=_matrix_cache,
    )


@app.get("/api/geocode", dependencies=[Depends(require_auth)])
async def api_geocode(q: str, limit: int = 8) -> Dict[str, Any]:
    if len(q.strip()) < 3:
        return {"results": []}
    try:
        provider = _provider()
        results = await provider.search(q.strip(), limit=limit)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "results": [
            {"label": r.label, "latitude": r.lat, "longitude": r.lon} for r in results
        ]
    }


# ---------------------------------------------------------------------------
# The solve endpoint
# ---------------------------------------------------------------------------
@app.post("/api/solve", response_model=SolveResponse, dependencies=[Depends(require_auth)])
async def api_solve(req: SolveRequest) -> SolveResponse:
    """Optimise a complete plan.

    Fully self-contained: everything the solver needs is in the request body,
    so scripts can call this without any stored state.
    """
    # 1. Cheap, plain-language checks before spending any API calls.
    errors, warnings = check_plan(req)
    if errors:
        return SolveResponse(
            status="infeasible",
            diagnostics=errors,
            warnings=warnings,
            distance_unit=req.settings.distance_unit.value,
        )

    # 2. Build nodes and fetch the travel matrix over unique coordinates only.
    nodes = build_nodes(req)
    coords = [n.coord for n in nodes]
    unique, index_map = dedupe(coords)

    if len(unique) < 2:
        return SolveResponse(
            status="infeasible",
            diagnostics=["All your locations are at the same place, so there's nothing to route."],
            distance_unit=req.settings.distance_unit.value,
        )

    try:
        provider = _provider()
        matrix = await provider.matrix(unique)
    except ProviderError as exc:
        return SolveResponse(
            status="error",
            diagnostics=[str(exc)],
            warnings=warnings,
            distance_unit=req.settings.distance_unit.value,
        )

    # 3. Solve off the event loop.
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        _solver_pool, solve, req, nodes, matrix.durations, matrix.distances, index_map
    )

    result.warnings = warnings + result.warnings
    if result.status == "infeasible" and not result.diagnostics:
        result.diagnostics = explain_no_solution(req)

    # 4. Road geometry for the map — cosmetic, so failures are non-fatal.
    if result.status == "solved":
        try:
            import httpx
            async with httpx.AsyncClient(timeout=45.0) as client:
                for route in result.routes:
                    pts = [Coord(s.latitude, s.longitude) for s in route.stops]
                    if len(pts) >= 2:
                        route.geometry = await provider.directions(client, pts)
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
@app.post("/api/export/csv", dependencies=[Depends(require_auth)])
async def api_export_csv(result: SolveResponse) -> StreamingResponse:
    """Dispatchers print. Give them a spreadsheet."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Vehicle", "Seq", "Type", "Location", "Address",
                     "Arrive", "Depart", "Delivered", "Late (min)"])

    def clock(minutes: float) -> str:
        m = int(round(minutes)) % (24 * 60)
        return f"{m // 60:02d}:{m % 60:02d}"

    for route in result.routes:
        for i, stop in enumerate(route.stops, start=1):
            delivered = "; ".join(f"{k}={v:g}" for k, v in stop.delivered.items())
            writer.writerow([
                route.vehicle_name, i, stop.kind, stop.location_name, stop.address,
                clock(stop.arrival_minutes), clock(stop.departure_minutes),
                delivered, f"{stop.late_by_minutes:g}" if stop.late_by_minutes else "",
            ])
        writer.writerow([])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="routes.csv"'},
    )


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Frontend not built.")
    return FileResponse(index_file)


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # Served from the root so its scope covers the whole app.
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")
