"""Resolve fuckingfast.co URLs to signed direct download URLs via Camoufox.

Uses a DedicatedResolver thread so that the Playwright/Camoufox browser instance
always runs on its own thread, avoiding any cross-thread greenlet switching crashes.
"""
from __future__ import annotations
import queue
import re
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from camoufox.sync_api import Camoufox

LINK_RE = re.compile(r"^https?://fuckingfast\.co/[a-z0-9]+", re.IGNORECASE)
_PART_RE = re.compile(r"\.part(\d+)\.", re.IGNORECASE)
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_AD_KEYWORDS = (
    "c8yq5.sbs",
    "doubleclick.net",
    "googlesyndication",
    "googletagmanager",
    ".sbs/?tag=",
    "adservice",
    "adnxs",
    "challenges.cloudflare.com/cdn-cgi/challenge-platform",
)

_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
RECYCLE_AFTER = 25


class ResolveError(Exception):
    pass


def sanitize_filename(name: str) -> str:
    cleaned = _INVALID_CHARS.sub("_", name).strip()
    return cleaned or "download.bin"


def filename_from_fragment(url: str) -> Optional[str]:
    if "#" in url:
        frag = url.split("#", 1)[1]
        if frag:
            return sanitize_filename(frag)
    return None


def _is_invalid_title(title: str) -> bool:
    t = title.strip().lower()
    return not t or any(
        bad in t
        for bad in (
            "just a moment",
            "cloudflare",
            "fuckingfast",
            "fucking fast",
            "ddos-guard",
            "attention required",
            "verify you are human",
            "turnstile",
        )
    )


class _BrowserSession:
    """Internal browser session managed solely on the DedicatedResolverThread."""

    def __init__(self) -> None:
        self._cm: Optional[Camoufox] = None
        self._browser = None
        self._resolves_count = 0

    def _ensure(self) -> None:
        if self._browser is not None and self._resolves_count >= RECYCLE_AFTER:
            self.hard_reset()
        if self._browser is not None:
            return

        self._resolves_count = 0
        self._cm = Camoufox(headless=True, humanize=False, geoip=False)
        self._browser = self._cm.__enter__()

    def hard_reset(self) -> None:
        try:
            if self._cm is not None:
                self._cm.__exit__(None, None, None)
        except Exception:
            pass
        self._cm = None
        self._browser = None
        self._resolves_count = 0

    def resolve_once(self, url: str) -> Tuple[str, str]:
        self._ensure()
        assert self._browser is not None
        self._resolves_count += 1

        page = self._browser.new_page()

        def _route(route):
            try:
                req = route.request
                if req.resource_type in _BLOCKED_RESOURCE_TYPES:
                    route.abort()
                    return
                if any(k in req.url for k in _AD_KEYWORDS):
                    route.abort()
                    return
                route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        page.route("**/*", _route)

        captured: dict[str, Optional[str]] = {"signed_url": None}

        def on_response(resp):
            try:
                if "/f/" in resp.url and resp.url.rstrip("/").endswith("/go"):
                    hx = resp.headers.get("hx-redirect") or resp.headers.get("HX-Redirect")
                    if hx:
                        captured["signed_url"] = hx
            except Exception:
                pass

        page.on("response", on_response)

        try:
            # Block popup windows
            page.add_init_script("window.open = function(){ return null; };")

            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2000)

            # Wait for the download button
            try:
                page.wait_for_selector("a.link-button", state="visible", timeout=25_000)
            except Exception:
                pass

            title = ""
            try:
                title = page.title() or ""
            except Exception:
                pass

            # First click (often triggers ad scripts / initial setup)
            try:
                page.click("a.link-button", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(1000)

            # Second click (triggers the HTMX POST to /f/<id>/go)
            try:
                page.click("a.link-button", timeout=15_000)
            except Exception:
                pass

            # Wait for captured signed_url
            deadline = time.time() + 20
            while captured["signed_url"] is None and time.time() < deadline:
                page.wait_for_timeout(200)

            # If still not captured, try one more click
            if not captured["signed_url"]:
                try:
                    page.click("a.link-button", timeout=5_000)
                except Exception:
                    pass
                deadline = time.time() + 10
                while captured["signed_url"] is None and time.time() < deadline:
                    page.wait_for_timeout(200)

            if not captured["signed_url"]:
                raise ResolveError(f"No hx-redirect download header received for {url}")

            # Pick filename: prefer fragment from FitGirl, then page title, then url hash
            filename = None
            frag = filename_from_fragment(url)
            if frag:
                filename = frag
            elif title and not _is_invalid_title(title):
                filename = sanitize_filename(title.strip())
            else:
                m = re.search(r"/([a-z0-9]+)$", url.split("#", 1)[0])
                filename = f"{m.group(1) if m else 'download'}.rar"

            return captured["signed_url"], filename

        finally:
            try:
                page.close()
            except Exception:
                pass


class Resolver:
    """Thread-safe Resolver managing a single dedicated thread for Camoufox.
    
    Any number of worker threads can call `resolve(url)` concurrently; requests
    are processed sequentially by the internal worker thread without cross-thread
    greenlet errors.
    """

    def __init__(self) -> None:
        self._in_q: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(
            target=self._resolver_thread_loop,
            name="DedicatedResolverWorker",
            daemon=True,
        )
        self._worker_thread.start()

    def _resolver_thread_loop(self) -> None:
        session = _BrowserSession()
        try:
            while not self._stop_event.is_set():
                try:
                    item = self._in_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                if item is None:
                    break

                url, retries, out_q = item
                last_err = None
                success = False

                for attempt in range(retries + 1):
                    if self._stop_event.is_set():
                        break
                    try:
                        signed_url, filename = session.resolve_once(url)
                        out_q.put((signed_url, filename, None))
                        success = True
                        # Brief pacing delay to prevent rapid-fire requests
                        time.sleep(0.5)
                        break
                    except Exception as e:
                        last_err = e
                        session.hard_reset()
                        time.sleep(2.0)

                if not success:
                    err = ResolveError(f"Resolve failed after retries: {last_err}")
                    out_q.put((None, None, err))

                self._in_q.task_done()
        finally:
            session.hard_reset()

    def resolve(self, url: str, retries: int = 2, timeout: float = 90.0) -> Tuple[str, str]:
        """Thread-safe resolve callable from any worker thread."""
        if self._stop_event.is_set():
            raise ResolveError("Resolver has been closed.")

        out_q: queue.Queue = queue.Queue()
        self._in_q.put((url, retries, out_q))

        try:
            signed_url, filename, err = out_q.get(timeout=timeout)
        except queue.Empty:
            raise ResolveError(f"Resolve timed out after {timeout} seconds for {url}")

        if err:
            raise err

        return signed_url, filename

    def close(self) -> None:
        """Stop resolver thread and close browser."""
        self._stop_event.set()
        self._in_q.put(None)
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)
