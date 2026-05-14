"""Enhanced Playwright-based scraping with scroll-triggered lazy loading and product extraction."""

import asyncio
from playwright.async_api import async_playwright

from crokrawl.stealth import STEALTH_INIT_SCRIPT


async def scrape_with_browser(
    url: str,
    wait_ms: int = 15000,
    headless: bool = True,
    scroll_pages: int = 3,
) -> list[dict]:
    """Scrape a page with stealth mode, scroll, and extract product cards.

    Returns list of dicts with name, grade, price, and href.
    """
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-web-security",
            "--disable-infobars",
            "--disable-extensions",
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="en-US",
        timezone_id="America/New_York",
    )

    # Layer 1: init script (stealth fingerprints)
    await context.add_init_script(STEALTH_INIT_SCRIPT)

    page = await context.new_page()

    # Intercept product API calls for direct extraction
    product_data: list[dict] = []

    async def _intercept_xhr(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if "_serverFn" in url and response.status == 200:
            try:
                body = await response.text()
                if len(body) > 100000:
                    # This is the big product listing payload
                    # Extract PSA 10 products via regex since it uses SFC custom format
                    import re
                    for m in re.finditer(r'"(PSA\s*10)"', body):
                        pass  # At least we know the payload arrived

            except:
                pass

    page.on("response", _intercept_xhr)

    print(f"Navigating to: {url[:100]}...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"Navigation error (continuing anyway): {e}")

    title = await page.title()
    print(f"Page title: {title}")

    # Wait initial load
    await page.wait_for_timeout(10000)

    # Scroll to trigger lazy-loaded product cards
    for i in range(scroll_pages):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(3000)
        print(f"  Scrolled page {i+1}/{scroll_pages}")

    # Final wait for hydration
    await page.wait_for_timeout(wait_ms)

    # Extract visible text content
    body_text = await page.inner_text("body")
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    # Parse product cards from text
    products = []
    for i, line in enumerate(lines):
        if "$" in line and len(line) < 80:
            import re
            price_match = re.match(r'\$([\d,]+\.?\d+)', line)
            if price_match:
                # Look backwards for product name and grade
                grade = ""
                name_parts = []
                for j in range(max(0, i-6), i):
                    prev = lines[j]
                    if "PSA 10" in prev:
                        grade = "PSA 10"
                    elif len(prev) > 10 and "$" not in prev and "Cart" not in prev and "for Pros" not in prev:
                        name_parts.append(prev)
                
                if name_parts and grade:
                    full_name = " ".join(name_parts)
                    if not full_name.endswith("- PSA 10"):
                        full_name = f"{full_name} - {grade}"
                    products.append({
                        "name": full_name,
                        "price": price_match.group(1),
                        "href": f"https://www.gamestop.com/search?q={full_name.replace(' ', '+')[:60]}",
                    })

                if "for Pros" in lines[i+1] if i+1 < len(lines) else "":
                    pass  # Skip pros price line

    result_products = []
    seen_names = set()
    for p in products:
        if p["name"] not in seen_names:
            seen_names.add(p["name"])
            result_products.append(p)

    await browser.close()
    await pw.stop()

    return result_products


if __name__ == "__main__":
    url = "https://www.gamestop.com/graded-trading-cards/gradedcollectibles-cards-pokemon?refine=%5B%22cgid%3Dgradedcollectibles-cards-pokemon%22%2C%22price%3D%28300..10000%29%22%2C%22c_providerGrade%3DGEM+MT+10%22%5D&page=1"

    products = asyncio.run(scrape_with_browser(url, wait_ms=15000, scroll_pages=3))

    if products:
        print(f"\nFound {len(products)} PSA 10 products:\n")
        for idx, p in enumerate(products[:10], 1):
            print(f"{idx}. **{p['name']}**")
            print(f"   Price: **${p['price']}**")
            print(f"   [View on GameStop]({p['href']})")
            print()
    else:
        print("No products extracted")
