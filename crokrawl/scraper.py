"""Content scraper — httpx + readability-lxml + markdownify with optional Playwright.

Handles ~80% of the web with httpx (static HTML).
For JS-rendered pages (SPAs), uses Playwright Chromium when enabled.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Any
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify
from readability import Document

from crokrawl.url_validation import is_safe_url
from crokrawl.stealth import STEALTH_INIT_SCRIPT, STEALTH_ARGS

logger = logging.getLogger(__name__)

# HTML pages that are likely JS-rendered (SPA detection)
SPA_INDICATORS = [
    'id="__next"',
    '__NEXT_DATA__',
    '__NUXT__',
    '__REDUX__',
    '__APOLLO_STATE__',
    'data-reactroot',
    'angular-version',
    'vue-app',
]


@dataclass
class ScrapeResult:
    """Result from scraping a single URL."""
    success: bool = True
    url: str = ""
    markdown: str = ""
    html: str = ""
    raw_text: str = ""
    title: str = ""
    description: str = ""
    source_url: str = ""
    status_code: int = 0
    error: str = ""
    metadata: dict = field(default_factory=dict)
    is_js_rendered: bool = False


class Scraper:
    """Content scraper using httpx + readability-lxml + markdownify.

    Falls back to Playwright Chromium for JS-rendered pages when enabled.
    """

    def __init__(self, config):
        self.config = config
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=float(config.timeout),
                write=10.0,
                pool=5.0,
            ),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
            },
        )
        self._context = None
        self._browser = None
        self._playwright = None
        self._js_render_available = False

    async def start(self):
        """Initialize scraper and optionally launch Playwright browser with anti-detection."""
        if self.config.js_render:
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                if self.config.stealth:
                    # Use stealth browser args and init scripts to bypass automation detection
                    launch_kwargs = {
                        "headless": self.config.headless,
                        "args": STEALTH_ARGS,
                    }
                else:
                    launch_kwargs = {"headless": self.config.headless}
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
                self._context = await self._browser.new_context(
                    user_agent=self._client.headers.get("User-Agent"),
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                    permissions=["geolocation"],
                )
                if self.config.stealth:
                    await self._context.add_init_script(STEALTH_INIT_SCRIPT)
                self._js_render_available = True
                logger.info("Playwright browser initialized for JS rendering (stealth=%s)", self.config.stealth)
            except Exception as e:
                logger.warning("Playwright unavailable, falling back to httpx only: %s", e)
                self._js_render_available = False
                await self._teardown_browser()

    async def _teardown_browser(self):
        """Best-effort teardown of Playwright resources."""
        for attr, closer in (
            ("_context", "close"),
            ("_browser", "close"),
            ("_playwright", "stop"),
        ):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                await getattr(obj, closer)()
            except Exception as e:
                logger.warning("Error closing %s: %s", attr, e)
            setattr(self, attr, None)
        self._js_render_available = False

    async def stop(self):
        """Close HTTP client and Playwright resources."""
        await self._teardown_browser()
        await self._client.aclose()

    async def scrape(
        self,
        url: str,
        formats: list[str] | None = None,
        only_main_content: bool = True,
        render_js: bool | None = None,
        force_js_render: bool = False,
        wait_for: int | None = None,
        **kwargs: Any,
    ) -> ScrapeResult:
        """Scrape a single URL. Uses httpx first, then Playwright if JS-rendered or forced."""
        result = ScrapeResult(url=url)

        if not is_safe_url(url):
            result.success = False
            result.error = "Access denied: URL targets a private/internal address"
            return result

        effective_js_render = render_js if render_js is not None else self.config.js_render
        effective_wait = wait_for if wait_for is not None else self.config.wait_for

        # If force_js_render is True, skip httpx and go directly to browser
        if force_js_render and self._context and effective_js_render:
            try:
                browser_html, raw_text, final_url = await self._fetch_with_browser(url, wait_ms=effective_wait)
                if browser_html:
                    result.html = browser_html
                    result.raw_text = raw_text
                    result.source_url = final_url
                    result.success = True
                    result.status_code = 200
                else:
                    result.success = False
                    result.error = "Browser fetch returned empty content"
                    return result
            except Exception as e:
                result.success = False
                result.error = f"Browser fetch failed: {e}"
                return result
        else:
            try:
                response = await self._client.get(url)
                final_url = str(response.url)
                if final_url != url and not is_safe_url(final_url):
                    result.success = False
                    result.error = "Redirect blocked (SSRF prevention)"
                    return result

                result.status_code = response.status_code
                html = response.text

                # Re-fetch with Playwright if JS-rendered and browser available
                is_spa = self._is_js_rendered(html, response)
                if effective_js_render and is_spa:
                    if self._context:
                        browser_html, raw_text, final = await self._fetch_with_browser(url, wait_ms=effective_wait)
                        if browser_html:
                            html = browser_html
                            result.raw_text = raw_text
                            result.source_url = final
                        else:
                            result.source_url = final_url
                    else:
                        # SPA detected but Playwright not available — warn client
                        result.source_url = final_url
                        result.metadata["js_render_skipped"] = True
                        result.metadata["js_render_reason"] = "Playwright browser not available"
                        result.metadata["extraction_method"] = "httpx-only-spa-detected"
                        logger.warning(
                            "SPA detected for %s but Playwright unavailable — returning limited shell content", url,
                        )
                else:
                    result.source_url = final_url

                # Set is_js_rendered flag for client awareness
                if is_spa:
                    result.is_js_rendered = True
                    if not self._context and effective_js_render:
                        result.metadata["js_render_skipped"] = True
                        result.metadata["js_render_reason"] = "Playwright browser not available"

                result.html = html
                soup = BeautifulSoup(html, "lxml")
                result.title = self._extract_title(soup)
                result.description = self._extract_description(soup)

                if only_main_content:
                    doc = Document(html, min_text_length=50)
                    article_html = doc.summary()
                    article_title = doc.title()
                    if article_title:
                        result.title = article_title
                    result.markdown = _html_to_markdown(article_html)
                    if not result.markdown.strip() and len(html) > 2000:
                        body = soup.find("body")
                        result.markdown = _html_to_markdown(str(body) if body else html)
                        if not result.markdown.strip():
                            result.markdown = _html_to_markdown(html)
                            result.metadata["extraction_method"] = "fallback-full"
                        else:
                            result.metadata["extraction_method"] = "fallback-body"
                    else:
                        result.metadata["extraction_method"] = "readability"
                else:
                    body = soup.find("body")
                    result.markdown = _html_to_markdown(str(body) if body else html)

                if formats and "links" in formats:
                    result.metadata["links"] = self._extract_links(soup, url)
                if formats and "json" in formats:
                    result.metadata["structured_data"] = self._extract_structured_data(soup)

            except httpx.HTTPError as e:
                result.success = False
                result.error = "Failed to fetch page"
                logger.error("Scrape HTTP error for %s: %s", url, e)
            except Exception as e:
                result.success = False
                result.error = "Scrape failed"
                logger.error("Scrape error for %s: %s", url, e)

        # Post-process: parse markdown from HTML if browser was used
        if result.success and result.html and not result.markdown:
            soup = BeautifulSoup(result.html, "lxml")
            result.title = self._extract_title(soup)
            result.description = self._extract_description(soup)
            if only_main_content:
                try:
                    doc = Document(result.html, min_text_length=50)
                    article_html = doc.summary()
                    result.markdown = _html_to_markdown(article_html)
                    result.metadata["extraction_method"] = "readability"
                except Exception:
                    body = soup.find("body")
                    result.markdown = _html_to_markdown(str(body) if body else result.html)
                    result.metadata["extraction_method"] = "fallback-body"
                if formats and "links" in formats:
                    result.metadata["links"] = self._extract_links(soup, url)
                if formats and "json" in formats:
                    result.metadata["structured_data"] = self._extract_structured_data(soup)
            else:
                body = soup.find("body")
                result.markdown = _html_to_markdown(str(body) if body else result.html)

        return result

    async def _fetch_with_browser(self, url: str, wait_ms: int = 500) -> tuple[str, str, str]:
        """Fetch page using Playwright browser for full JS rendering.
        
        Uses 'domcontentloaded' first to avoid Cloudflare JS challenge stalls,
        then waits explicitly for content. If Cloudflare block detected, retries
        with longer wait.
        
        Returns: (html, raw_text, final_url)
        """
        if not self._context:
            return "", "", url
        try:
            page = await self._context.new_page()
            # Use domcontentloaded instead of networkidle — Cloudflare challenges
            # sometimes prevent networkidle from ever firing
            await page.goto(url, wait_until="domcontentloaded", timeout=self.config.timeout * 1000)
            
            # Wait for JS challenge to resolve or dynamic content to load
            if wait_ms > 0:
                await page.wait_for_timeout(wait_ms)
            
            # Check if we got a Cloudflare block page
            html_data = await page.content()
            is_cf_block = "Cloudflare" in html_data or "blocked" in html_data[:2000].lower()
            
            if is_cf_block:
                logger.info("Cloudflare challenge detected, waiting longer for resolution...")
                await page.wait_for_timeout(10000)
                html_data = await page.content()
            
            # Get raw visible text — what a human sees on the page
            raw_text = await page.inner_text("body")
            
            final_url = page.url
            await page.close()
            return html_data, raw_text, final_url
        except Exception as e:
            logger.error("Browser fetch error for %s: %s", url, e)
            return "", "", url

    async def map_urls(self, url: str, max_depth: int = 2) -> list[str]:
        """Discover URLs on a domain without scraping content."""
        result_urls: set[str] = set()
        visited: set[str] = set()
        queue = [(url, 0)]
        domain = urlparse(url).netloc

        if not is_safe_url(url):
            return []

        while queue and len(visited) < 1000:
            current_url, depth = queue.pop(0)
            if current_url in visited or depth > max_depth:
                continue
            visited.add(current_url)
            try:
                response = await self._client.get(current_url, timeout=10)
                if response.status_code == 200:
                    result_urls.add(current_url)
                    soup = BeautifulSoup(response.text, "lxml")
                    for a_tag in soup.find_all("a", href=True):
                        href = a_tag["href"]
                        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
                            continue
                        full_url = urljoin(current_url, href)
                        if urlparse(full_url).netloc == domain and full_url not in visited:
                            queue.append((full_url, depth + 1))
            except Exception:
                pass
        return sorted(result_urls)

    def _is_js_rendered(self, html: str, response) -> bool:
        """Detect if page is likely JS-rendered (SPA)."""
        lower = html.lower()
        if any(indicator in lower for indicator in SPA_INDICATORS):
            return True

        soup = BeautifulSoup(html, "lxml")
        body = soup.find("body")
        if not body:
            return False

        body_text = body.get_text(strip=True)
        if len(html) > 5000 and len(body_text) < 200:
            return True
        if body_text.strip() in ("", "Loading...", "Please wait"):
            return True

        scripts = soup.find_all("script")
        if len(scripts) > 30 and len(body_text) < 500 and len(html) > 100000:
            return True

        for script in scripts:
            script.decompose()
        visible_text = body.get_text(strip=True)
        if len(html) > 80000 and len(visible_text) < 1500:
            return True
        return False

    def _extract_title(self, soup: BeautifulSoup) -> str:
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            return title_tag.string.strip()
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"].strip()
        return ""

    def _extract_description(self, soup: BeautifulSoup) -> str:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            return meta["content"].strip()
        og = soup.find("meta", property="og:description")
        if og and og.get("content"):
            return og["content"].strip()
        return ""

    def _extract_links(self, soup: BeautifulSoup, base_url: str) -> list[dict]:
        links = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = (a.get_text(strip=True) or "")[:100]
            try:
                full = urljoin(base_url, href)
                if not full.startswith(("http://", "https://")):
                    continue
                if href.startswith("#"):
                    continue
                if full in seen:
                    continue
                seen.add(full)
                links.append({"text": text, "href": full})
            except Exception:
                continue
        return links[:200]

    def _extract_structured_data(self, soup: BeautifulSoup) -> Optional[dict]:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                return json.loads(script.string or "{}")
            except (json.JSONDecodeError, Exception):
                continue
        return None


def _html_to_markdown(html: str) -> str:
    if not html:
        return ""
    md = markdownify(
        html,
        heading_style="ATX",
        code_language="default",
        strip=["img", "script", "style"],
        bullets="-",
        max_title_length=0,
    )
    lines = [line.rstrip() for line in md.split("\n")]
    cleaned = []
    blank_count = 0
    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)
    return "\n".join(cleaned).strip()
