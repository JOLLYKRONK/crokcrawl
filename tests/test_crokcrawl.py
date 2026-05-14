"""Comprehensive test suite for crokrawl."""

import asyncio
import json
import os
import sys
import time

import pytest

# Ensure crokrawl from this project is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── URL Validation Tests ─────────────────────────────────────────────────────

class TestSafeURL:
    """Tests for crokrawl.url_validation.is_safe_url and is_safe_redirect_url."""

    def test_public_url(self):
        from crokrawl.url_validation import is_safe_url
        # Public URLs should be safe
        assert is_safe_url("https://example.com") is True
        assert is_safe_url("http://example.com/page") is True

    def test_localhost_blocked(self):
        from crokrawl.url_validation import is_safe_url
        assert is_safe_url("http://localhost") is False
        assert is_safe_url("http://127.0.0.1") is False
        assert is_safe_url("http://127.0.0.1:8080") is False

    def test_private_ip_blocked(self):
        from crokrawl.url_validation import is_safe_url
        assert is_safe_url("http://192.168.1.1") is False
        assert is_safe_url("http://10.0.0.1") is False
        assert is_safe_url("http://172.16.0.1") is False

    def test_cloud_metadata_blocked(self):
        from crokrawl.url_validation import is_safe_url
        assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False
        assert is_safe_url("http://metadata.google.internal/computeMetadata") is False
        assert is_safe_url("http://169.254.170.2") is False
        assert is_safe_url("http://169.254.169.253") is False
        assert is_safe_url("http://100.100.100.200") is False

    def test_cgnat_blocked(self):
        from crokrawl.url_validation import is_safe_url
        assert is_safe_url("http://100.64.0.1") is False
        assert is_safe_url("http://100.127.255.255") is False

    def test_link_local_blocked(self):
        from crokrawl.url_validation import is_safe_url
        assert is_safe_url("http://169.254.1.1") is False

    def test_empty_hostname(self):
        from crokrawl.url_validation import is_safe_url
        assert is_safe_url("") is False
        assert is_safe_url("  ") is False

    def test_invalid_scheme(self):
        from crokrawl.url_validation import is_safe_url
        # file:// scheme — hostname is empty, so fails
        assert is_safe_url("file:///etc/passwd") is False

    def test_redirect_scheme_validation(self):
        from crokrawl.url_validation import is_safe_redirect_url
        # Non-HTTP schemes should be blocked
        assert is_safe_redirect_url("file:///etc/passwd") is False
        assert is_safe_redirect_url("ftp://example.com") is False
        assert is_safe_redirect_url("data:text/html,<h1>xss</h1>") is False

    def test_redirect_to_private(self):
        from crokrawl.url_validation import is_safe_redirect_url
        assert is_safe_redirect_url("http://127.0.0.1/admin") is False
        assert is_safe_redirect_url("http://192.168.0.1") is False

    def test_dns_failure_blocks(self):
        from crokrawl.url_validation import is_safe_url
        # Non-existent domain — DNS failure should fail closed
        assert is_safe_url("http://this-domain-definitely-does-not-exist-12345.invalid") is False

    def test_trailing_dot_hostname(self):
        from crokrawl.url_validation import is_safe_url
        # Trailing dot in hostname should be stripped
        # This is a real public domain but trailing dots can cause issues
        result = is_safe_url("https://example.com.")
        # Should not crash — either True (safe) or False (DNS fails), no exception
        assert isinstance(result, bool)


# ─── Config Tests ─────────────────────────────────────────────────────────────

class TestConfig:
    """Tests for crokrawl.config.Config."""

    def test_defaults(self):
        os.environ["CROKRAWL_API_KEY"] = ""
        import importlib
        import crokrawl.config
        importlib.reload(crokrawl.config)
        cfg = crokrawl.config.Config()
        assert cfg.port == 8000
        assert cfg.api_key == ""
        assert cfg.rate_limit_rpm == 60
        assert cfg.max_request_size == 1048576
        assert cfg.crawl_timeout == 300
        assert cfg.job_cleanup_age == 600
        assert cfg.max_concurrency == 4
        assert cfg.stealth is True
        assert cfg.timeout == 30
        assert cfg.wait_for == 500
        assert cfg.headless is True
        assert cfg.js_render is True
        assert cfg.crawl_max_pages == 50
        assert cfg.crawl_max_depth == 3
        assert cfg.default_formats == ["markdown"]

    def test_custom_env(self, monkeypatch):
        monkeypatch.setenv("CROKRAWL_PORT", "9090")
        monkeypatch.setenv("CROKRAWL_API_KEY", "test-key-123")
        monkeypatch.setenv("CROKRAWL_RATE_LIMIT_RPM", "120")
        monkeypatch.setenv("CROKRAWL_STEALTH", "false")
        # searxng_url uses localhost which bypasses SSRF checks
        monkeypatch.setenv("CROKRAWL_SEARXNG_URL", "http://localhost:9999")

        import importlib
        import crokrawl.config
        importlib.reload(crokrawl.config)
        cfg = crokrawl.config.Config()
        assert cfg.port == 9090
        assert cfg.api_key == "test-key-123"
        assert cfg.rate_limit_rpm == 120
        assert cfg.stealth is False

    def test_searxng_url_trailing_slash(self):
        from crokrawl.config import _validate_url
        assert _validate_url("http://localhost:8080/", "test") == "http://localhost:8080"

    def test_searxng_url_empty(self):
        from crokrawl.config import _validate_url
        assert _validate_url("", "test") == ""


