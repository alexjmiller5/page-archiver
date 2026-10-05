"""Bounded capture in a fresh browser, followed by atomic artifact publication."""

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright
from pydantic import BaseModel

from page_archiver.config import Settings
from page_archiver.network import CaptureProxy, validate_url


class CaptureFailure(Exception):
    """A stable public failure code, never an upstream exception message."""


class Artifact(BaseModel):
    path: str
    mime: str
    bytes: int
    sha256: str


class Outcome(BaseModel):
    status: str
    captured_at: str | None = None
    final_url: str | None = None
    title: str | None = None
    html: Artifact | None = None
    png: Artifact | None = None
    page_request_failures: int = 0


@dataclass
class Rendered:
    html: str
    png: bytes
    final_url: str
    title: str
    missing_resources: int
    page_request_failures: int = 0


def browser_executable(settings: Settings) -> str:
    candidates = (
        [settings.browser_executable]
        if settings.browser_executable
        else [
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise CaptureFailure("browser_unavailable")


def classify_page(title: str, text: str, password: bool) -> None:
    if len(text) < 500 and re.search(
        r"\b(loading(?:\s+(?:product|page|content|details))?|please wait|failed to load|unable to load)\b",
        text,
        re.I,
    ):
        raise CaptureFailure("partial")
    if len(text) < 3000 and re.search(
        r"captcha|robot check|access denied|just a moment|verify you are human|security check",
        title,
        re.I,
    ):
        raise CaptureFailure("blocked")
    if len(text) < 3000 and re.search(
        r"verify (that )?you (are|re) (a )?human|enter the characters you see|automated access|unusual traffic",
        text,
        re.I,
    ):
        raise CaptureFailure("blocked")
    if re.search(r"sign[ -]?in|log[ -]?in", title, re.I) and (
        password
        or (len(text) < 3000 and re.search(r"sign[ -]?in|log[ -]?in|continue with", text, re.I))
    ):
        raise CaptureFailure("login_required")
    if not text.strip():
        raise CaptureFailure("empty")


async def serialize(page, script: str, resource) -> str:
    """Run the serializer in its own world without changing the site's CSP."""
    session = await page.context.new_cdp_session(page)
    tasks = set()
    try:
        frame = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
        world = await session.send(
            "Page.createIsolatedWorld", {"frameId": frame, "worldName": "page-archiver"}
        )
        context_id = world["executionContextId"]

        async def evaluate(expression):
            result = await session.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "contextId": context_id,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            if "exceptionDetails" in result:
                raise CaptureFailure("browser_error")
            return result["result"].get("value")

        async def respond(event):
            request = json.loads(event["payload"])
            try:
                value = await resource(None, request["url"])
            except RuntimeError:
                value = None
            await evaluate(f"globalThis.__archiveReply({request['id']},{json.dumps(value)})")

        def received(event):
            if event["executionContextId"] == context_id and event["name"] == "__archiveSend":
                task = asyncio.create_task(respond(event))
                tasks.add(task)

        session.on("Runtime.bindingCalled", received)
        await session.send(
            "Runtime.addBinding", {"name": "__archiveSend", "executionContextName": "page-archiver"}
        )
        await evaluate("""(() => {
          // Only this isolated world's parser changes. The site's policy and
          // parser remain untouched. The safe HTML sink removes scripts and
          // handlers without needing a permissive Trusted Types policy.
          globalThis.DOMParser = class extends DOMParser {
            parseFromString(content, type) {
              try { return super.parseFromString(content, type); }
              catch (error) {
                if (!(error instanceof TypeError) || type !== 'text/html' ||
                    typeof Element.prototype.setHTML !== 'function') throw error;
                const doc = document.implementation.createHTMLDocument('');
                doc.documentElement.setHTML(String(content), {
                  sanitizer: {removeElements: ['script']}
                });
                return doc;
              }
            }
          };
          const pending = new Map(); let next = 0;
          globalThis.__archiveResource = url => new Promise((resolve, reject) => {
            const id = ++next; pending.set(id, {resolve, reject});
            globalThis.__archiveSend(JSON.stringify({id, url}));
          });
          globalThis.__archiveReply = (id, value) => {
            const request = pending.get(id); pending.delete(id);
            if (value === null) request.reject(new Error('resource_unavailable'));
            else request.resolve(value);
          };
        })()""")
        await evaluate(script)
        html = await evaluate("globalThis.__archivePage()")
        # Final cleanup needs an inert parser that preserves declarative shadow
        # templates. A fresh offline document has no source-site Trusted Types
        # policy, so no policy is weakened and no unsafe HTML sink is necessary.
        cleanup = await page.context.browser.new_context(
            offline=True, service_workers="block", accept_downloads=False
        )
        try:
            document = await cleanup.new_page()
            await document.evaluate(script)
            return await document.evaluate("html => globalThis.__archiveFinalize(html)", html)
        finally:
            await cleanup.close()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await session.detach()


async def _render(url: str, settings: Settings) -> Rendered:
    executable = browser_executable(settings)
    asset = Path(__file__).parent / "assets/capture.js"
    if not asset.is_file():
        raise CaptureFailure("asset_unavailable")
    missing = 0
    fetched = 0
    semaphore = asyncio.Semaphore(6)
    async with CaptureProxy() as proxy, async_playwright() as driver:
        proxy_url = f"http://127.0.0.1:{proxy.port}"
        # Chromium never inherits the daemon's credential environment.
        browser_env = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "TMPDIR",
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR",
                "FONTCONFIG_FILE",
                "FONTCONFIG_PATH",
                "LANG",
                "LC_ALL",
            }
        }
        browser = await driver.chromium.launch(
            executable_path=executable,
            headless=True,
            chromium_sandbox=True,
            proxy={"server": proxy_url, "bypass": "<-loopback>"},
            env=browser_env,
            args=[
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-features=WebTransport",
                "--disable-background-networking",
            ],
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                device_scale_factor=1,
                service_workers="block",
                accept_downloads=False,
            )

            # Every navigation, redirect and resource remains behind the proxy;
            # URL validation also rejects embedded HTTP credentials.
            async def route_request(route):
                try:
                    validate_url(route.request.url)
                except ValueError:
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()

            await context.route("**/*", route_request)
            async with httpx.AsyncClient(
                proxy=proxy_url,
                trust_env=False,
                timeout=10,
                follow_redirects=True,
                max_redirects=10,
            ) as client:

                async def resource(_source, target):
                    nonlocal missing, fetched
                    try:
                        validate_url(target)
                        async with semaphore, client.stream("GET", target) as response:
                            response.raise_for_status()
                            if response.status_code == 206:
                                raise CaptureFailure("partial")
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                fetched += len(chunk)
                                if (
                                    fetched > 200 * 1024 * 1024
                                    or len(data) + len(chunk) > 20 * 1024 * 1024
                                ):
                                    raise CaptureFailure("too_large")
                                data.extend(chunk)
                            return {
                                "status": response.status_code,
                                "url": str(response.url),
                                "mime": response.headers.get(
                                    "content-type", "application/octet-stream"
                                ),
                                "body": base64.b64encode(data).decode(),
                            }
                    except (httpx.HTTPError, ValueError, CaptureFailure):
                        missing += 1
                        raise RuntimeError("resource_unavailable") from None

                page = await context.new_page()
                content_failures = []
                pending_content = set()

                def request_started(request):
                    if request.resource_type in ("fetch", "xhr"):
                        pending_content.add(request)

                def request_failed(request):
                    if request in pending_content:
                        content_failures.append(True)
                    pending_content.discard(request)

                def response_received(response):
                    if response.request.resource_type in ("fetch", "xhr") and (
                        response.status == 206 or response.status >= 400
                    ):
                        content_failures.append(True)

                page.on("request", request_started)
                page.on("requestfinished", lambda request: pending_content.discard(request))
                page.on("requestfailed", request_failed)
                page.on("response", response_received)
                page.set_default_timeout(15000)
                response = await page.goto(url, wait_until="load", timeout=45000)
                if response is None or response.status >= 400:
                    raise CaptureFailure("http_error")
                if response.status == 206:
                    raise CaptureFailure("partial")
                try:
                    validate_url(page.url)
                except ValueError:
                    raise CaptureFailure("blocked_destination") from None
                if "text/html" not in response.headers.get("content-type", ""):
                    raise CaptureFailure("unsupported_content")
                try:
                    # Locator reads avoid a page-side eval on later polling ticks,
                    # which strict Trusted Types policies reject.
                    async with asyncio.timeout(5):
                        while not (await page.locator("body").inner_text()).strip():
                            await asyncio.sleep(0.05)
                except (TimeoutError, BrowserTimeout):
                    raise CaptureFailure("empty") from None
                await page.evaluate("document.fonts.ready")
                try:
                    async with asyncio.timeout(5):
                        while pending_content:
                            await asyncio.sleep(0.05)
                except TimeoutError:
                    # Background long polls need not finish for a page to be
                    # complete, but an explicitly busy main view must not pass.
                    if await page.locator('[aria-busy="true"]:visible').count():
                        raise CaptureFailure("partial") from None
                title = await page.title()
                classify_page(
                    title,
                    await page.locator("body").inner_text(),
                    await page.locator("input[type=password]:visible").count() > 0,
                )
                html = await serialize(page, asset.read_text(), resource)
                dimensions = await page.evaluate(
                    "({width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})"
                )
                if (
                    dimensions["width"] * dimensions["height"] > 50_000_000
                    or dimensions["height"] > 32767
                ):
                    raise CaptureFailure("too_large")
                png = await page.screenshot(full_page=True, animations="disabled", timeout=15000)
                if proxy.exhausted or fetched > 200 * 1024 * 1024:
                    raise CaptureFailure("too_large")
                if await page.locator('[aria-busy="true"]:visible').count():
                    raise CaptureFailure("partial")
                classify_page(
                    await page.title(),
                    await page.locator("body").inner_text(),
                    await page.locator("input[type=password]:visible").count() > 0,
                )
                return Rendered(html, png, page.url, title, missing, len(content_failures))
        finally:
            await browser.close()


