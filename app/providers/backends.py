"""Concrete routing backends.

All three speak the same interface, so switching is a config change:

  ROUTING_PROVIDER=locationiq   LOCATIONIQ_API_KEY=pk.xxx
  ROUTING_PROVIDER=ors          ORS_API_KEY=xxx
  ROUTING_PROVIDER=osrm         OSRM_URL=http://osrm:5000
                                NOMINATIM_URL=http://nominatim:8080

OSRM is the recommended setup for anyone running real daily volume: no key,
no rate limit, and the matrix call stops being the slow part of a solve.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import httpx

from .base import (Coord, GeocodeResult, MatrixResult, PairCache,
                   ProviderError, RoutingProvider)


class LocationIQProvider(RoutingProvider):
    """Hosted OSRM/Nominatim with an easy signup. Small free tier."""

    name = "locationiq"
    max_matrix_size = 24
    request_delay = 0.6  # free tier is ~2 requests/second

    def __init__(self, api_key: str = "", base_url: str = "", cache: Optional[PairCache] = None):
        super().__init__(api_key, base_url or "https://us1.locationiq.com/v1", cache)
        if not api_key:
            raise ProviderError(
                "No LocationIQ API key is configured. Add one in Settings, or "
                "switch to a self-hosted OSRM server."
            )

    async def search(self, query: str, limit: int = 8) -> List[GeocodeResult]:
        params = {
            "key": self.api_key, "q": query, "format": "json",
            "limit": str(limit), "accept-language": "en",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                r = await client.get(f"{self.base_url}/search.php", params=params)
                if r.status_code == 404:
                    return []
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise self._friendly_http_error(exc, "LocationIQ") from exc
            except httpx.RequestError as exc:
                raise ProviderError("Couldn't reach LocationIQ. Check the server's internet connection.") from exc
            return [
                GeocodeResult(label=d["display_name"], lat=float(d["lat"]), lon=float(d["lon"]))
                for d in r.json()
            ]

    async def _matrix_chunk(self, client, sources, destinations) -> MatrixResult:
        combined = list(sources) + list(destinations)
        path = ";".join(f"{c.lon},{c.lat}" for c in combined)
        src_idx = ";".join(str(i) for i in range(len(sources)))
        dst_idx = ";".join(str(i) for i in range(len(sources), len(combined)))
        params = {
            "key": self.api_key, "annotations": "distance,duration",
            "sources": src_idx, "destinations": dst_idx,
        }
        try:
            r = await client.get(f"{self.base_url}/matrix/driving/{path}", params=params)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._friendly_http_error(exc, "LocationIQ") from exc
        except httpx.RequestError as exc:
            raise ProviderError("Couldn't reach LocationIQ while building the distance matrix.") from exc
        data = r.json()
        return MatrixResult(durations=data["durations"], distances=data["distances"])

    async def directions(self, client, coords: Sequence[Coord]) -> List[List[float]]:
        path = ";".join(f"{c.lon},{c.lat}" for c in coords)
        params = {"key": self.api_key, "overview": "full", "geometries": "geojson"}
        try:
            r = await client.get(f"{self.base_url}/directions/driving/{path}", params=params)
            r.raise_for_status()
            return r.json()["routes"][0]["geometry"]["coordinates"]
        except (httpx.HTTPError, KeyError, IndexError):
            return []  # map polyline is cosmetic; never fail a solve over it


class OSRMProvider(RoutingProvider):
    """Self-hosted OSRM. No key, no limits. Geocoding via Nominatim."""

    name = "osrm"
    max_matrix_size = 200
    request_delay = 0.0

    def __init__(self, api_key: str = "", base_url: str = "",
                 cache: Optional[PairCache] = None, nominatim_url: str = ""):
        super().__init__(api_key, base_url or "http://localhost:5000", cache)
        self.nominatim_url = (nominatim_url or "").rstrip("/")

    async def search(self, query: str, limit: int = 8) -> List[GeocodeResult]:
        if not self.nominatim_url:
            raise ProviderError(
                "Address search needs a Nominatim server. Set NOMINATIM_URL, or "
                "enter coordinates manually."
            )
        params = {"q": query, "format": "json", "limit": str(limit)}
        headers = {"User-Agent": "RouteForge/1.0"}
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            try:
                r = await client.get(f"{self.nominatim_url}/search", params=params)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise self._friendly_http_error(exc, "Nominatim") from exc
            except httpx.RequestError as exc:
                raise ProviderError("Couldn't reach the Nominatim geocoding server.") from exc
            return [
                GeocodeResult(label=d["display_name"], lat=float(d["lat"]), lon=float(d["lon"]))
                for d in r.json()
            ]

    async def _matrix_chunk(self, client, sources, destinations) -> MatrixResult:
        combined = list(sources) + list(destinations)
        path = ";".join(f"{c.lon},{c.lat}" for c in combined)
        params = {
            "annotations": "distance,duration",
            "sources": ";".join(str(i) for i in range(len(sources))),
            "destinations": ";".join(str(i) for i in range(len(sources), len(combined))),
        }
        try:
            r = await client.get(f"{self.base_url}/table/v1/driving/{path}", params=params)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._friendly_http_error(exc, "OSRM") from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                "Couldn't reach the OSRM server. Check that the OSRM container "
                "is running and OSRM_URL is correct."
            ) from exc
        data = r.json()
        return MatrixResult(durations=data["durations"], distances=data["distances"])

    async def directions(self, client, coords: Sequence[Coord]) -> List[List[float]]:
        path = ";".join(f"{c.lon},{c.lat}" for c in coords)
        params = {"overview": "full", "geometries": "geojson"}
        try:
            r = await client.get(f"{self.base_url}/route/v1/driving/{path}", params=params)
            r.raise_for_status()
            return r.json()["routes"][0]["geometry"]["coordinates"]
        except (httpx.HTTPError, KeyError, IndexError):
            return []


class ORSProvider(RoutingProvider):
    """OpenRouteService — bigger free tier than LocationIQ."""

    name = "ors"
    max_matrix_size = 50
    request_delay = 1.0

    def __init__(self, api_key: str = "", base_url: str = "", cache: Optional[PairCache] = None):
        super().__init__(api_key, base_url or "https://api.openrouteservice.org", cache)
        if not api_key:
            raise ProviderError("No OpenRouteService API key is configured. Add one in Settings.")

    async def search(self, query: str, limit: int = 8) -> List[GeocodeResult]:
        params = {"api_key": self.api_key, "text": query, "size": str(limit)}
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                r = await client.get(f"{self.base_url}/geocode/search", params=params)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise self._friendly_http_error(exc, "OpenRouteService") from exc
            except httpx.RequestError as exc:
                raise ProviderError("Couldn't reach OpenRouteService.") from exc
            out = []
            for f in r.json().get("features", []):
                lon, lat = f["geometry"]["coordinates"]
                out.append(GeocodeResult(label=f["properties"].get("label", ""), lat=lat, lon=lon))
            return out

    async def _matrix_chunk(self, client, sources, destinations) -> MatrixResult:
        combined = list(sources) + list(destinations)
        body = {
            "locations": [[c.lon, c.lat] for c in combined],
            "sources": list(range(len(sources))),
            "destinations": list(range(len(sources), len(combined))),
            "metrics": ["duration", "distance"],
        }
        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        try:
            r = await client.post(
                f"{self.base_url}/v2/matrix/driving-car", json=body, headers=headers)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._friendly_http_error(exc, "OpenRouteService") from exc
        except httpx.RequestError as exc:
            raise ProviderError("Couldn't reach OpenRouteService while building the matrix.") from exc
        data = r.json()
        return MatrixResult(durations=data["durations"], distances=data["distances"])

    async def directions(self, client, coords: Sequence[Coord]) -> List[List[float]]:
        body = {"coordinates": [[c.lon, c.lat] for c in coords]}
        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        try:
            r = await client.post(
                f"{self.base_url}/v2/directions/driving-car/geojson", json=body, headers=headers)
            r.raise_for_status()
            return r.json()["features"][0]["geometry"]["coordinates"]
        except (httpx.HTTPError, KeyError, IndexError):
            return []


PROVIDERS = {
    "locationiq": LocationIQProvider,
    "osrm": OSRMProvider,
    "ors": ORSProvider,
}


def build_provider(name: str, *, api_key: str = "", base_url: str = "",
                   nominatim_url: str = "", cache: Optional[PairCache] = None) -> RoutingProvider:
    cls = PROVIDERS.get((name or "locationiq").lower())
    if cls is None:
        raise ProviderError(
            f"Unknown routing provider '{name}'. Choose one of: {', '.join(PROVIDERS)}."
        )
    if cls is OSRMProvider:
        return cls(api_key=api_key, base_url=base_url, cache=cache, nominatim_url=nominatim_url)
    return cls(api_key=api_key, base_url=base_url, cache=cache)
