#!/usr/bin/env python3
"""hotkey_client - system tray app that fires Discord Soundboard Agent sounds
(including local sounds) from global keyboard shortcuts.

No window on start - just a tray icon with two right-click options,
Settings and Exit. Run with pythonw.exe (not python.exe) so no console
window shows up either. See README.md for setup.

Config lives in %APPDATA%\\DiscordSoundboard\\config.json.
"""
import json
import os
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import urllib.error
import urllib.parse
import urllib.request

import pystray
from PIL import Image, ImageDraw
from pynput import keyboard

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "DiscordSoundboard")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

MODIFIER_KEYSYMS = {
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Alt_L": "alt", "Alt_R": "alt",
    "Shift_L": "shift", "Shift_R": "shift",
    "Super_L": "cmd", "Super_R": "cmd", "Win_L": "cmd", "Win_R": "cmd",
}
SPECIAL_KEY_MAP = {
    "escape": "esc", "return": "enter", "prior": "page_up", "next": "page_down",
    "space": "space", "tab": "tab", "backspace": "backspace", "delete": "delete",
    "insert": "insert", "home": "home", "end": "end",
    "up": "up", "down": "down", "left": "left", "right": "right",
}
FUNCTION_KEY_RE = re.compile(r"^f([1-9]|1[0-9]|20)$")

root = None
hotkey_listener = None
hotkey_lock = threading.Lock()
settings_win = None


