"""Real-browser tests with public-name fixtures and a test-only DNS seam."""

import asyncio
import os
import shutil
import struct
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import BrowserType, async_playwright

from page_archiver.capture import browser_executable, capture
from page_archiver.config import Settings
from page_archiver.network import public_addresses


CHROME = (
    os.environ.get("PAGE_ARCHIVER_BROWSER_EXECUTABLE")
    or shutil.which("chromium")
    or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
pytestmark = pytest.mark.skipif(not Path(CHROME).exists(), reason="installed Chromium required")


@asynccontextmanager
async def fixture_server(
    monkeypatch, page, status=200, resource_status=200, *, secure=False, data_delay=0
):
    requests = []

    async def handle(reader, writer):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            requests.append(raw)
            path = raw.split(b" ")[1].decode()
            if path == "/":
                mime, body = "text/html", page
            elif path == "/article":
                await asyncio.sleep(data_delay)
                mime, body = "text/plain", "Late article body"
            elif path == "/style.css":
                mime, body = (
                    "text/css",
                    "body { background: rgb(20, 40, 80); color: white; min-height: 1200px } h1 { font-size: 48px } @media (min-width: 1400px) { #columns { display: flex } #columns > div { width: 50% } }",
                )
            elif path == "/image.svg":
                mime, body = (
                    "image/svg+xml",
                    '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120"><rect width="240" height="120" fill="orange"/></svg>',
                )
            else:
                mime, body = "text/plain", "unexpected"
            data = body.encode()
            writer.write(
                f"HTTP/1.1 {status if path == '/' else resource_status} Fixture\r\nContent-Type: {mime}\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    # Pin only the fixture hostname. All other requests use real production policy.
    monkeypatch.setattr(
        "page_archiver.network.public_addresses",
        lambda host, port: (
            ["127.0.0.1"] if host == "fixture.example" else public_addresses(host, port)
        ),
    )
    if secure:
        launch = BrowserType.launch

        async def secure_fixture(browser_type, **kwargs):
            # Trusted Types requires a secure context. Trust only this test origin;
            # production capture never relaxes transport or page policy.
            kwargs["args"] = [
                *kwargs.get("args", []),
                f"--unsafely-treat-insecure-origin-as-secure=http://fixture.example:{port}",
            ]
            return await launch(browser_type, **kwargs)

        monkeypatch.setattr(BrowserType, "launch", secure_fixture)
    try:
        yield f"http://fixture.example:{port}/", requests
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


def test_real_browser_embeds_assets_and_reopens_offline(tmp_path, monkeypatch):
    async def scenario():
        page = '<!doctype html><title>Archive fixture</title><link rel="stylesheet" href="/style.css"><section id="columns"><div><style>#columns { outline: 3px solid red }</style><h1>Saved fixture</h1></div><div><form><input name="append"><input name="prepend"></form><img src="/image.svg"></div></section><noscript><img src="http://127.0.0.1:9/pixel"></noscript><script>document.body.dataset.loaded = "yes";</script>'
        settings = Settings(browser_executable=CHROME, capture_timeout=60)
        async with fixture_server(monkeypatch, page) as (url, requests):
            result = await capture(url, tmp_path / "result", settings)
        assert result.status == "succeeded", result
        html = Path(result.html.path).read_text()
        assert "data:image/svg+xml" in html
        assert "background:" in html
        assert "<script" not in html
        assert all(
            b"Authorization:" not in request and b"Cookie:" not in request for request in requests
        )
        png = Path(result.png.path).read_bytes()
        width, height = struct.unpack(">II", png[16:24])
        assert width == 1440 and height >= 1200
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=browser_executable(settings), chromium_sandbox=True
            )
            try:
                context = await browser.new_context(
                    offline=True,
                    java_script_enabled=False,
                    viewport={"width": 1440, "height": 1000},
                )
                page = await context.new_page()
                await page.goto(Path(result.html.path).as_uri())
                assert await page.locator("h1").inner_text() == "Saved fixture"
                assert (
                    await page.locator("#columns").evaluate("e => getComputedStyle(e).outlineWidth")
                    == "3px"
                )
                assert (
                    await page.locator("#columns").evaluate("e => getComputedStyle(e).display")
                    == "flex"
                )
                assert await page.locator("img").evaluate("(img) => img.naturalWidth") == 240
                assert (
                    await page.locator("body").evaluate(
                        "(body) => getComputedStyle(body).backgroundColor"
                    )
                    == "rgb(20, 40, 80)"
                )
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_browser_redirect_to_loopback_never_reaches_target(tmp_path, monkeypatch):
    async def scenario():
        hits = []

        async def private(reader, writer):
            hits.append(True)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(private, "127.0.0.1", 0)
        private_port = server.sockets[0].getsockname()[1]
        try:
            page = f'<title>Redirect</title><body>Redirecting<script>location.href="http://127.0.0.1:{private_port}/"</script>'
            async with fixture_server(monkeypatch, page) as (url, _):
                result = await capture(
                    url,
                    tmp_path / "result",
                    Settings(browser_executable=CHROME, capture_timeout=15),
                )
            assert result.status != "succeeded"
            assert hits == []
            assert not (tmp_path / "result").exists()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_real_bot_wall_is_classified_without_artifacts(tmp_path, monkeypatch):
    async def scenario():
        async with fixture_server(
            monkeypatch, "<title>Robot Check</title><body>Enter the characters you see"
        ) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "blocked"
        assert not (tmp_path / "result").exists()

    asyncio.run(scenario())


def test_browser_subresources_and_websockets_cannot_bypass_proxy(tmp_path, monkeypatch):
    async def scenario():
        hits = []

        async def private(reader, writer):
            hits.append(True)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(private, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            page = f'<title>Boundary fixture</title><body>Public content<img src="http://127.0.0.1:{port}/"><script>new WebSocket("ws://127.0.0.1:{port}/"); fetch("http://localhost:{port}/").catch(()=>{{}});</script>'
            async with fixture_server(monkeypatch, page) as (url, _):
                await capture(
                    url,
                    tmp_path / "result",
                    Settings(browser_executable=CHROME, capture_timeout=15),
                )
            assert hits == []
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "page,status,resource_status,expected",
    [
        ("<title>Fixture</title><body>Truncated article", 206, 200, "partial"),
        ('<title>Fixture</title><body>Article<img src="/image.svg">', 200, 206, "partial"),
        (
            '<title>Fixture</title><body>Loading product details...<script>fetch("http://127.0.0.1:9/data").catch(()=>{});</script>',
            200,
            200,
            "partial",
        ),
        (
            '<title>Fixture</title><body><main aria-busy="true">Article</main><script>fetch("/data").catch(()=>{});</script>',
            200,
            500,
            "partial",
        ),
        (
            '<title>Sign in</title><body>Sign in to continue<form><input type="email"><button>Continue</button></form>',
            200,
            200,
            "login_required",
        ),
        (
            "<title>Sign in</title><body>Sign in to continue<button>Continue with your provider</button>",
            200,
            200,
            "login_required",
        ),
    ],
)
def test_partial_pages_and_login_gates_never_publish(
    tmp_path, monkeypatch, page, status, resource_status, expected
):
    async def scenario():
        async with fixture_server(monkeypatch, page, status, resource_status) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == expected
        assert not (tmp_path / "result").exists()

    asyncio.run(scenario())


def test_complete_page_with_failed_background_request_records_warning(tmp_path, monkeypatch):
    async def scenario():
        page = '<title>Article</title><body><main><h1>Complete article</h1><p>The article content is available.</p></main><script>fetch("/metrics").catch(()=>{});</script>'
        async with fixture_server(monkeypatch, page, resource_status=500) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "succeeded", result
        assert result.page_request_failures == 1
        assert "Complete article" in Path(result.html.path).read_text()

    asyncio.run(scenario())


def test_capture_strict_csp_without_enabling_site_inline_scripts(tmp_path, monkeypatch):
    async def scenario():
        page = """<!doctype html><meta http-equiv="Content-Security-Policy" content="script-src 'none'"><title>CSP fixture</title><body><h1>Public article</h1><script>document.querySelector('h1').textContent='Unwanted inline script';</script>"""
        async with fixture_server(monkeypatch, page) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "succeeded", result
        assert "Public article" in Path(result.html.path).read_text()
        assert "Unwanted inline script" not in Path(result.html.path).read_text()

    asyncio.run(scenario())


def test_unused_styles_do_not_make_complete_article_partial(tmp_path, monkeypatch):
    async def scenario():
        page = "<title>Article</title><style>.absent-signup-alert { background-image: url(/missing.svg) } h1 { color: rgb(10, 20, 30) }</style><body><h1>Complete public article</h1>"
        async with fixture_server(monkeypatch, page, resource_status=404) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "succeeded", result
        html = Path(result.html.path).read_text()
        assert "Complete public article" in html
        assert "missing.svg" not in html
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=CHROME)
            try:
                context = await browser.new_context(offline=True, java_script_enabled=False)
                saved = await context.new_page()
                await saved.goto(Path(result.html.path).as_uri())
                assert (
                    await saved.locator("h1").evaluate("e => getComputedStyle(e).color")
                    == "rgb(10, 20, 30)"
                )
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_missing_visible_image_still_rejects_capture(tmp_path, monkeypatch):
    async def scenario():
        page = '<title>Article</title><body><h1>Public article</h1><img src="/missing.svg">'
        async with fixture_server(monkeypatch, page, resource_status=404) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "partial"
        assert not (tmp_path / "result").exists()

    asyncio.run(scenario())


