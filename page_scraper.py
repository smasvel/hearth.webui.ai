"""Full-page content extraction via a headless Playwright/Chromium browser.

Exists because DDGS() search snippets (~300 chars) and a plain `requests.get()` HTML
fetch both routinely miss the actual content of a page: many sites render their real
text client-side via JS, which a plain GET never sees (the exact gap
web_search_client.fetch_price_hints() worked around for prices specifically, before
this existed generally). This renders the real page in a real browser, then extracts
clean, readable text plus the fully-rendered HTML for regex-based extraction (prices
etc.) that needs to see JS-rendered markup, not just visible text.

Deliberately NOT used to query DuckDuckGo itself - verified live that DDG's own HTML
endpoint bot-detects and blocks a Playwright browser within 1-2 requests (a JS
anomaly-challenge page, then a hard "email us" error), even with a realistic
user-agent. The `ddgs` package already handles DDG's anti-bot posture properly and is
what this app uses for search discovery; this module is scoped to visiting the
ordinary websites DDGS's results point to, not to replacing DDGS as the search engine.

One browser launch per call, not a shared persistent instance - Playwright's sync API
is not safe to use across threads, and this Flask app runs threaded (each request its
own OS thread). Launch overhead is small (~0.3-0.5s, measured live on this machine),
so a fresh browser per search is simpler and safer than the alternative (a persistent
browser with per-thread synchronization, or one browser process per worker thread).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

import browse_history_store

try:
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


class PageScrapeError(RuntimeError):
    pass


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_AD_HOST_PARTS = (
    "doubleclick.", "googlesyndication.", "googleadservices.", "adservice.",
    "taboola.", "outbrain.", "adsystem.", "adnxs.", "criteo.", "amazon-adsystem.",
)
_AD_PATH_PARTS = (
    "/sponsored/", "/sponsor/", "/advertisement/", "/adclick", "/clickserve",
)


def is_allowed_navigation_url(url: str) -> bool:
    """Allow ordinary HTTP(S) pages, but never ad/tracker destinations."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        if parsed.scheme not in ("http", "https") or not host:
            return False
        if any(part in host for part in _AD_HOST_PARTS):
            return False
        if any(part in path for part in _AD_PATH_PARTS):
            return False
        query = (parsed.query or "").lower()
        if "ad_domain=" in query or "ad_provider=" in query or "campaignid=" in query:
            return False
        return True
    except (TypeError, ValueError):
        return False

# Strips chrome (nav/ads/scripts) before reading text, and prefers the semantic
# article/main container over the whole body so boilerplate (site nav, related-articles
# rails, comment widgets) doesn't dilute the actual content.
_EXTRACT_TEXT_JS = """() => {
    document.querySelectorAll(
        'script,style,nav,header,footer,aside,noscript,iframe,form,svg'
    ).forEach(e => e.remove());
    const main = document.querySelector('article, main, [role="main"]') || document.body;
    return main.innerText;
}"""

# Picks the page's most meaningful content images: og:image first (the page's own
# declaration of its representative image), then the largest actually-rendered <img>
# elements by on-screen area. Filters out the noise that dominates raw <img> lists —
# icons/logos/trackers (tiny rendered size), data: URIs, and SVGs (usually decorative
# vector chrome, and not something a chat <img> tag benefits from). Runs BEFORE
# _EXTRACT_TEXT_JS for the same reason screenshots do: that extractor deletes chunks of
# the DOM, taking its <img> elements with it.
_EXTRACT_IMAGE_URLS_JS = """() => {
    const urls = [];
    const og = document.querySelector('meta[property="og:image"], meta[name="og:image"]');
    if (og && og.content) urls.push(og.content);
    const imgs = [...document.querySelectorAll('img')]
        .map(img => {
            const r = img.getBoundingClientRect();
            return { src: img.currentSrc || img.src, area: r.width * r.height };
        })
        .filter(i => i.src && i.area >= 40000 && i.src.startsWith('http') && !i.src.toLowerCase().includes('.svg'))
        .sort((a, b) => b.area - a.area);
    for (const i of imgs) urls.push(i.src);
    return [...new Set(urls)].slice(0, 4);
}"""

_IMAGE_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _download_images(image_urls: list[str], dest_dir: Path, max_images: int, referer: str) -> list[str]:
    """Downloads up to max_images of the candidate URLs into dest_dir, returning saved
    filenames. Plain requests.get rather than routing back through the browser — the
    URLs were already discovered from the fully-rendered DOM, so all that's left is a
    byte fetch. Guards: content-type must be a raster image type we can name an
    extension for (also filters HTML error pages served with 200), 5MB size cap read
    incrementally so a mislabeled/huge file can't balloon memory or disk, Referer set
    since some CDNs (news sites especially) 403 hotlink-style requests without one.
    Any per-image failure just moves on to the next candidate — fail-soft like
    everything else in this module."""
    saved: list[str] = []
    for url in image_urls:
        if len(saved) >= max_images:
            break
        try:
            resp = requests.get(
                url,
                timeout=6,
                stream=True,
                headers={"User-Agent": _USER_AGENT, "Referer": referer},
            )
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            ext = _IMAGE_EXT_BY_TYPE.get(content_type)
            if not ext:
                continue
            data = b""
            for chunk in resp.iter_content(chunk_size=65536):
                data += chunk
                if len(data) > 5_000_000:
                    data = b""
                    break
            if not data:
                continue
            filename = f"pageimg_{uuid.uuid4().hex[:12]}{ext}"
            (dest_dir / filename).write_bytes(data)
            saved.append(filename)
        except Exception:
            continue
    return saved


