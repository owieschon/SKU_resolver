"""Web fetchers for HttpHtmlCatalogSource (deployment layer — urllib allowed).

Two fetchers, both `url -> html`:
  - static_fetcher: stdlib urllib; for server-rendered catalog pages.
  - playwright_fetcher: renders JS-heavy product grids ([web] extra; needs
    `playwright install`). Gated — not run in CI.

`urlopen` is injectable on the static fetcher so request building is unit-tested
without network.
"""
from __future__ import annotations

import urllib.request

_UA = 'sku-catalog-bot/1.0'


def static_fetcher(url: str, *, urlopen=None, timeout: float = 30.0) -> str:
    """Fetch a static HTML page as text."""
    from erp_transport.http_backend import certifi_urlopen
    opener = urlopen or certifi_urlopen
    req = urllib.request.Request(url, headers={'User-Agent': _UA,
                                               'Accept': 'text/html'})
    with opener(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'replace')


def playwright_fetcher(url: str, *, timeout_ms: int = 30000) -> str:
    """Render a JS-heavy page and return the settled DOM HTML. Needs the [web]
    extra and `playwright install chromium`; not exercised in CI."""
    try:
        from playwright.sync_api import sync_playwright  # pragma: no cover
    except ImportError as e:   # pragma: no cover - env-dependent
        raise RuntimeError("playwright_fetcher needs the [web] extra: "
                           "pip install '.[web]' && playwright install chromium"
                           ) from e
    with sync_playwright() as p:   # pragma: no cover - live only
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout_ms, wait_until='networkidle')
            return page.content()
        finally:
            browser.close()
