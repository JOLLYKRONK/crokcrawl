"""CLI entry point for crokrawl."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="crokrawl",
        description="Open-source Firecrawl-compatible scraping API",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port (overrides CROKRAWL_PORT env var)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (development mode)",
    )
    parser.add_argument(
        "--install-playwright",
        action="store_true",
        help="Install Playwright Chromium browser and exit",
    )

    args = parser.parse_args()

    if args.install_playwright:
        try:
            import playwright  # noqa: F401
        except ImportError:
            print(
                "Playwright is not installed. Re-sync with the browser extra:\n"
                "    uv sync --all-extras",
                file=sys.stderr,
            )
            sys.exit(1)
        import subprocess
        print("Installing Chromium browser via 'playwright install chromium' ...", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
        )
        sys.exit(result.returncode)

    port = args.port
    if port is None:
        from crokrawl.config import config
        port = config.port

    # Explicit import avoids uvicorn string-based re-importing, which breaks
    # in background processes where .pth file based editable installs don't load.
    from crokrawl.server import app
    import uvicorn
    uvicorn.run(
        app,
        host=args.host,
        port=port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
