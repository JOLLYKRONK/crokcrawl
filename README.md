# crokrawl — Open-Source Firecrawl Replacement

A self-hosted, minimal Firecrawl-compatible API. Drop it in, set `FIRECRAWL_API_URL=http://localhost:8000`, and it works.

## Features

- `/v1/scrape` — scrape a URL into clean Markdown (JS-rendered, anti-bot aware)
- `/v1/search` — web search via SearXNG, with optional result scraping
- `/v1/crawl` — async multi-page website crawler
- `/v1/map` — discover URLs on a domain
- Drop-in Firecrawl API compatibility (same request/response shapes)

## Quick Start

```bash
cd ~/.hermes/crokcrawl
uv sync
uv run playwright install chromium
uv run uvicorn crokcrawl.server:app --host 0.0.0.0 --port 8000
```

## Firecrawl Compatibility

Same API, same response shapes:

| Firecrawl | crokrawl |
|-----------|----------|
| `POST /v1/scrape` | `POST /v1/scrape` ✅ |
| `POST /v1/search` | `POST /v1/search` ✅ |
| `POST /v1/crawl` | `POST /v1/crawl` ✅ |
| `POST /v1/map` | `POST /v1/map` ✅ |

Response format matches:
```json
{"success": true, "data": {"markdown": "...", "metadata": {...}}}
```

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CROKRAWL_PORT` | `8000` | Server port |
| `CROKRAWL_SEARXNG_URL` | `http://localhost:8080` | SearXNG instance URL |
| `CROKRAWL_SEARXNG_API_KEY` | *(none)* | If your SearXNG requires auth |
| `CROKRAWL_MAX_CONCURRENCY` | `4` | Max concurrent scrapes |
| `CROKRAWL_STEALTH` | `true` | Use stealth mode (anti-bot) |
| `CROKRAWL_TIMEOUT` | `30` | Per-request timeout in seconds |
| `CROKRAWL_WAIT_FOR` | `500` | Extra ms to wait after page load |

## SearXNG Setup (optional but recommended)

```bash
docker run -d --name searxng -p 8080:8080 \
  -e SEARXNG_BASE_URL=http://localhost:8080/ \
  searxng/searxng:latest
```

Or use a public instance:
```bash
export CROKRAWL_SEARXNG_URL=https://search.sapti.me
```

## For Hermes Integration

Add to `~/.hermes/hermes-agent/tools/web_tools.py` as a new backend `"crokrawl"`:

```python
# In _get_backend() fallback:
("crokrawl", _has_env("CROKRAWL_URL")),

# In _is_backend_available():
if backend == "crokrawl":
    return True  # Always available once installed

# Use httpx to call http://localhost:8000/v1/scrape, etc.
```

Or set `FIRECRAWL_API_URL=http://localhost:8000` — the existing Firecrawl client code will work with crokrawl's compatible API.

kronk test

test2
