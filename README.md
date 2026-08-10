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
  on the network is a full soundboard with zero install.
- **CLI** (`soundctl.py`) — stdlib-only command-line client for scripting and
  testing.

## Features

- Cross-server sounds: play any sound from any server the bot is in, into
  whichever channel it's currently sitting in
- Auto-fitting tile grid that scales from a handful of sounds to a hundred
- Custom + unicode emoji icons resolved from Discord's CDN
- Manual join/leave, never auto-joins, with 4-hour idle auto-disconnect
- Survives restarts: detects the bot's existing voice presence instead of
  asking you to rejoin
- Optional API key for LAN deployments
- Documented HTTP API ([`API.md`](API.md)) for building your own integrations

## Quick start

Create a bot application at <https://discord.com/developers>, then invite it to
your server with these permissions:

- Connect
- Speak
- Use Soundboard
- Use External Sounds
- View Channels

Set your token in `docker-compose.yml` (and an optional `API_KEY`), then:

```bash
docker compose up -d --build
```

Open `http://<host>:8766/?key=<yourkey>`, pick a channel, and tap a sound.

## Usage

### Browser

Navigate to `http://<host>:8766/?key=<yourkey>`. Pick a channel from the
dropdown, hit **Join**, and tap sounds. **Leave** disconnects the bot.

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

Six endpoints, JSON in/out. See [`API.md`](API.md) for the full reference.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/status` | Connection state |
| GET | `/guilds` | Servers and joinable voice channels |
| GET | `/sounds` | Soundboard sounds (per-guild or all) |
| POST | `/join` | Join a voice channel |
| POST | `/leave` | Disconnect |
| POST | `/play` | Trigger a sound |

## Configuration

Set via environment variables (see `docker-compose.yml`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `DISCORD_TOKEN` | *(required)* | Bot token |
| `PORT` | `8766` | HTTP listen port |
| `API_KEY` | *(empty)* | If set, requires `?key=` on every request |
| `IDLE_TIMEOUT_SEC` | `14400` | Auto-disconnect after inactivity (4h) |

## Notes

- The bot plays sounds as itself and appears as a member in the voice channel —
  this is inherent to Discord's soundboard API, not a limitation of this project.
- Cross-server playback requires the **Use External Sounds** permission in the
  channel the bot is connected to.
- State is poll-based; there is no push/WebSocket channel. One bot, one voice
  connection, shared across all clients.

## Stack

Python · discord.py · aiohttp · Docker · Corsair iCUE SDK

## License

MIT
