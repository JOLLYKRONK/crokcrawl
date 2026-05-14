"""Configuration — all settings via environment variables."""

import os
from dataclasses import dataclass, field
from typing import Optional


def _validate_url(val: str, name: str) -> str:
    """Validate a URL configuration value. Returns cleaned URL or raises ValueError."""
    from crokcrawl.url_validation import is_safe_url
    cleaned = val.rstrip("/")
    if cleaned:
        # Allow localhost for internal backends (SearXNG, etc.)
        parsed_url = __import__('urllib.parse').parse.urlparse(cleaned)
        hostname = (parsed_url.hostname or "").lower()
        if hostname not in ("localhost", "127.0.0.1") and not is_safe_url(cleaned):
            raise ValueError(f"{name} must not resolve to a private/internal address: {val}")
    return cleaned


@dataclass
class Config:
    port: int = int(os.environ.get("CROKCRAWL_PORT", "8000"))

    # Authentication — API key for Bearer token (leave empty to disable)
    api_key: str = os.environ.get("CROKCRAWL_API_KEY", "")

    # Rate limiting — requests per minute per client IP
    rate_limit_rpm: int = int(os.environ.get("CROKCRAWL_RATE_LIMIT_RPM", "60"))

    # Request size limit in bytes (1MB default)
    max_request_size: int = int(os.environ.get("CROKCRAWL_MAX_REQUEST_SIZE", "1048576"))

    # Crawl job timeout in seconds (0 = no timeout)
    crawl_timeout: int = int(os.environ.get("CROKCRAWL_CRAWL_TIMEOUT", "300"))

    # Clean up completed jobs older than this many seconds (0 = never)
    job_cleanup_age: int = int(os.environ.get("CROKCRAWL_JOB_CLEANUP_AGE", "600"))

    searxng_url: str = _validate_url(
        os.environ.get("CROKCRAWL_SEARXNG_URL", "http://localhost:8080"),
        "CROKCRAWL_SEARXNG_URL",
    )
    searxng_api_key: Optional[str] = os.environ.get("CROKCRAWL_SEARXNG_API_KEY") or None
    max_concurrency: int = int(os.environ.get("CROKCRAWL_MAX_CONCURRENCY", "4"))
    stealth: bool = os.environ.get("CROKCRAWL_STEALTH", "true").lower() == "true"
    timeout: int = int(os.environ.get("CROKCRAWL_TIMEOUT", "30"))
    wait_for: int = int(os.environ.get("CROKCRAWL_WAIT_FOR", "500"))
    headless: bool = os.environ.get("CROKCRAWL_HEADLESS", "true").lower() == "true"
    js_render: bool = os.environ.get("CROKCRAWL_JS_RENDER", "true").lower() == "true"

    # SearXNG search engine presets (comma-separated list of engines)
    searxng_engines: str = os.environ.get("CROKCRAWL_SEARXNG_ENGINES", "google,bing,duckduckgo,brave")
    searxng_categories: str = os.environ.get("CROKCRAWL_SEARXNG_CATEGORIES", "")

    # Crawl settings
    crawl_max_pages: int = int(os.environ.get("CROKCRAWL_CRAWL_MAX_PAGES", "50"))
    crawl_max_depth: int = int(os.environ.get("CROKCRAWL_CRAWL_MAX_DEPTH", "3"))
    crawl_rate_limit: float = float(os.environ.get("CROKCRAWL_CRAWL_RATE_LIMIT", "1.0"))  # seconds between requests

    # Output formats
    default_formats: list[str] = field(default_factory=lambda: ["markdown"])


config = Config()
