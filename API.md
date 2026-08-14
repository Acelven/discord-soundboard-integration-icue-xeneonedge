# Discord Soundboard Agent — Local API Reference

A small HTTP service (aiohttp) that fronts a Discord bot. It joins a voice
channel on request and triggers your server's soundboard sounds. Any local
app can drive it over plain HTTP + JSON.

- **Base URL:** `http://<host>:8766` (default port `8766`, override with `PORT`)
- **Content type:** all responses are `application/json` (except `GET /`, which returns HTML)
- **Bodies:** POST bodies are JSON. The service parses the raw request body with
  `json.loads`; a `Content-Type` header is not required.
- **CORS:** every response includes `Access-Control-Allow-Origin: *`,
  `Access-Control-Allow-Private-Network: true`, and allows `GET, POST, OPTIONS`.
  Preflight `OPTIONS` on any path returns `200`.

---

## Authentication

The endpoints below split into two groups:

- **Legacy routes** (`GET /`, `/status`, `/guilds`, `/sounds`, `POST /join`,
  `/leave`, `/play`) — auth is optional, controlled by the `API_KEY`
  environment variable on the agent.
  - If `API_KEY` is **unset/empty**: no auth required.
  - If `API_KEY` is **set**: every request must include a key as a query
    parameter `?key=<key>` — either the shared `API_KEY`, or any user's
    personal API key (see below). There is no header-based auth.
- **Dashboard/account routes** (`/auth/*`, `/me`, `/me/apikey/regenerate`,
  `/users`, `/favorites`, `/history`, `/stats/top-sounds`) — always require
  an identified user, regardless of whether `API_KEY` is set: either a
  logged-in session cookie (set by `POST /auth/login`), or `?key=<personal
  API key>`. The shared `API_KEY` alone does **not** work here since it
  doesn't identify a user.

A missing or wrong key/session returns:

```
HTTP 401
{ "error": "bad api key" }        // legacy routes
{ "error": "login required" }     // dashboard/account routes
```

Admin-only routes (`/users`) return `403 { "error": "admin required" }` for a
non-admin.

### Personal API keys

Every dashboard user has their own API key (`GET /me` → `api_key`), separate
from the shared `API_KEY`. Requests authenticated with a personal key are
attributed to that user — history rows, `uploaded_by`, etc. use it — which is
what scripts/tools like a hotkey client should authenticate with instead of
the shared key. Regenerate it any time via `POST /me/apikey/regenerate`
(invalidates the old one immediately).

---

## Conventions

- **Success shape:** action endpoints return `{ "ok": true, ... }`.
- **Error shape:** `{ "error": "<message>" }` with an HTTP `500` (runtime errors)
  or `401` (auth). The message is the stringified exception; treat it as
  human-readable, not a stable machine code.
- **IDs** (guild, channel, sound) are returned as **strings**, and should be
  sent back as strings.
- **Voice model:** the bot's live voice connection is the source of truth. After
  an agent restart the audio client is gone but Discord may still show the bot
  in a channel ("ghost presence"). The agent detects this and reports the
  channel as connected, with `audio_live: false`. The next `POST /play` (or
  `POST /leave`) transparently reconnects the audio client.

---

## Endpoints

### GET /

Serves the built-in browser control page (HTML). Not useful for programmatic
integration; listed for completeness. Honors the API key.

---

### GET /status

Current connection state. Poll this (the widget polls every ~5s).

**Request**

```
GET /status?key=<API_KEY>
```

**Response `200`**

```json
{
  "ready": true,
  "voice_connected": true,
  "audio_live": true,
  "channel": "General",
  "guild": "Blasted Alliance"
}
```

| Field | Type | Meaning |
|---|---|---|
| `ready` | bool | Discord gateway connected and the bot is logged in. |
| `voice_connected` | bool | Bot is in a voice channel (live audio **or** recovered ghost presence). |
| `audio_live` | bool | `true` = audio client active; `false` = shown in channel but audio will reconnect on next action. |
| `channel` | string \| null | Voice channel name, or `null` if not connected. |
| `guild` | string \| null | Server name, or `null` if not connected. |