def test_article_discussing_captchas_is_not_a_bot_wall(tmp_path, monkeypatch):
    async def scenario():
        page = (
            "<title>Repository: CAPTCHA testing library</title><body><h1>Documentation</h1>"
            + "<p>Detailed documentation about browser testing.</p>" * 100
        )
        async with fixture_server(monkeypatch, page) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "succeeded", result

    asyncio.run(scenario())


def test_delayed_article_content_is_allowed_to_render(tmp_path, monkeypatch):
    async def scenario():
        page = '<title>Article</title><body><script>setTimeout(() => document.body.insertAdjacentHTML("beforeend", "<h1>Loaded article</h1>"), 500)</script>'
        async with fixture_server(monkeypatch, page) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "succeeded", result
        assert "Loaded article" in Path(result.html.path).read_text()

    asyncio.run(scenario())


def test_pending_article_request_finishes_before_snapshot(tmp_path, monkeypatch):
    async def scenario():
        page = '<title>Article</title><body><nav>Navigation</nav><main></main><script>fetch("/article").then(response => response.text()).then(text => document.querySelector("main").textContent = text);</script>'
        async with fixture_server(monkeypatch, page, data_delay=2) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "succeeded", result
        assert "Late article body" in Path(result.html.path).read_text()

    asyncio.run(scenario())


