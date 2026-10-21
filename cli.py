"""Command Line Interface (CLI) for FitGirl GTA V / FuckingFast Downloader.
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

from scraper import scrape_fitgirl_page
from downloader import DownloadManager, FileTask, STATUS_COMPLETED, STATUS_SKIPPED, STATUS_FAILED


def format_bytes(b: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024.0:
            return f"{b:.1f} {unit}"
        b /= 1024.0
    return f"{b:.1f} PB"


def format_time(seconds: int) -> str:
    if seconds <= 0:
        return "--:--:--"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def run_cli(
    url: str = "https://fitgirl-repacks.site/grand-theft-auto-v/",
    dest: str = "",
    include_bonus: bool = False,
    concurrency: int = 3,
):
    print("=" * 70)
    print("   FitGirl Repacks - GTA V Fast Downloader (FuckingFast)")
    print("=" * 70)

    if not dest:
        default_dir = str(Path.home() / "Downloads" / "GTA_V_FitGirl")
        entered = input(f"Enter destination folder to save files [{default_dir}]: ").strip()
        dest = entered if entered else default_dir

    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)
    print(f"\n[+] Saving files to: {dest_path.resolve()}")
    print(f"[+] Scraping parts from: {url}")

    try:
        res = scrape_fitgirl_page(url)
    except Exception as e:
        print(f"[-] Scrape Error: {e}")
        sys.exit(1)

    print(f"[+] Game Title: {res.title}")
    parts = res.parts if include_bonus else res.main_parts
    print(f"[+] Loaded {len(parts)} parts ({'including bonus' if include_bonus else 'main game only'}).")

    tasks = [FileTask(index=p.index, name=p.name, url=p.url) for p in parts]

    def _on_log(msg: str):
        print(f"  * {msg}")

    last_print = 0

    def _on_overall(stats: dict):
        nonlocal last_print
        now = time.time()
        if now - last_print < 1.0:
            return
        last_print = now

        pct = stats["overall_pct"]
        completed = stats["completed_tasks"]
        total = stats["total_tasks"]
        spd = format_bytes(stats["overall_speed_bps"])
        dled = format_bytes(stats["downloaded_bytes"])
        tot = format_bytes(stats["total_bytes"])
        eta = format_time(stats["eta_seconds"])

        sys.stdout.write(
            f"\r[Progress: {pct:5.1f}%] Parts: {completed}/{total} | DL: {dled}/{tot} | Speed: {spd}/s | ETA: {eta}  "
        )
        sys.stdout.flush()

    manager = DownloadManager(
        dest_dir=dest_path,
        tasks=tasks,
        max_concurrent=concurrency,
        on_overall_update=_on_overall,
        on_log=_on_log,
    )

    print("\n[+] Starting downloads (Press Ctrl+C to stop)...\n")
    try:
        manager.start()
        # Keep main thread alive until finished
        while True:
            time.sleep(1.0)
            completed_count = sum(1 for t in tasks if t.status in (STATUS_COMPLETED, STATUS_SKIPPED))
            if completed_count >= len(tasks):
                break
    except KeyboardInterrupt:
        print("\n\n[!] Stopping downloads gracefully...")
        manager.stop()
        print("[+] Download stopped. Partial files have been preserved for resuming.")
        sys.exit(0)

    print(f"\n\n[SUCCESS] All {len(tasks)} parts have been downloaded to: {dest_path.resolve()}!")
    print("You can now run setup_proper.exe to install the game.\n")