---

### GET /guilds

Every server the bot is in, with the voice channels it has permission to join.
Use this to build a channel picker.

**Request**

```
GET /guilds?key=<API_KEY>
```

**Response `200`**

```json
{
  "guilds": [
    {
      "id": "300367432680210442",
      "name": "Blasted Alliance",
      "channels": [
        { "id": "300373053794418689", "name": "Pajetum", "connected": false },
        { "id": "1269335261435789314", "name": "SF6", "connected": true }
      ]
    }
  ]
}
```

Only voice channels where the bot has the Connect permission are included.
`connected` marks the channel the bot is currently in (or shown in).

---

### POST /join

Join (or move to) a voice channel. This is the only way the bot enters voice —
it never auto-joins on startup.

**Request**

```
POST /join?key=<API_KEY>
Content-Type: application/json

{ "channel_id": "1269335261435789314" }
```

| Body field | Type | Required | Notes |
|---|---|---|---|
| `channel_id` | string | yes | A voice channel ID from `GET /guilds`. |

**Response `200`**

```json
{ "ok": true, "channel": "SF6", "guild": "Blasted Alliance" }
```

**Errors** — `500` with `{ "error": "channel not found or not a voice channel" }`
if the ID is wrong or the bot can't see it.

---

### POST /leave

Disconnect from voice. If the bot only has a recovered ghost presence (no live
audio client after a restart), the agent briefly reconnects to issue a clean
disconnect so Discord removes the lingering presence.

**Request**

```
POST /leave?key=<API_KEY>
```

Body is ignored (send `{}` or nothing).

**Response `200`**

```json
{ "ok": true }
```

---

### GET /sounds

List soundboard sounds. Returns the connected guild's sounds by default, a
specific guild's, or every guild's merged. Discord's default sounds are
appended to every successful listing (they are absent from the not-in-voice
empty response).

**Request**

```
GET /sounds?key=<API_KEY>
GET /sounds?key=<API_KEY>&guild_id=all
GET /sounds?key=<API_KEY>&guild_id=300367432680210442
```

| Query param | Values | Behavior |
|---|---|---|
| `guild_id` | *(omitted)* | Sounds from the guild the bot is currently connected to. If not in voice, returns `{ "sounds": [], "note": "not in voice" }`. |
| `guild_id` | `all` | Merge sounds from **every** guild the bot is in. Each sound is tagged with its `guild_name`. |
| `guild_id` | a guild ID | Sounds from that specific guild. |

**Response `200`**

```json
{
  "sounds": [
    {
      "sound_id": "1269341234567890",
      "name": "Airhorn",
      "volume": 1.0,
      "emoji_id": null,
      "emoji_name": "📯",
      "guild_id": "300367432680210442",
      "guild_name": "Blasted Alliance",
      "default": false,
      "available": true
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `sound_id` | string | Pass to `POST /play`. |
| `name` | string | Display name. |
| `volume` | number | 0.0–1.0, the sound's configured volume. |
| `emoji_id` | string \| null | Custom emoji ID. If set, icon is at `https://cdn.discordapp.com/emojis/<emoji_id>.png` (append `?size=96` etc.). |
| `emoji_name` | string \| null | Unicode emoji character, when the sound uses a standard emoji instead of a custom one. |
| `guild_id` | string \| null | Home server of the sound. |
| `guild_name` | string \| null | Home server name (populated in `guild_id=all` and single-guild modes; `null` for default sounds). |
| `default` | bool | `true` for Discord's built-in default sounds. |
| `available` | bool | Whether the sound is currently usable. |

**Icon resolution rule:** if `emoji_id` is non-null, load the CDN PNG above;
otherwise render `emoji_name` as a text glyph. Sounds may have neither, in which
case supply your own fallback.

---

