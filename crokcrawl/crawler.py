"""Multi-page website crawler — BFS with rate limiting and depth control.

Uses the scraper's httpx client (no browser needed).
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Any
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from crokcrawl.url_validation import is_safe_url

logger = logging.getLogger(__name__)


@dataclass
class CrawlJob:
    """State for an async crawl job."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    url: str = ""
    status: str = "pending"  # pending, running, completed, failed, cancelled
    max_pages: int = 50
    max_depth: int = 3
    results: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    progress: int = 0
    total_pages: int = 0
    started_at: float = 0.0
    completed_at: float = 0.0


class Crawler:
    """Async BFS website crawler using httpx."""

    def __init__(self, scraper, config):
        self.scraper = scraper
        self.config = config
        self._jobs: dict[str, CrawlJob] = {}
        self._seen: set[str] = set()  # global seen for this crawl

    def start_crawl(
        self,
        url: str,
        max_pages: int | None = None,
        max_depth: int | None = None,
        allow_external: bool = False,
        **kwargs: Any,
    ) -> CrawlJob:
        """Start a new crawl job. Returns immediately — use get_job_status() to poll."""
        job = CrawlJob(
            url=url,
            max_pages=max_pages or self.config.crawl_max_pages,
            max_depth=max_depth or self.config.crawl_max_depth,
            started_at=time.time(),
            status="running",
        )
        self._jobs[job.id] = job
        asyncio.create_task(self._run_crawl(job, allow_external=allow_external, **kwargs))
        return job

    async def _run_crawl(self, job: CrawlJob, allow_external: bool = False, **kwargs: Any):
        """Run BFS crawl in background with timeout."""
        job.status = "running"
        domain = urlparse(job.url).netloc
        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(job.url, 0)]
        start_time = time.time()

        while queue and len(visited) < job.max_pages:
            # Crawl timeout check
            if self.config.crawl_timeout > 0:
                elapsed = time.time() - start_time
                if elapsed > self.config.crawl_timeout:
                    logger.warning("Crawl timeout (%ds) reached for job %s", self.config.crawl_timeout, job.id)
                    job.status = "cancelled"
                    job.errors.append({"url": "", "error": f"Crawl timed out after {self.config.crawl_timeout}s"})
                    job.status = "completed"
                    job.completed_at = time.time()
                    job.total_pages = len(visited)
                    return

            current_url, depth = queue.pop(0)

            if current_url in visited or depth > job.max_depth:
                continue

            # SSRF protection — validate URL before scraping
            if not is_safe_url(current_url):
                job.errors.append({"url": current_url, "error": "Access denied: URL targets a private/internal address"})
                logger.warning("Blocked crawl URL (SSRF): %s", current_url)
                continue

            visited.add(current_url)
            job.progress = len(visited)

            try:
                # Scrape the page
                result = await self.scraper.scrape(
                    current_url,
                    only_main_content=True,
                    **kwargs,
                )

                job.results.append({
                    "url": result.source_url or current_url,
                    "markdown": result.markdown[:50000],  # cap for memory
                    "title": result.title or "",
                    "description": result.description or "",
                    "success": result.success,
                    "metadata": result.metadata,
                })

                # Extract links and add to queue
                if result.success and result.html and depth < job.max_depth:
                    soup = BeautifulSoup(result.html, 'lxml')
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
                            continue
                        try:
                            full = urljoin(current_url, href)
                            if not allow_external:
                                if urlparse(full).netloc != domain:
                                    continue
                            if full not in visited:
                                queue.append((full, depth + 1))
                        except Exception:
                            continue

                # Rate limiting
                await asyncio.sleep(self.config.crawl_rate_limit)

            except Exception as e:
                job.errors.append({"url": current_url, "error": "Fetch failed"})
                logger.warning("Crawl page failed (%s): %s", current_url, e)

        job.status = "completed"
        job.completed_at = time.time()
        job.total_pages = len(visited)

    def get_job(self, job_id: str) -> Optional[CrawlJob]:
        """Get crawl job by ID."""
        return self._jobs.get(job_id)

    def get_job_status(self, job_id: str) -> Optional[dict]:
        """Get crawl job status as dict (API-friendly)."""
        job = self._jobs.get(job_id)
        if not job:
            return None
        return {
            "id": job.id,
            "url": job.url,
            "status": job.status,
            "progress": job.progress,
            "total_pages": job.total_pages,
            "results_count": len(job.results),
            "errors_count": len(job.errors),
            "results": job.results,
            "errors": job.errors,
            "elapsed": time.time() - job.started_at,
        }

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job."""
        job = self._jobs.get(job_id)
        if job and job.status == "running":
            job.status = "cancelled"
            return True
        return False

    def list_jobs(self) -> list[dict]:
        """List all crawl jobs."""
        return [
            {
                "id": j.id,
                "url": j.url,
                "status": j.status,
                "progress": j.progress,
                "results_count": len(j.results),
            }
            for j in self._jobs.values()
        ]

    def cleanup_old_jobs(self) -> int:
        """Remove completed/cancelled jobs older than config.job_cleanup_age seconds.
        
        Returns the number of jobs removed.
        """
        now = time.time()
        if self.config.job_cleanup_age <= 0:
            return 0

        to_remove = []
        for job_id, job in self._jobs.items():
            if job.status in ("completed", "failed", "cancelled") and job.completed_at > 0:
                if now - job.completed_at > self.config.job_cleanup_age:
                    to_remove.append(job_id)

        for job_id in to_remove:
            del self._jobs[job_id]

        if to_remove:
            logger.info("Cleaned up %d old crawl jobs", len(to_remove))

        return len(to_remove)