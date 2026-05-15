"""Content scraper — httpx + readability-lxml + markdownify with optional Playwright.

Handles ~80% of the web with httpx (static HTML).
For JS-rendered pages (SPAs), uses Playwright Chromium when enabled.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Any
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify
from readability import Document

from crokcrawl.url_validation import is_safe_url

# Stealth evasions via undetected_playwright's Malenia
try:
    from undetected_playwright.tarnished import Malenia
    HAS_UNDETECTED = True
except Exception:
    HAS_UNDETECTED = False


logger = logging.getLogger(__name__)

# HTML patterns that likely JS-rendered (SPA detection)
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

# Randomized browser fingerprint profiles — rotate across these to avoid
# fingerprint-based detection. Each profile has distinct viewport, locale,
# timezone, and Accept-Language headers.
FINGERPRINT_PROFILES = [
    {
        "viewport": {"width": 1920, "height": 1080},
        "locale": "en-US",
        "timezone": "America/New_York",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "extra_headers": {
            "Accept-Language": "en-US,en;q=0.9",
        },
    },
    {
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-GB",
        "timezone": "Europe/London",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "extra_headers": {
            "Accept-Language": "en-GB,en;q=0.9",
        },
    },
    {
        "viewport": {"width": 1536, "height": 864},
        "locale": "de-DE",
        "timezone": "Europe/Berlin",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "extra_headers": {
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        },
    },
    {
        "viewport": {"width": 1440, "height": 900},
        "locale": "fr-FR",
        "timezone": "Europe/Paris",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "extra_headers": {
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        },
    },
    {
        "viewport": {"width": 1600, "height": 900},
        "locale": "en-CA",
        "timezone": "America/Toronto",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "extra_headers": {
            "Accept-Language": "en-CA,en;q=0.9",
        },
    },
]

# Stealth launch args — anti-detection flags passed to Chromium
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-features=VizDisplayCompositor",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--mute-audio",
]

# Domains known to always require JS rendering — SPA detection fails on these
# because they return pre-rendered shells with enough text to pass detection checks.
KNOWN_JS_HEAVY_DOMAINS = {
    "github.com": True,
    "twitter.com": True,
    "www.twitter.com": True,
    "x.com": True,
    "www.x.com": True,
    "www.reddit.com": True,
    "linkedin.com": True,
    "www.linkedin.com": True,
    "tiktok.com": True,
    "www.tiktok.com": True,
    "instagram.com": True,
    "www.instagram.com": True,
    "facebook.com": True,
    "www.facebook.com": True,
    "youtube.com": True,
    "www.youtube.com": True,
}


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

    def _new_fingerprint(self):
        """Return a random fingerprint profile to reduce fingerprint-based detection."""
        import random
        return random.choice(FINGERPRINT_PROFILES)

    async def start(self):
        """Initialize scraper and optionally launch Playwright browser with anti-detection."""
        if self.config.js_render:
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                launch_kwargs = {
                    "headless": self.config.headless,
                    "args": STEALTH_ARGS,
                }

                # Try to use system Chrome first, fall back to bundled
                system_chrome = os.environ.get(
                    "CROKCRAWL_CHROME_PATH",
                    "/home/jd/.agent-browser/browsers/chrome-148.0.7778.97/chrome"
                )
                if os.path.exists(system_chrome):
                    launch_kwargs["executable_path"] = system_chrome
                elif os.path.exists("/usr/bin/chromium-browser"):
                    launch_kwargs["executable_path"] = "/usr/bin/chromium-browser"
                elif os.path.exists("/usr/bin/google-chrome"):
                    launch_kwargs["executable_path"] = "/usr/bin/google-chrome"

                self._browser = await self._playwright.chromium.launch(**launch_kwargs)

                # Create a fresh fingerprint context for each session
                fp = self._new_fingerprint()
                self._context = await self._browser.new_context(
                    user_agent=fp["ua"],
                    viewport=fp["viewport"],
                    locale=fp["locale"],
                    timezone_id=fp["timezone"],
                    permissions=["geolocation"],
                    extra_http_headers=fp["extra_headers"],
                )
                # Apply Malenia stealth evasions (puppeteer-extra-plugin-stealth)
                if self.config.stealth and HAS_UNDETECTED:
                    await Malenia.apply_stealth(self._context)
                self._js_render_available = True
                logger.info(
                    "Playwright browser initialized for JS rendering (stealth=%s, fingerprint=%s/%s)",
                    self.config.stealth, fp["locale"], fp["timezone"],
                )
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
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        render_js: bool | None = None,
        force_js_render: bool = False,
        wait_ms: int | None = None,
        **kwargs: Any,
    ) -> ScrapeResult:
        """Scrape a single URL. Uses httpx first, then Playwright if JS-rendered or forced."""
        result = ScrapeResult(url=url)

        if not is_safe_url(url):
            result.success = False
            result.error = "Access denied: URL targets a private/internal address"
            return result

        effective_js_render = render_js if render_js is not None else self.config.js_render

        # Force JS rendering for known JS-heavy domains (GitHub, Twitter, etc.)
        # regardless of SPA detection — their pre-rendered shells fool the detector.
        parsed_url = urlparse(url)
        if parsed_url.netloc in KNOWN_JS_HEAVY_DOMAINS:
            effective_js_render = True
        effective_wait = wait_ms if wait_ms is not None else self.config.wait_for

        # Determine wait_for_selector based on URL patterns
        wait_for_selector = self._get_wait_selector(url)

        # If force_js_render is True, skip httpx and go directly to browser
        if force_js_render and self._context and effective_js_render:
            try:
                browser_html, raw_text, final_url = await self._fetch_with_browser(
                    url, wait_ms=effective_wait, wait_for_selector=wait_for_selector
                )
                if browser_html:
                    result.html = browser_html
                    result.raw_text = raw_text
                    result.source_url = final_url
                    result.success = True
                    result.status_code = 200
                    result.is_js_rendered = True
                    result.metadata["extraction_method"] = "playwright-force"
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
                # Also use browser for known JS-heavy domains regardless of SPA detection
                is_js_heavy = parsed_url.netloc in KNOWN_JS_HEAVY_DOMAINS
                if effective_js_render and (is_spa or is_js_heavy):
                    if self._context:
                        browser_html, raw_text, final = await self._fetch_with_browser(
                            url, wait_ms=effective_wait, wait_for_selector=wait_for_selector
                        )
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
                status_code = getattr(e, "response", None) and e.response.status_code or 0
                if status_code:
                    result.error = f"HTTP {status_code}"
                    result.metadata["error_type"] = "http_client_error"
                    result.metadata["status_code"] = status_code
                else:
                    result.error = "Fetch failed"
                    result.metadata["error_type"] = "network_error"
                logger.error("Scrape HTTP error for %s: %s", url, e)
            except Exception as e:
                result.success = False
                result.error = "Scrape failed"
                result.metadata["error_type"] = "unknown_error"
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

    def _get_wait_selector(self, url: str) -> str | None:
        """Return a CSS selector to wait for after page load, based on URL patterns.

        This ensures dynamic content (like GitHub commit lists) is fully rendered
        before we capture the page.
        """
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")

        # GitHub commit list pages — wait for the commit timeline
        if parsed.netloc == "github.com" and "/commits" in path:
            return ".TimelineItem"

        # GitHub pull request pages — wait for the PR timeline
        if parsed.netloc == "github.com" and "/pulls" in path:
            return ".TimelineItem"

        # Generic commit pages
        if parsed.netloc == "github.com" and path.endswith(("/commit", "/commits")):
            return ".commit-actions"

        # Discussions and some repo pages use the layout container
        if parsed.netloc == "github.com" and any(x in path for x in ["/discussions", "/projects"]):
            return ".Layout-main"

        # Reddit uses the post listing
        if parsed.netloc in ("reddit.com", "www.reddit.com"):
            return ".Post"

        # Twitter/X uses the timeline
        if parsed.netloc in ("twitter.com", "www.twitter.com", "x.com", "www.x.com"):
            return "[data-testid='timeline']"

        # LinkedIn uses the feed container
        if parsed.netloc in ("linkedin.com", "www.linkedin.com"):
            return ".feed-shared-update-v2"

        # Default: no specific selector needed
        return None

    async def _fetch_with_browser(
        self,
        url: str,
        wait_ms: int = 500,
        wait_for_selector: str | None = None,
    ) -> tuple[str, str, str]:
        """
        Fetch URL via Playwright browser. Waits for JS-rendered content.

        Uses 'domcontentloaded' first to avoid Cloudflare JS challenge stalls,
        then waits explicitly for content. Falls back to curl-impersonate on
        empty/bocked responses. Retries up to 2 times with fresh fingerprints.

        Returns: (html, raw_text, final_url)
        """
        if not self._context:
            return "", "", url

        last_error = None
        for attempt in range(3):
            try:
                # Each retry gets a fresh fingerprint profile + new context
                if attempt > 0:
                    fp = self._new_fingerprint()
                    ctx = await self._browser.new_context(
                        user_agent=fp["ua"],
                        viewport=fp["viewport"],
                        locale=fp["locale"],
                        timezone_id=fp["timezone"],
                        permissions=["geolocation"],
                        extra_http_headers=fp["extra_headers"],
                    )
                    if self.config.stealth and HAS_UNDETECTED:
                        await Malenia.apply_stealth(ctx)
                else:
                    ctx = self._context

                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=self.config.timeout * 1000)

                if wait_ms > 0:
                    await page.wait_for_timeout(wait_ms)

                if wait_for_selector:
                    try:
                        await page.wait_for_selector(wait_for_selector, timeout=15000)
                    except Exception:
                        logger.debug("wait_for_selector '%s' not found, continuing", wait_for_selector)

                html_data = await page.content()
                is_cf_block = "Cloudflare" in html_data or "blocked" in html_data[:2000].lower()
                if is_cf_block:
                    logger.info("Cloudflare challenge detected on attempt %d, retrying...", attempt + 1)
                    await page.close()
                    await ctx.close()
                    last_error = "Cloudflare challenge"
                    continue

                raw_text = await page.inner_text("body")
                final_url = page.url
                await page.close()
                if attempt > 0:
                    await ctx.close()
                return html_data, raw_text, final_url
            except Exception as e:
                last_error = e
                logger.debug("Browser fetch attempt %d failed for %s: %s", attempt + 1, url, e)

        # All Playwright attempts failed — fall back to curl-impersonate
        logger.info("Playwright failed for %s, falling back to curl-impersonate", url)
        html, raw_text, final_url = await self._fetch_with_curl_impersonate(url)
        if html:
            return html, raw_text, final_url

        logger.error("All browser fetch attempts failed for %s: %s", url, last_error)
        return "", "", url

    def _get_curl_curl_impersonate_path(self) -> str | None:
        """Locate curl-impersonate binary."""
        import shutil
        path = shutil.which("curl-impersonate")
        if path:
            return path
        for candidate in [
            "/usr/local/bin/curl-impersonate",
            "/usr/bin/curl-impersonate",
            "~/.local/bin/curl-impersonate",
        ]:
            import os
            if os.path.exists(os.path.expanduser(candidate)):
                return os.path.expanduser(candidate)
        return None

    async def _fetch_with_curl_impersonate(self, url: str) -> tuple[str, str, str]:
        """
        Fetch URL using curl-impersonate with real Chrome TLS/HTTP2 fingerprints.

        curl-impersonate impersonates Chrome's TLS handshake, HTTP/2 headers,
        and cipher suites — making the request look like a real browser even
        though it's a simple HTTP fetch. Works on sites that block headless
        Chrome but don't check JS execution.

        Returns: (html, raw_text, final_url)
        """
        curl_path = self._get_curl_curl_impersonate_path()
        if not curl_path:
            return "", "", url

        import subprocess, tempfile, os, asyncio
        # Write page content to temp file
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="wb") as f:
            tmpfile = f.name

        try:
            # curl-impersonate with Chrome fingerprints
            # -L: follow redirects
            # -s: silent
            # -o: output file
            # --tlsv1.2: TLS version
            # -k: accept cert errors (for anti-bot cert mismatches)
            cmd = [
                curl_path,
                "-L",
                "-s",
                "-o", tmpfile,
                "--tlsv1.2",
                "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "-H", "Accept-Language: en-US,en;q=0.9",
                "--noproxy", "*",  # force direct connection
                url,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            with open(tmpfile, "r", errors="replace") as f:
                html = f.read()
            if html and len(html) > 100:
                # Extract title from first <title> tag
                title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
                title = title_match.group(1) if title_match else ""
                # Basic raw_text extraction
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "lxml")
                raw_text = soup.get_text(separator=" ", strip=True)[:5000]
                return html, raw_text, url
        except Exception as e:
            logger.debug("curl-impersonate failed for %s: %s", url, e)
        finally:
            try:
                os.unlink(tmpfile)
            except Exception:
                pass
        return "", "", url

    async def map_urls(self, url: str, max_depth: int = 2, max_urls: int = 1000) -> list[str]:
        """Discover URLs on a domain without scraping content."""
        result_urls: set[str] = set()
        visited: set[str] = set()
        queue = [(url, 0)]
        domain = urlparse(url).netloc

        if not is_safe_url(url):
            return []

        while queue and len(visited) < max_urls:
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
        if len(html) > 5000 and len(body_text) < 300:
            return True
        if body_text.strip() in ("", "Loading...", "Please wait"):
            return True

        # Ratio-based detection: very little text relative to HTML size
        # indicates the HTML is boilerplate and content is rendered by JS
        if len(html) > 0 and len(body_text) / len(html) < 0.01:
            return True

        scripts = soup.find_all("script")
        if len(scripts) > 30 and len(body_text) < 2000 and len(html) > 100000:
            return True

        for script in scripts:
            script.decompose()
        visible_text = body.get_text(strip=True)
        if len(html) > 80000 and len(visible_text) < 3000:
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