async def capture(url: str, destination: Path, settings: Settings) -> Outcome:
    destination = Path(destination)
    if destination.exists():
        return Outcome(status="destination_exists")
    try:
        validate_url(url)
    except ValueError:
        return Outcome(status="invalid_url")
    try:
        async with asyncio.timeout(settings.capture_timeout):
            rendered = await _render(url, settings)
        if not rendered.html or not rendered.html.strip():
            raise CaptureFailure("empty")
        html = rendered.html.encode("utf-8")
        if max(len(html), len(rendered.png)) > settings.max_artifact_bytes:
            raise CaptureFailure("too_large")
        if (
            len(rendered.png) < 24
            or not rendered.png.startswith(b"\x89PNG\r\n\x1a\n")
            or not all(struct.unpack(">II", rendered.png[16:24]))
        ):
            raise CaptureFailure("invalid_artifact")
        if rendered.missing_resources:
            raise CaptureFailure("partial")
        result = Outcome(
            status="succeeded",
            captured_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            final_url=rendered.final_url,
            title=rendered.title,
            page_request_failures=rendered.page_request_failures,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".capture-", dir=destination.parent) as temporary:
            staging = Path(temporary) / "artifacts"
            staging.mkdir(mode=0o700)
            for name, data, mime in [
                ("html", html, "text/html"),
                ("png", rendered.png, "image/png"),
            ]:
                filename = "page." + name
                with (staging / filename).open("xb") as output:
                    os.chmod(output.name, 0o600)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                setattr(
                    result,
                    name,
                    Artifact(
                        path=str(destination / filename),
                        mime=mime,
                        bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    ),
                )
            manifest = staging / "capture.json"
            with manifest.open("x") as output:
                os.chmod(manifest, 0o600)
                output.write(result.model_dump_json(indent=2))
                output.flush()
                os.fsync(output.fileno())
            staging.rename(destination)
        return result
    except CaptureFailure as error:
        return Outcome(status=str(error))
    except (TimeoutError, BrowserTimeout):
        return Outcome(status="timeout")
    except BrowserError:
        return Outcome(status="browser_error")
    except OSError:
        return Outcome(status="storage_error")