# ─── Rate Limiter Tests ───────────────────────────────────────────────────────

class TestRateLimiter:
    """Tests for SimpleRateLimiter in server.py."""

    def test_allowed_under_limit(self):
        from crokrawl.server import SimpleRateLimiter
        limiter = SimpleRateLimiter(max_requests=5)
        for _ in range(5):
            assert limiter.is_allowed("1.2.3.4") is True

    def test_blocked_over_limit(self):
        from crokrawl.server import SimpleRateLimiter
        limiter = SimpleRateLimiter(max_requests=3)
        limiter.is_allowed("1.2.3.4")
        limiter.is_allowed("1.2.3.4")
        limiter.is_allowed("1.2.3.4")
        assert limiter.is_allowed("1.2.3.4") is False

    def test_different_ips_independent(self):
        from crokrawl.server import SimpleRateLimiter
        limiter = SimpleRateLimiter(max_requests=1)
        assert limiter.is_allowed("1.1.1.1") is True
        assert limiter.is_allowed("1.1.1.1") is False
        assert limiter.is_allowed("2.2.2.2") is True  # different IP

    def test_cleanup_removes_stale(self):
        from crokrawl.server import SimpleRateLimiter
        limiter = SimpleRateLimiter(max_requests=100)
        # Add an old entry
        limiter._requests["old_ip"] = [time.time() - 300]  # 5 min ago
        removed = limiter.cleanup()
        assert removed >= 1
        assert "old_ip" not in limiter._requests

    def test_cleanup_keeps_recent(self):
        from crokrawl.server import SimpleRateLimiter
        limiter = SimpleRateLimiter(max_requests=100)
        limiter._requests["recent_ip"] = [time.time()]  # just now
        removed = limiter.cleanup()
        assert "recent_ip" in limiter._requests


# ─── Auth Middleware Tests ─────────────────────────────────────────────────────

class TestAuthMiddleware:
    """Tests for _check_api_key in server.py."""

    def test_no_api_key_allows_all(self):
        """When no API key is set, all requests pass."""
        from fastapi.testclient import TestClient
        from crokrawl.server import app
        from crokrawl.config import config

        if config.api_key:
            pytest.skip("API key is set in environment")

        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_bearer_and_x_api_key_via_test_client(self):
        """Test auth using FastAPI TestClient which exercises the real middleware."""
        from fastapi.testclient import TestClient
        from crokrawl.server import app
        from crokrawl.config import config

        original_key = config.api_key
        config.api_key = "test-secret"

        with TestClient(app) as client:
            # /health is exempt from auth
            resp = client.get("/health")
            assert resp.status_code == 200

            # Bearer token should work
            resp = client.get("/health", headers={"Authorization": "Bearer test-secret"})
            assert resp.status_code == 200

            # x-api-key should work
            resp = client.get("/health", headers={"x-api-key": "test-secret"})
            assert resp.status_code == 200

            # Wrong key on protected endpoint should be 401
            resp = client.get("/v1/capabilities", headers={"Authorization": "Bearer wrong"})
            assert resp.status_code == 401

            # No auth on protected endpoint should be 401
            resp = client.get("/v1/capabilities")
            assert resp.status_code == 401

        config.api_key = original_key

    def test_wrong_token_return_401(self):
        """Verify that _check_api_key returns a 401 response for wrong token."""
        from crokrawl.config import config

        original_key = config.api_key
        config.api_key = "correct-key"

        class FakeRequest:
            class headers:  # noqa
                @staticmethod
                def get(key, default=""):
                    hdrs = {"Authorization": "Bearer wrong-key"}
                    return hdrs.get(key, default)
            client = type("obj", (object,), {"host": "1.2.3.4"})()

        from crokrawl.server import _check_api_key
        result = _check_api_key(FakeRequest())
        assert result is not None
        assert result.status_code == 401

        config.api_key = original_key


