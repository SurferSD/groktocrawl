"""SearXNG JSON API client."""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class SearchHealth:
    """Health information about a SearXNG search request.

    Attributes:
        engines_total: Number of engines queried by SearXNG.
        engines_responding: Number of engines that returned at least one result.
        empty_result: True when engines responded but no results were returned
            (distinct from an infrastructure failure).
        degraded: True when fewer than half of queried engines responded.
        detail: Human-readable summary of engine status.
    """

    engines_total: int = 0
    engines_responding: int = 0
    empty_result: bool = False
    degraded: bool = False
    detail: str = ""


# ── Firecrawl v2 → SearXNG category translation ────────────────
# Maps Firecrawl v2 search dimensions (sources, categories) to
# SearXNG-native category names. Unknown values pass through for
# forward compatibility. See ADR-0013 and issue #85.

_SOURCES_MAP = {
    "news": "news",
    "images": "images",
    "web": "general",
    "video": "videos",
    "social": "general",
}

_CATEGORIES_MAP = {
    "research": "science",
    "github": "it",
    "pdf": "general",
    "news": "news",
    "science": "science",
    "it": "it",
    "general": "general",
}


class SearXNGClient:
    """Client for the SearXNG search engine JSON API."""

    def __init__(self, base_url: str = "http://searxng:8080", max_searches: int = 5):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=15,
            headers={
                "User-Agent": "GroktoCrawl/0.1",
                "Accept": "text/html,application/json",
                "X-Forwarded-For": "127.0.0.1",
            },
        )
        self._search_count = 0
        self._max_searches = max_searches

    @staticmethod
    def _translate(
        sources: list[str] | None,
        categories: list[str] | None,
    ) -> list[str]:
        """Translate Firecrawl v2 sources/categories to SearXNG category names.

        Merges both dimensions into a single SearXNG categories list.
        Unknown values pass through for forward compatibility.
        Returns ``[\"general\"]`` if no mapping produces a category.
        """
        result: list[str] = []
        if sources:
            for s in sources:
                mapped = _SOURCES_MAP.get(s, s)
                if mapped and mapped not in result:
                    result.append(mapped)
        if categories:
            for c in categories:
                mapped = _CATEGORIES_MAP.get(c, c)
                if mapped and mapped not in result:
                    result.append(mapped)
        return result or ["general"]

    @staticmethod
    def _parse_engine_health(data: dict, results: list[dict]) -> SearchHealth:
        """Parse SearXNG engine status from the API response.

        Inspects the ``engines`` key in the SearXNG JSON response to
        determine how many engines were queried and how many returned
        results, building a ``SearchHealth`` summary.
        """
        engines = data.get("engines", [])
        engines_total = len(engines)
        engines_responding = sum(1 for e in engines if e.get("results", 0) > 0)

        empty_result = bool(
            engines_responding > 0 and not any(r.get("url") for r in results)
        )
        degraded = bool(engines_total > 0 and engines_responding < engines_total / 2)

        # Build a human-readable detail string
        if engines_total == 0:
            detail = "No engine status available from SearXNG"
        elif degraded:
            detail = (
                f"Degraded: {engines_responding}/{engines_total} engines "
                f"returned results"
            )
        elif empty_result:
            detail = f"All {engines_total} engines responded but returned no results"
        else:
            detail = (
                f"Healthy: {engines_responding}/{engines_total} engines "
                f"returned results"
            )

        return SearchHealth(
            engines_total=engines_total,
            engines_responding=engines_responding,
            empty_result=empty_result,
            degraded=degraded,
            detail=detail,
        )

    async def search(
        self,
        query: str,
        limit: int = 10,
        categories: list[str] | None = None,
        sources: list[str] | None = None,
    ) -> tuple[list[dict], SearchHealth]:
        """Search the web and return structured results with health info.

        Uses SearXNG's JSON API. When categories is None, defaults to "general".
        ``sources`` and ``categories`` are merged via ``_translate()`` before
        being passed to SearXNG.

        Enforces a per-request search budget. Raises ``RateLimitedError``
        when the budget is exhausted.

        Returns a tuple of (results, health) where:
        - results: list of dicts with keys: url, title, description, engine.
        - health: SearchHealth dataclass with engine status information.
        """
        # ── Per-request search budget check ────────────────────
        if self._search_count >= self._max_searches:
            from .exceptions import RateLimitedError

            raise RateLimitedError(
                detail=(
                    f"Search budget exceeded: {self._search_count}/{self._max_searches}"
                ),
                details={
                    "budget_used": self._search_count,
                    "budget_max": self._max_searches,
                },
            )
        self._search_count += 1

        effective_categories = self._translate(sources, categories)
        params = {
            "q": query,
            "format": "json",
            "language": "en",
            "pageno": 1,
        }
        params["categories"] = ",".join(effective_categories)

        try:
            resp = await self._client.get(
                f"{self.base_url}/search",
                params=params,  # type: ignore[arg-type]
            )
            if resp.status_code != 200:
                logger.warning(
                    "SearXNG returned %d: %s", resp.status_code, resp.text[:200]
                )
                return [], SearchHealth(
                    detail=f"SearXNG returned HTTP {resp.status_code}"
                )

            data = resp.json()
            results = []
            for item in data.get("results", []):
                results.append(
                    {
                        "url": item.get("url", ""),
                        "title": item.get("title", ""),
                        "description": item.get("content", ""),
                        "engine": item.get("engine", ""),
                    }
                )

            results = results[:limit]

            # ── Parse engine health ────────────────────────────────────
            health = self._parse_engine_health(data, results)

            return results, health

        except httpx.TimeoutException:
            logger.warning("SearXNG search timed out for query: %s", query)
            return [], SearchHealth(detail="SearXNG request timed out")
        except Exception as e:
            logger.error("SearXNG search failed: %s", e)
            return [], SearchHealth(detail=f"SearXNG search failed: {e}")

    async def close(self) -> None:
        await self._client.aclose()