### POST /play

Play a sound into the currently connected voice channel. The bot must be in
voice (via `POST /join`, or a recovered presence). If only a ghost presence
exists, the audio client reconnects automatically before playing.

Cross-server playback works: a `sound_id` from any guild the bot is in will
play, provided the bot has **Use External Sounds** in the connected channel.

**Request**

```
POST /play?key=<API_KEY>
Content-Type: application/json

{ "sound_id": "1269341234567890" }
```

| Body field | Type | Required | Notes |
|---|---|---|---|
| `sound_id` | string | yes | From `GET /sounds`. |

**Response `200`**

```json
{ "ok": true }
```

**Errors** — `500` with `{ "error": "not connected to a voice channel - pick one first" }`
if not in voice, or `{ "error": "unknown sound_id ..." }` if the ID isn't found
in any guild's soundboard or the defaults.

---

## Dashboard & account endpoints

These back the `/dashboard` UI but are plain JSON endpoints usable directly.
See Authentication above — all of these require an identified user.

### POST /auth/login

```
POST /auth/login
{ "username": "bob", "password": "..." }
```

`200 { "ok": true, "username": "bob", "role": "member" }` and sets a
signed, httpOnly `session` cookie. `401 { "error": "invalid credentials" }`
on failure. Sessions last 12h and are invalidated on agent restart (the
signing secret is generated fresh each process start).

### POST /auth/logout

Clears the session cookie. `200 { "ok": true }`.

### GET /me

Current user's profile. `200 { "username", "role", "api_key", "favorites": [sound_id...] }`.

### POST /me/apikey/regenerate

Rotates the caller's personal API key; the old one stops working immediately.
`200 { "ok": true, "api_key": "<new key>" }`.

### GET/POST/PATCH/DELETE /users

Admin-only (`403` otherwise).

- `GET /users` → `{ "users": [{ "username", "role", "created_at" }] }`
  (no password hashes or API keys — those are self-service via `GET /me`).
- `POST /users` body `{ "username", "password", "role": "admin"|"member" }`
  → `200 { "ok": true }`, `409` if the username exists.
- `PATCH /users/{username}` body `{ "password"?, "role"? }` → `200 { "ok": true }`.
- `DELETE /users/{username}` → `200 { "ok": true }`. `400` if deleting
  yourself or the last remaining admin.

### GET/POST /favorites

- `GET /favorites` → `{ "favorites": [sound_id...] }` for the caller.
- `POST /favorites` body `{ "sound_id", "action": "add"|"remove" }` →
  `{ "ok": true, "favorites": [...] }`.

### POST /channel-order

Saves the caller's drag-reordered server section order (used for both the
sounds picker and the "All Sounds" sectioning in the dashboard).

```
POST /channel-order
{ "order": ["Guild B", "Guild A"] }
```

`200 { "ok": true, "channel_order": [...] }`. `400` if `order` isn't a list
of strings. Servers not present in the saved order (e.g. the bot just joined
one) are appended at the end automatically — no need to include everything.
Also returned as `channel_order` in `GET /me`.

### POST /me/discord-id

Links the caller's Discord User ID, used by `POST /me/join-mine` below.

```
POST /me/discord-id
{ "discord_user_id": "123456789012345678" }
```

`200 { "ok": true, "discord_user_id": "123456789012345678" }`. `400` if the
value isn't purely numeric. Send an empty string to unlink. Also returned as
`discord_user_id` (`null` if unlinked) in `GET /me`.

### POST /me/join-mine

Finds whichever voice channel the caller's linked Discord account is
currently in — checked across every guild the bot is in — and joins it, the
same as a manual `POST /join` with that channel's id. Doesn't require the
privileged Members intent: it uses the bot's already-enabled `voice_states`
intent plus a targeted per-user lookup (cache or a single REST fetch), not a
full member list.

`200 { "ok": true, "channel": "...", "guild": "..." }`. `400` if the caller
hasn't linked a Discord User ID yet. `404` if the linked account isn't
currently visible in a voice channel in any guild the bot is in.