def test_background_long_poll_does_not_prevent_complete_capture(tmp_path, monkeypatch):
    async def scenario():
        page = '<title>Article</title><body><h1>Complete article</h1><script>fetch("/article").catch(() => {});</script>'
        async with fixture_server(monkeypatch, page, data_delay=30) as (url, _):
            result = await capture(
                url, tmp_path / "result", Settings(browser_executable=CHROME, capture_timeout=10)
            )
        assert result.status == "succeeded", result
        assert "Complete article" in Path(result.html.path).read_text()

    asyncio.run(scenario())


def test_trusted_types_site_captures_without_relaxing_site_policy(tmp_path, monkeypatch):
    async def scenario():
        page = """<!doctype html><meta http-equiv="Content-Security-Policy" content="require-trusted-types-for 'script'; trusted-types 'none'; script-src 'nonce-fixture'"><title>Article</title><body><p id="nested"></p><img src="/image.svg"><script nonce="fixture">setTimeout(() => { const heading = document.createElement('h1'); heading.textContent = 'Protected article'; heading.dataset.secure = String(window.isSecureContext); document.body.appendChild(heading); const nested = document.createElement('div'); nested.textContent = 'Nested content'; document.getElementById('nested').appendChild(nested); }, 300);</script><script>document.body.textContent='Unwanted script';</script>"""
        async with fixture_server(monkeypatch, page, secure=True) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "succeeded", result
        html = Path(result.html.path).read_text()
        assert "Protected article" in html
        assert 'data-secure="true"' in html
        assert "Unwanted script" not in html
        assert "data:image/svg+xml" in html

    asyncio.run(scenario())


def test_trusted_types_shadow_scripts_are_excluded_without_rewriting(tmp_path, monkeypatch):
    async def scenario():
        page = """<!doctype html><meta http-equiv="Content-Security-Policy" content="require-trusted-types-for 'script'; trusted-types 'none'"><title>Shadow article</title><body><h1>Public article</h1><div><template shadowrootmode="open"><p>Retained shadow content</p><script type="application/json">{"state":"private-script-data"}</script></template></div>"""
        async with fixture_server(monkeypatch, page, secure=True) as (url, _):
            result = await capture(url, tmp_path / "result", Settings(browser_executable=CHROME))
        assert result.status == "succeeded", result
        html = Path(result.html.path).read_text()
        assert "Retained shadow content" in html
        assert "private-script-data" not in html
        assert "<script" not in html

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "pixel_limit,expected", [(50_000_000, "too_large"), (60_000_000, "succeeded")]
)
def test_tall_pages_keep_full_resolution_within_configured_pixel_budget(
    tmp_path, monkeypatch, pixel_limit, expected
):
    async def scenario():
        page = '<title>Long article</title><body style="margin:0;height:40000px"><h1 style="margin:0">Long article</h1><footer style="position:absolute;top:39950px">End of article</footer>'
        settings = Settings(browser_executable=CHROME, max_screenshot_pixels=pixel_limit)
        async with fixture_server(monkeypatch, page) as (url, _):
            result = await capture(url, tmp_path / "result", settings)
        assert result.status == expected, result
        if expected == "succeeded":
            assert struct.unpack(">II", Path(result.png.path).read_bytes()[16:24]) == (1440, 40000)
            assert "End of article" in Path(result.html.path).read_text()
        else:
            assert not (tmp_path / "result").exists()

    asyncio.run(scenario())
