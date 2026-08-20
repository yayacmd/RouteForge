"""Routing provider abstraction.

One interface, several backends. This is what keeps RouteForge from being
hostage to a single vendor's free tier or terms of service:

  - LocationIQ   — easy signup, low free-tier rate limits (default)
  - OpenRouteService — larger free tier
  - OSRM         — self-hosted, unlimited, no key; best for real fleets

Every provider implements geocode/search, a duration+distance matrix, and
road-following directions geometry.

The matrix is the expensive call — it grows O(n²) and is the thing rate
limits punish. Two mitigations live here rather than in any one provider:
  1. Deduplication of identical coordinates before the request is built.
  2. A persistent on-disk cache keyed by coordinate pair, so re-solving a
     mostly-unchanged plan costs almost nothing.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import httpx


@dataclass
class Coord:
    lat: float
    lon: float

    @property
    def key(self) -> str:
        return f"{self.lat:.6f},{self.lon:.6f}"


@dataclass
class MatrixResult:
    """Square matrices indexed the same way as the input coordinate list."""
    durations: List[List[float]]   # seconds
    distances: List[List[float]]   # meters


@dataclass
class GeocodeResult:
    label: str
    lat: float
    lon: float


class ProviderError(RuntimeError):
    """Raised with a message safe to show a non-technical user."""


# ---------------------------------------------------------------------------
# Persistent pair cache
# ---------------------------------------------------------------------------
class PairCache:
    """Caches duration/distance for ordered coordinate pairs.

    Depots and regular customers barely move, so a day-to-day re-solve hits
    this cache for nearly every pair. Stored as a plain JSON file — readable,
    forkable, no database.
    """

    def __init__(self, path: Optional[Path] = None, max_entries: int = 200_000):
        self.path = path
        self.max_entries = max_entries
        self._data: Dict[str, List[float]] = {}
        self._dirty = False
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def save(self) -> None:
        if not (self.path and self._dirty):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if len(self._data) > self.max_entries:
                # Cheap eviction: keep the most recently inserted half.
                keys = list(self._data)[-(self.max_entries // 2):]
                self._data = {k: self._data[k] for k in keys}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data))
            tmp.replace(self.path)
            self._dirty = False
        except OSError:
            pass  # cache is an optimisation; never fatal

    @staticmethod
    def _key(a: Coord, b: Coord) -> str:
        return f"{a.key}|{b.key}"

    def get(self, a: Coord, b: Coord) -> Optional[Tuple[float, float]]:
        v = self._data.get(self._key(a, b))
        return (v[0], v[1]) if v else None

    def put(self, a: Coord, b: Coord, duration: float, distance: float) -> None:
        self._data[self._key(a, b)] = [duration, distance]
        self._dirty = True

    def stats(self) -> Dict[str, int]:
        return {"entries": len(self._data)}


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------
class RoutingProvider(ABC):
    """Interface every backend implements."""

    name: str = "base"
    #: Max coordinates per matrix request. Providers override.
    max_matrix_size: int = 25
    #: Seconds to pause between calls to respect rate limits.
    request_delay: float = 0.0

    def __init__(self, api_key: str = "", base_url: str = "", cache: Optional[PairCache] = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.cache = cache or PairCache()

    # -- required by subclasses ------------------------------------------
    @abstractmethod
    async def search(self, query: str, limit: int = 8) -> List[GeocodeResult]:
        """Address autocomplete."""

    @abstractmethod
    async def _matrix_chunk(
        self, client: httpx.AsyncClient, sources: Sequence[Coord], destinations: Sequence[Coord]
    ) -> MatrixResult:
        """Durations/distances from every source to every destination."""

    @abstractmethod
    async def directions(self, client: httpx.AsyncClient, coords: Sequence[Coord]) -> List[List[float]]:
        """Road-following polyline as [[lon, lat], ...]."""

    # -- shared behaviour -------------------------------------------------
    async def matrix(self, coords: Sequence[Coord]) -> MatrixResult:
        """Full square matrix, using the cache and chunking as needed."""
        n = len(coords)
        durations = [[0.0] * n for _ in range(n)]
        distances = [[0.0] * n for _ in range(n)]

        # Work out which pairs we still need.
        missing: List[Tuple[int, int]] = []
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                hit = self.cache.get(coords[i], coords[j])
                if hit:
                    durations[i][j], distances[i][j] = hit
                else:
                    missing.append((i, j))

        if not missing:
            return MatrixResult(durations, distances)

        # Fetch in square blocks sized to the provider's limit.
        need_idx = sorted({i for i, _ in missing} | {j for _, j in missing})
        size = max(2, self.max_matrix_size // 2)
        blocks = [need_idx[k:k + size] for k in range(0, len(need_idx), size)]

        timeout = httpx.Timeout(60.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for bi in blocks:
                for bj in blocks:
                    src = [coords[i] for i in bi]
                    dst = [coords[j] for j in bj]
                    chunk = await self._matrix_chunk(client, src, dst)
                    for a, i in enumerate(bi):
                        for b, j in enumerate(bj):
                            if i == j:
                                continue
                            d = float(chunk.durations[a][b])
                            m = float(chunk.distances[a][b])
                            if math.isnan(d) or math.isnan(m):
                                raise ProviderError(
                                    "The routing service couldn't find a road route "
                                    "between two of your locations. Check that every "
                                    "address is reachable by road."
                                )
                            durations[i][j], distances[i][j] = d, m
                            self.cache.put(coords[i], coords[j], d, m)
                    if self.request_delay:
                        await asyncio.sleep(self.request_delay)

        self.cache.save()
        return MatrixResult(durations, distances)

    @staticmethod
    def _friendly_http_error(exc: httpx.HTTPStatusError, provider: str) -> ProviderError:
        code = exc.response.status_code
        if code in (401, 403):
            return ProviderError(
                f"The {provider} API key was rejected. Check the key in Settings."
            )
        if code == 429:
            return ProviderError(
                f"{provider} is rate-limiting requests. Wait a minute and try "
                f"again, or switch to a self-hosted OSRM server for unlimited use."
            )
        return ProviderError(
            f"The {provider} routing service returned an error (HTTP {code}). "
            f"Try again in a moment."
        )


def dedupe(coords: Sequence[Coord]) -> Tuple[List[Coord], List[int]]:
    """Collapse identical points.

    Returns the unique list plus an index mapping original -> unique. The old
    desktop version duplicated every depot per vehicle, which inflated an
    O(n²) matrix badly; this removes that cost entirely.
    """
    unique: List[Coord] = []
    seen: Dict[str, int] = {}
    mapping: List[int] = []
    for c in coords:
        if c.key not in seen:
            seen[c.key] = len(unique)
            unique.append(c)
        mapping.append(seen[c.key])
    return unique, mapping
