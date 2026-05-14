"""Web search — SearXNG integration + DuckDuckGo fallback."""

import logging
from typing import Optional, Any

import httpx
from bs4 import BeautifulSoup

from crokcrawl.url_validation import is_safe_url

logger = logging.getLogger(__name__)


class SearchBackend:
    """Search backend using SearXNG or DuckDuckGo fallback."""

    def __init__(self, config):
        self.config = config
        self._client = httpx.AsyncClient(
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            },
        )

    async def search(
        self,
        query: str,
        limit: int = 5,
        lang: str | None = None,
        tbs: str | None = None,
        sources: list[str] | None = None,
        categories: list[str] | None = None,
        scrape_options: dict | None = None,
        **kwargs: Any,
    ) -> dict:
        """Search the web. Tries SearXNG first, falls back to DuckDuckGo.

        Returns results in Firecrawl-compatible format:
        {"success": true, "data": {"web": [{url, title, description}, ...]}}
        """
        # Try SearXNG first
        results = await self._search_searxng(
            query, limit, lang, tbs, sources, categories, scrape_options, **kwargs
        )

        if results and len(results.get("web", [])) > 0:
            return results

        # Fallback: DuckDuckGo
        logger.info("SearXNG unavailable/unsuccessful, falling back to DuckDuckGo")
        results = await self._search_ddg(
            query, limit, lang, tbs, sources, scrape_options, **kwargs
        )
        return results

    async def _search_searxng(
        self,
        query: str,
        limit: int,
        lang: str | None,
        tbs: str | None,
        sources: list[str] | None,
        categories: list[str] | None,
        scrape_options: dict | None,
        **kwargs: Any,
    ) -> dict:
        """Search using SearXNG instance."""
        url = f"{self.config.searxng_url}/search"

        params: dict = {
            "q": query,
            "format": "json",
            "engines": self.config.searxng_engines,
        }

        if lang:
            params["language"] = lang
        if tbs:
            params["time_range"] = tbs
        if categories:
            params["categories"] = ",".join(categories)

        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            # Normalize results
            raw_results = data.get("results", [])
            raw_results = raw_results[: limit * 3]  # get extra for scraping

            # Build response
            flat_results = []
            grouped: dict[str, list[dict]] = {}

            for r in raw_results:
                item_url = r.get("url", "")
                # Filter out unsafe URLs from search results
                if item_url and not is_safe_url(item_url):
                    logger.warning("Blocked unsafe search result: %s", item_url)
                    continue
                item = {
                    "url": item_url,
                    "title": r.get("title", ""),
                    "description": r.get("content", ""),
                    "score": self._score_result(r),
                }
                if r.get("publishedDate"):
                    item["publishedDate"] = r["publishedDate"]

                engine = r.get("engine", "web")
                if engine not in grouped:
                    grouped[engine] = []
                grouped[engine].append(item)
                flat_results.append(item)

            # Return grouped if sources specified, else flat with "web" key
            if sources:
                data_out = {}
                for src in sources:
                    if src == "web":
                        data_out["web"] = flat_results[:limit]
                    elif src == "news":
                        data_out["news"] = flat_results[:limit]
                    else:
                        data_out[src] = grouped.get(src, [])[:limit]
                return {"success": True, "data": data_out}
            else:
                return {"success": True, "data": {"web": flat_results[:limit]}}

        except httpx.HTTPError as e:
            logger.error("SearXNG request failed: %s", e)
            return {"success": True, "data": {"web": []}}
        except Exception as e:
            logger.error("SearXNG parse failed: %s", e)
            return {"success": True, "data": {"web": []}}

    async def _search_ddg(
        self,
        query: str,
        limit: int,
        lang: str | None,
        tbs: str | None,
        sources: list[str] | None,
        scrape_options: dict | None,
        **kwargs: Any,
    ) -> dict:
        """Search using the ``ddgs`` Python package (DuckDuckGo HTML scrape).

        This is a fallback when SearXNG is unavailable.
        Uses the community-maintained ``ddgs`` package which handles
        anti-bot measures better than raw HTML scraping.
        """
        try:
            import ddgs  # type: ignore  # noqa: F811
        except ImportError:
            logger.warning("ddgs package not installed — search disabled")
            return {"success": True, "data": {"web": []}}

        try:
            ddgs_client = ddgs.DDGS()
            results = []

            for result in ddgs_client.text(query, max_results=limit):
                results.append({
                    "url": result.get("href", result.get("url", "")),
                    "title": result.get("title", ""),
                    "description": result.get("body", result.get("description", "")),
                    "score": 10.0 - len(results) * 0.5,
                })

            return {
                "success": True,
                "data": {"web": results[:limit]},
            }

        except Exception as e:
            logger.error("DuckDuckGo search failed: %s", e)
            return {"success": True, "data": {"web": []}}

    def _score_result(self, result: dict) -> float:
        """Simple relevance score for a search result."""
        score = 10.0
        position = result.get("position", 0) or result.get("resultsIndex", 0)
        if position:
            score += (20 - position) * 0.5
        return round(score, 1)

    async def close(self):
        """Close HTTP client."""
        await self._client.aclose()
