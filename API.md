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

Optional, controlled by the `API_KEY` environment variable on the agent.

- If `API_KEY` is **unset/empty**: no auth required.
- If `API_KEY` is **set**: every request must include the key as a query
  parameter `?key=<API_KEY>`. There is no header-based auth.

A missing or wrong key returns:

```
HTTP 401
{ "error": "bad api key" }
```

The key applies to every endpoint including `GET /`.

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

## Environment variables (agent side)

| Var | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token. |
| `PORT` | `8766` | HTTP listen port. Binds `0.0.0.0`. |
| `API_KEY` | *(empty)* | If set, requires `?key=` on every request. |
| `IDLE_TIMEOUT_SEC` | `14400` | Auto-disconnect after this many seconds with no join/play activity (4h default). |

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
