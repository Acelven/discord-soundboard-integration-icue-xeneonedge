# Discord Soundboard Agent

Play your Discord server's soundboard from anywhere that can make an HTTP
request — a Corsair XENEON EDGE touchscreen, a phone browser, a shell script,
or your own app. A small headless bot sits in your voice channel and exposes a
clean local API; the front-ends are just clients.

## Why

Discord's soundboard only lives inside the official client, and triggering it
means alt-tabbing away from whatever you're doing. This project moves the
buttons onto a second screen (or any device on your LAN) without touching your
user account — it runs as a proper bot, so there's no self-botting and no ToS
risk.

## Components

- **Agent** (`agent.py`) — a `discord.py` bot wrapped in an aiohttp HTTP server.
  Joins voice on request, lists sounds across every server it's in, and triggers
  playback via Discord's official `send-soundboard-sound` endpoint. Ships as a
  Docker container; built to run on an always-on box like an Unraid server.
- **iCUE widget** — a touch grid for the XENEON EDGE that auto-fits every sound
  with its emoji icon, groups sounds by server into tabs, and shows live
  connection state with manual join/leave.
- **Browser page** — the agent serves its own control UI at `/`, so any browser
  on the network is a full soundboard with zero install, including any local
  sounds uploaded via the dashboard. No login, gated only by the shared
  `API_KEY` if you set one.
- **Dashboard** — a login-gated control UI at `/dashboard` with per-user
  accounts, favorites, play history, a local sound library, and an admin
  panel for managing users. Kept separate from `/` so the widget/CLI keep
  working exactly as before.
- **CLI** (`GoofyBot-discord-test.py`) — stdlib-only command-line client for scripting and
  testing.
- **Windows hotkey client** (`WindowsHotkeyClient/`) — a system-tray app (no
  window, right-click for Settings/Exit) that fires sounds from global
  keyboard shortcuts. Runs directly on Windows, separate from the Docker
  container. Prebuilt `.exe` releases are published automatically by CI — see
  [`WindowsHotkeyClient/README.md`](WindowsHotkeyClient/README.md).

## Features

- Cross-server sounds: play any sound from any server the bot is in, into
  whichever channel it's currently sitting in
- Auto-fitting tile grid that scales from a handful of sounds to a hundred
- Custom + unicode emoji icons resolved from Discord's CDN
- Manual join/leave, never auto-joins, with 4-hour idle auto-disconnect
- Survives restarts: detects the bot's existing voice presence instead of
  asking you to rejoin
- Optional shared API key for LAN deployments, **plus** per-user accounts
  with their own personal API keys (dashboard → "My API Key"), each
  attributed separately in play history
- Multi-user dashboard: search, favorites, recent-plays, and an admin panel
  to add/remove users — backed by a single `/data/users.json` file, no
  database required
- **Local sound library**: upload any audio clip, trim it in-browser with a
  Discord-style waveform picker, and play it by streaming it directly into
  voice — a separate path from Discord's soundboard, so it isn't limited to
  5.2s/512KB and multiple local sounds can overlap. Also supports
  **transferring** an existing Discord soundboard sound into the local
  library (a copy — the Discord original is untouched), which additionally
  sidesteps the "Use External Sounds" permission needed for cross-server
  soundboard playback, since the local copy is streamed rather than
  triggered via Discord's soundboard RPC
- Documented HTTP API ([`API.md`](API.md)) for building your own integrations

## Quick start

Create a bot application at <https://discord.com/developers>, then invite it to
your server with these permissions:

- Connect
- Speak
- Use Soundboard
- Use External Sounds
- View Channels

Copy `ServerDocker/.env.example` to `ServerDocker/.env` and fill in your
token (and an optional `API_KEY`) — `.env` is gitignored, so secrets never
end up in a commit:

```bash
cd ServerDocker
cp .env.example .env   # then edit .env
docker compose up -d --build
```

Open `http://<host>:8766/?key=<yourkey>`, pick a channel, and tap a sound.

On first boot the agent also creates a dashboard admin account — set
`ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env`, or leave `ADMIN_PASSWORD` unset
and read the generated one from `docker compose logs`. Open
`http://<host>:8766/dashboard` and sign in.

## Deploying a prebuilt image

Every push to `main` that touches `ServerDocker/` is built and published to
GitHub Container Registry by [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml),
tagged both `:latest` and an auto-incrementing `:v<N>` (so old builds stay
pullable for rollback). This lets a deployment host — an Unraid box, say —
run the agent without checking out the source or building anything itself.

