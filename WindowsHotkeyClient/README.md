# Windows Hotkey Client

A system-tray app that triggers Discord Soundboard Agent sounds (Discord
sounds and local sounds) from global keyboard shortcuts, without opening a
browser. No window on start — just a tray icon with three right-click
options: **Join My Channel** / **Leave Channel** (toggles based on current
voice state), **Settings**, and **Exit**.

The join/leave item uses the same [`/me/join-mine`](../API.md) endpoint as
the dashboard's "Join my channel" button — it brings the bot to whatever
voice channel your linked Discord account is currently in. That link is set
up once from the **dashboard** (☰ menu → My Discord Account), not from this
client; the client only needs the Agent URL and your personal API key
(below), and the agent already knows which Discord user that key belongs to.

Runs directly on Windows, separate from the agent's Docker container.

## Download

Every push to `main` that touches this folder is built and published as a
GitHub Release by [`.github/workflows/windows-client-release.yml`](../.github/workflows/windows-client-release.yml)
on a real Windows GitHub Actions runner (PyInstaller doesn't cross-compile,
so it has to build on Windows). Grab the latest `DiscordSoundboardHotkeys.exe`
from the repo's **Releases** page — no Python or build tooling needed. You can
also trigger a build manually from the Actions tab (`workflow_dispatch`).

## Run from source

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw hotkey_client.py
```

A local `.venv` keeps these dependencies off your global Python — if you've
already run `build.bat` (below), the venv it created already has everything
installed, so you can skip straight to the last line.

Use `pythonw` (not `python`) so no console window appears — that's the normal
way to run this day-to-day. Use `.venv\Scripts\python hotkey_client.py`
instead only while debugging, so you can see printed errors.

Right-click the tray icon → **Settings**:

1. Enter the agent's URL (e.g. `http://192.168.1.8:8766`) and your **personal
   API key** (dashboard → ☰ menu → API Key), then **Save connection**.
2. Click **Refresh sounds**, pick one from the dropdown, click **Record** and
   press your desired key combo, then **Add binding**.
3. Press that key combo anywhere on your PC to trigger the sound. Remove a
   binding any time with its **Remove** button — the change applies
   immediately, no restart needed.

Config (including your API key) is stored in
`%APPDATA%\DiscordSoundboard\config.json`.

## Build as an .exe locally

Only needed if you're changing the client and want to test a build yourself —
otherwise just use the Releases page above.

```powershell
.\build.bat
```

Creates (or reuses) a local `.venv` and installs dependencies into it — never
your global Python — then builds through that same venv end to end, so
there's no ambiguity about which `python`/`pip` gets used even if you have
multiple Python installs.

Produces `dist\DiscordSoundboardHotkeys.exe` — a standalone executable (no
Python install needed to run it), windowed (no console), with a generated
icon. Copy that one file anywhere.

## Run at Windows startup

Press `Win+R`, type `shell:startup`, hit Enter, and drop a shortcut there —
either to `dist\DiscordSoundboardHotkeys.exe`, or to
`.venv\Scripts\pythonw.exe` with `hotkey_client.py` as the argument (and this
folder as "Start in") if running from source.

## Notes

- Hotkeys are global — they fire even when another app is focused.
- The sound dropdown groups entries as `<Server> — <Sound>` for Discord
  sounds and `Local — <Sound>` for local library sounds.
- If the agent is unreachable when a hotkey fires (or when you click Join
  My Channel / Leave Channel), it just fails silently (logged to the console
  when run via `python`, not `pythonw`) — there's no in-app pop-up so a
  flaky connection doesn't spam error dialogs.
- If you haven't linked a Discord User ID in the dashboard yet, **Join My
  Channel** will fail the same way (silently) since the agent has nothing to
  join you to.
- The tray menu's label refreshes every few seconds in the background, so it
  may briefly show the wrong state if you join/leave from elsewhere (the
  dashboard, another device) right before opening the menu.
