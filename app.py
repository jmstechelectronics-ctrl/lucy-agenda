#!/usr/bin/env python3
"""Small desktop widget for TickTick's official Open API."""

from __future__ import annotations

import ast
import calendar
import webbrowser
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, date, time, timedelta
from pathlib import Path
from time import monotonic
from tkinter import messagebox, simpledialog
from urllib.parse import quote

import requests


APP_ID = "lucy-ticktick-widget"
VERSION = "1.0.0"
API = "https://api.ticktick.com/open/v1"
CONFIG_DIR = Path.home() / ".config" / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"
NOTIFICATION_FILE = CONFIG_DIR / "calendar-notifications.json"
TASK_CACHE_FILE = CONFIG_DIR / "ticktick-active-tasks.json"
REFRESH_MS = 5 * 60 * 1000
RETRY_MS = 15 * 1000
CALENDAR_REMINDER_MINUTES = 10
PULL_REFRESH_THRESHOLD = 140.0


def load_config() -> dict:
    defaults = {"version": 4, "x": 20, "y": 90, "width": 330, "height": 400,
                "opacity": 0.86, "upcoming_days": 14, "always_on_top": False,
                "calendar_notifications": True, "view_mode": "all",
                "hidden_calendar_ids": []}
    try:
        saved = json.loads(CONFIG_FILE.read_text())
        if saved.get("version", 1) < 3:
            saved.update({"version": 3, "width": 330, "height": 400, "opacity": 0.86})
        defaults.update(saved)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return defaults


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    os.chmod(CONFIG_FILE, 0o600)


