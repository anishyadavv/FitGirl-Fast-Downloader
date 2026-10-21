"""Scraper module for FitGirl Repacks game pages.

Finds and extracts all FuckingFast download parts (including setup and all .rar parts)
from a given FitGirl repack page.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import List, Optional

from bs4 import BeautifulSoup

FITGIRL_HOST_RE = re.compile(r"^https?://(www\.)?fitgirl-repacks\.site/", re.IGNORECASE)
FF_HREF_RE = re.compile(r"https?://fuckingfast\.co/[A-Za-z0-9]+", re.IGNORECASE)
_PART_RE = re.compile(r"\.part(\d+)\.", re.IGNORECASE)
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class DownloadPart:
    index: int
    name: str
    url: str
    is_main: bool = True
    is_bonus: bool = False
    part_num: int = 0
    size_str: str = ""


@dataclass
class ScrapeResult:
    title: str
    game_name: str
    url: str
    parts: List[DownloadPart] = field(default_factory=list)

    @property
    def main_parts(self) -> List[DownloadPart]:
        return [p for p in self.parts if p.is_main]

    @property
    def bonus_parts(self) -> List[DownloadPart]:
        return [p for p in self.parts if p.is_bonus]


class ScrapeError(Exception):
    pass


def is_fitgirl_url(url: str) -> bool:
    return bool(FITGIRL_HOST_RE.match(url.strip()))


def _clean_title(raw: str) -> str:
    if not raw:
        return "Unknown Game"
    t = re.split(r"\s+[-–—]\s+FitGirl", raw, maxsplit=1)[0].strip()
    return t or "Unknown Game"


def _clean_filename(name: str) -> str:
    cleaned = _INVALID_CHARS.sub("_", name).strip()
    return cleaned or "download.bin"


def _sort_key(part: DownloadPart) -> tuple[int, str, int]:
    # setup.exe first (0), main parts next (1), bonus parts last (2)
    category_order = 0 if "setup" in part.name.lower() else (2 if part.is_bonus else 1)
    base = part.name.lower()
    return (category_order, base, part.part_num)


def scrape_fitgirl_page(page_url: str) -> ScrapeResult:
    """Scrape FitGirl repack page for FuckingFast links.
    
    Tries curl_cffi first for instant fetching, with fallback to Camoufox.
    """
    page_url = page_url.strip()
    if not is_fitgirl_url(page_url):
        raise ScrapeError(f"Invalid FitGirl URL: {page_url}. Must start with https://fitgirl-repacks.site/")

    html_content = ""
    # 1. Try fast curl_cffi fetch
    try:
        from curl_cffi import requests
        resp = requests.get(page_url, impersonate="chrome124", timeout=15)
        if resp.status_code == 200:
            html_content = resp.text
    except Exception:
        html_content = ""

    # 2. Fallback to Camoufox if curl_cffi failed or was blocked
    if not html_content or "fuckingfast.co" not in html_content:
        try:
            from camoufox.sync_api import Camoufox
            with Camoufox(headless=True, humanize=False, geoip=False) as browser:
                page = browser.new_page()
                page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)
                html_content = page.content()
        except Exception as e:
            raise ScrapeError(f"Failed to fetch FitGirl page via browser fallback: {e}") from e

    if not html_content:
        raise ScrapeError("Failed to retrieve HTML content from page.")

    soup = BeautifulSoup(html_content, "html.parser")

    # Title extraction
    h1 = soup.find("h1", class_="entry-title")
    title_raw = h1.get_text(strip=True) if h1 else (soup.title.string if soup.title else "Grand Theft Auto V")
    title = _clean_title(title_raw)

    # Simplified game name (e.g. "Grand Theft Auto V" from "Grand Theft Auto V / GTA 5...")
    game_name = title.split("/")[0].split("-")[0].strip()

    # Find the FuckingFast section specifically
    ff_section = None
    for li in soup.find_all("li"):
        text = li.get_text()
        if "Filehoster: FuckingFast" in text or ("FuckingFast" in text and "REALLY" in text):
            ff_section = li
            break

    # If not found inside an <li>, search for any container near "Filehoster: FuckingFast"
    if not ff_section:
        for p in soup.find_all(["p", "div", "h3"]):
            if "Filehoster: FuckingFast" in p.get_text():
                ff_section = p.find_parent(["li", "div"]) or p
                break

    if not ff_section:
        # Fallback: search all anchors matching fuckingfast.co
        ff_anchors = soup.find_all("a", href=lambda h: h and "fuckingfast.co" in h)
    else:
        ff_anchors = ff_section.find_all("a", href=lambda h: h and "fuckingfast.co" in h)

    if not ff_anchors:
        raise ScrapeError("No FuckingFast links found on this page. Verify that this repack has FuckingFast mirrors.")

    raw_parts: List[DownloadPart] = []
    seen_urls: set[str] = set()

    for idx, a in enumerate(ff_anchors, start=1):
        href = a.get("href", "").strip()
        if not href or href in seen_urls:
            continue

        # Extract filename from URL hash or link text
        frag = href.split("#", 1)[1] if "#" in href else a.get_text(strip=True)
        filename = _clean_filename(frag)
        
        # Don't add generic anchor texts like "Click to show direct links" or "Filehoster: FuckingFast"
        if filename.lower() in ("click to show direct links", "filehoster__fuckingfast", "fuckingfast"):
            continue

        seen_urls.add(href)

        is_bonus = "bonus" in filename.lower() or "optional" in filename.lower()
        part_match = _PART_RE.search(filename)
        part_num = int(part_match.group(1)) if part_match else 0

        raw_parts.append(DownloadPart(
            index=idx,
            name=filename,
            url=href,
            is_main=not is_bonus,
            is_bonus=is_bonus,
            part_num=part_num
        ))

    # Sort parts: setup first, part001 to part105, then bonus parts
    raw_parts.sort(key=_sort_key)

    # Re-index
    for i, p in enumerate(raw_parts, start=1):
        p.index = i

    return ScrapeResult(
        title=title,
        game_name=game_name,
        url=page_url,
        parts=raw_parts
    )