### GET /history

```
GET /history?limit=50
```

Most recent plays first, across all users. Only plays made by an identified
user (personal API key or session, not the shared `API_KEY`) are logged.

```json
{ "history": [
  { "ts": 1734000000.1, "username": "bob", "sound_id": "...", "name": "Airhorn", "guild_name": "Blasted Alliance" }
] }
```

Capped at the last 500 plays.

### GET /stats/top-sounds

Most-played sounds, aggregated from history.

```json
{ "top": [{ "sound_id": "...", "name": "Airhorn", "count": 12 }] }
```

---

## Local sound library

Sounds that live outside Discord's soundboard entirely: uploaded clips, or
copies transferred from a Discord sound. Played by streaming directly into
the bot's voice connection (not Discord's soundboard RPC), so — unlike
`POST /play` — multiple local sounds can overlap, and playback doesn't
require the **Use External Sounds** permission. Requires `ffmpeg` in the
agent's environment (present in the Docker image; a bare `python agent.py`
run will return clean `500` errors from these endpoints instead).

### GET /config

```json
{ "max_local_sound_seconds": 15, "max_upload_bytes": 5000000 }
```

Limits enforced server-side regardless of what a client sends; useful for a
client-side upload UI to match them.

### GET /local-sounds

```json
{ "sounds": [
  { "id": "a1b2c3d4e5f6a7b8", "name": "Airhorn", "emoji": null,
    "duration_sec": 4.2, "uploaded_by": "bob", "created_at": 1734000000.1,
    "origin_sound_id": null }
] }
```

`origin_sound_id` is set when the entry came from `POST /sounds/{id}/transfer`
rather than a direct upload.

### POST /local-sounds

Multipart form (not JSON) — the client only supplies where to trim; the
agent does the actual cut/transcode via `ffmpeg`, never trusting a
client-side encode.

| Field | Required | Notes |
|---|---|---|
| `file` | yes | The original audio file. |
| `name` | yes | Display name. |
| `emoji` | no | A single emoji shown on the tile. |
| `start_sec` | yes | Trim start, in seconds. |
| `end_sec` | yes | Trim end, in seconds. Must be `> start_sec`. |

`end_sec - start_sec` is capped at `max_local_sound_seconds` server-side, and
the upload is rejected above `max_upload_bytes`. `200 { "ok": true, "sound": {...} }`
on success; `400` for a missing name or invalid range, `413` for an
oversized file, `500` if `ffmpeg` fails or isn't available.

### DELETE /local-sounds/{id}

Removes the entry and its audio file. Allowed for the uploader or an admin;
`403` otherwise, `404` if the id doesn't exist.

### POST /local-sounds/{id}/play

Streams the clip into the bot's current voice connection, overlapping any
other local sound already playing. `404` if the id is unknown or its audio
file is missing; `500` (with a human-readable `error`) if the bot isn't
connected to voice.

### POST /sounds/{sound_id}/transfer

Copies a Discord soundboard sound's audio into the local library —
**the original is left untouched on Discord**, this only ever adds a local
copy. Fetches the audio from Discord's CDN using the bot's own token, then
transcodes it with `ffmpeg`, capped at `max_local_sound_seconds`.

`200 { "ok": true, "sound": {...} }` with `origin_sound_id` set to
`sound_id`. `502` if the CDN fetch fails, `500` if the transcode fails.

### GET /local-sounds/{id}/file