# ---------- config ----------


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {"agent_url": "", "api_key": "", "bindings": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------- agent HTTP calls (stdlib only, matches GoofyBot-discord-test.py's style) ----------


def call(base, key, method, path, body=None, query=None, timeout=10):
    q = dict(query or {})
    if key:
        q["key"] = key
    url = base.rstrip("/") + path + ("?" + urllib.parse.urlencode(q) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "User-Agent": "DiscordSoundboardHotkeys/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_all_sounds(base, key):
    results = []
    d = call(base, key, "GET", "/sounds", query={"guild_id": "all"})
    for s in d.get("sounds", []):
        if s.get("default"):
            continue
        label = f"{s.get('guild_name') or 'Server'} — {s['name']}"
        results.append({"label": label, "sound_id": s["sound_id"], "is_local": False})
    try:
        d2 = call(base, key, "GET", "/local-sounds")
        for s in d2.get("sounds", []):
            label = f"Local — {s['name']}"
            results.append({"label": label, "sound_id": s["id"], "is_local": True})
    except Exception:
        pass  # older agent without local sounds, or a transient error - not fatal here
    results.sort(key=lambda x: x["label"].lower())
    return results


def play_sound(binding):
    cfg = load_config()
    base = cfg.get("agent_url", "").strip()
    key = cfg.get("api_key", "").strip()
    if not base:
        return
    try:
        if binding.get("is_local"):
            call(base, key, "POST", f"/local-sounds/{binding['sound_id']}/play", body={})
        else:
            call(base, key, "POST", "/play", body={"sound_id": binding["sound_id"]})
    except Exception as e:
        print("play failed:", e)


def is_voice_connected(base, key, timeout=3):
    # Short timeout since this backs the tray menu's label - a slow/unreachable
    # agent shouldn't make right-clicking the icon feel like it's hanging.
    status = call(base, key, "GET", "/status", timeout=timeout)
    return bool(status.get("voice_connected"))


def get_join_leave_label(item):
    cfg = load_config()
    base = cfg.get("agent_url", "").strip()
    key = cfg.get("api_key", "").strip()
    if not base:
        return "Join My Channel"
    try:
        return "Leave Channel" if is_voice_connected(base, key) else "Join My Channel"
    except Exception:
        return "Join My Channel"


def join_or_leave():
    cfg = load_config()
    base = cfg.get("agent_url", "").strip()
    key = cfg.get("api_key", "").strip()
    if not base:
        return
    try:
        if is_voice_connected(base, key):
            call(base, key, "POST", "/leave", body={})
        else:
            call(base, key, "POST", "/me/join-mine", body={})
    except Exception as e:
        print("join/leave failed:", e)


# ---------- hotkey string conversion ----------


def keysym_to_token(keysym):
    low = keysym.lower()
    if low in SPECIAL_KEY_MAP:
        return f"<{SPECIAL_KEY_MAP[low]}>"
    if FUNCTION_KEY_RE.match(low):
        return f"<{low}>"
    if len(low) == 1:
        return low
    return f"<{low}>"


def to_pynput_hotkey(combo_str):
    parts = combo_str.split("+")
    out = []
    for p in parts:
        if p in ("ctrl", "alt", "shift", "cmd"):
            out.append(f"<{p}>")
        else:
            out.append(p)
    return "+".join(out)


# ---------- global hotkey listener ----------


def make_trigger(binding):
    def _trigger():
        threading.Thread(target=play_sound, args=(binding,), daemon=True).start()
    return _trigger


def rebuild_hotkey_listener():
    global hotkey_listener
    with hotkey_lock:
        if hotkey_listener is not None:
            hotkey_listener.stop()
            hotkey_listener = None
        cfg = load_config()
        mapping = {}
        for b in cfg.get("bindings", []):
            try:
                combo = to_pynput_hotkey(b["hotkey"])
                mapping[combo] = make_trigger(b)
            except Exception as e:
                print("skipping invalid binding", b, e)
        if mapping:
            hotkey_listener = keyboard.GlobalHotKeys(mapping)
            hotkey_listener.start()


# ---------- settings window ----------


class SettingsWindow(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Discord Soundboard - Hotkeys")
        self.geometry("560x560")
        self.minsize(480, 420)
        self.recording = False
        self.held_mods = set()
        self.pending_combo = None
        self.sounds = []
        self._build_ui()
        self._load_into_fields()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        conn = ttk.LabelFrame(self, text="Connection")
        conn.pack(fill="x", padx=10, pady=8)
        ttk.Label(conn, text="Agent URL").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.url_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.url_var).grid(row=0, column=1, padx=4, pady=4, sticky="we")
        ttk.Label(conn, text="API Key").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.key_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.key_var, show="*").grid(row=1, column=1, padx=4, pady=4, sticky="we")
        ttk.Button(conn, text="Save connection", command=self._save_connection).grid(
            row=2, column=1, sticky="e", padx=4, pady=6)
        conn.columnconfigure(1, weight=1)

        add = ttk.LabelFrame(self, text="Add a hotkey")
        add.pack(fill="x", padx=10, pady=8)
        ttk.Label(add, text="Sound").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.sound_var = tk.StringVar()
        self.sound_combo = ttk.Combobox(add, textvariable=self.sound_var, state="readonly")
        self.sound_combo.grid(row=0, column=1, padx=4, pady=4, sticky="we")
        ttk.Button(add, text="Refresh sounds", command=self._refresh_sounds).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(add, text="Hotkey").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.combo_var = tk.StringVar(value="(none)")
        ttk.Entry(add, textvariable=self.combo_var, state="readonly").grid(
            row=1, column=1, padx=4, pady=4, sticky="we")
        ttk.Button(add, text="Record", command=self._start_recording).grid(row=1, column=2, padx=4, pady=4)

        ttk.Button(add, text="Add binding", command=self._add_binding).grid(
            row=2, column=1, sticky="e", padx=4, pady=6)
        add.columnconfigure(1, weight=1)

        listf = ttk.LabelFrame(self, text="Configured hotkeys")
        listf.pack(fill="both", expand=True, padx=10, pady=8)
        canvas = tk.Canvas(listf, highlightthickness=0)
        scrollbar = ttk.Scrollbar(listf, orient="vertical", command=canvas.yview)
        self.rows_frame = ttk.Frame(canvas)
        self.rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#888888").pack(fill="x", padx=10, pady=(0, 8))

        self.bind("<KeyPress>", self._on_key_press)

    def _load_into_fields(self):
        cfg = load_config()
        self.url_var.set(cfg.get("agent_url", ""))
        self.key_var.set(cfg.get("api_key", ""))
        self._render_bindings()
        if cfg.get("agent_url") and cfg.get("api_key"):
            self._refresh_sounds()

    def _save_connection(self):
        cfg = load_config()
        cfg["agent_url"] = self.url_var.get().strip()
        cfg["api_key"] = self.key_var.get().strip()
        save_config(cfg)
        self.status_var.set("Connection saved.")

    def _refresh_sounds(self):
        base = self.url_var.get().strip()
        key = self.key_var.get().strip()
        if not base:
            messagebox.showwarning("Missing Agent URL", "Set the Agent URL first.", parent=self)
            return
        self.status_var.set("Loading sounds…")
        self.update_idletasks()
        try:
            sounds = fetch_all_sounds(base, key)
        except Exception as e:
            messagebox.showerror("Couldn't load sounds", str(e), parent=self)
            self.status_var.set("Failed to load sounds.")
            return
        self.sounds = sounds
        self.sound_combo["values"] = [s["label"] for s in sounds]
        self.status_var.set(f"Loaded {len(sounds)} sounds.")

    def _start_recording(self):
        self.recording = True
        self.held_mods = set()
        self.pending_combo = None
        self.combo_var.set("Press keys…")
        self.focus_set()

    def _on_key_press(self, event):
        if not self.recording:
            return
        keysym = event.keysym
        if keysym in MODIFIER_KEYSYMS:
            self.held_mods.add(MODIFIER_KEYSYMS[keysym])
            return
        token = keysym_to_token(keysym)
        display_key = token[1:-1] if token.startswith("<") else token
        display = "+".join(sorted(self.held_mods) + [display_key])
        self.pending_combo = "+".join(sorted(self.held_mods) + [token])
        self.combo_var.set(display)
        self.recording = False

    def _add_binding(self):
        idx = self.sound_combo.current()
        if idx < 0:
            messagebox.showwarning("Pick a sound", "Choose a sound from the list first.", parent=self)
            return
        if not self.pending_combo:
            messagebox.showwarning("Set a hotkey", "Click Record and press a key combo first.", parent=self)
            return
        cfg = load_config()
        bindings = cfg.setdefault("bindings", [])
        if any(b["hotkey"] == self.pending_combo for b in bindings):
            messagebox.showwarning("Already bound", f"{self.combo_var.get()} is already assigned.", parent=self)
            return
        sound = self.sounds[idx]
        bindings.append({
            "hotkey": self.pending_combo,
            "sound_id": sound["sound_id"],
            "is_local": sound["is_local"],
            "label": sound["label"],
        })
        save_config(cfg)
        self.pending_combo = None
        self.combo_var.set("(none)")
        self._render_bindings()
        rebuild_hotkey_listener()
        self.status_var.set("Hotkey added.")

    def _remove_binding(self, hotkey):
        cfg = load_config()
        cfg["bindings"] = [b for b in cfg.get("bindings", []) if b["hotkey"] != hotkey]
        save_config(cfg)
        self._render_bindings()
        rebuild_hotkey_listener()

    def _render_bindings(self):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        bindings = load_config().get("bindings", [])
        if not bindings:
            ttk.Label(self.rows_frame, text="No hotkeys configured yet.", foreground="#888888").pack(
                anchor="w", padx=6, pady=6)
            return
        for b in bindings:
            row = ttk.Frame(self.rows_frame)
            row.pack(fill="x", padx=4, pady=2)
            ttk.Label(row, text=b["hotkey"], width=22).pack(side="left")
            ttk.Label(row, text=b.get("label", b["sound_id"])).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="Remove", command=lambda h=b["hotkey"]: self._remove_binding(h)).pack(side="right")

    def _on_close(self):
        global settings_win
        settings_win = None
        self.destroy()


def open_settings_window():
    global settings_win
    if settings_win is not None and settings_win.winfo_exists():
        settings_win.deiconify()
        settings_win.lift()
        settings_win.focus_force()
        return
    settings_win = SettingsWindow(root)


# ---------- tray icon ----------


def make_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=(88, 101, 242, 255))
    draw.polygon([(20, 24), (20, 40), (28, 40), (40, 50), (40, 14), (28, 24)], fill="white")
    draw.arc((36, 20, 50, 44), start=300, end=60, fill="white", width=3)
    return img


