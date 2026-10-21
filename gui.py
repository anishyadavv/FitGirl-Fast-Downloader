"""Modern CustomTkinter GUI for FitGirl GTA V / FuckingFast Downloader.
"""
from __future__ import annotations
import os
import shutil
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import customtkinter as ctk

from scraper import scrape_fitgirl_page, ScrapeResult, DownloadPart
from downloader import (
    DownloadManager,
    FileTask,
    STATUS_PENDING,
    STATUS_RESOLVING,
    STATUS_DOWNLOADING,
    STATUS_COMPLETED,
    STATUS_PAUSED,
    STATUS_FAILED,
    STATUS_SKIPPED,
)

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


def format_bytes(num_bytes: float) -> str:
    if num_bytes <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def format_time(seconds: int) -> str:
    if seconds <= 0:
        return "--:--:--"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def get_free_space(path_str: str) -> str:
    try:
        p = Path(path_str)
        if not p.exists():
            p = p.parent
        total, used, free = shutil.disk_usage(p)
        drive_letter = p.anchor or "Drive"
        return f"Free: {format_bytes(free)} on {drive_letter}"
    except Exception:
        return ""


class FitGirlDownloaderApp(ctk.CTk):
    def __init__(self, initial_url: str = "https://fitgirl-repacks.site/grand-theft-auto-v/", initial_dest: str = ""):
        super().__init__()

        self.title("FitGirl Repacks - GTA V Fast Downloader (FuckingFast)")
        self.geometry("1080x840")
        self.minsize(920, 680)

        # State
        self.scrape_result: ScrapeResult | None = None
        self.tasks: list[FileTask] = []
        self.download_manager: DownloadManager | None = None
        self.is_downloading = False
        self.is_paused = False

        default_folder = initial_dest or str(Path.home() / "Downloads" / "GTA_V_FitGirl")
        self.dest_folder_var = ctk.StringVar(value=default_folder)
        self.url_var = ctk.StringVar(value=initial_url)
        self.free_space_var = ctk.StringVar(value=get_free_space(default_folder))
        
        self.include_main_var = ctk.BooleanVar(value=True)
        self.include_bonus_var = ctk.BooleanVar(value=False)
        self.concurrent_var = ctk.IntVar(value=2)  # 2 is safest for FuckingFast rate limits

        self.tree_item_map: dict[int, str] = {}  # task.index -> tree iid

        self._build_ui()
        self.after(300, self.fetch_links)

    def _build_ui(self):
        # Header banner
        header_frame = ctk.CTkFrame(self, corner_radius=10, fg_color=("#1f2937", "#111827"))
        header_frame.pack(fill="x", padx=16, pady=(12, 6))

        title_lbl = ctk.CTkLabel(
            header_frame,
            text="GTA V FitGirl Repack Downloader",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=("#38bdf8", "#0ea5e9"),
        )
        title_lbl.pack(anchor="w", padx=16, pady=(10, 2))

        subtitle_lbl = ctk.CTkLabel(
            header_frame,
            text="High-speed multi-part downloader with automatic Cloudflare bypass and HTTP resume support",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        )
        subtitle_lbl.pack(anchor="w", padx=16, pady=(0, 10))

        # Inputs section
        input_frame = ctk.CTkFrame(self, corner_radius=10)
        input_frame.pack(fill="x", padx=16, pady=4)

        # URL row
        url_lbl = ctk.CTkLabel(input_frame, text="FitGirl URL:", width=95, anchor="w", font=ctk.CTkFont(weight="bold"))
        url_lbl.grid(row=0, column=0, padx=(16, 6), pady=(12, 6), sticky="w")

        self.url_entry = ctk.CTkEntry(input_frame, textvariable=self.url_var, height=32)
        self.url_entry.grid(row=0, column=1, padx=6, pady=(12, 6), sticky="ew")

        self.fetch_btn = ctk.CTkButton(
            input_frame,
            text="Fetch Links",
            width=120,
            height=32,
            command=self.fetch_links,
            fg_color="#0284c7",
            hover_color="#0369a1",
        )
        self.fetch_btn.grid(row=0, column=2, padx=(6, 16), pady=(12, 6))

        # Save Location row
        dest_lbl = ctk.CTkLabel(input_frame, text="Save Location:", width=95, anchor="w", font=ctk.CTkFont(weight="bold"))
        dest_lbl.grid(row=1, column=0, padx=(16, 6), pady=(6, 12), sticky="w")

        self.dest_entry = ctk.CTkEntry(input_frame, textvariable=self.dest_folder_var, height=32)
        self.dest_entry.grid(row=1, column=1, padx=6, pady=(6, 12), sticky="ew")

        dest_btn_frame = ctk.CTkFrame(input_frame, fg_color="transparent")
        dest_btn_frame.grid(row=1, column=2, padx=(6, 16), pady=(6, 12), sticky="e")

        self.browse_btn = ctk.CTkButton(
            dest_btn_frame,
            text="Browse...",
            width=80,
            height=32,
            command=self.browse_destination,
        )
        self.browse_btn.pack(side="left", padx=(0, 6))

        self.free_lbl = ctk.CTkLabel(
            dest_btn_frame,
            textvariable=self.free_space_var,
            font=ctk.CTkFont(size=11),
            text_color="#10b981",
        )
        self.free_lbl.pack(side="left")

        input_frame.grid_columnconfigure(1, weight=1)

        # Options & Controls bar
        opts_frame = ctk.CTkFrame(self, corner_radius=10)
        opts_frame.pack(fill="x", padx=16, pady=4)

        self.cb_main = ctk.CTkCheckBox(
            opts_frame,
            text="Main Game (Setup + 105 Parts)",
            variable=self.include_main_var,
            command=self._apply_filter,
            font=ctk.CTkFont(weight="bold"),
        )
        self.cb_main.pack(side="left", padx=(16, 12), pady=10)

        self.cb_bonus = ctk.CTkCheckBox(
            opts_frame,
            text="Optional Bonus Content (6 Parts)",
            variable=self.include_bonus_var,
            command=self._apply_filter,
        )
        self.cb_bonus.pack(side="left", padx=12, pady=10)

        conc_lbl = ctk.CTkLabel(opts_frame, text="Concurrent:")
        conc_lbl.pack(side="left", padx=(20, 6), pady=10)

        self.conc_menu = ctk.CTkOptionMenu(
            opts_frame,
            values=["1", "2", "3", "4"],
            variable=ctk.StringVar(value=str(self.concurrent_var.get())),
            command=lambda v: self.concurrent_var.set(int(v)),
            width=65,
            height=28,
        )
        self.conc_menu.pack(side="left", padx=6, pady=10)

        self.open_folder_btn = ctk.CTkButton(
            opts_frame,
            text="Open Folder",
            width=100,
            height=28,
            fg_color="#374151",
            hover_color="#4b5563",
            command=self.open_dest_folder,
        )
        self.open_folder_btn.pack(side="right", padx=(6, 16), pady=10)

        self.retry_failed_btn = ctk.CTkButton(
            opts_frame,
            text="Retry Failed",
            width=100,
            height=28,
            fg_color="#d97706",
            hover_color="#b45309",
            command=self.retry_failed_downloads,
        )
        self.retry_failed_btn.pack(side="right", padx=6, pady=10)

        # Overall Status Dashboard
        dash_frame = ctk.CTkFrame(self, corner_radius=10, fg_color=("#1e293b", "#0f172a"))
        dash_frame.pack(fill="x", padx=16, pady=4)

        self.overall_progress_bar = ctk.CTkProgressBar(dash_frame, height=14)
        self.overall_progress_bar.pack(fill="x", padx=16, pady=(12, 8))
        self.overall_progress_bar.set(0.0)

        dash_grid = ctk.CTkFrame(dash_frame, fg_color="transparent")
        dash_grid.pack(fill="x", padx=16, pady=(0, 12))

        self.stat_parts_lbl = ctk.CTkLabel(dash_grid, text="Parts: 0 / 0 Completed", font=ctk.CTkFont(size=13, weight="bold"))
        self.stat_parts_lbl.grid(row=0, column=0, sticky="w", padx=(0, 20))

        self.stat_bytes_lbl = ctk.CTkLabel(dash_grid, text="Downloaded: 0 B", font=ctk.CTkFont(size=13))
        self.stat_bytes_lbl.grid(row=0, column=1, sticky="w", padx=(0, 20))

        self.stat_speed_lbl = ctk.CTkLabel(dash_grid, text="Speed: 0.0 MB/s", font=ctk.CTkFont(size=13, weight="bold"), text_color="#38bdf8")
        self.stat_speed_lbl.grid(row=0, column=2, sticky="w", padx=(0, 20))

        self.stat_eta_lbl = ctk.CTkLabel(dash_grid, text="ETA: --:--:--", font=ctk.CTkFont(size=13))
        self.stat_eta_lbl.grid(row=0, column=3, sticky="w")

        dash_grid.grid_columnconfigure(0, weight=1)
        dash_grid.grid_columnconfigure(1, weight=1)
        dash_grid.grid_columnconfigure(2, weight=1)
        dash_grid.grid_columnconfigure(3, weight=1)

        # Parts Table
        table_frame = ctk.CTkFrame(self, corner_radius=10)
        table_frame.pack(fill="both", expand=True, padx=16, pady=4)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background="#1e1e1e",
            foreground="#e5e7eb",
            fieldbackground="#1e1e1e",
            rowheight=26,
            font=("Segoe UI", 10),
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background="#2d3748",
            foreground="#f9fafb",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
        )
        style.map("Treeview", background=[("selected", "#0284c7")])

        columns = ("index", "name", "size", "progress", "speed", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")

        self.tree.heading("index", text="#")
        self.tree.heading("name", text="File Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("progress", text="Progress")
        self.tree.heading("speed", text="Speed")
        self.tree.heading("status", text="Status")

        self.tree.column("index", width=45, anchor="center")
        self.tree.column("name", width=430, anchor="w")
        self.tree.column("size", width=100, anchor="e")
        self.tree.column("progress", width=100, anchor="center")
        self.tree.column("speed", width=110, anchor="e")
        self.tree.column("status", width=150, anchor="center")

        tree_scroll_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll_y.set)

        self.tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        tree_scroll_y.pack(side="right", fill="y", padx=(0, 4), pady=4)

        # Action Buttons row
        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.pack(fill="x", padx=16, pady=(8, 12))

        self.start_btn = ctk.CTkButton(
            action_frame,
            text="START DOWNLOAD",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=40,
            width=180,
            fg_color="#10b981",
            hover_color="#059669",
            command=self.start_download,
        )
        self.start_btn.pack(side="left", padx=(0, 10))

        self.pause_btn = ctk.CTkButton(
            action_frame,
            text="Pause",
            font=ctk.CTkFont(size=13),
            height=40,
            width=110,
            fg_color="#f59e0b",
            hover_color="#d97706",
            state="disabled",
            command=self.toggle_pause,
        )
        self.pause_btn.pack(side="left", padx=6)

        self.stop_btn = ctk.CTkButton(
            action_frame,
            text="Stop / Cancel",
            font=ctk.CTkFont(size=13),
            height=40,
            width=120,
            fg_color="#ef4444",
            hover_color="#dc2626",
            state="disabled",
            command=self.stop_download,
        )
        self.stop_btn.pack(side="left", padx=6)

        self.status_msg_lbl = ctk.CTkLabel(
            action_frame,
            text="Ready. Click 'START DOWNLOAD' to begin.",
            font=ctk.CTkFont(size=12),
            text_color="gray",
        )
        self.status_msg_lbl.pack(side="left", padx=16)

    def browse_destination(self):
        selected = filedialog.askdirectory(
            initialdir=self.dest_folder_var.get() or "D:\\",
            title="Select Folder to Save Downloaded Game Files",
        )
        if selected:
            self.dest_folder_var.set(selected)
            self.free_space_var.set(get_free_space(selected))
            self._apply_filter()

    def open_dest_folder(self):
        folder = self.dest_folder_var.get()
        if folder:
            p = Path(folder)
            p.mkdir(parents=True, exist_ok=True)
            try:
                os.startfile(folder)
            except Exception as e:
                messagebox.showerror("Error", f"Could not open folder: {e}")

    def fetch_links(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Input Required", "Please enter a valid FitGirl Repack URL.")
            return

        self.fetch_btn.configure(state="disabled", text="Fetching...")
        self.status_msg_lbl.configure(text="Scraping FitGirl page for FuckingFast parts...")

        def _bg_fetch():
            try:
                res = scrape_fitgirl_page(url)
                self.after(0, lambda: self._on_fetch_success(res))
            except Exception as e:
                self.after(0, lambda: self._on_fetch_error(str(e)))

        threading.Thread(target=_bg_fetch, daemon=True).start()

    def _on_fetch_success(self, res: ScrapeResult):
        self.scrape_result = res
        self.fetch_btn.configure(state="normal", text="Fetch Links")
        self.status_msg_lbl.configure(text=f"Loaded {len(res.parts)} parts for: {res.game_name}")
        self._apply_filter()

    def _on_fetch_error(self, err_msg: str):
        self.fetch_btn.configure(state="normal", text="Fetch Links")
        self.status_msg_lbl.configure(text=f"Failed to fetch links: {err_msg}")
        messagebox.showerror("Scrape Error", f"Failed to retrieve links from page:\n\n{err_msg}")

    def _apply_filter(self):
        if not self.scrape_result:
            return

        filtered_parts: list[DownloadPart] = []
        if self.include_main_var.get():
            filtered_parts.extend(self.scrape_result.main_parts)
        if self.include_bonus_var.get():
            filtered_parts.extend(self.scrape_result.bonus_parts)

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.tree_item_map.clear()

        dest_dir = Path(self.dest_folder_var.get())
        self.tasks = []

        for p in filtered_parts:
            task = FileTask(index=p.index, name=p.name, url=p.url)
            final_file = dest_dir / p.name
            if final_file.exists() and final_file.stat().st_size > 0:
                task.status = STATUS_SKIPPED
                task.downloaded_bytes = final_file.stat().st_size
                task.total_bytes = task.downloaded_bytes

            self.tasks.append(task)
            size_disp = format_bytes(task.downloaded_bytes) if task.downloaded_bytes > 0 else "-"
            prog_disp = "100%" if task.status == STATUS_SKIPPED else "0%"
            
            iid = self.tree.insert(
                "",
                "end",
                values=(task.index, task.name, size_disp, prog_disp, "-", task.status),
            )
            self.tree_item_map[task.index] = iid

        completed = sum(1 for t in self.tasks if t.status == STATUS_SKIPPED)
        self.stat_parts_lbl.configure(text=f"Parts: {completed} / {len(self.tasks)} Completed")

    def start_download(self):
        if not self.tasks:
            messagebox.showwarning("No Tasks", "No download parts loaded. Please click 'Fetch Links' first.")
            return

        dest_dir = self.dest_folder_var.get().strip()
        if not dest_dir:
            messagebox.showwarning("Folder Required", "Please select a destination folder to save the files.")
            return

        try:
            total, used, free = shutil.disk_usage(Path(dest_dir).anchor or "C:\\")
            needed_bytes = len(self.tasks) * 500 * 1024 * 1024
            if free < needed_bytes:
                ans = messagebox.askyesno(
                    "Low Disk Space",
                    f"The selected drive only has {format_bytes(free)} free, but the repack may need ~{format_bytes(needed_bytes)}.\n\nDo you want to continue anyway?",
                )
                if not ans:
                    return
        except Exception:
            pass

        self.is_downloading = True
        self.is_paused = False
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="Pause", fg_color="#f59e0b")
        self.stop_btn.configure(state="normal")
        self.fetch_btn.configure(state="disabled")
        self.dest_entry.configure(state="disabled")
        self.browse_btn.configure(state="disabled")

        self.download_manager = DownloadManager(
            dest_dir=dest_dir,
            tasks=self.tasks,
            max_concurrent=self.concurrent_var.get(),
            on_task_update=self._on_task_update,
            on_overall_update=self._on_overall_update,
            on_log=self._on_log,
        )

        threading.Thread(target=self.download_manager.start, daemon=True).start()
        self.status_msg_lbl.configure(text="Downloads in progress...")

    def toggle_pause(self):
        if not self.download_manager:
            return
        if not self.is_paused:
            self.download_manager.pause()
            self.is_paused = True
            self.pause_btn.configure(text="Resume", fg_color="#10b981", hover_color="#059669")
            self.status_msg_lbl.configure(text="Downloads paused.")
        else:
            self.download_manager.resume()
            self.is_paused = False
            self.pause_btn.configure(text="Pause", fg_color="#f59e0b", hover_color="#d97706")
            self.status_msg_lbl.configure(text="Downloads resumed.")

    def stop_download(self):
        if self.download_manager:
            self.download_manager.stop()
            self.download_manager = None

        self.is_downloading = False
        self.is_paused = False
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="Pause", fg_color="#f59e0b")
        self.stop_btn.configure(state="disabled")
        self.fetch_btn.configure(state="normal")
        self.dest_entry.configure(state="normal")
        self.browse_btn.configure(state="normal")
        self.status_msg_lbl.configure(text="Downloads stopped.")

    def retry_failed_downloads(self):
        if self.download_manager:
            self.download_manager.retry_failed()
        else:
            # Re-queue failed tasks and start
            failed = [t for t in self.tasks if t.status == STATUS_FAILED]
            if not failed:
                messagebox.showinfo("No Failures", "There are currently no failed tasks to retry.")
                return
            for t in failed:
                t.status = STATUS_PENDING
                t.error_msg = ""
                t.signed_url = None
                self._update_task_row(t)
            self.start_download()

    def _on_task_update(self, task: FileTask):
        self.after(0, lambda: self._update_task_row(task))

    def _update_task_row(self, task: FileTask):
        iid = self.tree_item_map.get(task.index)
        if not iid or not self.tree.exists(iid):
            return

        size_disp = format_bytes(task.total_bytes or task.downloaded_bytes)
        prog_disp = f"{task.progress_pct:.1f}%"
        speed_disp = f"{format_bytes(task.speed_bps)}/s" if task.speed_bps > 0 else "-"
        status_disp = task.status
        if task.status == STATUS_DOWNLOADING and task.eta_seconds:
            status_disp = f"DL ({format_time(task.eta_seconds)})"
        elif task.status == STATUS_FAILED and task.error_msg:
            status_disp = f"Failed ({task.error_msg[:20]})"

        self.tree.item(
            iid,
            values=(task.index, task.name, size_disp, prog_disp, speed_disp, status_disp),
        )

    def _on_overall_update(self, stats: dict):
        self.after(0, lambda: self._update_overall_dashboard(stats))

    def _update_overall_dashboard(self, stats: dict):
        total_tasks = stats["total_tasks"]
        completed = stats["completed_tasks"]
        downloaded = stats["downloaded_bytes"]
        total_bytes = stats["total_bytes"]
        speed = stats["overall_speed_bps"]
        pct = stats["overall_pct"]
        eta = stats["eta_seconds"]

        self.overall_progress_bar.set(pct / 100.0)
        self.stat_parts_lbl.configure(text=f"Parts: {completed} / {total_tasks} Completed")
        
        if total_bytes > 0:
            self.stat_bytes_lbl.configure(text=f"Downloaded: {format_bytes(downloaded)} / {format_bytes(total_bytes)} ({pct:.1f}%)")
        else:
            self.stat_bytes_lbl.configure(text=f"Downloaded: {format_bytes(downloaded)}")

        self.stat_speed_lbl.configure(text=f"Speed: {format_bytes(speed)}/s")
        self.stat_eta_lbl.configure(text=f"ETA: {format_time(eta)}")

        if completed >= total_tasks and total_tasks > 0:
            self.status_msg_lbl.configure(text="All files downloaded successfully!")
            self.stop_download()
            messagebox.showinfo(
                "Download Completed!",
                f"All {total_tasks} parts have been successfully downloaded to:\n{self.dest_folder_var.get()}\n\nYou can now run setup_proper.exe to install the game!",
            )

    def _on_log(self, msg: str):
        if len(msg) < 80:
            self.after(0, lambda: self.status_msg_lbl.configure(text=msg))


def run_gui(initial_url: str = "https://fitgirl-repacks.site/grand-theft-auto-v/", initial_dest: str = ""):
    app = FitGirlDownloaderApp(initial_url=initial_url, initial_dest=initial_dest)
    app.mainloop()


if __name__ == "__main__":
    run_gui()