One-time setup on that host: create a [classic personal access token](https://github.com/settings/tokens)
with the **`read:packages`** scope, then:

```bash
docker login ghcr.io -u <your-github-username>
```

Copy `docker-compose.ghcr.yml` and `.env.example` to the deployment host (no
need for the rest of the source), fill in `.env` there the same way as above,
then:

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

To roll back, edit the image tag from `:latest` to a specific `:v<N>` and
re-run the two commands above.

## Usage

### Browser

Navigate to `http://<host>:8766/?key=<yourkey>`. Pick a channel from the
dropdown, hit **Join**, and tap sounds. **Leave** disconnects the bot.

### Dashboard

Navigate to `http://<host>:8766/dashboard` and sign in. The **☰** menu opens
the nav drawer (your API key, admin panel, log out); tapping the channel pill
at the top opens the join/leave picker, which also has a **📍 Join my
channel** button — link your Discord User ID once from the drawer's **My
Discord Account**, and this brings the bot straight to whatever voice channel
you're currently sitting in, in any server it's in, no dropdown needed.
Tapping the sounds pill below it opens
a picker for which view you're looking at — **All Sounds** gives you an
overview sectioned by source (Favorites, then Local, then each server), while
the others (Favorites, Recent, Local, per-server) show a single flat grid.
Server sections can be dragged into whatever order you like from that same
picker (grab the ≡ handle) — it's saved per-user and reflected in both the
picker and the All Sounds sections. Search sits above the grid, not in the
top bar.

Right-click (or long-press on touch) any sound tile for a context menu:
favorite/unfavorite, download the audio file, and — for Discord sounds not
already copied — "Make local" (checks first whether a local copy already
exists and shows "Already local" instead of duplicating it).

The **+ Add sound** button beside the Local section (in both the Local tab
and the All Sounds view) opens the upload flow: pick a file, drag the two
handles on the waveform to select up to `MAX_LOCAL_SOUND_SECONDS`, optionally
pick an emoji from the picker, name it, and submit — the agent
trims/transcodes it server-side. Local sounds can overlap when played — this
path requires the Docker image (ffmpeg), not a bare `python agent.py` run.

### CLI

```bash
export SOUND_AGENT_URL=http://<host>:8766
export SOUND_AGENT_KEY=<yourkey>

python soundctl.py channels                 # list servers + voice channels
python soundctl.py join <channel_id>        # join a voice channel
python soundctl.py sounds all               # list every sound
python soundctl.py play <sound_id>          # trigger a sound
python soundctl.py leave                    # disconnect
```

### iCUE widget

Import the `.icuewidget` package in iCUE, add it to your XENEON EDGE layout,
and set the **Agent URL** (and **API Key**, if used) in the widget settings.
Tap the top bar to pick a channel, then tap sounds.

## API

JSON in/out. See [`API.md`](API.md) for the full reference.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/status` | Connection state |
| GET | `/guilds` | Servers and joinable voice channels |
| GET | `/sounds` | Soundboard sounds (per-guild or all) |
| POST | `/join` | Join a voice channel |
| POST | `/leave` | Disconnect |
| POST | `/play` | Trigger a sound |
| POST | `/auth/login`, `/auth/logout` | Dashboard session login/logout |
| GET | `/me` | Current user, role, personal API key |
| POST | `/me/apikey/regenerate` | Rotate your personal API key |
| GET/POST/PATCH/DELETE | `/users` | Admin-only user management |
| GET/POST | `/favorites` | Per-user favorite sounds |
| POST | `/channel-order` | Save your drag-reordered server section order |
| POST | `/me/discord-id` | Link your Discord User ID |
| POST | `/me/join-mine` | Join whatever voice channel your linked Discord account is currently in |
| GET | `/history`, `/stats/top-sounds` | Play history / most-played |
| GET | `/config` | Local-sound upload limits |
| GET/POST/DELETE | `/local-sounds` | Local sound library (upload/list/delete) |
| POST | `/local-sounds/{id}/play` | Play a local sound (overlaps other local sounds) |
| POST | `/sounds/{sound_id}/transfer` | Copy a Discord sound into the local library |
| GET | `/local-sounds/{id}/file` | Download a local sound's audio file |
| GET | `/sounds/{sound_id}/download` | Download a Discord sound's audio file |

## Configuration

Set via environment variables (see `docker-compose.yml`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `DISCORD_TOKEN` | *(required)* | Bot token |
| `PORT` | `8766` | HTTP listen port |
| `API_KEY` | *(empty)* | Shared key; if set, requires `?key=` on legacy routes (`/`, `/guilds`, ...) |
| `IDLE_TIMEOUT_SEC` | `14400` | Auto-disconnect after inactivity (4h) |
| `DATA_DIR` | `/data` | Where `users.json`/`history.jsonl` live (the mounted volume) |
| `ADMIN_USERNAME` | `admin` | Bootstrap dashboard admin username |
| `ADMIN_PASSWORD` | *(random, logged once)* | Bootstrap dashboard admin password. Only used the first time `users.json` doesn't exist yet. |
| `MAX_LOCAL_SOUND_SECONDS` | `15` | Max length of an uploaded/transferred local sound |
| `MAX_UPLOAD_BYTES` | `5000000` | Max upload size for `/local-sounds` |

## Notes

- The bot plays sounds as itself and appears as a member in the voice channel —
  this is inherent to Discord's soundboard API, not a limitation of this project.
- Cross-server playback requires the **Use External Sounds** permission in the
  channel the bot is connected to.
- State is poll-based; there is no push/WebSocket channel. One bot, one voice
  connection, shared across all clients.
- Local sounds bypass **Use External Sounds** entirely, since they're streamed
  directly into voice rather than triggered via Discord's soundboard RPC —
  transferring a sound is a handy workaround if that permission isn't set.
- Local-sound playback and upload/transfer require `ffmpeg`, which is only
  installed in the Docker image — a bare `python agent.py` run can serve the
  dashboard, but local-sound endpoints will fail cleanly instead of crashing.

## Stack

Python · discord.py · aiohttp · ffmpeg · Docker · Corsair iCUE SDK

## License

MIT