def on_join_or_leave(icon, item):
    threading.Thread(target=join_or_leave, daemon=True).start()


def on_settings(icon, item):
    root.after(0, open_settings_window)


def on_exit(icon, item):
    icon.stop()
    root.after(0, root.destroy)


def poll_menu_refresh(icon):
    # Dynamic menu item text (get_join_leave_label) is re-evaluated whenever
    # the menu is shown, but nudging update_menu() periodically too means the
    # label stays accurate even if a particular pystray backend only
    # re-renders on an explicit update rather than on every right-click.
    while True:
        time.sleep(6)
        try:
            icon.update_menu()
        except Exception:
            pass


def main():
    global root
    root = tk.Tk()
    root.withdraw()

    rebuild_hotkey_listener()

    icon = pystray.Icon(
        "discord-soundboard-hotkeys",
        make_icon_image(),
        "Discord Soundboard Hotkeys",
        menu=pystray.Menu(
            pystray.MenuItem(get_join_leave_label, on_join_or_leave),
            pystray.MenuItem("Settings", on_settings),
            pystray.MenuItem("Exit", on_exit),
        ),
    )
    threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=poll_menu_refresh, args=(icon,), daemon=True).start()

    root.mainloop()

    with hotkey_lock:
        if hotkey_listener is not None:
            hotkey_listener.stop()


if __name__ == "__main__":
    main()
