"""Downloader engine with multi-threading, resuming, speed calculation,
rate-limit backoff, and thread-safe resolver integration for FuckingFast.
"""
from __future__ import annotations
import os
import time
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue, Empty
from typing import Callable, Dict, List, Optional, Any

from resolver import Resolver, ResolveError


STATUS_PENDING = "Pending"
STATUS_RESOLVING = "Resolving"
STATUS_DOWNLOADING = "Downloading"
STATUS_COMPLETED = "Completed"
STATUS_PAUSED = "Paused"
STATUS_FAILED = "Failed"
STATUS_SKIPPED = "Already Exists"


@dataclass
class FileTask:
    index: int
    name: str
    url: str
    status: str = STATUS_PENDING
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_bps: float = 0.0
    error_msg: str = ""
    signed_url: Optional[str] = None
    eta_seconds: Optional[int] = None
    retry_count: int = 0

    @property
    def progress_pct(self) -> float:
        if self.total_bytes <= 0:
            return 100.0 if self.status in (STATUS_COMPLETED, STATUS_SKIPPED) else 0.0
        return min(100.0, (self.downloaded_bytes / self.total_bytes) * 100.0)


class DownloadManager:
    def __init__(
        self,
        dest_dir: str | Path,
        tasks: List[FileTask],
        max_concurrent: int = 2,
        on_task_update: Optional[Callable[[FileTask], None]] = None,
        on_overall_update: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.dest_dir = Path(dest_dir)
        self.tasks = tasks
        self.max_concurrent = max(1, min(6, max_concurrent))
        self.on_task_update = on_task_update
        self.on_overall_update = on_overall_update
        self.on_log = on_log

        self.resolver = Resolver()
        self._queue: Queue[FileTask] = Queue()
        self._workers: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._stats_thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._active_speeds: Dict[int, float] = {}

    def log(self, message: str) -> None:
        if self.on_log:
            self.on_log(message)

    def start(self) -> None:
        """Start downloading queued tasks."""
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._pause_event.clear()

        # Enqueue pending tasks
        pending_count = 0
        for task in self.tasks:
            final_path = self.dest_dir / task.name
            if final_path.exists() and final_path.stat().st_size > 0:
                part_size = final_path.stat().st_size
                task.status = STATUS_SKIPPED
                task.downloaded_bytes = part_size
                task.total_bytes = part_size
                if self.on_task_update:
                    self.on_task_update(task)
            elif task.status != STATUS_COMPLETED:
                task.status = STATUS_PENDING
                self._queue.put(task)
                pending_count += 1
                if self.on_task_update:
                    self.on_task_update(task)

        self.log(f"Starting downloader: {pending_count} parts queued with {self.max_concurrent} concurrent connections.")
        self.log(f"Destination folder: {self.dest_dir.resolve()}")

        # Start worker threads
        for i in range(self.max_concurrent):
            t = threading.Thread(target=self._worker_loop, name=f"DownloadWorker-{i+1}", daemon=True)
            t.start()
            self._workers.append(t)

        # Start statistics ticker thread
        self._stats_thread = threading.Thread(target=self._stats_ticker, name="StatsTicker", daemon=True)
        self._stats_thread.start()

    def pause(self) -> None:
        self.log("Pausing active downloads...")
        self._pause_event.set()
        with self._lock:
            for task in self.tasks:
                if task.status in (STATUS_PENDING, STATUS_DOWNLOADING, STATUS_RESOLVING):
                    task.status = STATUS_PAUSED
                    task.speed_bps = 0.0
                    if self.on_task_update:
                        self.on_task_update(task)

    def resume(self) -> None:
        self.log("Resuming downloads...")
        self._pause_event.clear()
        with self._lock:
            for task in self.tasks:
                if task.status == STATUS_PAUSED:
                    task.status = STATUS_PENDING
                    self._queue.put(task)
                    if self.on_task_update:
                        self.on_task_update(task)

    def stop(self) -> None:
        self.log("Stopping download manager...")
        self._stop_event.set()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except Empty:
                break

        for w in self._workers:
            if w.is_alive():
                w.join(timeout=1.5)
        self._workers.clear()

        try:
            self.resolver.close()
        except Exception:
            pass

        self.log("Download manager stopped.")

    def retry_failed(self) -> None:
        """Re-queue any failed tasks."""
        count = 0
        with self._lock:
            for task in self.tasks:
                if task.status == STATUS_FAILED:
                    task.status = STATUS_PENDING
                    task.error_msg = ""
                    task.signed_url = None
                    self._queue.put(task)
                    count += 1
                    if self.on_task_update:
                        self.on_task_update(task)
        if count > 0:
            self.log(f"Re-queued {count} failed tasks for retry.")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                time.sleep(0.5)
                continue

            try:
                task = self._queue.get(timeout=1.0)
            except Empty:
                continue

            if self._stop_event.is_set():
                self._queue.task_done()
                break

            try:
                self._download_task_with_retries(task)
            except Exception as e:
                task.status = STATUS_FAILED
                task.error_msg = str(e)
                self.log(f"[{task.name}] Failed: {e}")
                if self.on_task_update:
                    self.on_task_update(task)
            finally:
                with self._lock:
                    self._active_speeds.pop(task.index, None)
                self._queue.task_done()

    def _download_task_with_retries(self, task: FileTask) -> None:
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            try:
                self._execute_download(task)
                return  # Success
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait_time = 15 * attempt
                    self.log(f"[{task.name}] Rate limit reached (HTTP 429). Backing off for {wait_time}s...")
                    time.sleep(wait_time)
                elif e.code in (401, 403, 410):
                    self.log(f"[{task.name}] Download token expired ({e.code}). Re-resolving link...")
                    task.signed_url = None
                    time.sleep(2.0)
                else:
                    self.log(f"[{task.name}] HTTP Error {e.code}: {e.reason}. Attempt {attempt}/{max_attempts}...")
                    time.sleep(3.0)
            except Exception as e:
                self.log(f"[{task.name}] Network issue ({type(e).__name__}: {e}). Retrying ({attempt}/{max_attempts})...")
                time.sleep(3.0)

        raise ResolveError(f"Download failed after {max_attempts} attempts.")

    def _execute_download(self, task: FileTask) -> None:
        final_file = self.dest_dir / task.name
        part_file = self.dest_dir / f"{task.name}.part"

        # If already completed on disk
        if final_file.exists() and final_file.stat().st_size > 0:
            task.status = STATUS_COMPLETED
            task.downloaded_bytes = final_file.stat().st_size
            task.total_bytes = task.downloaded_bytes
            task.speed_bps = 0.0
            if self.on_task_update:
                self.on_task_update(task)
            return

        # 1. Resolve signed direct URL if not already cached
        if not task.signed_url:
            task.status = STATUS_RESOLVING
            self.log(f"[{task.name}] Resolving link via FuckingFast...")
            if self.on_task_update:
                self.on_task_update(task)

            signed_url, _ = self.resolver.resolve(task.url, retries=2)
            task.signed_url = signed_url

        if self._stop_event.is_set() or self._pause_event.is_set():
            return

        # 2. Check for partial download to resume
        downloaded = 0
        if part_file.exists():
            downloaded = part_file.stat().st_size
            task.downloaded_bytes = downloaded
            self.log(f"[{task.name}] Resuming from {downloaded / (1024*1024):.1f} MB...")

        # 3. HTTP Range streaming
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/135.0",
            "Referer": "https://fuckingfast.co/",
        }
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"

        req = urllib.request.Request(task.signed_url, headers=headers)
        task.status = STATUS_DOWNLOADING
        if self.on_task_update:
            self.on_task_update(task)

        mode = "ab" if downloaded > 0 else "wb"
        with urllib.request.urlopen(req, timeout=30) as resp, open(part_file, mode) as out_f:
            content_range = resp.headers.get("Content-Range")
            content_length = resp.headers.get("Content-Length")

            if content_range:
                try:
                    total = int(content_range.split("/")[-1])
                    task.total_bytes = total
                except Exception:
                    pass
            elif content_length:
                task.total_bytes = downloaded + int(content_length)

            chunk_size = 512 * 1024  # 512 KB
            speed_window_time = time.time()
            speed_window_bytes = 0

            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    return

                chunk = resp.read(chunk_size)
                if not chunk:
                    break

                out_f.write(chunk)
                chunk_len = len(chunk)
                downloaded += chunk_len
                speed_window_bytes += chunk_len
                task.downloaded_bytes = downloaded

                now = time.time()
                elapsed = now - speed_window_time
                if elapsed >= 0.5:
                    speed = speed_window_bytes / elapsed
                    task.speed_bps = speed
                    with self._lock:
                        self._active_speeds[task.index] = speed

                    rem = max(0, task.total_bytes - task.downloaded_bytes)
                    task.eta_seconds = int(rem / speed) if speed > 0 else 0

                    speed_window_time = now
                    speed_window_bytes = 0

                    if self.on_task_update:
                        self.on_task_update(task)

        if self._stop_event.is_set() or self._pause_event.is_set():
            return

        # 4. Finished part successfully, atomically rename
        if part_file.exists():
            if final_file.exists():
                try:
                    final_file.unlink()
                except Exception:
                    pass
            part_file.rename(final_file)

        task.status = STATUS_COMPLETED
        task.downloaded_bytes = task.total_bytes or final_file.stat().st_size
        task.speed_bps = 0.0
        task.eta_seconds = 0
        with self._lock:
            self._active_speeds.pop(task.index, None)

        self.log(f"[{task.name}] Completed successfully! ({task.downloaded_bytes / (1024*1024):.1f} MB)")
        if self.on_task_update:
            self.on_task_update(task)

    def _stats_ticker(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(0.5)

            with self._lock:
                total_speed = sum(self._active_speeds.values())

            total_bytes = 0
            downloaded_bytes = 0
            completed_count = 0
            active_count = 0

            for t in self.tasks:
                total_bytes += t.total_bytes
                downloaded_bytes += t.downloaded_bytes
                if t.status in (STATUS_COMPLETED, STATUS_SKIPPED):
                    completed_count += 1
                elif t.status == STATUS_DOWNLOADING:
                    active_count += 1

            total_tasks = len(self.tasks)
            overall_pct = (downloaded_bytes / total_bytes * 100.0) if total_bytes > 0 else (
                (completed_count / total_tasks * 100.0) if total_tasks > 0 else 0.0
            )

            rem_bytes = max(0, total_bytes - downloaded_bytes)
            eta_seconds = int(rem_bytes / total_speed) if total_speed > 1024 else 0

            stats = {
                "total_tasks": total_tasks,
                "completed_tasks": completed_count,
                "active_tasks": active_count,
                "total_bytes": total_bytes,
                "downloaded_bytes": downloaded_bytes,
                "overall_speed_bps": total_speed,
                "overall_pct": overall_pct,
                "eta_seconds": eta_seconds,
            }

            if self.on_overall_update:
                self.on_overall_update(stats)