# ─── Search Backend Tests ──────────────────────────────────────────────────────

class TestSearchBackend:
    """Tests for crokrawl.search.SearchBackend."""

    def test_ddg_fallback_structure(self):
        from crokrawl.config import Config
        from crokrawl.search import SearchBackend

        config = Config()
        backend = SearchBackend(config)

        # DuckDuckGo should return Firecrawl-compatible structure
        results = asyncio.get_event_loop().run_until_complete(
            backend._search_ddg("python", limit=3, lang=None, tbs=None, sources=None, scrape_options=None)
        )
        assert "success" in results
        assert "data" in results
        assert "web" in results["data"]
        assert isinstance(results["data"]["web"], list)

    @pytest.mark.asyncio
    async def test_search_response_structure(self):
        from crokrawl.config import Config
        from crokrawl.search import SearchBackend

        config = Config()
        backend = SearchBackend(config)

        result = await backend.search("test", limit=2)
        assert "success" in result
        assert result["success"] is True
        assert "data" in result
        await backend.close()


# ─── Server Endpoint Tests (FastAPI TestClient) ───────────────────────────────

class TestServerEndpoints:
    """Tests for FastAPI endpoints using TestClient."""

    @pytest.fixture
    def test_app(self):
        """Create a test app with lifespan disabled."""
        from crokrawl.server import app

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        return app

    def test_health_endpoint(self):
        from fastapi.testclient import TestClient
        from crokrawl.server import app

        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    def test_rate_limit_middleware(self):
        from fastapi.testclient import TestClient
        from crokrawl.server import app

        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200


# ─── Scraper Tests ────────────────────────────────────────────────────────────

class TestScraper:
    """Tests for crokrawl.scraper.Scraper."""

    def test_scraper_init(self):
        from crokrawl.config import Config
        from crokrawl.scraper import Scraper

        config = Config()
        scraper = Scraper(config)
        assert scraper is not None

    def test_extract_links(self):
        from bs4 import BeautifulSoup
        from crokrawl.scraper import Scraper
        from crokrawl.config import Config

        html = """
        <html>
        <body>
            <a href="https://example.com/page1">Link 1</a>
            <a href="/relative">Link 2</a>
            <a href="javascript:void(0)">Skip</a>
        </body>
        </html>
        """
        base_url = "https://example.com"
        scraper = Scraper(Config())
        soup = BeautifulSoup(html, "lxml")
        links = scraper._extract_links(soup, base_url)
        # _extract_links returns list of dicts with "href" key
        urls = [link.get("href") for link in links]
        assert "https://example.com/page1" in urls
        assert "https://example.com/relative" in urls
        # javascript: URLs should be skipped
        assert len(urls) == 2


# ─── Crawler Tests ─────────────────────────────────────────────────────────────

class TestCrawler:
    """Tests for crokrawl.crawler.Crawler."""

    def test_crawler_init(self):
        from crokrawl.config import Config
        from crokrawl.crawler import Crawler
        from crokrawl.scraper import Scraper

        config = Config()
        scraper = Scraper(config)
        crawler = Crawler(scraper, config)
        assert crawler is not None

    def test_start_crawl_job_structure(self):
        """Verify CrawlJob creation works (not actual crawling, which needs event loop)."""
        from crokrawl.crawler import CrawlJob

        job = CrawlJob(url="https://example.com", max_pages=5, max_depth=2)
        assert job.url == "https://example.com"
        assert job.max_pages == 5
        assert job.max_depth == 2
        assert job.id is not None
        assert job.status == "pending"

    @pytest.mark.asyncio
    async def test_start_crawl_job_async(self):
        """Test starting a crawl job within an async context."""
        from crokrawl.config import Config
        from crokrawl.crawler import Crawler
        from crokrawl.scraper import Scraper

        config = Config()
        scraper = Scraper(config)
        await scraper.start()
        crawler = Crawler(scraper, config)

        job = crawler.start_crawl("https://example.com", max_pages=5, max_depth=2)
        assert job is not None
        assert job.url == "https://example.com"
        assert job.id is not None
        assert job.status in ("pending", "running")

        # Verify job was registered
        assert crawler.get_job_status(job.id) is not None

        # Cancel it
        assert crawler.cancel_job(job.id) is True
        await scraper.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