Downloads a local sound's raw audio file (`audio/mpeg`, `Content-Disposition:
attachment`). `404` if the id or its audio file is missing.

### GET /sounds/{sound_id}/download

Downloads a Discord soundboard sound's audio, proxied live from Discord's CDN
using the bot's own token — unlike `/transfer`, this doesn't persist anything
server-side, it's a one-off download. `502` if the CDN fetch fails.

---

## Environment variables (agent side)

| Var | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token. |
| `PORT` | `8766` | HTTP listen port. Binds `0.0.0.0`. |
| `API_KEY` | *(empty)* | Shared key; if set, requires `?key=` on legacy routes. |
| `IDLE_TIMEOUT_SEC` | `14400` | Auto-disconnect after this many seconds with no join/play activity (4h default). |
| `DATA_DIR` | `/data` | Where `users.json` / `history.jsonl` / `local_sounds/` are stored. |
| `ADMIN_USERNAME` | `admin` | Bootstrap dashboard admin username (first boot only). |
| `ADMIN_PASSWORD` | *(random, logged once)* | Bootstrap dashboard admin password (first boot only). |
| `MAX_LOCAL_SOUND_SECONDS` | `15` | Max length of an uploaded/transferred local sound. |
| `MAX_UPLOAD_BYTES` | `5000000` | Max upload size for `POST /local-sounds`. |

---

## Integration examples

### Bash / curl

```bash
BASE="http://192.168.1.8:8766"
KEY="yourkey"

# status
curl -s "$BASE/status?key=$KEY"

# list all sounds across servers
curl -s "$BASE/sounds?key=$KEY&guild_id=all"

# join a channel
curl -s -X POST "$BASE/join?key=$KEY" -d '{"channel_id":"1269335261435789314"}'

# play a sound
curl -s -X POST "$BASE/play?key=$KEY" -d '{"sound_id":"1269341234567890"}'

# leave
curl -s -X POST "$BASE/leave?key=$KEY" -d '{}'
```

### Python

```python
import requests

BASE = "http://192.168.1.8:8766"
KEY = "yourkey"
p = {"key": KEY}

def status():
    return requests.get(f"{BASE}/status", params=p).json()

def sounds(scope="all"):
    q = dict(p, guild_id=scope) if scope else p
    return requests.get(f"{BASE}/sounds", params=q).json()["sounds"]

def join(channel_id):
    return requests.post(f"{BASE}/join", params=p,
                         json={"channel_id": channel_id}).json()

def play(sound_id):
    return requests.post(f"{BASE}/play", params=p,
                         json={"sound_id": sound_id}).json()

def leave():
    return requests.post(f"{BASE}/leave", params=p, json={}).json()
```

### JavaScript (browser or Node)

```javascript
const BASE = "http://192.168.1.8:8766";
const KEY = "yourkey";
const q = (path) => `${BASE}${path}${path.includes("?") ? "&" : "?"}key=${KEY}`;

const status = () => fetch(q("/status")).then(r => r.json());
const sounds = () => fetch(q("/sounds?guild_id=all")).then(r => r.json());
const join = (id) => fetch(q("/join"), { method: "POST", body: JSON.stringify({ channel_id: id }) }).then(r => r.json());
const play = (id) => fetch(q("/play"), { method: "POST", body: JSON.stringify({ sound_id: id }) }).then(r => r.json());
const leave = () => fetch(q("/leave"), { method: "POST", body: "{}" }).then(r => r.json());
```

---

## Typical flow

1. `GET /status` — are we connected? (`voice_connected`)
2. If not, `GET /guilds` → let the user pick → `POST /join` with the `channel_id`.
3. `GET /sounds?guild_id=all` — render tiles; resolve icons via `emoji_id`/`emoji_name`.
4. `POST /play` with a `sound_id` on user action.
5. `POST /leave` when done (or rely on `IDLE_TIMEOUT_SEC`).

## Notes & limits

- **No WebSocket / events.** State is poll-based; there is no push channel.
- **Last-write-wins** on join/leave; concurrent callers share one bot, one voice
  connection. There is no per-caller session.
- **No rate limiting** in the agent itself, but Discord rate-limits soundboard
  sends server-side. Rapid `POST /play` bursts may be dropped by Discord.
- Error messages are human-readable strings, not stable codes — match on HTTP
  status (`401` auth, `500` runtime) rather than message text.