def available() -> bool:
    """False if the `playwright` package isn't installed, or its browser binaries were
    never downloaded (`playwright install chromium`) - callers should fall back to
    snippet-only results rather than raise, same fail-soft contract as the rest of the
    web-search plumbing."""
    return _PLAYWRIGHT_AVAILABLE


def fetch_pages(
    urls: list[str],
    timeout: float = 10.0,
    settle_ms: int = 400,
    screenshot_dir: Path | str | None = None,
    history_query: str | None = None,
    history_source: str | None = None,
) -> dict[str, dict]:
    """Visits each URL once in a single short-lived headless browser, returning
    {url: {"text": <clean visible text>, "html": <fully JS-rendered HTML>,
    "screenshot": <filename or None>, "images": [<filenames>]}} for whichever URLs
    succeeded. Text and HTML come from the same page load so callers needing different
    things (readable article text vs. regex-scanning rendered markup for price tags)
    don't pay for two separate visits to the same URL. When screenshot_dir is set, a
    viewport PNG of each rendered page is saved there (unique filename, returned as
    "screenshot") — viewport-sized, not full_page, deliberately: these are shown inline
    in a chat transcript as visual evidence of what was browsed, where a 20000px-tall
    full-page capture of an infinite-scroll site would be unusably huge, not more
    informative — plus up to 2 of the page's own content images (og:image / largest
    rendered <img>s, see _EXTRACT_IMAGE_URLS_JS + _download_images), so the chat can
    show a product photo or article image itself, not only the page screenshot. Both
    captures run BEFORE the text-extraction JS, because _EXTRACT_TEXT_JS mutates the
    live DOM (removes nav/header/footer/etc.) — a screenshot taken after would show
    that stripped-down skeleton, and the <img> elements would be gone entirely.
    Failures (timeout, DNS, bot-blocked, screenshot write error, whatever) are silently
    omitted/None, never raised - one flaky page shouldn't sink an otherwise-good batch
    of research results, and the caller already has snippet-only data to fall back to."""
    out: dict[str, dict] = {}
    urls = [
        u for u in dict.fromkeys(urls)
        if u and is_allowed_navigation_url(u)
    ]  # de-dupe, preserve order; ads are never navigation candidates
    if not urls or not _PLAYWRIGHT_AVAILABLE:
        return out
    if screenshot_dir is not None:
        screenshot_dir = Path(screenshot_dir)
    try:
        with sync_playwright() as p:
            # GPU rendering explicitly disabled: this machine's iGPU is shared with the
            # LLM backends (Vulkan/llama.cpp) and already hit a real "not enough memory
            # for command submission" crash earlier from GPU memory pressure - scraping
            # plain page text has no need for GPU-accelerated compositing, so there's no
            # reason to let Chromium's default GPU usage add any risk of contending with
            # or destabilizing whatever model is currently loaded.
            browser = p.chromium.launch(headless=True, args=["--disable-gpu", "--disable-dev-shm-usage"])
            try:
                context = browser.new_context(user_agent=_USER_AGENT, viewport={"width": 1280, "height": 800})
                # Block ad/tracker requests, including an ad redirect attempting to become
                # the top-level navigation. The scraper never clicks page elements; this
                # closes the remaining redirect/subresource route into ad infrastructure.
                def block_ads(route):
                    if is_allowed_navigation_url(route.request.url):
                        route.continue_()
                    else:
                        route.abort()

                context.route("**/*", block_ads)
                for url in urls:
                    # Every visit lands in browse_history — successes AND failures
                    # (the AI history page should show links that were attempted and
                    # didn't load, not silently omit them). history_query/source come
                    # from the caller (the search query / which browse path), the
                    # title from the rendered page itself.
                    visit_ok = False
                    visit_title = None
                    try:
                        page = context.new_page()
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                            if not is_allowed_navigation_url(page.url):
                                continue
                            page.wait_for_timeout(settle_ms)
                            try:
                                visit_title = (page.title() or "").strip() or None
                            except Exception:
                                visit_title = None
                            screenshot_name = None
                            page_images: list[str] = []
                            if screenshot_dir is not None:
                                try:
                                    screenshot_name = f"shot_{uuid.uuid4().hex[:12]}.png"
                                    page.screenshot(path=str(screenshot_dir / screenshot_name))
                                except Exception:
                                    screenshot_name = None
                                try:
                                    image_urls = page.evaluate(_EXTRACT_IMAGE_URLS_JS) or []
                                    page_images = _download_images(
                                        image_urls, screenshot_dir, max_images=2, referer=url
                                    )
                                except Exception:
                                    page_images = []
                            text = (page.evaluate(_EXTRACT_TEXT_JS) or "").strip()
                            html = page.content()
                            if text or html:
                                visit_ok = True
                                out[url] = {
                                    "text": text,
                                    "html": html,
                                    "screenshot": screenshot_name,
                                    "images": page_images,
                                }
                        finally:
                            page.close()
                    except Exception:
                        pass
                    finally:
                        browse_history_store.add_visit(
                            url, visit_title, history_query, history_source, visit_ok
                        )
            finally:
                browser.close()
    except Exception:
        return out
    return out
