"""Bounded capture in a fresh browser, followed by atomic artifact publication."""

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import shutil
import struct
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from importlib.metadata import version

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
    missing_resources: int = 0
    incomplete_regions: int = 0


@dataclass
class Rendered:
    html: str
    png: bytes
    final_url: str
    title: str
    missing_resources: int
    page_request_failures: int = 0
    incomplete_regions: int = 0


def partial_warning(missing: int, regions: int) -> str:
    parts = []
    if missing:
        parts.append(f"{missing} resource requests could not be saved")
    if regions:
        parts.append(f"{regions} section{' was' if regions == 1 else 's were'} still loading")
    return "Incomplete archive: " + "; ".join(parts) + "." if parts else ""


async def unfinished_sections(page) -> list:
    """Retain secondary loading regions only around readable primary content."""
    regions = await page.locator('[aria-busy="true"]:visible').element_handles()
    if not regions:
        return []
    ready = await page.evaluate(r"""() => {
        const ignored = '[aria-busy="true"],header,footer,nav,aside,script,style,' +
            '[role="banner"],[role="contentinfo"],[role="navigation"],' +
            '[role="complementary"],[hidden],[aria-hidden="true"]';
        const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
        const root = document.querySelector('main,[role="main"]') || document.body;
        if (root.closest('[aria-busy="true"]')) return false;
        const heading = [...root.querySelectorAll('h1,[role="heading"][aria-level="1"]')]
            .some(e => visible(e) && !e.closest(ignored) && e.innerText.trim());
        if (!heading) return false;
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let text = '', node;
        while ((node = walker.nextNode())) {
            const e = node.parentElement;
            if (e && visible(e) && !e.closest(ignored)) text += node.textContent;
        }
        return text.replace(/\s+/g, ' ').trim().length >= 200;
    }""")
    if not ready:
        raise CaptureFailure("partial")
    return regions


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


