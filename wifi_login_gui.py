from __future__ import annotations

import configparser
import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import wifi_login as core

FROZEN = getattr(sys, "frozen", False)
HERE = core.HERE
CONFIG = HERE / "config.ini"
ALL_ADAPTERS = "(all adapters)"
ANY_WIFI = "(stay on whatever Wi-Fi Windows picks)"
DEFAULT_LABEL = "Default (any other Wi-Fi / cable)"
WINDOW_TITLE = "Wi-Fi Auto Login (FortiGate)"
PICKER_PASSWORD = "password"
WIFI_POLL_MS = 5000

_MUTEX_NAME = "Local\\WiFiAutoLogin_single_instance"
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = ctypes.c_void_p
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_mutex = None


def claim_single_instance() -> bool:
    global _mutex
    handle = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    err = ctypes.get_last_error()
    if not handle or err in (183, 5):
        if handle:
            _kernel32.CloseHandle(handle)
        return False
    _mutex = handle
    return True


def release_single_instance() -> None:
    global _mutex
    if _mutex:
        _kernel32.CloseHandle(_mutex)
        _mutex = None


def open_in_browser(url: str) -> None:
    import os
    if url.strip():
        os.startfile(url.strip())


def focus_existing_window() -> None:
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)


DEFAULTS = {
    "login": {"username": "", "password": ""},
    "portal": {
        "portal_url": "https://192.168.201.254:1003",
        "probe_urls": "http://landing.lkphils.com, https://landing.lkphils.com, http://www.google.com, "
        "https://www.google.com, http://www.yahoo.com, http://neverssl.com, http://www.bing.com, "
        "http://example.com, http://github.com, http://www.msftconnecttest.com/redirect",
        "success_url": "http://landing.lkphils.com",
        "success_text": "Start browsing, Welcome to, L&K Industries",
    },
    "wifi": {"target": "", "keep_connected": "no"},
    "behavior": {
        "tries_per_site": "10",
        "shuffle_sites": "yes",
        "attempts_before_reset": "3",
        "login_retries": "20",
        "delay_seconds": "2",
        "max_attempts": "0",
        "reset_network": "yes",
        "fix_dns": "yes",
        "adapter_name": "",
        "watch_interval": "30",
        "watch_mode": "no",
        "auto_start": "no",
    },
}

STATUS_COLORS = {
    "idle": "#6b7785",
    "checking": "#2f71b2",
    "logging in": "#c97a00",
    "resetting": "#8e44ad",
    "offline": "#c0392b",
    "connected": "#1e8e3e",
    "failed": "#c0392b",
    "stopped": "#6b7785",
}


