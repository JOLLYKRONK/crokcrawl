"""FastAPI server — Firecrawl-compatible REST API."""

import logging
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from crokcrawl.config import config
from crokcrawl.scraper import Scraper, ScrapeResult
from crokcrawl.crawler import Crawler
from crokcrawl.search import SearchBackend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ─── Pydantic models (Firecrawl-compatible) ──────────────────────────────────


class ScrapeRequest(BaseModel):
    url: str
    formats: list[str] = Field(default_factory=lambda: list(config.default_formats))
    only_main_content: bool = True
    include_tags: list[str] | None = None
    exclude_tags: list[str] | None = None
    render_js: bool | None = None
    force_js_render: bool = False  # Use Playwright even without SPA detection
    wait_ms: int | None = None
    css_selector: str | None = None
    json_schema: dict | None = None
    # Removed: proxy (SSRF risk) — use server-side proxy config instead


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=5, ge=1, le=20)
    lang: str | None = None
    tbs: str | None = None
    sources: list[str] | None = None
    categories: list[str] | None = None
    scrape_options: dict | None = None
    summarize_results: bool = False
    answer: bool = False
    # Removed: llm_api_key — unused field, credential harvesting risk


class CrawlRequest(BaseModel):
    url: str
    max_depth: int = Field(default=2, ge=1, le=10)
    max_pages: int = Field(default=50, ge=1, le=500)
    allow_external: bool = False
    ignore_sitemap: bool = False


class MapRequest(BaseModel):
    url: str
    max_depth: int = Field(default=2, ge=1, le=10)
    use_sitemap: bool = True
    max_urls: int = Field(default=1000, ge=1, le=10000)  # cap for URL discovery


# ─── Authentication middleware ────────────────────────────────────────────────

def _check_api_key(request: Request) -> Optional[JSONResponse]:
    """Validate the API key. Returns None if allowed, or a 401 JSONResponse if not.

    Returning a response (rather than raising HTTPException) is required because
    Starlette's BaseHTTPMiddleware does not catch FastAPI HTTPException — a raise
    here would surface as a 500 to the client.
    """
    if not config.api_key:
        return None  # No auth configured — allow all

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == config.api_key:
        return None

    if request.headers.get("x-api-key", "") == config.api_key:
        return None

    logger.warning("Authentication failed from %s", request.client.host if request.client else "unknown")
    return JSONResponse(status_code=401, content={"detail": "Authentication required"})


# ─── Rate limiting middleware ─────────────────────────────────────────────────

class SimpleRateLimiter:
    """In-memory rate limiter (per-client, requests per minute)."""

    def __init__(self, max_requests: int = 60):
        self.max_requests = max_requests
        self._requests: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_requests))

    def is_allowed(self, client_ip: str) -> bool:
        """Return True if the request is allowed. Sliding window."""
        now = time.time()
        window = 60.0
        timestamps = self._requests[client_ip]
        # Remove timestamps older than the window
        while timestamps and now - timestamps[0] >= window:
            timestamps.popleft()
        if len(timestamps) >= self.max_requests:
            return False
        timestamps.append(now)
        return True

    def cleanup(self) -> int:
        """Remove entries older than 2 minutes. Returns count removed."""
        now = time.time()
        removed = 0
        for ip, timestamps in list(self._requests.items()):
            while timestamps and now - timestamps[0] >= 120:
                timestamps.popleft()
            if not timestamps:
                del self._requests[ip]
                removed += 1
        return removed


_rate_limiter = SimpleRateLimiter(config.rate_limit_rpm)


# ─── Application lifecycle ────────────────────────────────────────────────────

scraper: Optional[Scraper] = None
crawler: Optional[Crawler] = None
search_backend: Optional[SearchBackend] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scraper, crawler, search_backend

    logger.info("Starting crokcrawl...")
    scraper = Scraper(config)
    await scraper.start()
    crawler = Crawler(scraper, config)
    search_backend = SearchBackend(config)

    # Periodic cleanup task
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(60)
            if crawler:
                crawler.cleanup_old_jobs()
            _rate_limiter.cleanup()

    import asyncio
    asyncio.create_task(_periodic_cleanup())

    yield

    await scraper.stop()
    if search_backend:
        await search_backend.close()


app = FastAPI(
    title="crokcrawl",
    description="Open-source web scraping API with JS rendering",
    version="0.2.1",
    lifespan=lifespan,
)

# CORS — restrict to same-origin by default
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Check API key for all protected endpoints (except /health)."""
    if request.url.path != "/health" and not request.url.path.startswith("/openapi") and not request.url.path.startswith("/docs"):
        auth_result = _check_api_key(request)
        if auth_result:
            return auth_result

    # Rate limiting — skip for health check
    if request.url.path != "/health":
        client_ip = request.client.host if request.client else "unknown"
        if not _rate_limiter.is_allowed(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": "60"},
            )

    response = await call_next(request)
    return response


@app.middleware("http")
async def size_limit_middleware(request: Request, call_next):
    """Limit request body size."""
    if request.method in ("POST", "PUT") and request.headers.get("content-length"):
        try:
            content_length = int(request.headers["content-length"])
            if content_length > config.max_request_size:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )
        except (ValueError, TypeError):
            pass

    response = await call_next(request)
    return response


# ─── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Health check — simplified to avoid information disclosure."""
    return {"status": "ok"}