def classify_page(
    title: str, text: str, password: bool, *, secondary_loading: bool = False
) -> None:
    if (
        not secondary_loading
        and len(text) < 500
        and re.search(
            r"\b(loading(?:\s+(?:product|page|content|details))?|please wait|failed to load|unable to load)\b",
            text,
            re.I,
        )
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
    if re.search(r"\b(?:sign[ -]?in|log[ -]?in)\b", title, re.I) and (
        password
        or (
            len(text) < 3000
            and re.search(r"\b(?:sign[ -]?in|log[ -]?in|continue with)\b", text, re.I)
        )
    ):
        raise CaptureFailure("login_required")
    if not text.strip():
        raise CaptureFailure("empty")


async def dismiss_optional_dialogs(page) -> bool:
    changed = False
    banner = page.locator("#onetrust-banner-sdk")
    if await banner.is_visible():
        rejected = await page.evaluate("""() => {
            if (typeof window.OneTrust?.RejectAll !== 'function') return false;
            try { window.OneTrust.RejectAll(); return true; }
            catch { return false; }
        }""")
        if rejected:
            try:
                await banner.wait_for(state="hidden", timeout=2000)
                changed = True
            except BrowserTimeout:
                pass
    # Keep element identity as visible dialogs are removed from the document.
    for dialog in await page.locator(
        '[role="dialog"][aria-modal="true"]:visible'
    ).element_handles():
        try:
            if not await dialog.query_selector('input[type="email"]'):
                continue
            if await dialog.query_selector('input[type="password"]'):
                continue
            buttons = []
            for button in await dialog.query_selector_all('button[type="button"]'):
                if re.fullmatch(r"\s*Continue to site\s*", await button.inner_text(), re.I):
                    buttons.append(button)
            if len(buttons) != 1 or not await buttons[0].is_visible():
                continue
            await buttons[0].click(timeout=2000)
            await dialog.wait_for_element_state("hidden", timeout=2000)
            changed = True
        except BrowserError:
            # Optional UI must not turn readable content into a failed capture.
            pass
    return changed


async def serialize(page, script: str, resource, incomplete_regions: int = 0) -> tuple[str, int]:
    """Run the serializer in its own world without changing the site's CSP."""
    session = await page.context.new_cdp_session(page)
    tasks = set()
    missing = 0
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
            nonlocal missing
            request = json.loads(event["payload"])
            try:
                value = await resource(None, request["url"])
            except RuntimeError:
                missing += 1
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
        # SingleFile can omit an image whose dimensions never became available.
        # Such a snapshot must not become a complete success merely because the
        # serializer never asked us to fetch that image.
        unloaded_images = await page.locator("img").evaluate_all("""images => images.filter(img =>
            /^https?:/i.test(img.currentSrc || img.src) && img.getClientRects().length &&
            getComputedStyle(img).visibility !== 'hidden' &&
            (!img.complete || !img.naturalWidth)).length""")
        # Final cleanup needs an inert parser that preserves declarative shadow
        # templates. A fresh offline document has no source-site Trusted Types
        # policy, so no policy is weakened and no unsafe HTML sink is necessary.
        cleanup = await page.context.browser.new_context(
            offline=True, service_workers="block", accept_downloads=False
        )
        try:
            document = await cleanup.new_page()
            await document.evaluate(script)
            cleaned = await document.evaluate(
                "args => globalThis.__archiveFinalize(args.html, args.warning)",
                {
                    "html": html,
                    "warning": partial_warning(missing + unloaded_images, incomplete_regions),
                },
            )
            return cleaned, unloaded_images
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
            headless=settings.browser_headless,
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
                headers={
                    "User-Agent": f"PageArchiver/{version('page-archiver')} (+https://github.com/alexjmiller5/page-archiver)"
                },
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
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
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
                    await page.wait_for_load_state("load", timeout=5000)
                except BrowserTimeout:
                    # A stalled asset must not hide an otherwise readable page.
                    # Serialization and live-image checks report missing assets.
                    pass
                await dismiss_optional_dialogs(page)
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
                    await unfinished_sections(page)
                readiness_deadline = asyncio.get_running_loop().time() + 15
                while True:
                    title = await page.title()
                    try:
                        pending_regions = await unfinished_sections(page)
                        classify_page(
                            title,
                            await page.locator("body").inner_text(),
                            await page.locator("input[type=password]:visible").count() > 0,
                            secondary_loading=bool(pending_regions),
                        )
                        break
                    except CaptureFailure as error:
                        if (
                            str(error) != "partial"
                            or asyncio.get_running_loop().time() >= readiness_deadline
                        ):
                            raise
                        await asyncio.sleep(0.1)
                await dismiss_optional_dialogs(page)
                pending_regions = await unfinished_sections(page)
                incomplete_regions = len(pending_regions)
                if incomplete_regions and not settings.retain_partial:
                    raise CaptureFailure("partial")
                html, unloaded_images = await serialize(
                    page, asset.read_text(), resource, incomplete_regions
                )
                # Deferred-content loading can reveal a late opt-out dialog.
                # Retry serialization once after a supported dismissal.
                if await dismiss_optional_dialogs(page):
                    missing = 0
                    html, unloaded_images = await serialize(
                        page, asset.read_text(), resource, incomplete_regions
                    )
                missing += unloaded_images
                # Match Playwright's disabled-animation screenshots. This
                # fresh page is closed after capture, so no restoration is
                # needed. Include animations inside open shadow roots.
                await page.evaluate("""async () => {
                    const visit = root => {
                        const finish = () => {
                            for (const animation of root.getAnimations()) {
                                if (!animation.effect || !animation.playbackRate) continue;
                                try {
                                    if (Number.isFinite(animation.effect.getComputedTiming().endTime))
                                        animation.finish();
                                    else animation.cancel();
                                } catch {}
                            }
                        };
                        finish();
                        root.addEventListener('transitionrun', finish);
                        root.addEventListener('animationstart', finish);
                        for (const element of root.querySelectorAll('*'))
                            if (element.shadowRoot) visit(element.shadowRoot);
                    };
                    visit(document);
                    // Animation completion handlers may also change layout.
                    await new Promise(resolve => requestAnimationFrame(() =>
                        requestAnimationFrame(resolve)));
                }""")
                dimensions = await page.evaluate(
                    "({width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})"
                )
                pixels = dimensions["width"] * dimensions["height"]
                scale = min(1.0, math.sqrt(settings.max_screenshot_pixels / pixels))
                if scale < settings.min_screenshot_scale:
                    raise CaptureFailure("too_large")
                if scale == 1:
                    png = await page.screenshot(
                        full_page=True, animations="disabled", timeout=15000
                    )
                else:
                    # Scale the bitmap, not CSS layout or the retained HTML. CDP
                    # applies this before allocating the output screenshot.
                    session = await context.new_cdp_session(page)
                    try:
                        async with asyncio.timeout(15):
                            shot = await session.send(
                                "Page.captureScreenshot",
                                {
                                    "format": "png",
                                    "captureBeyondViewport": True,
                                    "clip": {
                                        "x": 0,
                                        "y": 0,
                                        **dimensions,
                                        "scale": math.floor(scale * 1000) / 1000,
                                    },
                                },
                            )
                        png = base64.b64decode(shot["data"])
                    finally:
                        await session.detach()
                if proxy.exhausted or fetched > 200 * 1024 * 1024:
                    raise CaptureFailure("too_large")
                remaining_regions = await unfinished_sections(page)
                if not await page.evaluate(
                    "args => args.current.every(node => args.previous.includes(node))",
                    {"previous": pending_regions, "current": remaining_regions},
                ):
                    # Newly loading content was not represented in the saved
                    # warning. Do not publish an understated partial capture.
                    raise CaptureFailure("partial")
                classify_page(
                    await page.title(),
                    await page.locator("body").inner_text(),
                    await page.locator("input[type=password]:visible").count() > 0,
                    secondary_loading=bool(remaining_regions),
                )
                return Rendered(
                    html, png, page.url, title, missing, len(content_failures), incomplete_regions
                )
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
        incomplete = rendered.missing_resources or rendered.incomplete_regions
        if incomplete and not settings.retain_partial:
            raise CaptureFailure("partial")
        html = rendered.html.encode("utf-8")
        if max(len(html), len(rendered.png)) > settings.max_artifact_bytes:
            raise CaptureFailure("too_large")
        if (
            len(rendered.png) < 24
            or not rendered.png.startswith(b"\x89PNG\r\n\x1a\n")
            or not all(struct.unpack(">II", rendered.png[16:24]))
        ):
            raise CaptureFailure("invalid_artifact")
        width, height = struct.unpack(">II", rendered.png[16:24])
        if width * height > settings.max_screenshot_pixels:
            raise CaptureFailure("too_large")
        result = Outcome(
            status="partial" if incomplete else "succeeded",
            missing_resources=rendered.missing_resources,
            incomplete_regions=rendered.incomplete_regions,
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