def load_notification_history() -> dict[str, str]:
    try:
        saved = json.loads(NOTIFICATION_FILE.read_text())
        return saved if isinstance(saved, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_notification_history(history: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    NOTIFICATION_FILE.write_text(json.dumps(history, indent=2) + "\n")
    os.chmod(NOTIFICATION_FILE, 0o600)


def load_task_cache() -> dict[str, dict]:
    try:
        saved = json.loads(TASK_CACHE_FILE.read_text())
        return saved if isinstance(saved, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_task_cache(tasks: dict[str, dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    safe_tasks = {
        key: {field: value for field, value in task.items() if field != "_due"}
        for key, task in tasks.items()
    }
    payload = json.dumps(safe_tasks, indent=2) + "\n"
    try:
        if TASK_CACHE_FILE.read_text() == payload:
            return
    except OSError:
        pass
    TASK_CACHE_FILE.write_text(payload)
    os.chmod(TASK_CACHE_FILE, 0o600)


def parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo:
                parsed = parsed.astimezone()
            return parsed
        except ValueError:
            continue
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo:
                parsed = parsed.astimezone()
            return parsed
        except ValueError:
            continue
    return None


class TickTickAPI:
    def __init__(self) -> None:
        lookup = subprocess.run(
            ["secret-tool", "lookup", "application", APP_ID, "account", "access-token"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.token = lookup.stdout.strip() if lookup.returncode == 0 else None
        if not self.token:
            raise RuntimeError("TickTick authorization has not been completed")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.token}", "User-Agent": APP_ID})
        self.task_cache = load_task_cache()

    def _request(self, method: str, path: str, payload: dict | None = None):
        response = self.session.request(method, API + path, json=payload, timeout=20)
        if response.status_code == 401:
            raise RuntimeError("TickTick authorization expired. Run TickTick Widget Setup again.")
        response.raise_for_status()
        return response.json() if response.content else None

    def tasks(self) -> list[dict]:
        projects = self._request("GET", "/project") or []
        all_tasks: list[dict] = []
        active_by_key: dict[str, dict] = {}
        for project in projects:
            project_id = project.get("id")
            if not project_id or project.get("closed"):
                continue
            data = self._request("GET", f"/project/{project_id}/data") or {}
            for task in data.get("tasks", []):
                if task.get("status", 0) == 0:
                    task["_project_name"] = project.get("name", "")
                    task["_project_id"] = project_id
                    all_tasks.append(task)
                    active_by_key[f"{project_id}|{task.get('id', '')}"] = task

        # The project feed can occasionally omit an active task. Preserve every
        # previously seen task until its individual record explicitly confirms
        # completion or abandonment.
        for key, cached in self.task_cache.items():
            if key in active_by_key:
                continue
            project_id = cached.get("projectId") or cached.get("_project_id")
            task_id = cached.get("id")
            if not project_id or not task_id:
                continue
            try:
                current = self._request("GET", f"/project/{project_id}/task/{task_id}") or {}
            except requests.RequestException:
                current = {}
            status = current.get("status")
            if status == 0:
                current["_project_name"] = cached.get("_project_name", "")
                current["_project_id"] = project_id
                active_by_key[key] = current
                all_tasks.append(current)
            elif status not in (-1, 2):
                # A temporary lookup failure is not evidence of completion.
                active_by_key[key] = cached
                all_tasks.append(cached)

        self.task_cache = active_by_key
        save_task_cache(self.task_cache)
        return all_tasks

    def complete(self, task: dict) -> None:
        project_id = task.get("projectId") or task.get("_project_id")
        task_id = task.get("id")
        if not project_id or not task_id:
            raise RuntimeError("TickTick did not supply the task identifiers")
        self._request("POST", f"/project/{project_id}/task/{task_id}/complete")
        self.task_cache.pop(f"{project_id}|{task_id}", None)
        save_task_cache(self.task_cache)

    def projects(self) -> list[dict]:
        return [project for project in (self._request("GET", "/project") or [])
                if project.get("id") and not project.get("closed")]

    def create(self, project_id: str, title: str, due: datetime | None = None) -> None:
        payload = {"title": title, "projectId": project_id}
        if due:
            payload["dueDate"] = due.strftime("%Y-%m-%dT%H:%M:%S%z")
        self._request("POST", f"/project/{project_id}/task", payload)

    def update(self, task: dict, changes: dict) -> None:
        project_id = task.get("projectId") or task.get("_project_id")
        task_id = task.get("id")
        if not project_id or not task_id:
            raise RuntimeError("TickTick did not supply the task identifiers")
        payload = {"id": task_id, "projectId": project_id, **changes}
        self._request("POST", f"/project/{project_id}/task/{task_id}", payload)

    def delete(self, task: dict) -> None:
        project_id = task.get("projectId") or task.get("_project_id")
        task_id = task.get("id")
        if not project_id or not task_id:
            raise RuntimeError("TickTick did not supply the task identifiers")
        self._request("DELETE", f"/project/{project_id}/task/{task_id}")
        self.task_cache.pop(f"{project_id}|{task_id}", None)
        save_task_cache(self.task_cache)


class GoogleCalendarAPI:
    """Read events through Lucy's existing GNOME Google Online Account."""

    GOA_BUS = "org.gnome.OnlineAccounts"
    GOA_ACCOUNTS = "/org/gnome/OnlineAccounts/Accounts/"
    CALENDAR_API = "https://www.googleapis.com/calendar/v3"

    def __init__(self, upcoming_days: int = 14) -> None:
        self.upcoming_days = upcoming_days
        self.session = requests.Session()
        self.account_path_cache: str | None = None
        self.access_token_cache: str | None = None
        self.access_token_expires_at = 0.0
        self.calendars_cache: list[dict] | None = None
        self.calendars_cached_at = 0.0

    @staticmethod
    def _gdbus(*args: str) -> str:
        result = subprocess.run(
            ["gdbus", "call", "--session", "--dest", GoogleCalendarAPI.GOA_BUS, *args],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Google Calendar connection is unavailable")
        return result.stdout.strip()

    def _account_path(self) -> str:
        if self.account_path_cache:
            return self.account_path_cache
        result = subprocess.run(
            ["busctl", "--user", "tree", self.GOA_BUS, "--list", "--no-pager"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        for path in result.stdout.splitlines():
            path = path.strip()
            if not path.startswith(self.GOA_ACCOUNTS):
                continue
            provider = self._gdbus(
                "--object-path", path, "--method", "org.freedesktop.DBus.Properties.Get",
                "org.gnome.OnlineAccounts.Account", "ProviderType",
            )
            disabled = self._gdbus(
                "--object-path", path, "--method", "org.freedesktop.DBus.Properties.Get",
                "org.gnome.OnlineAccounts.Account", "CalendarDisabled",
            )
            if "'google'" in provider and "false" in disabled.lower():
                self.account_path_cache = path
                return path
        raise RuntimeError("No enabled Google Calendar account was found on Lucy")

    def _access_token(self, force: bool = False) -> str:
        if (not force and self.access_token_cache
                and monotonic() < self.access_token_expires_at):
            return self.access_token_cache
        output = self._gdbus(
            "--object-path", self._account_path(),
            "--method", "org.gnome.OnlineAccounts.OAuth2Based.GetAccessToken",
        )
        try:
            token, expires_in = ast.literal_eval(output)
        except (SyntaxError, ValueError, TypeError) as exc:
            raise RuntimeError("Google Calendar access could not be read") from exc
        self.access_token_cache = token
        self.access_token_expires_at = monotonic() + max(60, int(expires_in) - 60)
        return token

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self.session.get(
            self.CALENDAR_API + path,
            params=params, timeout=20,
        )
        if response.status_code == 401:
            self.session.headers["Authorization"] = f"Bearer {self._access_token(force=True)}"
            response = self.session.get(self.CALENDAR_API + path, params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    def change_event(self, event: dict, payload: dict | None) -> None:
        path = f"/calendars/{quote(event['_calendar_id'], safe='')}/events/{quote(event['id'], safe='')}"
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}"}
        if event.get('_raw', {}).get('etag'):
            headers['If-Match'] = event['_raw']['etag']
        method = 'DELETE' if payload is None else 'PATCH'
        response = requests.request(method, self.CALENDAR_API + path, headers=headers,
                                    json=payload, params={'sendUpdates': 'all'}, timeout=20)
        if response.status_code == 412:
            raise RuntimeError('This event changed elsewhere. Refresh and try again.')
        if response.status_code == 403:
            raise RuntimeError('Google did not allow editing this event. Check calendar permissions.')
        response.raise_for_status()

    def create_event(self, calendar_id: str, payload: dict) -> None:
        response = self.session.post(
            self.CALENDAR_API + f"/calendars/{quote(calendar_id, safe='')}/events",
            headers={"Authorization": f"Bearer {self._access_token()}"}, json=payload,
            params={"sendUpdates": "all"}, timeout=20,
        )
        response.raise_for_status()

    @staticmethod
    def _event_time(value: dict | None) -> tuple[datetime | None, bool]:
        if not value:
            return None, False
        if value.get("dateTime"):
            parsed = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
            return parsed.astimezone(), False
        if value.get("date"):
            parsed = datetime.combine(date.fromisoformat(value["date"]), time.min)
            return parsed.astimezone(), True
        return None, False

    def events(self) -> list[dict]:
        now = datetime.now().astimezone()
        start = datetime.combine(date.today(), time.min).astimezone()
        end = start + timedelta(days=self.upcoming_days + 1)
        self.session.headers["Authorization"] = f"Bearer {self._access_token()}"
        if self.calendars_cache is None or monotonic() - self.calendars_cached_at >= 3600:
            self.calendars_cache = self._get(
                "/users/me/calendarList", {"showHidden": "false"}
            ).get("items", [])
            self.calendars_cached_at = monotonic()
        calendars = self.calendars_cache
        all_events: list[dict] = []
        for calendar in calendars:
            hidden_ids = set(self.config_data.get("hidden_calendar_ids", [])) if hasattr(self, "config_data") else set()
            if calendar.get("deleted") or not calendar.get("selected", True) or calendar.get("id") in hidden_ids:
                continue
            calendar_id = calendar.get("id")
            if not calendar_id:
                continue
            data = self._get(
                f"/calendars/{quote(calendar_id, safe='')}/events",
                {
                    "timeMin": start.isoformat(), "timeMax": end.isoformat(),
                    "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
                },
            )
            for event in data.get("items", []):
                if event.get("status") == "cancelled":
                    continue
                begins, all_day = self._event_time(event.get("start"))
                finishes, _ = self._event_time(event.get("end"))
                if not begins:
                    continue
                all_events.append({
                    "_source": "calendar", "id": event.get("id", ""),
                    "_calendar_id": calendar_id, "_editable": calendar.get("accessRole") in ("owner", "writer"),
                    "_raw": event, "_url": event.get("htmlLink", "https://calendar.google.com"),
                    "title": event.get("summary") or "Untitled event",
                    "_start": begins, "_end": finishes, "_all_day": all_day,
                    "_calendar_name": calendar.get("summary", "Google Calendar"),
                    "_calendar_color": calendar.get("backgroundColor", "#4285f4"),
                })
        return all_events


class Widget(tk.Tk):
    BG = "#111318"
    CARD = "#20232a"
    CARD_HOVER = "#292d36"
    TEXT = "#f6f7f9"
    MUTED = "#9ca3af"
    RED = "#ff6b6b"
    BLUE = "#66a3ff"
    GREEN = "#65d39a"

    def __init__(self) -> None:
        super().__init__(className="TickTickWidget")
        self.config_data = load_config()
        self.api: TickTickAPI | None = None
        self.calendar_api = GoogleCalendarAPI(int(self.config_data.get("upcoming_days", 14)))
        self.calendar_api.config_data = self.config_data
        self.tasks_by_group: dict[str, list[dict]] = {}
        self.last_task_groups: dict[str, list[dict]] = {}
        self.last_calendar_groups: dict[str, list[dict]] = {"Today": [], "Upcoming": []}
        self.calendar_events: list[dict] = []
        self.notification_history = load_notification_history()
        self.events: queue.Queue[tuple] = queue.Queue()
        self.event_read_fd, self.event_write_fd = os.pipe()
        os.set_blocking(self.event_read_fd, False)
        os.set_blocking(self.event_write_fd, False)
        self.createfilehandler(self.event_read_fd, tk.READABLE, self._on_event_pipe)
        self.drag_origin = None
        self.resize_origin = None
        self.task_title_labels: list[tk.Label] = []
        self.save_job = None
        self.refresh_job = None
        self.fetch_in_progress = False
        self.scroll_velocity = 0.0
        self.scroll_animation_job = None
        self.pull_distance = 0.0
        self.pull_release_job = None
        self.content_offset = 0.0
        self.spring_velocity = 0.0
        self.spring_job = None
        self.restore_status_after_spring = False
        self.notification_job = None
        self.last_status_text = "Starting…"
        self.last_status_color = self.MUTED
        self.last_render_key: tuple | None = None

        self.title(f"Lucy Agenda v{VERSION}")
        self.configure(bg=self.BG)
        # A managed splash window stays frameless while still allowing GNOME/Mutter
        # to keep it below normal application windows like a desktop widget.
        self.attributes("-type", "splash")
        self.geometry(
            f"{self.config_data['width']}x{self.config_data['height']}+"
            f"{self.config_data['x']}+{self.config_data['y']}"
        )
        self.minsize(250, 220)
        self.attributes("-alpha", float(self.config_data.get("opacity", 0.90)))
        self.attributes("-topmost", bool(self.config_data.get("always_on_top", False)))

        self._build()
        self.bind("<Configure>", self._schedule_position_save)
        self.bind("<Escape>", lambda _e: self.iconify())
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.after(1500, self._set_x11_widget_hints)
        self.after(500, self.refresh)
        self._schedule_calendar_notification_check(2000)

    def _build(self) -> None:
        header = tk.Frame(self, bg="#181b21", height=48, cursor="fleur",
                          highlightthickness=1, highlightbackground="#30343d")
        header.pack(fill="x")
        header.pack_propagate(False)
        header.bind("<ButtonPress-1>", self._drag_start)
        header.bind("<B1-Motion>", self._drag_move)

        title_box = tk.Frame(header, bg="#181b21")
        title_box.pack(side="left", padx=7, pady=6)
        title_box.bind("<ButtonPress-1>", self._drag_start)
        title_box.bind("<B1-Motion>", self._drag_move)
        tk.Label(title_box, text="Agenda", bg="#181b21", fg=self.TEXT,
                 font=("Ubuntu", 12, "bold")).pack(anchor="w")
        self.status = tk.Label(title_box, text="Starting…", bg="#181b21", fg=self.MUTED,
                               font=("Ubuntu", 8))
        self.status.pack(anchor="w")

        refresh = tk.Button(header, text="↻", command=self.refresh, bg="#181b21", fg=self.MUTED,
                            activebackground="#2d323c", activeforeground=self.TEXT,
                            relief="flat", bd=0, font=("Ubuntu", 15), width=1, cursor="hand2")
        refresh.configure(highlightthickness=0, takefocus=0)
        refresh.pack(side="right", padx=(0, 2), pady=6)
        close = tk.Button(header, text="×", command=self.withdraw, bg="#1b1e24", fg=self.MUTED,
                          activebackground="#7b3138", activeforeground="white",
                          relief="flat", bd=0, font=("Ubuntu", 14), width=1, cursor="hand2")
        close.configure(highlightthickness=0, takefocus=0)
        close.pack(side="right", pady=6)
        self.top_button = tk.Button(
            header, text="TOP", command=self._toggle_always_on_top,
            relief="flat", bd=0, font=("Ubuntu", 8, "bold"), width=3,
            cursor="hand2", highlightthickness=0, takefocus=0,
        )
        self.top_button.pack(side="right", padx=0, pady=9)
        self._update_top_button()
        self.notification_button = tk.Button(
            header, text="🔔", command=self._toggle_calendar_notifications,
            relief="flat", bd=0, font=("Ubuntu", 9), width=1,
            cursor="hand2", highlightthickness=0, takefocus=0,
        )
        self.notification_button.pack(side="right", padx=0, pady=9)
        self._update_notification_button()
        self.menu_button = tk.Button(
            header, text="⋯", command=self._show_app_menu, relief="flat", bd=0,
            font=("Ubuntu", 14, "bold"), width=1, cursor="hand2",
            highlightthickness=0, takefocus=0, bg="#1b1e24", fg=self.MUTED,
            activebackground="#2d323c", activeforeground=self.TEXT,
        )
        self.menu_button.pack(side="right", padx=0, pady=6)
        self.add_button = tk.Button(
            header, text="+", command=self._show_add_menu, relief="flat", bd=0,
            font=("Ubuntu", 16, "bold"), width=1, cursor="hand2",
            highlightthickness=0, takefocus=0, bg="#1b1e24", fg=self.MUTED,
            activebackground="#2d323c", activeforeground=self.TEXT,
        )
        self.add_button.pack(side="right", padx=0, pady=6)
        # Repack the title after the controls so compact widths always reserve
        # room for TOP, close and refresh.
        title_box.pack_forget()
        title_box.pack(side="left", padx=(7, 1), pady=6, fill="x", expand=True)

        self.canvas = tk.Canvas(self, bg=self.BG, highlightthickness=0, bd=0)
        self.body = tk.Frame(self.canvas, bg=self.BG)
        self.body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.body_window, width=e.width))
        # Touchpads on Linux can report scrolling either as MouseWheel deltas or
        # as X11 Button-4/Button-5 events. Handle both, including small deltas
        # that would otherwise round down to zero.
        self.canvas.bind_all("<MouseWheel>", self._scroll_list)
        self.canvas.bind_all("<Button-4>", self._scroll_list)
        self.canvas.bind_all("<Button-5>", self._scroll_list)

        self.resize_grip = tk.Label(
            self, text="◢", bg=self.BG, fg="#596170", font=("Ubuntu", 12),
            cursor="bottom_right_corner", padx=2, pady=0,
        )
        self.resize_grip.place(relx=1.0, rely=1.0, x=-2, y=-2, anchor="se")
        self.resize_grip.bind("<ButtonPress-1>", self._resize_start)
        self.resize_grip.bind("<B1-Motion>", self._resize_move)
        self.resize_grip.bind("<ButtonRelease-1>", self._resize_end)

    def _set_x11_widget_hints(self) -> None:
        self.update_idletasks()
        windows = subprocess.run(["wmctrl", "-lx"], capture_output=True, text=True, check=False)
        matching = [line.split()[0] for line in windows.stdout.splitlines()
                    if "ticktickwidget.ticktickwidget" in line.lower()]
        wid = matching[-1] if matching else f"0x{self.winfo_id():x}"
        # Keep it on every workspace and out of task switchers. GNOME's BELOW
        # layer sits underneath the desktop surface itself, making the widget
        # invisible, so leave it in the normal layer where apps naturally cover it.
        for pair in ("add,sticky", "add,skip_taskbar,skip_pager"):
            subprocess.run(
                ["wmctrl", "-i", "-r", wid, "-b", pair],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )

    def _drag_start(self, event) -> None:
        self.drag_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event) -> None:
        if self.drag_origin:
            self.geometry(f"+{event.x_root - self.drag_origin[0]}+{event.y_root - self.drag_origin[1]}")

    def _resize_start(self, event) -> None:
        self.resize_origin = (
            event.x_root, event.y_root, self.winfo_width(), self.winfo_height()
        )

    def _resize_move(self, event) -> None:
        if not self.resize_origin:
            return
        start_x, start_y, start_width, start_height = self.resize_origin
        width = max(250, start_width + event.x_root - start_x)
        height = max(220, start_height + event.y_root - start_y)
        self.geometry(f"{width}x{height}")

    def _resize_end(self, _event=None) -> None:
        self.resize_origin = None
        self._save_position()

    def _scroll_list(self, event):
        if getattr(event, "num", None) == 4:
            pixels = -42.0
        elif getattr(event, "num", None) == 5:
            pixels = 42.0
        elif getattr(event, "delta", 0):
            # Preserve the scale of high-resolution touchpad deltas while
            # keeping traditional 120-step mouse wheels comfortable.
            pixels = -float(event.delta) * (0.35 if abs(event.delta) >= 120 else 0.8)
        else:
            return None

        at_top = self.canvas.yview()[0] <= 0.001
        if pixels < 0 and at_top:
            self.scroll_velocity = 0.0
            if self.spring_job:
                self.after_cancel(self.spring_job)
                self.spring_job = None
            self.pull_distance = min(190.0, self.pull_distance + abs(pixels) * 0.55)
            # Rubber-band resistance: the list follows the gesture, but more
            # slowly as it approaches the refresh threshold.
            visual_offset = min(105.0, self.pull_distance * 0.55)
            self._set_content_offset(visual_offset)
            percent = min(100, int(self.pull_distance / PULL_REFRESH_THRESHOLD * 100))
            message = "Release to refresh" if percent >= 100 else f"Pull to refresh · {percent}%"
            self.status.configure(text=message, fg=self.BLUE)
            if self.pull_release_job:
                self.after_cancel(self.pull_release_job)
            self.pull_release_job = self.after(180, self._finish_pull_refresh)
            return "break"

        if self.pull_distance:
            self._cancel_pull_refresh()
        self.scroll_velocity = max(-60.0, min(60.0, self.scroll_velocity + pixels * 0.30))
        if not self.scroll_animation_job:
            self.scroll_animation_job = self.after(16, self._animate_scroll)
        return "break"

    def _animate_scroll(self) -> None:
        self.scroll_animation_job = None
        bbox = self.canvas.bbox("all")
        if not bbox:
            self.scroll_velocity = 0.0
            return
        content_height = max(1.0, float(bbox[3] - bbox[1]))
        viewport_height = max(1.0, float(self.canvas.winfo_height()))
        max_offset = max(0.0, content_height - viewport_height)
        current_offset = self.canvas.yview()[0] * content_height
        requested_offset = current_offset + self.scroll_velocity
        new_offset = max(0.0, min(max_offset, requested_offset))
        self.canvas.yview_moveto(new_offset / content_height)
        hit_top = requested_offset < 0.0
        hit_bottom = requested_offset > max_offset
        if hit_top:
            self._set_content_offset(min(42.0, self.content_offset + abs(self.scroll_velocity) * 0.45))
            self.scroll_velocity *= 0.38
        elif hit_bottom:
            self._set_content_offset(max(-42.0, self.content_offset - abs(self.scroll_velocity) * 0.45))
            self.scroll_velocity *= 0.38
        else:
            self.scroll_velocity *= 0.88
        if abs(self.scroll_velocity) >= 0.45:
            self.scroll_animation_job = self.after(16, self._animate_scroll)
        else:
            self.scroll_velocity = 0.0
            if self.content_offset:
                self._start_content_spring()

    def _set_content_offset(self, offset: float) -> None:
        self.content_offset = offset
        self.canvas.coords(self.body_window, 0, offset)

    def _start_content_spring(self, restore_status: bool = False) -> None:
        self.restore_status_after_spring = self.restore_status_after_spring or restore_status
        if not self.spring_job:
            self.spring_job = self.after(16, self._animate_content_spring)

    def _animate_content_spring(self) -> None:
        self.spring_job = None
        self.spring_velocity += -self.content_offset * 0.16
        self.spring_velocity *= 0.72
        self._set_content_offset(self.content_offset + self.spring_velocity)
        if abs(self.content_offset) < 0.45 and abs(self.spring_velocity) < 0.45:
            self.spring_velocity = 0.0
            self._set_content_offset(0.0)
            if self.restore_status_after_spring and not self.fetch_in_progress:
                self.status.configure(text=self.last_status_text, fg=self.last_status_color)
            self.restore_status_after_spring = False
        else:
            self.spring_job = self.after(16, self._animate_content_spring)

    def _cancel_pull_refresh(self) -> None:
        self.pull_distance = 0.0
        if self.pull_release_job:
            self.after_cancel(self.pull_release_job)
            self.pull_release_job = None
        self._start_content_spring(restore_status=True)

    def _finish_pull_refresh(self) -> None:
        self.pull_release_job = None
        should_refresh = self.pull_distance >= PULL_REFRESH_THRESHOLD
        self.pull_distance = 0.0
        if should_refresh and not self.fetch_in_progress:
            self.status.configure(text="Refreshing…", fg=self.BLUE)
            self.refresh()
            self._start_content_spring()
        elif not self.fetch_in_progress:
            self._start_content_spring(restore_status=True)

    def _update_top_button(self) -> None:
        enabled = bool(self.config_data.get("always_on_top", False))
        self.top_button.configure(
            text="TOP✓" if enabled else "TOP",
            bg="#315f91" if enabled else "#1b1e24",
            fg=self.TEXT if enabled else self.MUTED,
            activebackground="#3e75ad" if enabled else "#2d323c",
            activeforeground=self.TEXT,
        )

    def _toggle_always_on_top(self) -> None:
        enabled = not bool(self.config_data.get("always_on_top", False))
        self.config_data["always_on_top"] = enabled
        self.attributes("-topmost", enabled)
        self._update_top_button()
        save_config(self.config_data)

    def _update_notification_button(self) -> None:
        enabled = bool(self.config_data.get("calendar_notifications", True))
        self.notification_button.configure(
            bg="#315f91" if enabled else "#1b1e24",
            fg=self.TEXT if enabled else self.MUTED,
            activebackground="#3e75ad" if enabled else "#2d323c",
            activeforeground=self.TEXT,
        )

    def _show_add_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="New TickTick task…", command=self._new_task_dialog)
        menu.add_command(label="New calendar event…", command=self._new_calendar_dialog)
        try:
            menu.tk_popup(self.add_button.winfo_rootx(), self.add_button.winfo_rooty() + self.add_button.winfo_height())
        finally:
            menu.grab_release()

    def _show_app_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        mode = self.config_data.get("view_mode", "all")
        menu.add_command(label="Focus mode" if mode != "focus" else "Exit focus mode",
                         command=self._toggle_focus_mode)
        menu.add_command(label="Today only" if mode != "today" else "Show all",
                         command=self._toggle_today_mode)
        menu.add_separator()
        calendars = self.calendar_api.calendars_cache or []
        hidden = set(self.config_data.get("hidden_calendar_ids", []))
        if calendars:
            calendar_menu = tk.Menu(menu, tearoff=False)
            for item in calendars:
                calendar_id = item.get("id")
                if calendar_id:
                    value = tk.BooleanVar(value=calendar_id not in hidden)
                    calendar_menu.add_checkbutton(label=item.get("summary", "Calendar"), variable=value,
                        command=lambda cid=calendar_id, var=value: self._set_calendar_visible(cid, var.get()))
            menu.add_cascade(label="Visible calendars", menu=calendar_menu)
        else:
            menu.add_command(label="Load calendar choices", command=self.refresh)
        try:
            menu.tk_popup(self.menu_button.winfo_rootx(), self.menu_button.winfo_rooty() + self.menu_button.winfo_height())
        finally:
            menu.grab_release()

    def _new_task_dialog(self) -> None:
        if not self.api:
            self.refresh()
            return
        title = simpledialog.askstring("New TickTick task", "Task title:", parent=self)
        if not title or not title.strip():
            return
        try:
            projects = self.api.projects()
        except Exception as exc:
            self._show_error(f"Could not load TickTick lists: {exc}")
            return
        win = tk.Toplevel(self); win.title("New TickTick task"); win.configure(bg=self.BG); win.transient(self)
        tk.Label(win, text="Save “%s” to:" % title.strip(), bg=self.BG, fg=self.TEXT).pack(padx=14, pady=(14, 6))
        selected = tk.StringVar(value=projects[0]["id"] if projects else "")
        for project in projects:
            tk.Radiobutton(win, text=project.get("name", "Untitled"), value=project["id"], variable=selected,
                           bg=self.BG, fg=self.TEXT, selectcolor=self.CARD, anchor="w").pack(fill="x", padx=14)
        def create():
            due_text = simpledialog.askstring("Due date", "Optional due date and time (YYYY-MM-DD HH:MM):", parent=win)
            try:
                due = datetime.strptime(due_text.strip(), "%Y-%m-%d %H:%M").astimezone() if due_text else None
            except ValueError:
                messagebox.showerror("Check date and time", "Use YYYY-MM-DD HH:MM.", parent=win); return
            win.destroy(); self._create_task(selected.get(), title.strip(), due)
        tk.Button(win, text="Create task", command=create).pack(pady=12)

    def _create_task(self, project_id: str, title: str, due: datetime | None) -> None:
        if not self.api or not project_id:
            return
        self.status.configure(text="Creating task…", fg=self.BLUE)
        def work():
            try:
                assert self.api is not None
                self.api.create(project_id, title, due)
                self._queue_event(("task_saved",))
            except Exception as exc:
                self._queue_event(("error", f"Task creation failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _new_calendar_dialog(self) -> None:
        calendars = [item for item in (self.calendar_api.calendars_cache or [])
                     if item.get("id") and item.get("accessRole") in ("owner", "writer")]
        if not calendars:
            self.status.configure(text="Loading calendars…", fg=self.BLUE)
            threading.Thread(target=self._load_calendars_then_open, daemon=True).start()
            return
        title = simpledialog.askstring("New calendar event", "Event title:", parent=self)
        if not title or not title.strip():
            return
        when = simpledialog.askstring("Start time", "Start (YYYY-MM-DD HH:MM):",
                                      initialvalue=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"), parent=self)
        if when is None:
            return
        try:
            start = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M").astimezone()
        except ValueError:
            messagebox.showerror("Check date and time", "Use YYYY-MM-DD HH:MM.", parent=self); return
        duration = simpledialog.askinteger("Duration", "Duration in minutes:", initialvalue=60, minvalue=1, maxvalue=1440, parent=self)
        if duration is None:
            return
        self._create_calendar_event(calendars[0]["id"], title.strip(), start, duration)

    def _load_calendars_then_open(self) -> None:
        try:
            self.calendar_api.events()
            self._queue_event(("open_calendar_dialog",))
        except Exception as exc:
            self._queue_event(("error", f"Could not load calendars: {exc}"))

    def _create_calendar_event(self, calendar_id: str, title: str, start: datetime, duration: int) -> None:
        self.status.configure(text="Creating event…", fg=self.BLUE)
        payload = {"summary": title, "start": {"dateTime": start.isoformat()},
                   "end": {"dateTime": (start + timedelta(minutes=duration)).isoformat()}}
        def work():
            try:
                self.calendar_api.create_event(calendar_id, payload)
                self._queue_event(("calendar_saved",))
            except Exception as exc:
                self._queue_event(("error", f"Calendar event creation failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _set_calendar_visible(self, calendar_id: str, visible: bool) -> None:
        hidden = set(self.config_data.get("hidden_calendar_ids", []))
        hidden.discard(calendar_id) if visible else hidden.add(calendar_id)
        self.config_data["hidden_calendar_ids"] = sorted(hidden)
        save_config(self.config_data)
        self.refresh()

    def _toggle_focus_mode(self) -> None:
        self.config_data["view_mode"] = "all" if self.config_data.get("view_mode") == "focus" else "focus"
        save_config(self.config_data); self._render(self.last_task_groups, self.last_calendar_groups, [])

    def _toggle_today_mode(self) -> None:
        self.config_data["view_mode"] = "all" if self.config_data.get("view_mode") == "today" else "today"
        save_config(self.config_data); self._render(self.last_task_groups, self.last_calendar_groups, [])

    def _toggle_calendar_notifications(self) -> None:
        enabled = not bool(self.config_data.get("calendar_notifications", True))
        self.config_data["calendar_notifications"] = enabled
        self._update_notification_button()
        save_config(self.config_data)
        self._schedule_calendar_notification_check(100 if enabled else None)

    def _schedule_position_save(self, _event=None) -> None:
        wraplength = max(140, self.winfo_width() - 105)
        for label in self.task_title_labels:
            label.configure(wraplength=wraplength)
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_job = self.after(700, self._save_position)

    def _save_position(self) -> None:
        self.config_data.update({"x": self.winfo_x(), "y": self.winfo_y(),
                                 "width": self.winfo_width(), "height": self.winfo_height()})
        save_config(self.config_data)
        self.save_job = None

    def refresh(self) -> None:
        if self.fetch_in_progress:
            return
        self.status.configure(text="Syncing…", fg=self.BLUE)
        if not self.api:
            try:
                self.api = TickTickAPI()
            except Exception as exc:
                self._show_error(str(exc))
                return
        self.fetch_in_progress = True
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self) -> None:
        source_errors: list[str] = []
        task_groups = None
        calendar_groups = None
        calendar_events = None
        try:
            assert self.api is not None
            task_groups = self._group(self.api.tasks())
            self.last_task_groups = task_groups
        except Exception as exc:
            print(f"TickTick refresh error: {exc!r}", file=sys.stderr, flush=True)
            source_errors.append("TickTick")
        try:
            calendar_events = self.calendar_api.events()
            calendar_groups = self._group_calendar(calendar_events)
            self.last_calendar_groups = calendar_groups
        except Exception as exc:
            print(f"Google Calendar refresh error: {exc!r}", file=sys.stderr, flush=True)
            source_errors.append("Calendar")
        if task_groups is None and calendar_groups is None:
            self._queue_event(("network_error",))
            return
        self._queue_event((
            "render", task_groups or self.last_task_groups,
            calendar_groups or self.last_calendar_groups, source_errors,
            calendar_events if calendar_groups is not None else self.calendar_events,
        ))

    def _queue_event(self, event: tuple) -> None:
        self.events.put(event)
        try:
            os.write(self.event_write_fd, b"1")
        except BlockingIOError:
            # A pending byte already guarantees that Tk will drain the queue.
            pass

    def _on_event_pipe(self, _file, _mask) -> None:
        try:
            while os.read(self.event_read_fd, 4096):
                pass
        except BlockingIOError:
            pass
        self._drain_events()

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "render":
                    self.fetch_in_progress = False
                    self.calendar_events = event[4]
                    self._render(event[1], event[2], event[3])
                    self._schedule_calendar_notification_check()
                elif event[0] == "network_error":
                    self.fetch_in_progress = False
                    self._show_network_error()
                elif event[0] == "error":
                    self.fetch_in_progress = False
                    self._show_error(event[1])
                elif event[0] == "calendar_saved":
                    self.status.configure(text="Calendar updated", fg=self.BLUE)
                    self.refresh()
                elif event[0] == "task_saved":
                    self.status.configure(text="Task updated", fg=self.BLUE)
                    self.refresh()
                elif event[0] == "open_calendar_dialog":
                    self._new_calendar_dialog()
                elif event[0] == "completed":
                    event[1].destroy()
                    self.after(200, self.refresh)
        except queue.Empty:
            pass

    def _group(self, tasks: list[dict]) -> dict[str, list[dict]]:
        today = date.today()
        groups = {"Overdue": [], "Today": [], "Unscheduled": [], "Upcoming": []}
        for task in tasks:
            due = parse_due(task.get("dueDate") or task.get("startDate"))
            if not due:
                groups["Unscheduled"].append(task)
                continue
            due_day = due.date()
            task["_due"] = due
            if due_day < today:
                groups["Overdue"].append(task)
            elif due_day == today:
                groups["Today"].append(task)
            else:
                groups["Upcoming"].append(task)
        for items in groups.values():
            items.sort(key=lambda task: (
                -int(task.get("priority", 0)),
                task["_due"].timestamp() if task.get("_due") else float("inf"),
                task.get("title", "").lower(),
            ))
        return groups

    def _group_calendar(self, events: list[dict]) -> dict[str, list[dict]]:
        today = date.today()
        now = datetime.now().astimezone()
        groups = {"Today": [], "Upcoming": []}
        for event in events:
            begins = event["_start"]
            finishes = event.get("_end") or begins
            if not event.get("_all_day") and finishes <= now:
                continue
            # Google Calendar end times are exclusive, including all-day dates.
            final_day = (finishes - timedelta(microseconds=1)).date()
            if begins.date() <= today <= final_day:
                groups["Today"].append(event)
            elif begins.date() > today:
                groups["Upcoming"].append(event)
        for items in groups.values():
            items.sort(key=lambda event: (event["_start"], event.get("title", "").lower()))
        return groups

    def _check_calendar_notifications(self) -> None:
        self.notification_job = None
        try:
            if not self.config_data.get("calendar_notifications", True):
                return
            now = datetime.now().astimezone()
            changed = False
            for event in self.calendar_events:
                if event.get("_all_day"):
                    continue
                begins = event.get("_start")
                if not begins or begins <= now:
                    continue
                notify_at = begins - timedelta(minutes=CALENDAR_REMINDER_MINUTES)
                if now < notify_at:
                    continue
                key = f"{event.get('id', '')}|{begins.isoformat()}|{CALENDAR_REMINDER_MINUTES}"
                if key in self.notification_history:
                    continue
                minutes = max(1, int((begins - now).total_seconds() / 60) + 1)
                result = subprocess.run(
                    [
                        "notify-send", "--app-name=TickTick Widget", "--icon=x-office-calendar",
                        f"Calendar event in {minutes} minute{'s' if minutes != 1 else ''}",
                        f"{event.get('title', 'Untitled event')}\n{event.get('_calendar_name', 'Google Calendar')}",
                    ],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
                if result.returncode == 0:
                    self.notification_history[key] = now.isoformat()
                    changed = True
            if changed:
                save_notification_history(self.notification_history)
        except Exception as exc:
            print(f"Calendar notification error: {exc!r}", file=sys.stderr, flush=True)
        finally:
            self._schedule_calendar_notification_check()

    def _schedule_calendar_notification_check(self, delay_ms: int | None = None) -> None:
        if self.notification_job:
            self.after_cancel(self.notification_job)
            self.notification_job = None
        if not self.config_data.get("calendar_notifications", True):
            return
        if delay_ms is None:
            now = datetime.now().astimezone()
            waits: list[float] = []
            for event in self.calendar_events:
                if event.get("_all_day"):
                    continue
                begins = event.get("_start")
                if not begins or begins <= now:
                    continue
                key = f"{event.get('id', '')}|{begins.isoformat()}|{CALENDAR_REMINDER_MINUTES}"
                if key in self.notification_history:
                    continue
                notify_at = begins - timedelta(minutes=CALENDAR_REMINDER_MINUTES)
                waits.append(max(0.0, (notify_at - now).total_seconds()))
            if not waits or min(waits) * 1000 > REFRESH_MS:
                # The regular five-minute data refresh will schedule the next
                # exact reminder without an additional idle wakeup.
                return
            delay_ms = max(1000, int(min(waits) * 1000))
        self.notification_job = self.after(delay_ms, self._check_calendar_notifications)

    def _render(self, groups: dict[str, list[dict]], calendar_groups: dict[str, list[dict]],
                source_errors: list[str]) -> None:
        self.tasks_by_group = groups
        mode = self.config_data.get("view_mode", "all")
        display_groups = {name: list(items) for name, items in groups.items()}
        display_calendar = {name: list(items) for name, items in calendar_groups.items()}
        if mode == "today":
            display_groups = {name: (items if name == "Today" else []) for name, items in display_groups.items()}
            display_calendar = {name: (items if name == "Today" else []) for name, items in display_calendar.items()}
        elif mode == "focus":
            display_groups = {name: (items if name == "Overdue" else []) for name, items in display_groups.items()}
            next_event = next(iter(display_calendar.get("Today", []) or display_calendar.get("Upcoming", [])), None)
            display_calendar = {"Today": [next_event] if next_event else [], "Upcoming": []}
        task_total = sum(len(v) for v in display_groups.values())
        event_total = sum(len(v) for v in display_calendar.values())
        render_key = tuple(
            (group_name,
             tuple((task.get("id"), task.get("title"), task.get("status"),
                    task.get("dueDate"), task.get("startDate"), task.get("priority"))
                   for task in display_groups[group_name]),
             tuple((event.get("id"), event.get("title"),
                    event.get("_start").isoformat() if event.get("_start") else "",
                    event.get("_end").isoformat() if event.get("_end") else "")
                   for event in display_calendar.get(group_name, [])))
            for group_name in ("Overdue", "Today", "Unscheduled", "Upcoming")
        ) + ((mode,),)
        colors = {"Overdue": self.RED, "Today": self.BLUE,
                  "Unscheduled": "#c5a3ff", "Upcoming": self.GREEN}
        if render_key != self.last_render_key:
            # Most refreshes have no data change. Keeping the existing card tree
            # avoids the visible blank flash caused by destroying and rebuilding it.
            previous_yview = self.canvas.yview()[0]
            self.task_title_labels = []
            for child in self.body.winfo_children():
                child.destroy()
            next_event = next(iter(display_calendar.get("Today", []) or display_calendar.get("Upcoming", [])), None)
            if next_event and not next_event.get("_all_day"):
                remaining = next_event["_start"] - datetime.now().astimezone()
                if remaining.total_seconds() > 0:
                    mins = int(remaining.total_seconds() // 60)
                    label = f"NEXT · {next_event['title']} in {mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"NEXT · {next_event['title']} in {max(1, mins)}m"
                    tk.Label(self.body, text=label, bg="#152131", fg="#9bbfe8", font=("Ubuntu", 8, "bold"),
                             anchor="w", padx=10, pady=6, wraplength=max(180, self.winfo_width() - 30)).pack(fill="x", padx=9, pady=(8, 2))
            for group_name in ("Overdue", "Today", "Unscheduled", "Upcoming"):
                tasks = display_groups[group_name]
                calendar_events = display_calendar.get(group_name, [])
                if not tasks and not calendar_events:
                    continue
                heading = tk.Frame(self.body, bg=self.BG)
                heading.pack(fill="x", padx=12, pady=(12, 5))
                tk.Label(heading, text=group_name.upper(), bg=self.BG, fg=colors[group_name],
                         font=("Ubuntu", 8, "bold")).pack(side="left")
                tk.Label(heading, text=str(len(tasks) + len(calendar_events)), bg=self.BG, fg=self.MUTED,
                         font=("Ubuntu", 8)).pack(side="right")
                for task in tasks:
                    self._task_card(task, group_name)
                for event in calendar_events:
                    self._calendar_card(event, group_name)
            # The resize grip sits over the canvas corner. This reserved space
            # keeps the final card and its time fully visible when scrolled down.
            tk.Frame(self.body, bg=self.BG, height=24).pack(fill="x")
            self.last_render_key = render_key
            self.after_idle(lambda: self._finish_render_layout(previous_yview))
        view_label = " · FOCUS" if mode == "focus" else (" · TODAY" if mode == "today" else "")
        status = f"{task_total} T · {event_total} CAL{view_label} · {datetime.now():%H:%M}"
        if source_errors:
            status += " · retrying"
        color = self.RED if source_errors else self.MUTED
        self.last_status_text = status
        self.last_status_color = color
        self.status.configure(text=status, fg=color)
        self._schedule_refresh(RETRY_MS if source_errors else REFRESH_MS)

    def _finish_render_layout(self, previous_yview: float) -> None:
        self.body.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(previous_yview)

    def _schedule_refresh(self, delay_ms: int) -> None:
        if self.refresh_job:
            self.after_cancel(self.refresh_job)
        self.refresh_job = self.after(delay_ms, self.refresh)

    def _show_network_error(self) -> None:
        self.last_status_text = "Offline · reconnecting…"
        self.last_status_color = self.RED
        self.status.configure(text=self.last_status_text, fg=self.last_status_color)
        self._schedule_refresh(RETRY_MS)

    def _task_card(self, task: dict, group_name: str) -> None:
        card = tk.Frame(self.body, bg=self.CARD, padx=7, pady=6,
                        highlightthickness=1, highlightbackground="#292d35")
        card.pack(fill="x", padx=9, pady=2)
        priority = int(task.get("priority", 0))
        checkbox_color = self.RED if priority >= 5 else ("#f6b85f" if priority >= 3 else self.MUTED)
        done = tk.Button(card, text="○", command=lambda: self._complete(task, card),
                         bg=self.CARD, fg=checkbox_color, activebackground=self.CARD_HOVER,
                         activeforeground=self.GREEN, relief="flat", bd=0,
                         font=("Ubuntu", 14), cursor="hand2", width=2)
        done.configure(highlightthickness=0, takefocus=0, padx=0, pady=0)
        done.pack(side="left", anchor="n", padx=(0, 4))
        text = tk.Frame(card, bg=self.CARD)
        text.pack(side="left", fill="x", expand=True)
        title = tk.Label(
            text, text=task.get("title") or "Untitled task", bg=self.CARD, fg=self.TEXT,
            font=("Ubuntu", 9, "bold"), justify="left", anchor="w",
            wraplength=max(140, self.winfo_width() - 105),
        )
        title.pack(fill="x")
        self.task_title_labels.append(title)
        due = task.get("_due")
        due_text = ""
        if due:
            due_text = due.strftime("%a %d %b") if group_name == "Upcoming" else due.strftime("%H:%M")
            if task.get("isAllDay") or due.strftime("%H:%M") == "00:00":
                due_text = due.strftime("%a %d %b") if group_name != "Today" else "Today"
        project = task.get("_project_name", "")
        metadata = "  ·  ".join(filter(None, (due_text, project)))
        tk.Label(text, text=metadata, bg=self.CARD, fg=self.MUTED,
                 font=("Ubuntu", 8), anchor="w").pack(fill="x", pady=(2, 0))

        url = f"https://ticktick.com/webapp/#p/{quote(task.get('projectId', ''), safe='')}/tasks/{quote(task['id'], safe='')}"
        self._bind_open(card, url, exclude=done)
        def bind_menu(widget):
            widget.bind('<Button-3>', lambda mouse: self._task_menu(mouse, task))
            for child in widget.winfo_children():
                bind_menu(child)
        bind_menu(card)

    def _task_menu(self, mouse, task: dict) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Reschedule…", command=lambda: self._reschedule_task(task))
        priorities = tk.Menu(menu, tearoff=False)
        for label, value in (("No priority", 0), ("Low", 1), ("Medium", 3), ("High", 5)):
            priorities.add_command(label=label, command=lambda p=value: self._save_task(task, {"priority": p}))
        menu.add_cascade(label="Priority", menu=priorities)
        menu.add_command(label="Move to list…", command=lambda: self._move_task_dialog(task))
        menu.add_command(label="Open in TickTick", command=lambda: webbrowser.open(
            f"https://ticktick.com/webapp/#p/{quote(task.get('projectId') or task.get('_project_id', ''), safe='')}/tasks/{quote(task['id'], safe='')}"))
        menu.add_separator()
        menu.add_command(label="Delete task…", command=lambda: self._delete_task(task))
        try:
            menu.tk_popup(mouse.x_root, mouse.y_root)
        finally:
            menu.grab_release()

    def _save_task(self, task: dict, changes: dict) -> None:
        if not self.api:
            return
        self.status.configure(text="Saving task…", fg=self.BLUE)
        def work():
            try:
                assert self.api is not None
                self.api.update(task, changes)
                self._queue_event(("task_saved",))
            except Exception as exc:
                self._queue_event(("error", f"Task update failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _reschedule_task(self, task: dict) -> None:
        current = task.get("_due") or datetime.now().astimezone()
        answer = simpledialog.askstring("Reschedule task", "Due date and time (YYYY-MM-DD HH:MM):",
                                        initialvalue=current.strftime("%Y-%m-%d %H:%M"), parent=self)
        if answer is None:
            return
        try:
            due = datetime.strptime(answer.strip(), "%Y-%m-%d %H:%M").astimezone()
        except ValueError:
            messagebox.showerror("Check date and time", "Use YYYY-MM-DD HH:MM.", parent=self)
            return
        self._save_task(task, {"dueDate": due.strftime("%Y-%m-%dT%H:%M:%S%z"), "startDate": due.strftime("%Y-%m-%dT%H:%M:%S%z")})

    def _move_task_dialog(self, task: dict) -> None:
        if not self.api:
            return
        try:
            projects = self.api.projects()
        except Exception as exc:
            self._show_error(f"Could not load TickTick lists: {exc}")
            return
        win = tk.Toplevel(self); win.title("Move task to list"); win.configure(bg=self.BG); win.transient(self)
        tk.Label(win, text="Move “%s” to:" % task.get("title", "task"), bg=self.BG, fg=self.TEXT, wraplength=280).pack(padx=14, pady=(14, 7))
        selected = tk.StringVar(value=task.get("projectId") or task.get("_project_id", ""))
        for project in projects:
            tk.Radiobutton(win, text=project.get("name", "Untitled"), value=project["id"], variable=selected,
                           bg=self.BG, fg=self.TEXT, selectcolor=self.CARD, anchor="w").pack(fill="x", padx=14)
        tk.Button(win, text="Move", command=lambda: (self._save_task(task, {"projectId": selected.get()}), win.destroy())).pack(pady=12)

    def _delete_task(self, task: dict) -> None:
        if not self.api or not messagebox.askyesno("Delete task", f"Delete “{task.get('title', 'this task')}”?", parent=self):
            return
        self.status.configure(text="Deleting task…", fg=self.BLUE)
        def work():
            try:
                assert self.api is not None
                self.api.delete(task)
                self._queue_event(("task_saved",))
            except Exception as exc:
                self._queue_event(("error", f"Task deletion failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _bind_open(self, widget, url, exclude=None):
        if widget is exclude:
            return
        widget.bind('<Double-Button-1>', lambda _event: webbrowser.open(url))
        for child in widget.winfo_children():
            self._bind_open(child, url, exclude)

    def _calendar_card(self, event: dict, group_name: str) -> None:
        card = tk.Frame(self.body, bg="#19202a", padx=0, pady=0,
                        highlightthickness=1, highlightbackground="#2b3543")
        card.pack(fill="x", padx=9, pady=2)
        stripe = tk.Frame(card, bg=event.get("_calendar_color", self.BLUE), width=4)
        stripe.pack(side="left", fill="y")
        stripe.pack_propagate(False)
        badge = tk.Label(card, text="CAL", bg="#19202a", fg=self.BLUE,
                         font=("Ubuntu", 7, "bold"), width=4)
        badge.pack(side="left", anchor="n", padx=(5, 2), pady=8)
        text = tk.Frame(card, bg="#19202a")
        text.pack(side="left", fill="x", expand=True, padx=(0, 7), pady=6)
        title = tk.Label(
            text, text=event.get("title") or "Untitled event", bg="#19202a", fg=self.TEXT,
            font=("Ubuntu", 9, "bold"), justify="left", anchor="w",
            wraplength=max(140, self.winfo_width() - 115),
        )
        title.pack(fill="x")
        self.task_title_labels.append(title)
        begins = event["_start"]
        if event.get("_all_day"):
            when = "All day" if group_name == "Today" else begins.strftime("%a %d %b · All day")
        else:
            when = begins.strftime("%H:%M") if group_name == "Today" else begins.strftime("%a %d %b · %H:%M")
        metadata = f"{when}  ·  {event.get('_calendar_name', 'Google Calendar')}"
        tk.Label(text, text=metadata, bg="#19202a", fg="#9bbfe8",
                 font=("Ubuntu", 8), anchor="w").pack(fill="x", pady=(2, 0))
        def bind_menu(widget):
            widget.bind('<Button-3>', lambda mouse: self._calendar_menu(mouse, event))
            for child in widget.winfo_children(): bind_menu(child)
        bind_menu(card)
        self._bind_open(card, event["_url"])

    def _calendar_menu(self, mouse, event):
        menu = tk.Menu(self, tearoff=False)
        writable = 'normal' if event.get('_editable') else 'disabled'
        menu.add_command(label="Reschedule…", state=writable, command=lambda: self._calendar_editor(event))
        menu.add_command(label="Edit details…", state=writable, command=lambda: self._calendar_editor(event))
        menu.add_command(label="Open in Google Calendar", command=lambda: webbrowser.open(event['_url']))
        menu.add_separator()
        menu.add_command(label="Delete event…", state=writable, command=lambda: self._delete_calendar_event(event))
        try:
            menu.tk_popup(mouse.x_root, mouse.y_root)
        finally:
            menu.grab_release()

    def _delete_calendar_event(self, event):
        occurrence = " (this occurrence)" if event['_raw'].get('recurringEventId') else ""
        if messagebox.askyesno("Delete event", f"Delete {event['title']}{occurrence}?\nGuests will be notified if applicable.", parent=self):
            self._save_calendar_event(event, None)

    def _save_calendar_event(self, event, payload):
        self.status.configure(text="Saving calendar…", fg=self.BLUE)
        def work():
            try:
                self.calendar_api.change_event(event, payload)
                self._queue_event(('calendar_saved',))
            except Exception as exc:
                self._queue_event(('error', f'Calendar update failed: {exc}'))
        threading.Thread(target=work, daemon=True).start()

    def _calendar_editor(self, event):
        win = tk.Toplevel(self)
        win.title("Edit calendar event")
        win.configure(bg=self.BG)
        win.transient(self)
        win.attributes('-topmost', True)
        win.resizable(False, False)
        raw = event['_raw']
        start = event['_start']
        end = event['_end'] or start + timedelta(hours=1)
        selected = tk.StringVar(value=start.date().isoformat())
        end_date = tk.StringVar(value=(end.date()-timedelta(days=1) if event['_all_day'] else end.date()).isoformat())
        start_time = tk.StringVar(value=start.strftime('%H:%M'))
        end_time = tk.StringVar(value=end.strftime('%H:%M'))
        all_day = tk.BooleanVar(value=event['_all_day'])
        title = tk.StringVar(value=event['title'])
        location = tk.StringVar(value=raw.get('location', ''))
        month = [start.year, start.month]
        body = tk.Frame(win, bg=self.BG, padx=12, pady=10)
        body.pack()
        def field(label, variable):
            tk.Label(body, text=label, bg=self.BG, fg=self.TEXT).pack(anchor='w')
            entry = tk.Entry(body, textvariable=variable, width=35)
            entry.pack(fill='x', pady=(0, 5))
            return entry
        field('Title', title)
        grid = tk.Frame(body, bg=self.BG)
        grid.pack(fill='x')
        def pick(day):
            old = date.fromisoformat(selected.get())
            new = date(month[0], month[1], day)
            end_date.set((date.fromisoformat(end_date.get()) + (new-old)).isoformat())
            selected.set(new.isoformat())
            previous[0] = datetime.combine(new, time.fromisoformat(start_time.get()))
            draw()
        def move(delta):
            index = month[0]*12+month[1]-1+delta
            month[:] = [index//12, index%12+1]
            draw()
        def draw():
            for child in grid.winfo_children(): child.destroy()
            tk.Button(grid, text='‹', command=lambda: move(-1)).grid(row=0,column=0)
            tk.Label(grid, text=f'{calendar.month_name[month[1]]} {month[0]}', bg=self.BG, fg=self.TEXT).grid(row=0,column=1,columnspan=5)
            tk.Button(grid, text='›', command=lambda: move(1)).grid(row=0,column=6)
            for col, day in enumerate(['Mo','Tu','We','Th','Fr','Sa','Su']):
                tk.Label(grid,text=day,bg=self.BG,fg=self.MUTED).grid(row=1,column=col)
            for row, week in enumerate(calendar.monthcalendar(*month), 2):
                for col, day in enumerate(week):
                    if day:
                        chosen = selected.get() == date(*month,day).isoformat()
                        tk.Button(grid,text=str(day),width=2,bg=self.BLUE if chosen else self.CARD,fg=self.TEXT,command=lambda d=day: pick(d)).grid(row=row,column=col)
        draw()
        field('Start date (YYYY-MM-DD)', selected)
        start_entry = field('Start time (HH:MM)', start_time)
        field('End date (YYYY-MM-DD)', end_date)
        end_entry = field('End time (HH:MM)', end_time)
        previous = [start]
        def preserve_duration(*_):
            try:
                new = datetime.fromisoformat(selected.get()+'T'+start_time.get())
                old = previous[0].replace(tzinfo=None)
                finish = datetime.fromisoformat(end_date.get()+'T'+end_time.get()) + (new-old)
                end_date.set(finish.date().isoformat()); end_time.set(finish.strftime('%H:%M'))
                previous[0] = new
            except ValueError: pass
        start_entry.bind('<FocusOut>', preserve_duration)
        def toggle():
            for entry in (start_entry, end_entry): entry.configure(state='disabled' if all_day.get() else 'normal')
        tk.Checkbutton(body,text='All day',variable=all_day,command=toggle,bg=self.BG,fg=self.TEXT,selectcolor=self.CARD).pack(anchor='w')
        toggle()
        field('Location', location)
        tk.Label(body,text='Description',bg=self.BG,fg=self.TEXT).pack(anchor='w')
        description = tk.Text(body,width=35,height=3)
        description.insert('1.0',raw.get('description','')); description.pack()
        if raw.get('recurringEventId'):
            tk.Label(body,text='Changes apply to this occurrence.',bg=self.BG,fg=self.MUTED).pack()
            tk.Button(body,text='Edit whole series in Google Calendar',command=lambda: webbrowser.open(event['_url'])).pack()
        def save():
            try:
                first = date.fromisoformat(selected.get()); last = date.fromisoformat(end_date.get())
                if all_day.get():
                    if last < first: raise ValueError('End date must not precede start date.')
                    starts={'date':first.isoformat()}; ends={'date':(last+timedelta(days=1)).isoformat()}
                else:
                    a=datetime.combine(first,time.fromisoformat(start_time.get())).astimezone()
                    b=datetime.combine(last,time.fromisoformat(end_time.get())).astimezone()
                    if b <= a: raise ValueError('End time must be after start time.')
                    starts={'dateTime':a.isoformat()}; ends={'dateTime':b.isoformat()}
                if not title.get().strip(): raise ValueError('Enter a title.')
            except ValueError as exc:
                messagebox.showerror('Check date and time',str(exc),parent=win); return
            payload={'summary':title.get().strip(),'location':location.get(),'description':description.get('1.0','end-1c'),'start':starts,'end':ends}
            if raw.get('attendees') and not messagebox.askyesno('Notify guests','Save these changes and notify event guests?',parent=win): return
            self._save_calendar_event(event,payload); win.destroy()
        buttons=tk.Frame(body,bg=self.BG); buttons.pack(pady=(10,0))
        tk.Button(buttons,text='Cancel',command=win.destroy).pack(side='left',padx=6)
        tk.Button(buttons,text='Save',command=save).pack(side='left',padx=6)

    def _complete(self, task: dict, card: tk.Frame) -> None:
        if not self.api:
            return
        for child in card.winfo_children():
            child.configure(cursor="watch")
        self.status.configure(text="Completing task…", fg=self.BLUE)
        threading.Thread(target=self._complete_worker, args=(task, card), daemon=True).start()

    def _complete_worker(self, task: dict, card: tk.Frame) -> None:
        try:
            assert self.api is not None
            self.api.complete(task)
            self._queue_event(("completed", card))
        except Exception as exc:
            self._queue_event(("error", f"Could not complete task: {exc}"))

    def _show_error(self, text: str) -> None:
        self.status.configure(text=text, fg=self.RED)
        if "authorization" in text.lower():
            for child in self.body.winfo_children():
                child.destroy()
            tk.Label(self.body, text="TickTick authorization required", bg=self.BG, fg=self.TEXT,
                     font=("Ubuntu", 13, "bold")).pack(padx=20, pady=(50, 8))
            tk.Label(self.body, text="Complete the one-time setup in the open browser tab.",
                     bg=self.BG, fg=self.MUTED, font=("Ubuntu", 10), wraplength=330,
                     justify="center").pack(padx=20)


if __name__ == "__main__":
    Widget().mainloop()