def _split_probes(text: str) -> list[str]:
    return [p.strip() for p in text.replace(",", "\n").splitlines() if p.strip()]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(WINDOW_TITLE)
        self.minsize(360, 300)
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry(f"{min(700, sw - 40)}x{min(760, sh - 100)}")
        except Exception:
            pass
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.live_settings: core.Settings | None = None
        self.profiles: dict[str, dict] = {}
        self.cur_profile = ""
        self.windows_wifis: list[str] = []
        self.last_ssid: str | None = None
        self._title_clicks: list[float] = []
        self.directory: dict[str, str] = self._load_directory()

        core.set_hooks(
            log_fn=lambda line: self.events.put(("log", line)),
            status_fn=lambda state, detail="": self.events.put(("status", (state, detail))),
        )

        self._make_vars()
        self._build()
        self._load()
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, lambda: threading.Thread(target=self._warm_up, daemon=True).start())
        self.after(250, self._poll_wifi)
        self.after(400, self._refresh_wifi_list)
        if self.v_autostart.get() and any(p["username"] and p["password"] for p in self.profiles.values()):
            self.after(1500, self._start)

    def _warm_up(self):
        try:
            core.load_requests()
        except Exception:
            pass
        core.cleanup_old_logs()

    def _make_vars(self):
        self.v_user = tk.StringVar()
        self.v_pass = tk.StringVar()
        self.v_show = tk.BooleanVar(value=False)
        self.v_remember = tk.BooleanVar(value=False)
        self.v_profile = tk.StringVar()
        self.v_profile_hint = tk.StringVar()
        self.v_portal = tk.StringVar()
        self.v_success = tk.StringVar()
        self.v_tries = tk.StringVar()
        self.v_login_retries = tk.StringVar()
        self.v_per_site = tk.StringVar()
        self.v_shuffle = tk.BooleanVar()
        self.v_success_text = tk.StringVar()
        self.v_delay = tk.StringVar()
        self.v_max = tk.StringVar()
        self.v_reset = tk.BooleanVar()
        self.v_adapter = tk.StringVar()
        self.v_watch = tk.BooleanVar()
        self.v_watch_int = tk.StringVar()
        self.v_autostart = tk.BooleanVar()
        self.v_fixdns = tk.BooleanVar()
        self.v_wifi_now = tk.StringVar(value="checking...")
        self.v_wifi_target = tk.StringVar()
        self.v_wifi_keep = tk.BooleanVar()
        self.v_status = tk.StringVar(value="Idle")

    def _load_directory(self) -> dict:
        try:
            data = json.loads((HERE / "users.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items() if str(k).strip()}

    def _title_click(self, _event=None):
        now = time.monotonic()
        self._title_clicks = [t for t in self._title_clicks if now - t < 3] + [now]
        if len(self._title_clicks) >= 5:
            self._title_clicks.clear()
            self._open_user_picker()

    def _open_user_picker(self):
        if not self.directory:
            messagebox.showinfo("User list", "No user list found (users.json is missing or empty).")
            return
        pw = simpledialog.askstring("Password", "Password:", show="•", parent=self)
        if pw is None:
            return
        if pw != PICKER_PASSWORD:
            messagebox.showerror("Password", "Wrong password.")
            return
        win = tk.Toplevel(self)
        win.title("Pick a user")
        win.transient(self)
        win.resizable(False, True)
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Double-click a user to fill in their username and password.").pack(
            anchor="w", pady=(0, 6)
        )
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text="Search").pack(side="left")
        v_search = tk.StringVar()
        ttk.Entry(row, textvariable=v_search).pack(side="left", fill="x", expand=True, padx=(6, 0))
        box = ttk.Frame(frame)
        box.pack(fill="both", expand=True)
        lb = tk.Listbox(box, height=14, width=36, activestyle="dotbox")
        sb = ttk.Scrollbar(box, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        names = sorted(self.directory, key=str.lower)

        def fill(_e=None):
            shown = lb.get(0, "end")
            sel = lb.curselection()
            if not shown or not sel:
                return
            name = shown[sel[0]]
            self.v_user.set(name)
            self.v_pass.set(self.directory[name])
            self.v_remember.set(True)
            self.v_show.set(False)
            self._toggle_show()
            win.destroy()

        def refresh(*_a):
            q = v_search.get().strip().lower()
            lb.delete(0, "end")
            for n in names:
                if q in n.lower():
                    lb.insert("end", n)
            if lb.size():
                lb.selection_set(0)

        v_search.trace_add("write", refresh)
        lb.bind("<Double-Button-1>", fill)
        lb.bind("<Return>", fill)
        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Fill", command=fill).pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 6))
        refresh()
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        win.geometry(f"+{self.winfo_rootx() + 60}+{self.winfo_rooty() + 120}")
        lb.focus_set()

    def _build(self):
        pad = {"padx": 6, "pady": 2}
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0,
                           background=ttk.Style().lookup("TFrame", "background") or self.cget("background"))
        vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        hbar = ttk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        root = ttk.Frame(canvas, padding=10)
        win = canvas.create_window(0, 0, window=root, anchor="nw")
        root.columnconfigure(0, weight=1)

        def _fit(_e=None):
            w, h = canvas.winfo_width(), canvas.winfo_height()
            canvas.itemconfigure(win, width=max(root.winfo_reqwidth(), w), height=max(root.winfo_reqheight(), h))
            canvas.configure(scrollregion=canvas.bbox("all"))
            need_v = root.winfo_reqheight() > h
            need_h = root.winfo_reqwidth() > w
            (vbar.grid if need_v else vbar.grid_remove)()
            (hbar.grid if need_h else hbar.grid_remove)()

        canvas.bind("<Configure>", _fit)
        root.bind("<Configure>", _fit)

        def _wheel(e):
            w = self.winfo_containing(e.x_root, e.y_root)
            if isinstance(w, tk.Text):
                return
            if root.winfo_reqheight() > canvas.winfo_height():
                canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

        self.bind_all("<MouseWheel>", _wheel)

        top = ttk.Frame(root)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(1, weight=1)
        self.status_dot = tk.Label(top, text="●", font=("Segoe UI", 22), fg=STATUS_COLORS["idle"])
        self.status_dot.grid(row=0, column=0, rowspan=2)
        ttk.Label(top, textvariable=self.v_status, font=("Segoe UI", 13, "bold")).grid(row=0, column=1, sticky="w")
        admin = core.is_admin()
        ttk.Label(
            top,
            text="Running as Administrator" if admin else "Not Administrator - ipconfig /release & /renew may fail",
            foreground="#1e8e3e" if admin else "#c0392b",
        ).grid(row=1, column=1, sticky="w")
        if not admin:
            ttk.Button(top, text="Restart as Admin", command=self._restart_admin).grid(row=0, column=2, rowspan=2)

        wf = ttk.LabelFrame(root, text="Wi-Fi", padding=6)
        wf.grid(row=1, column=0, sticky="ew")
        wf.columnconfigure(1, weight=1)
        ttk.Label(wf, text="Connected now").grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(wf, textvariable=self.v_wifi_now, font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w", **pad)
        ttk.Button(wf, text="Refresh", command=lambda: (self._poll_wifi(once=True), self._refresh_wifi_list())).grid(
            row=0, column=2, sticky="ew", **pad
        )
        ttk.Label(wf, text="Use this Wi-Fi").grid(row=1, column=0, sticky="w", **pad)
        self.cb_wifi = ttk.Combobox(wf, textvariable=self.v_wifi_target, width=36)
        self.cb_wifi.grid(row=1, column=1, sticky="ew", **pad)
        self.b_join = ttk.Button(wf, text="Connect now", command=self._join_now)
        self.b_join.grid(row=1, column=2, sticky="ew", **pad)
        ttk.Checkbutton(
            wf, text="Keep the PC on this Wi-Fi (rejoin it if it drops or Windows switches to another one)",
            variable=self.v_wifi_keep,
        ).grid(row=2, column=0, columnspan=3, sticky="w", **pad)

        acc_title = ttk.Label(root, text="Login for each Wi-Fi")
        acc = ttk.LabelFrame(root, labelwidget=acc_title, padding=6)
        acc.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        acc.columnconfigure(1, weight=1)
        acc_title.bind("<Button-1>", self._title_click)
        ttk.Label(acc, text="Settings for").grid(row=0, column=0, sticky="w", **pad)
        self.cb_profile = ttk.Combobox(acc, textvariable=self.v_profile, state="readonly", width=36)
        self.cb_profile.grid(row=0, column=1, sticky="ew", **pad)
        self.cb_profile.bind("<<ComboboxSelected>>", self._on_profile_selected)
        self.b_del_profile = ttk.Button(acc, text="Delete", width=7, command=self._delete_profile)
        self.b_del_profile.grid(row=0, column=2, **pad)
        ttk.Label(acc, textvariable=self.v_profile_hint, foreground="#6b7785").grid(
            row=1, column=1, columnspan=2, sticky="w", padx=6
        )
        ttk.Label(acc, text="Username").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(acc, textvariable=self.v_user).grid(row=2, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(acc, text="Password").grid(row=3, column=0, sticky="w", **pad)
        self.e_pass = ttk.Entry(acc, textvariable=self.v_pass, show="•")
        self.e_pass.grid(row=3, column=1, sticky="ew", **pad)
        ttk.Checkbutton(acc, text="Show", variable=self.v_show, command=self._toggle_show).grid(row=3, column=2, **pad)
        ttk.Checkbutton(
            acc, text="Remember password (stored as plain text in config.ini)", variable=self.v_remember
        ).grid(row=4, column=1, columnspan=2, sticky="w", **pad)

        por = ttk.LabelFrame(root, text="Portal (for the Wi-Fi chosen above)", padding=6)
        por.grid(row=3, column=0, sticky="ew", pady=6)
        por.columnconfigure(1, weight=1)
        ttk.Label(por, text="Portal address\n(blank = find it)").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(por, textvariable=self.v_portal).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(por, text="Open", width=6, command=lambda: open_in_browser(self.v_portal.get())).grid(
            row=0, column=2, **pad
        )
        ttk.Label(por, text="Trigger sites\n(one per line)").grid(row=1, column=0, sticky="nw", **pad)
        self.t_probes = tk.Text(por, height=3, width=40, font=("Consolas", 9))
        self.t_probes.grid(row=1, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(por, text="Connected when\nthis site loads").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(por, textvariable=self.v_success).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(por, text="Open", width=6, command=lambda: open_in_browser(self.v_success.get())).grid(
            row=2, column=2, **pad
        )
        ttk.Label(por, text="...and the page\ncontains any of").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(por, textvariable=self.v_success_text).grid(row=3, column=1, columnspan=2, sticky="ew", **pad)

        beh = ttk.LabelFrame(root, text="Behavior (all Wi-Fi)", padding=6)
        beh.grid(row=4, column=0, sticky="ew")
        for c in (1, 3):
            beh.columnconfigure(c, weight=1)
        ttk.Label(beh, text="Tries per site, then next site").grid(row=0, column=0, sticky="w", **pad)
        ttk.Spinbox(beh, from_=1, to=100, textvariable=self.v_per_site, width=6).grid(
            row=0, column=1, sticky="w", **pad
        )
        ttk.Label(beh, text="Fails before network reset").grid(row=0, column=2, sticky="w", **pad)
        ttk.Spinbox(beh, from_=1, to=100, textvariable=self.v_tries, width=6).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(beh, text="Logins per portal visit").grid(row=0, column=4, sticky="w", **pad)
        ttk.Spinbox(beh, from_=1, to=100, textvariable=self.v_login_retries, width=6).grid(
            row=0, column=5, sticky="w", **pad
        )
        ttk.Label(beh, text="Max tries (0 = forever)").grid(row=1, column=0, sticky="w", **pad)
        ttk.Spinbox(beh, from_=0, to=10000, textvariable=self.v_max, width=6).grid(row=1, column=1, sticky="w", **pad)
        ttk.Checkbutton(beh, text="Run flushdns / release / renew", variable=self.v_reset).grid(
            row=1, column=2, columnspan=2, sticky="w", **pad
        )
        ttk.Label(beh, text="Adapter to release/renew").grid(row=2, column=0, sticky="w", **pad)
        self.cb_adapter = ttk.Combobox(beh, textvariable=self.v_adapter, width=28)
        self.cb_adapter.grid(row=2, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Button(beh, text="Refresh", command=self._refresh_adapters).grid(row=2, column=3, sticky="w", **pad)
        ttk.Checkbutton(beh, text="Watch mode (stay running, re-login when kicked out)", variable=self.v_watch).grid(
            row=3, column=0, columnspan=2, sticky="w", **pad
        )
        ttk.Label(beh, text="Check every (s)").grid(row=3, column=2, sticky="w", **pad)
        ttk.Spinbox(beh, from_=5, to=3600, textvariable=self.v_watch_int, width=6).grid(
            row=3, column=3, sticky="w", **pad
        )
        ttk.Label(beh, text="Delay between tries (s)").grid(row=4, column=0, sticky="w", **pad)
        ttk.Spinbox(beh, from_=0, to=120, increment=0.5, textvariable=self.v_delay, width=6).grid(
            row=4, column=1, sticky="w", **pad
        )
        ttk.Checkbutton(beh, text="Shuffle site order every round", variable=self.v_shuffle).grid(
            row=4, column=2, columnspan=2, sticky="w", **pad
        )
        ttk.Checkbutton(
            beh, text="Start automatically when the app opens (needs remembered password)", variable=self.v_autostart
        ).grid(row=5, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(
            beh, text="Fix broken Wi-Fi DNS after login (use 8.8.8.8 / 1.1.1.1 - undone on another Wi-Fi)",
            variable=self.v_fixdns,
        ).grid(row=6, column=0, columnspan=4, sticky="w", **pad)

        btns = ttk.Frame(root)
        btns.grid(row=5, column=0, sticky="ew", pady=6)
        self.b_start = ttk.Button(btns, text="▶  Start", command=self._start)
        self.b_start.pack(side="left", padx=(0, 6))
        self.b_stop = ttk.Button(btns, text="■  Stop", command=self._stop, state="disabled")
        self.b_stop.pack(side="left", padx=6)
        ttk.Button(btns, text="Save / Apply", command=self._save_clicked).pack(side="left", padx=6)
        self.b_reset = ttk.Button(btns, text="Reset network now", command=self._reset_now)
        self.b_reset.pack(side="left", padx=6)
        ttk.Button(btns, text="Clear", command=lambda: self.log_box.delete("1.0", "end")).pack(side="right")
        ttk.Button(btns, text="Open log .txt", command=self._open_log).pack(side="right", padx=6)

        root.rowconfigure(6, weight=1)
        self.log_box = ScrolledText(root, height=8, font=("Consolas", 9), wrap="word")
        self.log_box.grid(row=6, column=0, sticky="nsew")
        self.log_box.tag_config("good", foreground="#1e8e3e", font=("Consolas", 9, "bold"))
        self.log_box.tag_config("bad", foreground="#c0392b")
        self.log_box.tag_config("head", foreground="#2f71b2")

    @staticmethod
    def _profile_from(get) -> dict:
        p = {k: get(k) for k in core.PROFILE_KEYS}
        p["probe_urls"] = "\n".join(_split_probes(p["probe_urls"]))
        p["remember"] = bool(p["password"])
        return p

    def _stash_profile(self) -> None:
        self.profiles[self.cur_profile] = {
            "username": self.v_user.get().strip(),
            "password": self.v_pass.get(),
            "remember": self.v_remember.get(),
            "portal_url": self.v_portal.get().strip(),
            "probe_urls": "\n".join(_split_probes(self.t_probes.get("1.0", "end"))),
            "success_url": self.v_success.get().strip(),
            "success_text": self.v_success_text.get().strip(),
        }

    def _show_profile(self, name: str) -> None:
        p = self.profiles[name]
        self.cur_profile = name
        self.v_profile.set(name or DEFAULT_LABEL)
        self.v_user.set(p["username"])
        self.v_pass.set(p["password"])
        self.v_remember.set(p["remember"])
        self.v_portal.set(p["portal_url"])
        self.t_probes.delete("1.0", "end")
        self.t_probes.insert("1.0", p["probe_urls"])
        self.v_success.set(p["success_url"])
        self.v_success_text.set(p["success_text"])
        self.b_del_profile.config(state="normal" if name else "disabled")
        self._update_profile_hint()

    def _update_profile_hint(self) -> None:
        if not self.cur_profile:
            hint = "Used for every Wi-Fi that has no settings of its own, and for cable/LAN."
        else:
            hint = f"Used only when the PC is on the Wi-Fi '{self.cur_profile}'."
        if self.last_ssid and self.last_ssid not in self.profiles:
            hint += f"   (Now on '{self.last_ssid}': uses Default)"
        self.v_profile_hint.set(hint)

    def _profile_choices(self) -> list[str]:
        names = [n for n in self.profiles if n]
        for n in self.windows_wifis + ([self.last_ssid] if self.last_ssid else []):
            if n and n not in names:
                names.append(n)
        return [DEFAULT_LABEL] + names

    def _on_profile_selected(self, _event=None) -> None:
        text = self.v_profile.get()
        name = "" if text == DEFAULT_LABEL else text
        if name == self.cur_profile:
            return
        self._stash_profile()
        if name not in self.profiles:
            new = dict(self.profiles[""])
            new["portal_url"] = ""
            self.profiles[name] = new
            self._append(f"New settings for Wi-Fi '{name}' (copied from Default, portal address = find "
                         f"automatically). Change what differs, then Save / Apply.", "head")
        self._show_profile(name)

    def _delete_profile(self) -> None:
        name = self.cur_profile
        if not name:
            return
        if not messagebox.askyesno("Delete", f"Delete the settings for Wi-Fi '{name}'?\n"
                                             f"That Wi-Fi will then use Default."):
            return
        del self.profiles[name]
        self._show_profile("")
        self._save()
        self._append(f"Settings for '{name}' deleted.", "head")

    def _read_config(self) -> configparser.ConfigParser:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_dict(DEFAULTS)
        cp.read(CONFIG, encoding="utf-8")
        return cp

    def _load(self):
        cp = self._read_config()
        sec_of = {"username": "login", "password": "login"}
        self.profiles = {"": self._profile_from(lambda k: cp.get(sec_of.get(k, "portal"), k).strip())}
        for sec in cp.sections():
            if sec.lower().startswith(core.PROFILE_PREFIX):
                name = sec[len(core.PROFILE_PREFIX):].strip()
                if name:
                    d = self.profiles[""]
                    self.profiles[name] = self._profile_from(
                        lambda k, s=sec, d=d: cp.get(s, k, fallback=d[k] if k != "password" else "").strip()
                    )
        self._show_profile("")
        self.cb_profile["values"] = self._profile_choices()

        self.v_wifi_target.set(cp.get("wifi", "target") or ANY_WIFI)
        self.v_wifi_keep.set(cp.getboolean("wifi", "keep_connected", fallback=False))
        self.cb_wifi["values"] = [ANY_WIFI]
        b = cp["behavior"]
        self.v_tries.set(b.get("attempts_before_reset"))
        self.v_login_retries.set(b.get("login_retries"))
        self.v_shuffle.set(b.getboolean("shuffle_sites", fallback=True))
        self.v_per_site.set(b.get("tries_per_site"))
        self.v_delay.set(b.get("delay_seconds"))
        self.v_max.set(b.get("max_attempts"))
        self.v_reset.set(b.getboolean("reset_network", fallback=True))
        self.v_fixdns.set(b.getboolean("fix_dns", fallback=True))
        self.v_adapter.set(b.get("adapter_name") or ALL_ADAPTERS)
        self.v_watch.set(b.getboolean("watch_mode", fallback=False))
        self.v_autostart.set(b.getboolean("auto_start", fallback=False))
        self.v_watch_int.set(b.get("watch_interval"))
        self.cb_adapter["values"] = [ALL_ADAPTERS]
        self.after(300, self._refresh_adapters)

    def _probe_list(self) -> list[str]:
        return _split_probes(self.t_probes.get("1.0", "end"))

    def _wifi_target(self) -> str:
        t = self.v_wifi_target.get().strip()
        return "" if t == ANY_WIFI else t

    def _validate(self) -> bool:
        checks = [
            (self.v_per_site, int, "Tries per site"),
            (self.v_tries, int, "Fails before network reset"),
            (self.v_login_retries, int, "Logins per portal visit"),
            (self.v_delay, float, "Delay between tries"),
            (self.v_max, int, "Max tries"),
            (self.v_watch_int, float, "Check every"),
        ]
        for var, typ, name in checks:
            try:
                if typ(var.get()) < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid value", f"'{name}' must be a positive number.")
                return False
        if not self._probe_list():
            messagebox.showerror("Missing", "Add at least one trigger site (e.g. http://neverssl.com).")
            return False
        if self.v_wifi_keep.get() and not self._wifi_target():
            messagebox.showerror("Missing", "Pick the Wi-Fi to stay on (Use this Wi-Fi), or untick 'Keep the PC on this Wi-Fi'.")
            return False
        return True

    def _save(self) -> None:
        self._stash_profile()
        adapter = self.v_adapter.get().strip()
        cp = configparser.ConfigParser(interpolation=None)

        def section(p: dict) -> dict:
            return {
                "portal_url": p["portal_url"],
                "probe_urls": ", ".join(_split_probes(p["probe_urls"])),
                "success_url": p["success_url"],
                "success_text": p["success_text"],
            }

        d = self.profiles[""]
        cp["login"] = {"username": d["username"], "password": d["password"] if d["remember"] else ""}
        cp["portal"] = section(d)
        cp["wifi"] = {"target": self._wifi_target(), "keep_connected": "yes" if self.v_wifi_keep.get() else "no"}
        cp["behavior"] = {
            "tries_per_site": self.v_per_site.get().strip(),
            "shuffle_sites": "yes" if self.v_shuffle.get() else "no",
            "attempts_before_reset": self.v_tries.get().strip(),
            "login_retries": self.v_login_retries.get().strip(),
            "delay_seconds": self.v_delay.get().strip(),
            "max_attempts": self.v_max.get().strip(),
            "reset_network": "yes" if self.v_reset.get() else "no",
            "fix_dns": "yes" if self.v_fixdns.get() else "no",
            "adapter_name": "" if adapter == ALL_ADAPTERS else adapter,
            "watch_interval": self.v_watch_int.get().strip(),
            "watch_mode": "yes" if self.v_watch.get() else "no",
            "auto_start": "yes" if self.v_autostart.get() else "no",
        }
        for name, p in self.profiles.items():
            if name:
                cp[core.PROFILE_PREFIX + name] = {
                    "username": p["username"],
                    "password": p["password"] if p["remember"] else "",
                    **section(p),
                }
        with open(CONFIG, "w", encoding="utf-8") as f:
            f.write("; Settings for wifi_login.py - edited by wifi_login_gui.py\n"
                    "; [login]/[portal] = Default.  [profile <Wi-Fi name>] = settings for that Wi-Fi only.\n")
            cp.write(f)
        self.cb_profile["values"] = self._profile_choices()

    def _build_settings(self) -> core.Settings:
        s = core.Settings(CONFIG)
        for name, p in self.profiles.items():
            if name in s.profiles:
                s.profiles[name]["password"] = p["password"]
                s.profiles[name]["username"] = p["username"]
        s.use_profile(self.last_ssid or "")
        return s

    def _save_clicked(self):
        if not self._validate():
            return
        self._save()
        if self.live_settings is not None:
            self.live_settings.__dict__.update(self._build_settings().__dict__)
            self._append("Settings applied to the running session.", "head")
        else:
            self._append("Settings saved.", "head")

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self._validate():
            return
        self._save()
        if not any(p["username"] and p["password"] for p in self.profiles.values()):
            if not messagebox.askyesno(
                "No username / password",
                "No username and password are set.\n\nContinue anyway? (Only works on Wi-Fi whose portal "
                "has just an Accept / Continue button.)",
            ):
                return
        self.live_settings = self._build_settings()
        s = self.live_settings
        core.STOP.clear()
        core.log("=" * 70)
        core.log(f"START  admin={core.is_admin()}  settings for: "
                 + ", ".join(core.profile_label(n) for n in s.profiles)
                 + f"  |  Wi-Fi to use: {s.wifi_target or 'any'} (keep on it: {s.wifi_keep})")
        for name, p in s.profiles.items():
            core.log(f"       [{core.profile_label(name)}] user={p['username'] or '(none)'}  "
                     f"portal={p['portal_url'] or 'auto-detect'}  success={p['success_url'] or 'internet test'} "
                     f"containing {p['success_text'] or '-'}")
            core.log(f"       [{core.profile_label(name)}] sites={', '.join(_split_probes(p['probe_urls']))}")
        core.log(f"       tries/site={s.tries_per_site} logins/portal={s.login_retries} shuffle={s.shuffle_sites} "
                 f"reset_after={s.attempts_before_reset} reset={s.reset_network} adapter={s.adapter or 'all'} "
                 f"delay={s.delay:g}s max={s.max_attempts or 'forever'} watch={self.v_watch.get()}")
        self._set_running(True)
        watch = self.v_watch.get()
        self.worker = threading.Thread(target=self._work, args=(s, watch), daemon=True)
        self.worker.start()

    def _work(self, settings, watch):
        try:
            core.run(settings, watch)
        except Exception as e:
            self.events.put(("log", f"ERROR: {e!r}"))
            self.events.put(("status", ("failed", str(e))))
        finally:
            self.events.put(("done", None))

    def _stop(self):
        core.STOP.set()
        self.v_status.set("Stopping...")
        self.b_stop.config(state="disabled")

    def _run_job(self, job) -> None:
        core.STOP.clear()
        self._set_running(True)

        def wrapper():
            try:
                job()
            except core.Stopped:
                core.status("stopped")
            except Exception as e:
                core.log(f"ERROR: {e!r}")
            finally:
                self.events.put(("done", None))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def _reset_now(self):
        if self.worker and self.worker.is_alive():
            return
        if not self._validate():
            return
        self._save()
        s = self._build_settings()

        def job():
            core.reset_network(s)
            core.log("Network reset finished.")
            core.status("idle", "network reset done")

        self._run_job(job)

    def _join_now(self):
        target = self._wifi_target()
        if not target:
            messagebox.showinfo("Wi-Fi", "Pick the Wi-Fi to connect to in 'Use this Wi-Fi'.")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Wi-Fi", "Stop the login first, or tick 'Keep the PC on this Wi-Fi' and press "
                                         "Save / Apply - the running login will switch by itself.")
            return

        def job():
            core.log(f"Connecting to Wi-Fi '{target}'...")
            core.status("checking", f"joining Wi-Fi {target}")
            ok = core.connect_wifi(target)
            core.log_network_info()
            core.status("idle", f"on Wi-Fi {target}" if ok else f"could not join {target}")
            self.events.put(("wifi", core.wifi_status()))

        self._run_job(job)

    def _set_running(self, running: bool):
        self.b_start.config(state="disabled" if running else "normal")
        self.b_reset.config(state="disabled" if running else "normal")
        self.b_join.config(state="disabled" if running else "normal")
        self.b_stop.config(state="normal" if running else "disabled")

    def _refresh_adapters(self):
        threading.Thread(
            target=lambda: self.events.put(("adapters", core.list_adapters())), daemon=True
        ).start()

    def _refresh_wifi_list(self):
        def job():
            saved = core.wifi_profiles()
            visible = [n for n, _auth in core.visible_networks() if n]
            self.events.put(("wifi_list", (saved, visible)))

        threading.Thread(target=job, daemon=True).start()

    def _poll_wifi(self, once: bool = False):
        threading.Thread(target=lambda: self.events.put(("wifi", core.wifi_status())), daemon=True).start()
        if not once:
            self.after(WIFI_POLL_MS, self._poll_wifi)

    def _on_wifi(self, st: dict) -> None:
        if not st["present"]:
            text = "no Wi-Fi adapter (cable / LAN)"
        elif st["connected"]:
            used = st["ssid"] if st["ssid"] in self.profiles else "Default"
            text = f"{st['ssid']}   (settings used: {used})"
        elif st["radio_off"]:
            text = "Wi-Fi is turned OFF in Windows"
        else:
            text = "not connected to any Wi-Fi"
        self.v_wifi_now.set(text)
        ssid = st["ssid"]
        if ssid != self.last_ssid:
            self.last_ssid = ssid
            if ssid and core._dns_state_file().exists():
                threading.Thread(target=core.restore_dns, args=(ssid,), daemon=True).start()
            if ssid and ssid in self.profiles and ssid != self.cur_profile:
                self._stash_profile()
                self._show_profile(ssid)
            self.cb_profile["values"] = self._profile_choices()
            self._update_profile_hint()

    def _open_log(self):
        path = core.log_file_path()
        if not path.exists():
            core.LOG_DIR.mkdir(exist_ok=True)
            path.touch()
        import os
        os.startfile(path)

    def _toggle_show(self):
        self.e_pass.config(show="" if self.v_show.get() else "•")

    def _restart_admin(self):
        params = "" if FROZEN else f'"{Path(__file__).resolve()}"'
        release_single_instance()
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, str(HERE), 1)
        if ret > 32:
            core.STOP.set()
            self.destroy()
        else:
            claim_single_instance()

    def _on_close(self):
        core.STOP.set()
        self.destroy()

    def _append(self, line: str, tag: str | None = None):
        if tag is None:
            low = line.lower()
            if "connected" in low and "not connected" not in low:
                tag = "good"
            elif any(w in low for w in ("rejected", "failed", "error", "giving up", "no response", "not working")):
                tag = "bad"
        self.log_box.insert("end", line + "\n", tag or ())
        self.log_box.see("end")

    def _portal_found(self, detail: str) -> None:
        ssid, _, base = detail.partition("|")
        name = ssid if ssid in self.profiles else ""
        if name == self.cur_profile:
            self._stash_profile()
        if not self.profiles[name]["portal_url"]:
            self.profiles[name]["portal_url"] = base
            if name == self.cur_profile:
                self.v_portal.set(base)

    def _pump(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "status":
                    state, detail = payload
                    if state == "portal_found":
                        self._portal_found(detail)
                        continue
                    self.v_status.set(state.capitalize() + (f"  —  {detail}" if detail else ""))
                    self.status_dot.config(fg=STATUS_COLORS.get(state, "#6b7785"))
                elif kind == "adapters":
                    self.cb_adapter["values"] = [ALL_ADAPTERS, *payload]
                elif kind == "wifi":
                    self._on_wifi(payload)
                elif kind == "wifi_list":
                    saved, visible = payload
                    self.windows_wifis = saved + [v for v in visible if v not in saved]
                    self.cb_wifi["values"] = [ANY_WIFI] + self.windows_wifis
                    self.cb_profile["values"] = self._profile_choices()
                elif kind == "done":
                    self._set_running(False)
                    self.live_settings = None
                    if self.v_status.get().startswith("Stopping"):
                        self.v_status.set("Stopped")
        except queue.Empty:
            pass
        self.after(100, self._pump)


def selftest() -> None:
    import time
    t0 = time.perf_counter()
    app = App()
    app.update()
    lines = ["window_ready_secs=%.2f" % (time.perf_counter() - t0)]
    s = core.new_session()
    for url in ("http://example.com", "https://www.google.com"):
        try:
            r = s.get(url, timeout=10)
            lines.append("%s -> %s %d chars, form=%s" % (url, r.status_code, len(r.text), bool(core.find_login_form(r.text))))
        except Exception as e:
            lines.append("%s -> ERROR %r" % (url, e))
    lines += [
        "frozen=%s" % FROZEN,
        "folder=%s" % HERE,
        "adapters=%s" % core.list_adapters(),
        "wifi=%s" % core.wifi_status(),
        "wifi_saved=%s" % core.wifi_profiles(),
        "route=%s" % (core.default_route(),),
        "profiles=%s" % list(app.profiles),
        "sites=%d" % len(app._probe_list()),
        "log=%s" % core.log_file_path(),
        "requests=%s" % core.requests.__version__,
    ]
    app.destroy()
    (HERE / "selftest.txt").write_text("\n".join(lines) + "\nOK\n", encoding="utf-8")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--startuptest" in sys.argv:
        _app = App()
        _app.update()
        _app.destroy()
    elif claim_single_instance():
        App().mainloop()
    else:
        focus_existing_window()