@app.get("/v1/capabilities")
async def capabilities():
    """Report supported features — Firecrawl-compatible."""
    return {
        "formats": ["markdown", "html", "rawHtml", "plainText", "links", "json", "summary"],
        "scrape": {
            "js_render": bool(scraper._js_render_available) if scraper else False,
            "stealth": config.stealth,
        },
        "search": {"available": bool(search_backend)},
    }


@app.post("/v1/scrape")
async def scrape(req: ScrapeRequest):
    """Scrape a URL into clean Markdown.

    Firecrawl-compatible: POST /v1/scrape
    Request: {"url": "...", "formats": ["markdown"]}
    Response: {"success": true, "data": {"markdown": "...", "metadata": {...}}}
    """
    result: ScrapeResult = await scraper.scrape(
        url=req.url,
        formats=req.formats,
        only_main_content=req.only_main_content,
        include_tags=req.include_tags,
        exclude_tags=req.exclude_tags,
        render_js=req.render_js,
        force_js_render=req.force_js_render,
        wait_ms=req.wait_ms,
    )

    if not result.success:
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "error": result.error,
                "data": None,
            },
        )

    # Build response matching Firecrawl shape
    data: dict = {}
    if "markdown" in req.formats:
        data["markdown"] = result.markdown
    if "html" in req.formats:
        data["html"] = result.html
    if "links" in req.formats:
        data["links"] = result.metadata.get("links", [])

    data["metadata"] = {
        "title": result.title,
        "sourceURL": result.source_url,
        "statusCode": result.status_code,
        "description": result.description,
        "is_js_rendered": result.is_js_rendered,
    }
    data["metadata"].update(result.metadata)

    return {
        "success": True,
        "data": data,
    }


@app.post("/v1/search")
async def search(req: SearchRequest):
    """Search the web with optional result scraping.

    Firecrawl-compatible: POST /v1/search
    Request: {\"query\": \"...\", \"limit\": 5}
    Response: {\"success\": true, \"data\": {\"web\": [{url, title, description}, ...]}}
    """
    if not search_backend:
        raise HTTPException(status_code=503, detail="Search not configured")

    result = await search_backend.search(
        query=req.query,
        limit=req.limit,
        lang=req.lang,
        tbs=req.tbs,
        sources=req.sources,
        categories=req.categories,
        scrape_options=req.scrape_options,
    )

    # Normalize to Firecrawl-compatible shape
    if isinstance(result.get("data"), dict):
        # Already grouped — good
        pass
    else:
        # Flat list → convert to grouped by source or flat with "web" key
        flat = result.get("data", [])
        if req.sources:
            # Group by engine/source
            grouped = {"web": flat}
            result = {"success": True, "data": grouped}
        else:
            # Return flat — but we'll match Firecrawl's expected shape
            # Firecrawl expects {"success": true, "data": {"web": [...]}}
            result = {"success": True, "data": {"web": flat}}

    return result


@app.post("/v1/crawl")
async def crawl(req: CrawlRequest):
    """Start an async website crawl.

    Firecrawl-compatible: POST /v1/crawl
    Request: {\"url\": \"...\", \"maxDepth\": 2, \"maxPages\": 50}
    Response: {\"success\": true, \"id\": \"abc123\", \"url\": \"...\"}
    """
    job = crawler.start_crawl(
        url=req.url,
        max_pages=req.max_pages,
        max_depth=req.max_depth,
        allow_external=req.allow_external,
    )

    return {
        "success": True,
        "id": job.id,
        "url": job.url,
        "status": job.status,
    }


@app.get("/v1/crawl/{job_id}")
async def crawl_status(job_id: str):
    """Check crawl job status.

    Firecrawl-compatible: GET /v1/crawl/{id}
    """
    status = crawler.get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "success": True,
        "id": job_id,
        "status": status["status"],
        "data": status["results"],
        "errors": status["errors"],
    }


@app.delete("/v1/crawl/{job_id}")
async def crawl_cancel(job_id: str):
    """Cancel a running crawl.

    Firecrawl-compatible: DELETE /v1/crawl/{id}
    """
    ok = crawler.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "message": "Crawl cancelled"}


@app.post("/v1/map")
async def map_urls(req: MapRequest):
    """Discover URLs on a domain.

    Firecrawl-compatible: POST /v1/map
    Request: {\"url\": \"...\"}
    Response: {\"success\": true, \"links\": [\"url1\", \"url2\", ...]}
    """
    urls = await scraper.map_urls(req.url, max_depth=req.max_depth, max_urls=req.max_urls)

    return {
        "success": True,
        "links": urls,
    }


# ─── Entry point ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("crokcrawl.server:app", host="0.0.0.0", port=config.port, reload=False)
