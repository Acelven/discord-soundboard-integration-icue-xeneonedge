# agent.py - Discord soundboard agent v2 (Docker / Unraid friendly)
#
# HTTP API (default port 8766, bind 0.0.0.0 for container use):
#   GET  /guilds   -> servers the bot is in + their voice channels
#   POST /join     -> {"channel_id": "..."} bot joins that voice channel
#   POST /leave    -> disconnect from voice
#   GET  /sounds   -> soundboard of the connected guild (+ defaults)
#                     optional ?guild_id= to pull another guild's sounds
#   POST /play     -> {"sound_id": "..."}
#   GET  /status   -> connection state
#
# Env vars:
#   DISCORD_TOKEN   (required) bot token
#   PORT            default 8766
#   API_KEY         optional; if set, requests must include ?key=<API_KEY>
#                   after container restarts
#
# The widget's "Agent URL" then points at http://<acenas>:8766 (append
# ?key=... via the widget's API Key setting if you set one).

import os
import json
import time
import hmac
import hashlib
import secrets
import tempfile
import asyncio
from array import array
import discord
import aiohttp
from aiohttp import web

TOKEN = os.environ.get("DISCORD_TOKEN", "")
PORT = int(os.environ.get("PORT", "8766"))
API_KEY = os.environ.get("API_KEY", "")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
HISTORY_MAX_ROWS = 500
SESSION_TTL_SEC = 12 * 3600
SESSION_SECRET = secrets.token_bytes(32)  # in-memory; a restart invalidates sessions

LOCAL_SOUNDS_DIR = os.path.join(DATA_DIR, "local_sounds")
LOCAL_SOUNDS_INDEX = os.path.join(LOCAL_SOUNDS_DIR, "index.json")
MAX_LOCAL_SOUND_SECONDS = float(os.environ.get("MAX_LOCAL_SOUND_SECONDS", "15"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(5_000_000)))

intents = discord.Intents.default()


# ---------- user store (no DB - a JSON file under /data) ----------


def hash_password(password, salt_hex=None):
    salt_hex = salt_hex or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return salt_hex, digest.hex()


def verify_password(password, salt_hex, hash_hex):
    _, computed = hash_password(password, salt_hex)
    return hmac.compare_digest(computed, hash_hex)


def load_users():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(users):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, USERS_FILE)


def find_user(users, username):
    for u in users:
        if u["username"] == username:
            return u
    return None


def find_user_by_apikey(users, key):
    for u in users:
        if u.get("api_key") == key:
            return u
    return None


def bootstrap_admin():
    if os.path.exists(USERS_FILE):
        return
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD")
    generated = not password
    password = password or secrets.token_urlsafe(12)
    salt, h = hash_password(password)
    save_users([{
        "username": username,
        "salt": salt,
        "hash": h,
        "api_key": secrets.token_urlsafe(32),
        "role": "admin",
        "favorites": [],
        "channel_order": [],
        "created_at": time.time(),
    }])
    if generated:
        print(f"Bootstrap admin created: {username} / {password}  (set ADMIN_PASSWORD to control this; change it after first login)")
    else:
        print(f"Bootstrap admin created: {username}")


def make_session(username):
    expiry = str(int(time.time()) + SESSION_TTL_SEC)
    msg = f"{username}:{expiry}"
    sig = hmac.new(SESSION_SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}:{sig}"


def verify_session(cookie_value):
    try:
        username, expiry, sig = cookie_value.split(":", 2)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(SESSION_SECRET, f"{username}:{expiry}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    if int(expiry) < time.time():
        return None
    return username


def append_history(username, sound_id, name, guild_name):
    os.makedirs(DATA_DIR, exist_ok=True)
    row = json.dumps({
        "ts": time.time(), "username": username, "sound_id": sound_id,
        "name": name, "guild_name": guild_name,
    })
    lines = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    lines.append(row + "\n")
    lines = lines[-HISTORY_MAX_ROWS:]
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, HISTORY_FILE)


def read_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    rows = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ---------- local sound library (uploads + sounds transferred from Discord) ----------


def load_local_sounds():
    if not os.path.exists(LOCAL_SOUNDS_INDEX):
        return []
    with open(LOCAL_SOUNDS_INDEX, "r", encoding="utf-8") as f:
        return json.load(f)


def save_local_sounds(sounds):
    os.makedirs(LOCAL_SOUNDS_DIR, exist_ok=True)
    tmp = LOCAL_SOUNDS_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sounds, f, indent=2)
    os.replace(tmp, LOCAL_SOUNDS_INDEX)


def local_sound_path(sound_id):
    return os.path.join(LOCAL_SOUNDS_DIR, f"{sound_id}.mp3")


async def ffmpeg_trim(src_path, out_path, start_sec, duration_sec):
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", src_path, "-ss", str(start_sec), "-t", str(duration_sec),
            "-ar", "48000", "-ac", "2", "-b:a", "128k", out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await proc.wait()
    except OSError as e:
        print("ffmpeg trim failed to start:", e)
        return False
    return rc == 0 and os.path.exists(out_path)


async def find_sound_meta(agent, sound_id):
    # Returns (name, emoji_name). emoji_name is only set for a unicode emoji -
    # a custom per-server emoji (emoji_id) can't be represented as the plain
    # unicode string local sounds store, so it's left None in that case.
    for g in agent.guilds:
        try:
            for s in await agent.guild_sounds(g.id):
                if str(s.id) == sound_id:
                    return s.name, emoji_fields(s)[1]
        except Exception:
            continue
    for s in await agent.get_defaults():
        if str(s.id) == sound_id:
            return s.name, emoji_fields(s)[1]
    return f"sound-{sound_id}", None


async def fetch_discord_sound_bytes(sound_id):
    url = f"https://cdn.discordapp.com/soundboard-sounds/{sound_id}"
    headers = {"Authorization": f"Bot {TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"discord cdn fetch failed ({resp.status})")
            return await resp.read()


def safe_filename(name, fallback):
    cleaned = "".join(c for c in name if c.isalnum() or c in " -_").strip()
    return (cleaned or fallback) + ".mp3"


def emoji_fields(sound):
    e = getattr(sound, "emoji", None)
    if e is None:
        return None, None
    if getattr(e, "id", None):
        return str(e.id), None
    return None, e.name


def sound_to_dict(sound, is_default, guild_id=None, guild_name=None):
    emoji_id, emoji_name = emoji_fields(sound)
    return {
        "sound_id": str(sound.id),
        "name": sound.name,
        "volume": getattr(sound, "volume", 1.0),
        "emoji_id": emoji_id,
        "emoji_name": emoji_name,
        "guild_id": str(guild_id) if guild_id else None,
        "guild_name": guild_name,
        "default": is_default,
        "available": getattr(sound, "available", True),
    }


class MixerSource(discord.AudioSource):
    # The only source ever passed to VoiceClient.play() for local-sound
    # playback. Holds zero or more active FFmpegPCMAudio readers and sums
    # their PCM frames each 20ms tick, so local sounds can overlap - unlike
    # discord.py's VoiceClient, which only plays one AudioSource at a time.
    FRAME_BYTES = 3840  # 20ms of 48kHz stereo 16-bit PCM
    SAMPLES = FRAME_BYTES // 2

    def __init__(self):
        self.readers = []

    def add(self, source):
        self.readers.append(source)

    def read(self):
        if not self.readers:
            return b"\x00" * self.FRAME_BYTES
        mix = [0] * self.SAMPLES
        alive = []
        for r in self.readers:
            frame = r.read()
            if not frame:
                r.cleanup()
                continue
            if len(frame) < self.FRAME_BYTES:
                frame = frame + b"\x00" * (self.FRAME_BYTES - len(frame))
            for i, s in enumerate(array("h", frame[:self.FRAME_BYTES])):
                mix[i] += s
            alive.append(r)
        self.readers = alive
        if not alive:
            return b"\x00" * self.FRAME_BYTES
        return array("h", (max(-32768, min(32767, v)) for v in mix)).tobytes()

    def is_opus(self):
        return False

    def cleanup(self):
        for r in self.readers:
            r.cleanup()
        self.readers = []


class Agent(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.sound_cache = {}  # guild_id -> (timestamp, [sounds])
        self.default_sounds = []
        self.last_activity = None  # monotonic time of last join/play
        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT_SEC", str(4 * 3600)))
        self.ghost_channel = None  # (guild_id, channel_id) bot is shown in but has no live audio client
        self.mixer = None  # MixerSource attached to the current voice client, for local sounds

    # ---------- voice ----------

    def detect_presence(self):
        # After a reconnect the audio client is gone, but Discord still shows
        # the bot in a channel. Read that from the freshly-synced voice state.
        for g in self.guilds:
            me = g.me
            if me and me.voice and me.voice.channel:
                return (g.id, me.voice.channel.id)
        return None

    def status_channel(self):
        # Live audio client wins; otherwise fall back to detected ghost presence.
        vc = self.current_vc()
        if vc:
            return vc.channel
        if self.ghost_channel:
            g = self.get_guild(self.ghost_channel[0])
            if g:
                ch = g.get_channel(self.ghost_channel[1])
                if ch:
                    return ch
        return None

    def current_vc(self):
        for vc in self.voice_clients:
            if vc.is_connected():
                return vc
        return None

    async def join_channel(self, channel_id):
        ch = self.get_channel(int(channel_id))
        if not isinstance(ch, discord.VoiceChannel):
            raise RuntimeError("channel not found or not a voice channel")
        vc = self.current_vc()
        if vc:
            if vc.channel.id == ch.id:
                return ch
            await vc.move_to(ch)
        else:
            await ch.connect(self_deaf=False, self_mute=False)
            self.mixer = None  # fresh voice client - any old mixer is stale
        self.ghost_channel = None
        self.last_activity = asyncio.get_event_loop().time()
        return ch

    async def leave(self):
        vc = self.current_vc()
        if vc:
            await vc.disconnect()
        # If we only had a ghost presence (audio client lost after a restart),
        # reconnect just long enough to issue a clean disconnect so Discord
        # removes the lingering presence.
        elif self.ghost_channel:
            g = self.get_guild(self.ghost_channel[0])
            ch = g.get_channel(self.ghost_channel[1]) if g else None
            if ch:
                try:
                    reconnected = await ch.connect(self_deaf=False, self_mute=False)
                    await reconnected.disconnect()
                except Exception as e:
                    print("ghost leave reconnect failed:", e)
        self.ghost_channel = None
        self.last_activity = None
        self.mixer = None

    async def play_local(self, path):
        vc = self.current_vc()
        if not vc and self.ghost_channel:
            g = self.get_guild(self.ghost_channel[0])
            ch = g.get_channel(self.ghost_channel[1]) if g else None
            if ch:
                try:
                    vc = await ch.connect(self_deaf=False, self_mute=False)
                    self.ghost_channel = None
                    self.mixer = None  # fresh voice client - any old mixer is stale
                except Exception as e:
                    raise RuntimeError("could not reconnect audio to channel: " + str(e))
        if not vc:
            raise RuntimeError("not connected to a voice channel - pick one first")
        if self.mixer is None or not vc.is_playing():
            self.mixer = MixerSource()
            vc.play(self.mixer)
        self.mixer.add(discord.FFmpegPCMAudio(path))
        self.last_activity = asyncio.get_event_loop().time()

    # ---------- sounds ----------

    async def guild_sounds(self, guild_id, force=False):
        guild = self.get_guild(int(guild_id))
        if not guild:
            raise RuntimeError("bot is not in that guild")
        now = asyncio.get_event_loop().time()
        cached = self.sound_cache.get(guild.id)
        if cached and not force and now - cached[0] < 60:
            return cached[1]
        sounds = list(await guild.fetch_soundboard_sounds())
        self.sound_cache[guild.id] = (now, sounds)
        return sounds

    async def get_defaults(self):
        if not self.default_sounds:
            try:
                self.default_sounds = list(await self.fetch_soundboard_default_sounds())
            except Exception as e:
                print("default sounds fetch failed:", e)
        return self.default_sounds

    async def play(self, sound_id):
        vc = self.current_vc()
        if not vc and self.ghost_channel:
            # Presence recovered after a restart but no audio client yet — reconnect.
            g = self.get_guild(self.ghost_channel[0])
            ch = g.get_channel(self.ghost_channel[1]) if g else None
            if ch:
                try:
                    vc = await ch.connect(self_deaf=False, self_mute=False)
                    self.ghost_channel = None
                except Exception as e:
                    raise RuntimeError("could not reconnect audio to channel: " + str(e))
        if not vc:
            raise RuntimeError("not connected to a voice channel - pick one first")
        home = vc.channel.guild
        search_guilds = [home] + [g for g in self.guilds if g.id != home.id]

        async def find(force=False):
            for g in search_guilds:
                try:
                    for s in await self.guild_sounds(g.id, force=force):
                        if str(s.id) == str(sound_id):
                            return s
                except Exception:
                    continue
            for s in await self.get_defaults():
                if str(s.id) == str(sound_id):
                    return s
            return None

        sound = await find()
        if sound is None:  # caches may be stale
            sound = await find(force=True)
        if sound is None:
            raise RuntimeError("unknown sound_id " + str(sound_id))
        self.last_activity = asyncio.get_event_loop().time()
        await vc.channel.send_sound(sound)
        return sound

    async def idle_watchdog(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(60)
            if self.last_activity is None:
                continue
            # Consider both a live audio client and a recovered ghost presence.
            if not self.current_vc() and not self.ghost_channel:
                self.last_activity = None
                continue
            idle = asyncio.get_event_loop().time() - self.last_activity
            if idle >= self.idle_timeout:
                print(f"Idle {int(idle)}s >= {self.idle_timeout}s, disconnecting")
                try:
                    await self.leave()
                except Exception as e:
                    print("idle disconnect failed:", e)

    # ---------- lifecycle ----------

    async def setup_hook(self):
        runner = web.AppRunner(build_app(self))
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        print(f"HTTP API on 0.0.0.0:{PORT} (API key {'ON' if API_KEY else 'off'})")

    async def on_ready(self):
        print(f"Logged in as {self.user} in {len(self.guilds)} guild(s)")
        # Do NOT auto-join. But if Discord still shows the bot sitting in a
        # channel from before a restart, detect and report that instead of
        # pretending we're disconnected.
        self.ghost_channel = self.detect_presence()
        if self.ghost_channel:
            g = self.get_guild(self.ghost_channel[0])
            ch = g.get_channel(self.ghost_channel[1]) if g else None
            print(f"Detected existing presence: {g.name if g else '?'} / {ch.name if ch else '?'}")
            self.last_activity = asyncio.get_event_loop().time()
        self.loop.create_task(self.idle_watchdog())

    async def on_voice_state_update(self, member, before, after):
        # Track our own presence so status stays truthful even without an audio client.
        if member.id != self.user.id:
            return
        if after.channel:
            self.ghost_channel = (after.channel.guild.id, after.channel.id)
        else:
            self.ghost_channel = None


# ---------- HTTP layer ----------


INDEX_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Discord Soundboard</title>
<style>
body{margin:0;font-family:'Segoe UI',Arial,sans-serif;background:#101418;color:#fff;-webkit-user-select:none;user-select:none}
#top{display:flex;align-items:center;gap:10px;padding:10px 14px;position:sticky;top:0;background:#101418;z-index:5}
#dot{width:12px;height:12px;border-radius:50%;background:#666}
#dot.on{background:#00d26a}#dot.off{background:#f03e3e}
select,button{font-size:16px;padding:8px 12px;border-radius:8px;border:none;background:#1e2530;color:#fff}
button{background:#5865f2}
h3{margin:18px 14px 8px;opacity:.5;text-transform:uppercase;letter-spacing:.1em;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;padding:0 14px}
.tile{background:#1e2530;border-radius:10px;padding:10px 4px;display:flex;flex-direction:column;align-items:center;cursor:pointer;transition:background .15s}
.tile:active{transform:scale(.94)}
.tile.ok{background:#14532d}.tile.fail{background:#7f1d1d}
.tile .e{font-size:34px;line-height:1.2;height:40px}
.tile .e img{width:36px;height:36px;object-fit:contain}
.tile .n{font-size:12px;opacity:.85;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style></head><body>
<div id="top"><div id="dot"></div><select id="chan"></select><button id="join">Join</button><button id="leave" style="background:#f03e3e">Leave</button><span id="st"></span></div>
<div id="content"></div>
<script>
const KEY = new URLSearchParams(location.search).get("key") || "";
const q = p => p + (KEY ? (p.includes("?")?"&":"?") + "key=" + encodeURIComponent(KEY) : "");
async function guilds(){
  const d = await (await fetch(q("/guilds"))).json();
  const sel = document.getElementById("chan"); sel.innerHTML = "";
  (d.guilds||[]).forEach(g => g.channels.forEach(c => {
    const o = document.createElement("option");
    o.value = c.id; o.textContent = g.name + " / " + c.name;
    if (c.connected) o.selected = true;
    sel.appendChild(o);
  }));
}
async function status(){
  try{
    const s = await (await fetch(q("/status"))).json();
    document.getElementById("dot").className = s.voice_connected ? "on" : "off";
    document.getElementById("st").textContent = s.voice_connected ? ((s.audio_live ? "" : "~ ") + s.guild+" / "+s.channel) : "not in voice";
  }catch(e){ document.getElementById("dot").className = "off"; }
}
async function sounds(){
  const d = await (await fetch(q("/sounds?guild_id=all"))).json();
  const groups = {}; const order = [];
  (d.sounds||[]).forEach(s => {
    if (s.default) return;
    const k = s.guild_name || "Server";
    if(!groups[k]){groups[k]=[];order.push(k);}
    groups[k].push(s);
  });
  const c = document.getElementById("content"); c.innerHTML = "";
  order.forEach(name => {
    const h = document.createElement("h3"); h.textContent = name; c.appendChild(h);
    const g = document.createElement("div"); g.className = "grid";
    groups[name].forEach(s => {
      const t = document.createElement("div"); t.className = "tile";
      const e = document.createElement("div"); e.className = "e";
      if (s.emoji_id){
        const i = document.createElement("img");
        i.src = "https://cdn.discordapp.com/emojis/" + s.emoji_id + ".png?size=64";
        i.onerror = () => { e.textContent = "\\uD83D\\uDD0A"; };
        e.appendChild(i);
      } else e.textContent = s.emoji_name || "\\uD83D\\uDD0A";
      const n = document.createElement("div"); n.className = "n"; n.textContent = s.name;
      t.appendChild(e); t.appendChild(n);
      t.onclick = async () => {
        try{
          const r = await fetch(q("/play"), {method:"POST", body: JSON.stringify({sound_id: s.sound_id})});
          t.classList.add(r.ok ? "ok" : "fail");
        }catch(e){ t.classList.add("fail"); }
        setTimeout(()=>t.classList.remove("ok","fail"), 500);
      };
      g.appendChild(t);
    });
    c.appendChild(g);
  });
}
document.getElementById("join").onclick = async () => {
  const id = document.getElementById("chan").value;
  await fetch(q("/join"), {method:"POST", body: JSON.stringify({channel_id: id})});
  status(); sounds();
};
document.getElementById("leave").onclick = async () => {
  await fetch(q("/leave"), {method:"POST", body: "{}"});
  status();
};
guilds(); sounds(); status(); setInterval(status, 5000);
</script></body></html>"""


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Sign in - Discord Soundboard</title>
<style>
:root{
  --bg:#0b0d10; --bg-elev1:#12151a; --bg-elev2:#171b21;
  --border:rgba(255,255,255,.07); --text:#e9eaee; --text-faint:#6b7280;
  --accent:#5865f2; --accent-hover:#6b76f5; --danger:#f87171;
  --radius:14px; --radius-sm:9px; --shadow:0 16px 40px rgba(0,0,0,.5);
}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg);color:var(--text);-webkit-user-select:none;user-select:none;padding:20px}
form{background:var(--bg-elev1);border:1px solid var(--border);box-shadow:var(--shadow);
  padding:32px;border-radius:var(--radius);width:280px;display:flex;flex-direction:column;gap:14px}
h1{margin:0 0 4px;font-size:19px;font-weight:600}
p.sub{margin:-10px 0 4px;font-size:12.5px;color:var(--text-faint)}
input{font-family:inherit;font-size:15px;padding:11px 12px;border-radius:var(--radius-sm);
  border:1px solid var(--border);background:var(--bg-elev2);color:var(--text)}
input:focus{outline:none;border-color:var(--accent)}
button{font-family:inherit;font-size:15px;font-weight:500;padding:11px 12px;border-radius:var(--radius-sm);
  border:none;background:var(--accent);color:#fff;cursor:pointer;transition:background .15s}
button:hover{background:var(--accent-hover)}
#err{color:var(--danger);font-size:13px;min-height:16px}
</style></head><body>
<form id="f">
<h1>Discord Soundboard</h1>
<p class="sub">Sign in to the dashboard</p>
<input id="u" placeholder="Username" autocomplete="username" required>
<input id="p" type="password" placeholder="Password" autocomplete="current-password" required>
<div id="err"></div>
<button type="submit">Sign in</button>
</form>
<script>
document.getElementById("f").onsubmit = async (e) => {
  e.preventDefault();
  const r = await fetch("/auth/login", {method:"POST", body: JSON.stringify({
    username: document.getElementById("u").value,
    password: document.getElementById("p").value,
  })});
  if (r.ok) { location.href = "/dashboard"; }
  else { document.getElementById("err").textContent = "Invalid username or password"; }
};
</script></body></html>"""


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>Discord Soundboard - Dashboard</title>
<style>
:root{
  --bg:#0b0d10; --bg-elev1:#12151a; --bg-elev2:#171b21; --bg-elev3:#1f242c;
  --border:rgba(255,255,255,.07); --border-strong:rgba(255,255,255,.16);
  --text:#e9eaee; --text-dim:#9aa1ac; --text-faint:#6b7280;
  --accent:#5865f2; --accent-hover:#6b76f5;
  --danger:#ef4444; --danger-hover:#f2605c; --success:#22c55e;
  --radius:14px; --radius-sm:9px; --shadow:0 16px 40px rgba(0,0,0,.5);
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-user-select:none;user-select:none;overscroll-behavior-y:none}
body.modal-open{overflow:hidden}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bg-elev3);border-radius:6px}
::-webkit-scrollbar-thumb:hover{background:var(--border-strong)}

input{font-family:inherit;font-size:14px;background:var(--bg-elev2);border:1px solid var(--border);
  border-radius:var(--radius-sm);color:var(--text);padding:10px 12px}
input:focus{outline:none;border-color:var(--accent)}
input::placeholder{color:var(--text-faint)}
.btn{font-family:inherit;font-size:14px;font-weight:500;padding:10px 16px;border-radius:var(--radius-sm);
  border:1px solid transparent;background:var(--accent);color:#fff;cursor:pointer;transition:background .15s}
.btn:hover{background:var(--accent-hover)}
.btn.ghost{background:var(--bg-elev2);border-color:var(--border);color:var(--text)}
.btn.ghost:hover{background:var(--bg-elev3);border-color:var(--border-strong)}
.btn.danger{background:var(--danger)}
.btn.danger:hover{background:var(--danger-hover)}
.btn:disabled{opacity:.4;cursor:default;background:var(--bg-elev2)}

#topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:10px;height:56px;
  padding:0 14px;background:rgba(11,13,16,.88);backdrop-filter:blur(10px);border-bottom:1px solid var(--border)}
.icon-btn{width:38px;height:38px;flex:none;display:flex;align-items:center;justify-content:center;
  background:var(--bg-elev2);border:1px solid var(--border);border-radius:var(--radius-sm);
  color:var(--text);cursor:pointer;font-size:17px;transition:background .15s,border-color .15s}
.icon-btn:hover{background:var(--bg-elev3);border-color:var(--border-strong)}
.channel-pill{flex:1;min-width:0;display:flex;align-items:center;gap:9px;height:38px;padding:0 14px;
  background:var(--bg-elev2);border:1px solid var(--border);border-radius:999px;cursor:pointer;
  color:var(--text);font-size:13.5px;transition:border-color .15s}
.channel-pill:hover{border-color:var(--border-strong)}
.channel-pill .label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#dot{width:8px;height:8px;border-radius:50%;background:var(--text-faint);flex:none}
#dot.on{background:var(--success)}
#dot.off{background:var(--danger)}

#drawerBackdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:39}
#drawerBackdrop.show{display:block}
#drawer{position:fixed;top:0;bottom:0;left:0;width:280px;max-width:82vw;background:var(--bg-elev1);
  border-right:1px solid var(--border);z-index:40;transform:translateX(-100%);
  transition:transform .22s ease;display:flex;flex-direction:column;padding:18px 12px;box-shadow:var(--shadow)}
#drawer.show{transform:translateX(0)}
.drawer-user{padding:8px 10px 16px;border-bottom:1px solid var(--border);margin-bottom:10px}
.drawer-name{font-size:16px;font-weight:600}
.drawer-role{font-size:11px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.07em;margin-top:3px}
.drawer-item{display:block;width:100%;text-align:left;padding:13px 10px;border-radius:var(--radius-sm);
  background:none;border:none;color:var(--text);font-family:inherit;font-size:14.5px;cursor:pointer}
.drawer-item:hover{background:var(--bg-elev3)}
.drawer-item.danger{color:var(--danger)}
.drawer-spacer{flex:1}

#tabsWrap{position:sticky;top:56px;z-index:8;background:var(--bg);padding-top:10px;border-bottom:1px solid var(--border)}
#tabSelectRow{display:flex;padding:0 14px 10px}
#tabSelectBtn .chev{margin-left:auto;opacity:.55;font-size:11px;flex:none}
#searchWrap{padding:0 14px 12px}
#search{width:100%}

.section-header{margin:20px 14px 10px;display:flex;align-items:center;gap:8px}
.section-header .label{font-size:11.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim)}
.section-header .count{background:var(--bg-elev3);padding:1px 7px;border-radius:999px;font-size:11px;color:var(--text-faint)}
.section-header .section-action{margin-left:auto;padding:5px 12px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:10px;padding:0 14px 18px}
.tile{position:relative;background:var(--bg-elev2);border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:13px 6px 10px;display:flex;flex-direction:column;align-items:center;gap:6px;cursor:pointer;
  transition:background .15s,border-color .15s,transform .08s}
.tile:hover{border-color:var(--border-strong);background:var(--bg-elev3)}
.tile:active{transform:scale(.95)}
.tile.ok{background:rgba(34,197,94,.18);border-color:rgba(34,197,94,.4)}
.tile.fail{background:rgba(239,68,68,.18);border-color:rgba(239,68,68,.4)}
.tile .e{font-size:29px;line-height:1;height:34px;display:flex;align-items:center;justify-content:center}
.tile .e img{width:32px;height:32px;object-fit:contain}
.tile .n{font-size:12px;color:var(--text-dim);max-width:100%;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;text-align:center}
.tile .fav-badge{position:absolute;top:6px;right:7px;font-size:10px;color:#fbbf24}
.empty{opacity:.55;padding:36px 14px;font-size:13.5px;text-align:center}

.ctx-menu{position:fixed;display:none;background:var(--bg-elev2);border:1px solid var(--border-strong);
  border-radius:var(--radius-sm);padding:6px;min-width:190px;box-shadow:var(--shadow);z-index:50}
.ctx-menu.show{display:block}
.ctx-item{padding:10px 12px;border-radius:6px;font-size:13.5px;cursor:pointer;color:var(--text)}
.ctx-item:hover{background:var(--bg-elev3)}
.ctx-item.disabled{opacity:.4;cursor:default}
.ctx-item.disabled:hover{background:none}
.ctx-item.danger{color:var(--danger)}

#waveCanvas{width:100%;height:80px;background:var(--bg);border-radius:var(--radius-sm);touch-action:none;display:block}
.hint{opacity:.7;font-size:12px}
.emoji-picker{display:none;position:absolute;top:100%;left:0;right:0;margin-top:4px;background:var(--bg-elev3);
  border:1px solid var(--border-strong);border-radius:var(--radius-sm);padding:8px;z-index:30;box-shadow:var(--shadow)}
.emoji-picker.show{display:block}
.emoji-picker input{width:100%;box-sizing:border-box;margin-bottom:6px}
.emoji-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:2px;max-height:160px;overflow-y:auto}
.emoji-grid button{background:none;border:none;font-size:20px;padding:4px 0;border-radius:6px;cursor:pointer}
.emoji-grid button:hover{background:var(--bg-elev2)}
.emoji-grid .none{grid-column:1/-1;opacity:.5;font-size:12px;text-align:center;padding:8px 0}

.overlay{display:none;position:fixed;inset:0;background:rgba(4,5,7,.75);backdrop-filter:blur(2px);z-index:45;
  overflow-y:auto;padding:16px}
.overlay.show{display:flex;align-items:flex-start;justify-content:center}
.panel{width:100%;max-width:440px;margin-top:6vh;background:var(--bg-elev1);border:1px solid var(--border);
  border-radius:var(--radius);padding:22px;display:flex;flex-direction:column;gap:12px;box-shadow:var(--shadow)}
.panel h2{margin:0 0 4px;font-size:16px;font-weight:600}
.panel .close{text-align:center;color:var(--text-dim);padding:8px;cursor:pointer;font-size:13px}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border)}
.row:last-child{border-bottom:none}
.row .meta{font-size:12px;color:var(--text-faint)}
code{background:var(--bg);border:1px solid var(--border);padding:8px 10px;border-radius:var(--radius-sm);
  font-size:12.5px;word-break:break-all;flex:1;color:var(--text-dim)}
.picker-group{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text-faint);
  margin:14px 2px 6px}
.picker-group:first-child{margin-top:0}
.picker-row{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;
  border-radius:var(--radius-sm);background:var(--bg-elev2);border:1px solid var(--border);margin-bottom:6px;
  cursor:pointer;font-size:14px}
.picker-row:hover{border-color:var(--border-strong)}
.picker-row.active{border-color:var(--accent);background:rgba(88,101,242,.14)}
.picker-row.dragging{opacity:.5;border-color:var(--accent)}
.drag-handle{cursor:grab;padding:2px 8px;margin:-8px -6px -8px 6px;opacity:.5;font-size:16px;touch-action:none;flex:none}
.drag-handle:hover{opacity:1}
.drag-handle:active{cursor:grabbing}

@media (max-width:560px){
  .grid{grid-template-columns:repeat(auto-fill,minmax(78px,1fr));gap:8px}
  .panel{margin-top:0;align-self:flex-end;border-radius:var(--radius) var(--radius) 0 0;max-width:100%}
  .overlay.show{align-items:flex-end}
}
</style></head><body>

<div id="topbar">
  <button id="menuBtn" class="icon-btn" aria-label="Menu">☰</button>
  <button id="channelBtn" class="channel-pill">
    <span id="dot"></span><span class="label" id="channelLabel">Select a voice channel</span>
  </button>
</div>

<div id="drawerBackdrop"></div>
<nav id="drawer">
  <div class="drawer-user">
    <div class="drawer-name" id="drawerName"></div>
    <div class="drawer-role" id="drawerRole"></div>
  </div>
  <button class="drawer-item" id="drawerApiKey">API Key</button>
  <button class="drawer-item" id="drawerAdmin" style="display:none">User Management</button>
  <div class="drawer-spacer"></div>
  <button class="drawer-item danger" id="drawerLogout">Log out</button>
</nav>

<div id="tabsWrap">
  <div id="tabSelectRow">
    <button id="tabSelectBtn" class="channel-pill">
      <span class="label" id="tabSelectLabel">All Sounds</span><span class="chev">&#9662; Change</span>
    </button>
  </div>
  <div id="searchWrap"><input id="search" placeholder="Search sounds..."></div>
</div>
<main id="content"></main>

<div class="ctx-menu" id="ctxMenu"></div>

<div class="overlay" id="tabOverlay"><div class="panel">
  <h2>Sounds</h2>
  <div id="tabList"></div>
  <div class="close" id="closeTab">Close</div>
</div></div>

<div class="overlay" id="channelOverlay"><div class="panel">
  <h2>Voice channel</h2>
  <div id="channelList"></div>
  <button class="btn danger" id="leaveBtn" style="display:none">Leave voice</button>
  <div class="close" id="closeChannel">Close</div>
</div></div>

<div class="overlay" id="meOverlay"><div class="panel">
  <h2>My API key</h2>
  <div class="row"><code id="meKey"></code></div>
  <button class="btn ghost" id="regenKey">Regenerate key</button>
  <div class="close" id="closeMe">Close</div>
</div></div>

<div class="overlay" id="adminOverlay"><div class="panel">
  <h2>Users</h2>
  <div id="userRows"></div>
  <form id="newUserForm" style="display:flex;gap:6px;flex-wrap:wrap">
    <input id="newUsername" placeholder="username" required style="flex:1;min-width:100px">
    <input id="newPassword" placeholder="password" required style="flex:1;min-width:100px">
    <select id="newRole"><option value="member">member</option><option value="admin">admin</option></select>
    <button class="btn" type="submit">Add user</button>
  </form>
  <div class="close" id="closeAdmin">Close</div>
</div></div>

<div class="overlay" id="uploadOverlay"><div class="panel">
  <h2>Upload sound</h2>
  <input type="file" id="uploadFile" accept="audio/*">
  <canvas id="waveCanvas" width="400" height="80"></canvas>
  <div id="trimInfo" class="hint">select a file</div>
  <button id="previewBtn" class="btn ghost" disabled>▶ Preview selection</button>
  <audio id="previewAudio" style="display:none"></audio>
  <input id="uploadName" placeholder="name" maxlength="32">
  <div style="position:relative">
    <button type="button" id="emojiTrigger" class="btn ghost" style="width:100%;text-align:left">Pick emoji (optional)</button>
    <div id="emojiPicker" class="emoji-picker">
      <input id="emojiSearch" placeholder="Search emoji...">
      <div id="emojiGrid" class="emoji-grid"></div>
    </div>
  </div>
  <button id="uploadSubmit" class="btn">Upload</button>
  <div class="close" id="closeUpload">Close</div>
</div></div>

<script>
let me = null;
let allSounds = [];
let localSounds = [];
let favorites = [];
let channelOrder = [];  // saved per-user order of server section names
let history = [];
let activeTab = null;
let searchTerm = "";
let config = {max_local_sound_seconds: 15, max_upload_bytes: 5000000};
let waveBuffer = null, audioDuration = 0, trimStart = 0, trimEnd = 0, dragging = null, selectedFile = null;
let previewUrl = null, previewTimer = null;
let viewStart = 0, viewEnd = 0;  // visible waveform window (seconds) - zooms in while dragging a handle
let selectedEmoji = "";

const KEY_ALL = "All Sounds";
const KEY_FAV = "\\u2B50 Favorites";
const KEY_RECENT = "\\u{1F550} Recent";
const KEY_LOCAL = "\\uD83C\\uDFB5 Local";

const EMOJI_LIST = [
  ["😀","grin"], ["😂","laugh lol joy"], ["🤣","rofl laugh"], ["😅","sweat nervous"],
  ["😆","laughing"], ["😉","wink"], ["😎","cool sunglasses"], ["😭","cry sad"],
  ["😱","scream shocked"], ["🙄","eyeroll"], ["😡","angry mad rage"], ["🥳","party celebrate"],
  ["🤯","mindblown explode"], ["😴","sleep tired"], ["🤡","clown"], ["👻","ghost spooky"],
  ["💀","skull dead"], ["☠️","skull crossbones danger"], ["👽","alien"], ["🤖","robot beep"],
  ["👍","thumbsup yes good"], ["👎","thumbsdown no bad"], ["👏","clap applause"], ["🙌","raise hands hooray"],
  ["🤝","handshake deal"], ["✌️","peace"], ["🤙","shaka call"], ["💪","muscle strong flex"],
  ["🎉","party tada confetti"], ["🔥","fire lit hot"], ["💯","hundred perfect"], ["⭐","star"],
  ["✨","sparkles magic"], ["💥","boom explosion hit"], ["⚡","zap lightning bolt"], ["🎶","music notes"],
  ["🎵","note music"], ["📯","horn airhorn"], ["🔊","loud speaker volume"], ["🔔","bell ring"],
  ["🚨","siren alarm police"], ["🎺","trumpet"], ["🥁","drum"], ["🎸","guitar rock"],
  ["🐸","frog"], ["🐶","dog woof"], ["🐱","cat meow"], ["🦆","duck quack"],
  ["🐔","chicken cluck"], ["🦉","owl hoot"], ["🐍","snake hiss"], ["🐷","pig oink"],
  ["💩","poop"], ["👑","crown king"], ["🏆","trophy win"], ["❤️","heart love"],
  ["💔","broken heart sad"], ["❓","question"], ["❗","exclamation alert"], ["⏰","alarm clock time"],
  ["🚀","rocket launch"], ["🎃","pumpkin halloween"], ["🧠","brain smart"], ["😈","devil evil"],
];

function openOverlay(id){
  document.getElementById(id).classList.add("show");
  document.body.classList.add("modal-open");
}
function closeOverlay(id){
  document.getElementById(id).classList.remove("show");
  document.body.classList.remove("modal-open");
}

async function api(path, opts){
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/dashboard"; throw new Error("unauthenticated"); }
  return r;
}

async function loadMe(){
  const d = await (await api("/me")).json();
  me = d;
  favorites = d.favorites || [];
  channelOrder = d.channel_order || [];
  document.getElementById("drawerName").textContent = d.username;
  document.getElementById("drawerRole").textContent = d.role;
  document.getElementById("drawerAdmin").style.display = d.role === "admin" ? "" : "none";
  document.getElementById("meKey").textContent = d.api_key;
}

let channelsData = [];

async function fetchGuilds(){
  const d = await (await api("/guilds")).json();
  channelsData = d.guilds || [];
}

function renderChannelList(){
  const list = document.getElementById("channelList"); list.innerHTML = "";
  let anyConnected = false;
  channelsData.forEach(g => {
    const gh = document.createElement("div"); gh.className = "picker-group"; gh.textContent = g.name;
    list.appendChild(gh);
    g.channels.forEach(c => {
      const row = document.createElement("div");
      row.className = "picker-row" + (c.connected ? " active" : "");
      row.textContent = c.name;
      if (c.connected) anyConnected = true;
      row.onclick = async () => {
        await api("/join", {method:"POST", body: JSON.stringify({channel_id: c.id})});
        closeOverlay("channelOverlay");
        refreshStatus(); fetchSounds();
      };
      list.appendChild(row);
    });
  });
  document.getElementById("leaveBtn").style.display = anyConnected ? "" : "none";
}

document.getElementById("channelBtn").onclick = () => {
  openOverlay("channelOverlay");
  fetchGuilds().then(renderChannelList);
};
document.getElementById("closeChannel").onclick = () => closeOverlay("channelOverlay");
document.getElementById("leaveBtn").onclick = async () => {
  await api("/leave", {method:"POST", body: "{}"});
  closeOverlay("channelOverlay");
  refreshStatus();
};

async function refreshStatus(){
  try{
    const s = await (await api("/status")).json();
    document.getElementById("dot").className = s.voice_connected ? "on" : "off";
    document.getElementById("channelLabel").textContent = s.voice_connected
      ? (s.audio_live === false ? "~ " : "") + (s.guild ? s.guild + " / " : "") + (s.channel || "connected")
      : "Select a voice channel";
  }catch(e){
    document.getElementById("dot").className = "off";
    document.getElementById("channelLabel").textContent = "agent offline";
  }
}

async function fetchSounds(){
  const d = await (await api("/sounds?guild_id=all")).json();
  allSounds = (d.sounds||[]).filter(s => !s.default);
  render();
}

async function fetchHistory(){
  try{
    const d = await (await api("/history?limit=30")).json();
    history = d.history || [];
  }catch(e){ history = []; }
  render();
}

async function fetchConfig(){
  try{ config = await (await api("/config")).json(); }catch(e){}
}

async function fetchLocalSounds(){
  try{
    const d = await (await api("/local-sounds")).json();
    localSounds = d.sounds || [];
  }catch(e){ localSounds = []; }
  render();
}

function combinedSounds(){
  const discordItems = allSounds.map(s => Object.assign({is_local:false}, s));
  const localItems = localSounds.map(s => ({
    sound_id: "local:" + s.id, local_id: s.id, name: s.name,
    emoji_id: null, emoji_name: s.emoji || "\\uD83C\\uDFB5",
    guild_name: null, is_local: true, uploaded_by: s.uploaded_by,
  }));
  return discordItems.concat(localItems);
}

function groups(){
  const combined = combinedSounds();
  const bySound = {}; combined.forEach(s => bySound[s.sound_id] = s);
  const g = {}; const order = [];
  g[KEY_FAV] = favorites.map(id => bySound[id]).filter(Boolean); order.push(KEY_FAV);
  const seen = new Set(); const recentSounds = [];
  history.forEach(h => {
    if (seen.has(h.sound_id)) return; seen.add(h.sound_id);
    recentSounds.push(bySound[h.sound_id] || {sound_id: h.sound_id, name: h.name, emoji_name: null, emoji_id: null});
  });
  g[KEY_RECENT] = recentSounds; order.push(KEY_RECENT);
  g[KEY_LOCAL] = localSounds.map(s => bySound["local:" + s.id]).filter(Boolean); order.push(KEY_LOCAL);
  const serverNames = [];
  allSounds.forEach(s => {
    const k = s.guild_name || "Server";
    if(!g[k]){g[k]=[];serverNames.push(k);}
    g[k].push(s);
  });
  applyChannelOrder(serverNames).forEach(k => order.push(k));
  return {g, order};
}

function applyChannelOrder(names){
  // Respect the user's saved drag order for names we know about; anything
  // new (a server the bot just joined, etc.) is appended at the end.
  const known = channelOrder.filter(n => names.includes(n));
  const rest = names.filter(n => !channelOrder.includes(n));
  return [...known, ...rest];
}

let tabOrder = [KEY_ALL];

function renderTabs(order){
  if (activeTab === null || order.indexOf(activeTab) < 0) activeTab = order[0];
  tabOrder = order;
  document.getElementById("tabSelectLabel").textContent = activeTab;
}

const FIXED_TABS = [KEY_ALL, KEY_FAV, KEY_RECENT, KEY_LOCAL];

function buildTabRow(name, draggable){
  const row = document.createElement("div");
  row.className = "picker-row" + (name === activeTab ? " active" : "") + (draggable ? " draggable" : "");
  row.dataset.name = name;
  const label = document.createElement("span"); label.textContent = name; label.style.flex = "1";
  row.appendChild(label);
  row.addEventListener("click", (e) => {
    if (e.target.closest(".drag-handle")) return;
    activeTab = name;
    closeOverlay("tabOverlay");
    render();
  });
  if (draggable){
    const handle = document.createElement("span");
    handle.className = "drag-handle"; handle.textContent = "≡"; handle.title = "Drag to reorder";
    row.appendChild(handle);
    attachDragReorder(row, handle);
  }
  return row;
}

function attachDragReorder(row, handle){
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    row.classList.add("dragging");

    function onMove(e2){
      const list = document.getElementById("tabList");
      const siblings = Array.from(list.querySelectorAll(".picker-row.draggable")).filter(r => r !== row);
      const after = siblings.find(sib => e2.clientY < sib.getBoundingClientRect().top + sib.getBoundingClientRect().height / 2);
      if (after) list.insertBefore(row, after);
      else list.appendChild(row);
    }
    function onUp(e3){
      handle.releasePointerCapture(e3.pointerId);
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      row.classList.remove("dragging");
      commitChannelOrder();
    }
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  });
}

function commitChannelOrder(){
  const list = document.getElementById("tabList");
  channelOrder = Array.from(list.querySelectorAll(".picker-row.draggable")).map(r => r.dataset.name);
  api("/channel-order", {method:"POST", body: JSON.stringify({order: channelOrder})}).catch(() => {});
  render();
}

function renderTabList(){
  const list = document.getElementById("tabList"); list.innerHTML = "";
  tabOrder.filter(n => FIXED_TABS.includes(n)).forEach(name => list.appendChild(buildTabRow(name, false)));
  const servers = tabOrder.filter(n => !FIXED_TABS.includes(n));
  if (servers.length){
    const gh = document.createElement("div"); gh.className = "picker-group"; gh.textContent = "Servers - drag to reorder";
    list.appendChild(gh);
    servers.forEach(name => list.appendChild(buildTabRow(name, true)));
  }
}

document.getElementById("tabSelectBtn").onclick = () => {
  openOverlay("tabOverlay");
  renderTabList();
};
document.getElementById("closeTab").onclick = () => closeOverlay("tabOverlay");

function playSound(s, tile){
  const url = s.is_local ? ("/local-sounds/" + s.local_id + "/play") : "/play";
  const body = s.is_local ? "{}" : JSON.stringify({sound_id: s.sound_id});
  api(url, {method:"POST", body})
    .then(r => { tile.classList.add(r.ok ? "ok" : "fail"); if (r.ok) setTimeout(fetchHistory, 300); })
    .catch(() => tile.classList.add("fail"))
    .finally(() => setTimeout(()=>tile.classList.remove("ok","fail"), 500));
}

function toggleFavorite(s){
  const isFav = favorites.includes(s.sound_id);
  api("/favorites", {method:"POST", body: JSON.stringify({sound_id: s.sound_id, action: isFav ? "remove" : "add"})})
    .then(r => r.json()).then(d => { favorites = d.favorites || []; render(); });
}

function transferSound(s){
  api("/sounds/" + s.sound_id + "/transfer", {method:"POST"})
    .then(r => r.json()).then(d => { if (d.ok) fetchLocalSounds(); else alert(d.error || "transfer failed"); });
}

function deleteLocalSound(s){
  if (!confirm("Delete " + s.name + "?")) return;
  api("/local-sounds/" + s.local_id, {method:"DELETE"}).then(() => fetchLocalSounds());
}

function downloadSound(s){
  const url = s.is_local ? ("/local-sounds/" + s.local_id + "/file") : ("/sounds/" + s.sound_id + "/download");
  window.open(url, "_blank");
}

function closeContextMenu(){
  document.getElementById("ctxMenu").classList.remove("show");
}

function addCtxItem(menu, label, onClick, opts){
  opts = opts || {};
  const item = document.createElement("div");
  item.className = "ctx-item" + (opts.disabled ? " disabled" : "") + (opts.danger ? " danger" : "");
  item.textContent = label;
  if (onClick && !opts.disabled){
    item.addEventListener("click", (e) => { e.stopPropagation(); onClick(); closeContextMenu(); });
  }
  menu.appendChild(item);
}

function openContextMenu(s, x, y){
  const menu = document.getElementById("ctxMenu");
  menu.innerHTML = "";
  const isFav = favorites.includes(s.sound_id);
  addCtxItem(menu, isFav ? "\\u2B50 Remove favorite" : "\\u2B50 Add favorite", () => toggleFavorite(s));
  addCtxItem(menu, "\\uD83D\\uDCE5 Download", () => downloadSound(s));
  if (!s.is_local){
    const already = localSounds.some(ls => ls.origin_sound_id === s.sound_id);
    addCtxItem(menu, already ? "\\u2705 Already local" : "\\uD83C\\uDFB5 Make local",
      already ? null : () => transferSound(s), {disabled: already});
  }
  if (s.is_local && me && (s.uploaded_by === me.username || me.role === "admin")){
    addCtxItem(menu, "\\u274C Delete", () => deleteLocalSound(s), {danger: true});
  }
  menu.style.left = x + "px"; menu.style.top = y + "px";
  menu.classList.add("show");
  requestAnimationFrame(() => {
    const rect = menu.getBoundingClientRect();
    let left = x, top = y;
    if (left + rect.width > window.innerWidth) left = window.innerWidth - rect.width - 8;
    if (top + rect.height > window.innerHeight) top = window.innerHeight - rect.height - 8;
    menu.style.left = Math.max(8, left) + "px";
    menu.style.top = Math.max(8, top) + "px";
  });
}
document.getElementById("ctxMenu").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", closeContextMenu);
document.addEventListener("scroll", closeContextMenu, true);
window.addEventListener("resize", closeContextMenu);

function buildTile(s){
  const t = document.createElement("div"); t.className = "tile";
  if (favorites.includes(s.sound_id)){
    const badge = document.createElement("div"); badge.className = "fav-badge"; badge.textContent = "\\u2B50";
    t.appendChild(badge);
  }
  const e = document.createElement("div"); e.className = "e";
  if (s.emoji_id){
    const i = document.createElement("img");
    i.src = "https://cdn.discordapp.com/emojis/" + s.emoji_id + ".png?size=64";
    i.onerror = () => { e.textContent = "\\uD83D\\uDD0A"; };
    e.appendChild(i);
  } else e.textContent = s.emoji_name || "\\uD83D\\uDD0A";
  const n = document.createElement("div"); n.className = "n"; n.textContent = s.name;
  t.appendChild(e); t.appendChild(n);

  let startX = 0, startY = 0, moved = false, longPressed = false, pressTimer = null;
  function begin(x, y){
    startX = x; startY = y; moved = false; longPressed = false;
    pressTimer = setTimeout(() => { longPressed = true; openContextMenu(s, x, y); }, 500);
  }
  function track(x, y){
    if (Math.abs(x - startX) > 12 || Math.abs(y - startY) > 12){
      moved = true;
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
    }
  }
  function end(){ if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } }
  t.addEventListener("pointerdown", (e2) => begin(e2.clientX, e2.clientY));
  t.addEventListener("pointermove", (e2) => track(e2.clientX, e2.clientY));
  t.addEventListener("pointerup", end);
  t.addEventListener("pointercancel", end);
  t.addEventListener("pointerleave", end);
  t.addEventListener("contextmenu", (e2) => { e2.preventDefault(); openContextMenu(s, e2.clientX, e2.clientY); });
  t.addEventListener("click", () => { if (longPressed || moved) return; playSound(s, t); });
  return t;
}

function buildGrid(list){
  const grid = document.createElement("div"); grid.className = "grid";
  list.forEach(s => grid.appendChild(buildTile(s)));
  return grid;
}

function buildSectionHeader(name, count, actionLabel, actionFn){
  const header = document.createElement("div"); header.className = "section-header";
  const label = document.createElement("span"); label.className = "label"; label.textContent = name;
  const c = document.createElement("span"); c.className = "count"; c.textContent = count;
  header.appendChild(label); header.appendChild(c);
  if (actionLabel){
    const btn = document.createElement("button");
    btn.className = "btn ghost section-action";
    btn.textContent = actionLabel;
    btn.onclick = (e) => { e.stopPropagation(); actionFn(); };
    header.appendChild(btn);
  }
  return header;
}

function render(){
  const {g, order} = groups();
  renderTabs([KEY_ALL, ...order]);
  const c = document.getElementById("content"); c.innerHTML = "";

  if (activeTab === KEY_ALL){
    const sections = [KEY_FAV, KEY_LOCAL, ...order.filter(n => n !== KEY_FAV && n !== KEY_RECENT && n !== KEY_LOCAL)];
    sections.forEach(name => {
      const list = (g[name] || []).filter(Boolean).filter(s => s.name.toLowerCase().includes(searchTerm));
      const isLocal = name === KEY_LOCAL;
      if (!list.length && !isLocal) return;
      c.appendChild(buildSectionHeader(name, list.length, isLocal ? "+ Add sound" : null, isLocal ? openUpload : null));
      if (list.length){
        c.appendChild(buildGrid(list));
      } else {
        const e = document.createElement("div"); e.className = "empty"; e.textContent = "no local sounds yet";
        c.appendChild(e);
      }
    });
    return;
  }

  const list = (g[activeTab] || []).filter(Boolean).filter(s => s.name.toLowerCase().includes(searchTerm));
  if (activeTab === KEY_LOCAL){
    c.appendChild(buildSectionHeader(KEY_LOCAL, list.length, "+ Add sound", openUpload));
  }
  if (!list.length){
    const e = document.createElement("div"); e.className = "empty";
    e.textContent = activeTab === KEY_LOCAL ? "no local sounds yet" : "nothing here";
    c.appendChild(e); return;
  }
  c.appendChild(buildGrid(list));
}

document.getElementById("search").oninput = (e) => { searchTerm = e.target.value.toLowerCase(); render(); };

function openDrawer(){
  document.getElementById("drawer").classList.add("show");
  document.getElementById("drawerBackdrop").classList.add("show");
  document.body.classList.add("modal-open");
}
function closeDrawer(){
  document.getElementById("drawer").classList.remove("show");
  document.getElementById("drawerBackdrop").classList.remove("show");
  document.body.classList.remove("modal-open");
}
document.getElementById("menuBtn").onclick = openDrawer;
document.getElementById("drawerBackdrop").onclick = closeDrawer;
function openUpload(){ openOverlay("uploadOverlay"); }
document.getElementById("drawerApiKey").onclick = () => { closeDrawer(); openOverlay("meOverlay"); };
document.getElementById("drawerAdmin").onclick = () => { closeDrawer(); openOverlay("adminOverlay"); loadUsers(); };
document.getElementById("drawerLogout").onclick = async () => {
  await fetch("/auth/logout", {method:"POST"});
  location.href = "/dashboard";
};

document.getElementById("closeMe").onclick = () => closeOverlay("meOverlay");
document.getElementById("regenKey").onclick = async () => {
  const d = await (await api("/me/apikey/regenerate", {method:"POST"})).json();
  document.getElementById("meKey").textContent = d.api_key;
};

async function loadUsers(){
  const d = await (await api("/users")).json();
  const c = document.getElementById("userRows"); c.innerHTML = "";
  (d.users||[]).forEach(u => {
    const row = document.createElement("div"); row.className = "row";
    const label = document.createElement("div"); label.textContent = u.username + " \\u00b7 " + u.role;
    row.appendChild(label);
    const del = document.createElement("button"); del.className = "btn danger"; del.textContent = "Delete";
    del.onclick = async () => {
      if (!confirm("Delete " + u.username + "?")) return;
      await api("/users/" + encodeURIComponent(u.username), {method:"DELETE"});
      loadUsers();
    };
    row.appendChild(del);
    c.appendChild(row);
  });
}
document.getElementById("closeAdmin").onclick = () => closeOverlay("adminOverlay");
document.getElementById("newUserForm").onsubmit = async (e) => {
  e.preventDefault();
  await api("/users", {method:"POST", body: JSON.stringify({
    username: document.getElementById("newUsername").value,
    password: document.getElementById("newPassword").value,
    role: document.getElementById("newRole").value,
  })});
  document.getElementById("newUserForm").reset();
  loadUsers();
};

function computeZoomSpan(){
  // ~10% of the clip, at least 1s (or the whole clip if it's shorter than that)
  return Math.min(audioDuration, Math.max(1, audioDuration * 0.1));
}

function centerZoom(t){
  const span = computeZoomSpan();
  let vs = t - span / 2, ve = t + span / 2;
  if (vs < 0) { ve -= vs; vs = 0; }
  if (ve > audioDuration) { vs -= (ve - audioDuration); ve = audioDuration; }
  viewStart = Math.max(0, vs);
  viewEnd = Math.min(audioDuration, ve);
}

function drawWave(){
  const canvas = document.getElementById("waveCanvas");
  const ctxc = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctxc.fillStyle = "#101418"; ctxc.fillRect(0, 0, w, h);
  if (waveBuffer && audioDuration){
    const span = Math.max(0.001, viewEnd - viewStart);
    const samplesPerSec = waveBuffer.length / audioDuration;
    ctxc.fillStyle = "#3a4252";
    for (let x = 0; x < w; x++){
      const t0 = viewStart + (x / w) * span;
      const t1 = viewStart + ((x + 1) / w) * span;
      const i0 = Math.max(0, Math.floor(t0 * samplesPerSec));
      const i1 = Math.min(waveBuffer.length, Math.max(i0 + 1, Math.floor(t1 * samplesPerSec)));
      let max = 0;
      for (let i = i0; i < i1; i++){
        const v = Math.abs(waveBuffer[i]);
        if (v > max) max = v;
      }
      const bh = Math.max(1, max * h);
      ctxc.fillRect(x, (h - bh) / 2, 1, bh);
    }
    const sx = ((trimStart - viewStart) / span) * w, ex = ((trimEnd - viewStart) / span) * w;
    ctxc.fillStyle = "rgba(0,0,0,.55)";
    ctxc.fillRect(0, 0, Math.max(0, Math.min(w, sx)), h);
    ctxc.fillRect(Math.max(0, Math.min(w, ex)), 0, w - Math.max(0, Math.min(w, ex)), h);
    ctxc.fillStyle = "#5865f2";
    if (sx > -3 && sx < w + 3) ctxc.fillRect(Math.max(0, Math.min(w - 3, sx - 2)), 0, 3, h);
    if (ex > -3 && ex < w + 3) ctxc.fillRect(Math.max(0, Math.min(w - 3, ex - 2)), 0, 3, h);
  }
  const info = document.getElementById("trimInfo");
  const zoomed = audioDuration && (viewEnd - viewStart) < audioDuration - 0.001;
  info.textContent = audioDuration
    ? (trimEnd - trimStart).toFixed(2) + "s selected (max " + config.max_local_sound_seconds + "s)" + (zoomed ? " \\u2014 zoomed in" : "")
    : "select a file";
}

function stopPreview(){
  const audio = document.getElementById("previewAudio");
  audio.pause();
  if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
}

document.getElementById("uploadFile").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  stopPreview();
  selectedFile = file;
  waveBuffer = null; audioDuration = 0;
  document.getElementById("previewBtn").disabled = true;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  document.getElementById("previewAudio").src = previewUrl;
  try{
    const buf = await file.arrayBuffer();
    const actx = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await actx.decodeAudioData(buf);
    waveBuffer = decoded.getChannelData(0);
    audioDuration = decoded.duration;
    trimStart = 0;
    trimEnd = Math.min(audioDuration, config.max_local_sound_seconds);
    viewStart = 0; viewEnd = audioDuration;
    document.getElementById("previewBtn").disabled = false;
  }catch(err){
    document.getElementById("trimInfo").textContent = "could not decode audio";
  }
  drawWave();
};

function renderEmojiGrid(filter){
  const grid = document.getElementById("emojiGrid");
  grid.innerHTML = "";
  const f = (filter || "").trim().toLowerCase();
  const matches = EMOJI_LIST.filter(([, name]) => !f || name.includes(f));
  if (!matches.length){
    const none = document.createElement("div"); none.className = "none"; none.textContent = "no matches";
    grid.appendChild(none);
    return;
  }
  matches.forEach(([e, name]) => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = e; b.title = name;
    b.onclick = () => {
      selectedEmoji = e;
      document.getElementById("emojiTrigger").textContent = e + "  (change)";
      document.getElementById("emojiPicker").classList.remove("show");
    };
    grid.appendChild(b);
  });
}

document.getElementById("emojiTrigger").onclick = (e) => {
  e.stopPropagation();
  const picker = document.getElementById("emojiPicker");
  const opening = !picker.classList.contains("show");
  picker.classList.toggle("show", opening);
  if (opening){
    document.getElementById("emojiSearch").value = "";
    renderEmojiGrid("");
    document.getElementById("emojiSearch").focus();
  }
};
document.getElementById("emojiSearch").oninput = (e) => renderEmojiGrid(e.target.value);
document.getElementById("emojiPicker").onclick = (e) => e.stopPropagation();
document.addEventListener("click", () => document.getElementById("emojiPicker").classList.remove("show"));

document.getElementById("previewBtn").onclick = () => {
  if (!audioDuration) return;
  stopPreview();
  const audio = document.getElementById("previewAudio");
  audio.currentTime = trimStart;
  audio.play();
  previewTimer = setTimeout(stopPreview, Math.max(0, (trimEnd - trimStart) * 1000));
};

(function initWaveDrag(){
  const canvas = document.getElementById("waveCanvas");
  let lastClientX = 0;
  canvas.addEventListener("pointerdown", (e) => {
    if (!audioDuration) return;
    stopPreview();
    const rect = canvas.getBoundingClientRect();
    // picking which handle to grab always happens against the full-clip view,
    // since the view resets to full on every pointerup
    const x = (e.clientX - rect.left) / rect.width * canvas.width;
    const sx = (trimStart / audioDuration) * canvas.width, ex = (trimEnd / audioDuration) * canvas.width;
    dragging = Math.abs(x - sx) < Math.abs(x - ex) ? "start" : "end";
    lastClientX = e.clientX;
    centerZoom(dragging === "start" ? trimStart : trimEnd);
    drawWave();
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!dragging || !audioDuration) return;
    const rect = canvas.getBoundingClientRect();
    const span = viewEnd - viewStart;
    // Move the handle by how far the pointer actually moved this tick,
    // scaled by the current zoom's pixels-per-second - not by recomputing an
    // absolute position against the view window. The view window shifts when
    // panning near an edge (below), and deriving an absolute position from a
    // window that just moved causes the value to jump even when the pointer
    // didn't, which snowballs into a runaway sprint near the edges.
    const deltaTime = ((e.clientX - lastClientX) / rect.width) * span;
    lastClientX = e.clientX;
    const maxLen = config.max_local_sound_seconds;
    if (dragging === "start"){
      trimStart = Math.max(0, Math.min(trimStart + deltaTime, trimEnd - 0.05));
      if (trimEnd - trimStart > maxLen) trimStart = trimEnd - maxLen;
    } else {
      trimEnd = Math.min(audioDuration, Math.max(trimEnd + deltaTime, trimStart + 0.05));
      if (trimEnd - trimStart > maxLen) trimEnd = trimStart + maxLen;
    }
    // Keep the zoom window fixed while dragging (a stable reference frame is
    // much easier to fine-tune against) and only pan it once the handle gets
    // close to the edge of what's currently visible.
    const val = dragging === "start" ? trimStart : trimEnd;
    const margin = span * 0.15;
    if (val < viewStart + margin || val > viewEnd - margin) centerZoom(val);
    drawWave();
  });
  window.addEventListener("pointerup", () => {
    if (!dragging) return;
    dragging = null;
    viewStart = 0; viewEnd = audioDuration;
    drawWave();
  });
})();

document.getElementById("uploadSubmit").onclick = async () => {
  if (!selectedFile || !audioDuration) { alert("pick a file first"); return; }
  const name = document.getElementById("uploadName").value.trim();
  if (!name) { alert("name required"); return; }
  const fd = new FormData();
  fd.append("file", selectedFile);
  fd.append("name", name);
  fd.append("emoji", selectedEmoji);
  fd.append("start_sec", trimStart.toFixed(2));
  fd.append("end_sec", trimEnd.toFixed(2));
  const r = await api("/local-sounds", {method:"POST", body: fd});
  if (r.ok){
    resetUploadForm();
    document.getElementById("uploadOverlay").classList.remove("show");
    fetchLocalSounds();
  } else {
    const d = await r.json().catch(() => ({}));
    alert(d.error || "upload failed");
  }
};

function resetUploadForm(){
  stopPreview();
  if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
  document.getElementById("previewAudio").removeAttribute("src");
  document.getElementById("previewBtn").disabled = true;
  document.getElementById("uploadFile").value = "";
  document.getElementById("uploadName").value = "";
  selectedEmoji = "";
  document.getElementById("emojiTrigger").textContent = "Pick emoji (optional)";
  document.getElementById("emojiPicker").classList.remove("show");
  document.getElementById("trimInfo").textContent = "select a file";
  waveBuffer = null; audioDuration = 0; selectedFile = null;
  viewStart = 0; viewEnd = 0;
  drawWave();
}

document.getElementById("closeUpload").onclick = () => {
  stopPreview();
  closeOverlay("uploadOverlay");
};

(async function start(){
  await loadMe();
  await fetchConfig();
  refreshStatus(); await fetchSounds(); await fetchHistory(); await fetchLocalSounds();
  setInterval(refreshStatus, 5000);
  setInterval(fetchHistory, 20000);
})();
</script></body></html>"""


def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # Chromium Private Network Access: pages fetching into private IP space
    # require this on the preflight or requests are silently blocked
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


def guard(request):
    # Legacy guard for the pre-existing routes (/, /guilds, /join, /leave,
    # /sounds, /status, /play): preserves old behavior (open when API_KEY is
    # unset) while layering personal API keys and session cookies on top.
    # Identifies the caller on request["user"] when possible, for handlers
    # that log history / check ownership.
    key = request.query.get("key")
    if key:
        if API_KEY and key == API_KEY:
            request["user"] = None
            return None
        users = load_users()
        u = find_user_by_apikey(users, key)
        if u:
            request["user"] = u["username"]
            return None
        if not API_KEY:
            request["user"] = None
            return None
        return cors(web.json_response({"error": "bad api key"}, status=401))
    cookie = request.cookies.get("session")
    if cookie:
        uname = verify_session(cookie)
        if uname and find_user(load_users(), uname):
            request["user"] = uname
            return None
    if not API_KEY:
        request["user"] = None
        return None
    return cors(web.json_response({"error": "bad api key"}, status=401))


def require_user(request):
    # Auth for dashboard-only routes: must resolve to an actual account
    # (personal API key or session cookie), regardless of the shared API_KEY.
    key = request.query.get("key")
    users = load_users()
    if key:
        u = find_user_by_apikey(users, key)
        if u:
            return u["username"], None
    else:
        cookie = request.cookies.get("session")
        if cookie:
            uname = verify_session(cookie)
            if uname and find_user(users, uname):
                return uname, None
    return None, cors(web.json_response({"error": "login required"}, status=401))


def require_admin(request):
    username, err = require_user(request)
    if err:
        return None, err
    u = find_user(load_users(), username)
    if not u or u.get("role") != "admin":
        return None, cors(web.json_response({"error": "admin required"}, status=403))
    return username, None


def build_app(agent: Agent):
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(request):
        if (err := guard(request)):
            return err
        return cors(web.Response(text=INDEX_HTML, content_type="text/html"))

    @routes.get("/dashboard")
    async def dashboard(request):
        cookie = request.cookies.get("session")
        username = verify_session(cookie) if cookie else None
        if username and find_user(load_users(), username):
            return cors(web.Response(text=DASHBOARD_HTML, content_type="text/html"))
        return cors(web.Response(text=LOGIN_HTML, content_type="text/html"))

    @routes.options("/{tail:.*}")
    async def options(_):
        return cors(web.Response())

    @routes.post("/auth/login")
    async def login(request):
        try:
            body = json.loads(await request.text() or "{}")
        except Exception:
            return cors(web.json_response({"error": "bad json"}, status=400))
        username = body.get("username") or ""
        password = body.get("password") or ""
        u = find_user(load_users(), username)
        if not u or not verify_password(password, u["salt"], u["hash"]):
            return cors(web.json_response({"error": "invalid credentials"}, status=401))
        resp = cors(web.json_response({"ok": True, "username": u["username"], "role": u["role"]}))
        resp.set_cookie("session", make_session(u["username"]), httponly=True, samesite="Lax",
                         max_age=SESSION_TTL_SEC, path="/")
        return resp

    @routes.post("/auth/logout")
    async def logout(request):
        resp = cors(web.json_response({"ok": True}))
        resp.del_cookie("session", path="/")
        return resp

    @routes.get("/me")
    async def me(request):
        username, err = require_user(request)
        if err:
            return err
        u = find_user(load_users(), username)
        return cors(web.json_response({
            "username": u["username"], "role": u["role"],
            "api_key": u["api_key"], "favorites": u.get("favorites", []),
            "channel_order": u.get("channel_order", []),
        }))

    @routes.post("/me/apikey/regenerate")
    async def regenerate_apikey(request):
        username, err = require_user(request)
        if err:
            return err
        users = load_users()
        u = find_user(users, username)
        u["api_key"] = secrets.token_urlsafe(32)
        save_users(users)
        return cors(web.json_response({"ok": True, "api_key": u["api_key"]}))

    @routes.get("/users")
    async def list_users(request):
        _, err = require_admin(request)
        if err:
            return err
        data = [{"username": u["username"], "role": u["role"], "created_at": u.get("created_at")}
                for u in load_users()]
        return cors(web.json_response({"users": data}))

    @routes.post("/users")
    async def create_user(request):
        _, err = require_admin(request)
        if err:
            return err
        body = json.loads(await request.text() or "{}")
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        role = body.get("role") if body.get("role") in ("admin", "member") else "member"
        if not username or not password:
            return cors(web.json_response({"error": "username and password required"}, status=400))
        users = load_users()
        if find_user(users, username):
            return cors(web.json_response({"error": "user already exists"}, status=409))
        salt, h = hash_password(password)
        users.append({
            "username": username, "salt": salt, "hash": h,
            "api_key": secrets.token_urlsafe(32), "role": role,
            "favorites": [], "channel_order": [], "created_at": time.time(),
        })
        save_users(users)
        return cors(web.json_response({"ok": True}))

    @routes.patch("/users/{username}")
    async def patch_user(request):
        _, err = require_admin(request)
        if err:
            return err
        target = request.match_info["username"]
        users = load_users()
        u = find_user(users, target)
        if not u:
            return cors(web.json_response({"error": "not found"}, status=404))
        body = json.loads(await request.text() or "{}")
        if body.get("password"):
            u["salt"], u["hash"] = hash_password(body["password"])
        if body.get("role") in ("admin", "member"):
            u["role"] = body["role"]
        save_users(users)
        return cors(web.json_response({"ok": True}))

    @routes.delete("/users/{username}")
    async def delete_user(request):
        admin_username, err = require_admin(request)
        if err:
            return err
        target = request.match_info["username"]
        if target == admin_username:
            return cors(web.json_response({"error": "cannot delete yourself"}, status=400))
        users = load_users()
        victim = find_user(users, target)
        if not victim:
            return cors(web.json_response({"error": "not found"}, status=404))
        admins = [u for u in users if u.get("role") == "admin"]
        if victim.get("role") == "admin" and len(admins) <= 1:
            return cors(web.json_response({"error": "cannot delete the last admin"}, status=400))
        save_users([u for u in users if u["username"] != target])
        return cors(web.json_response({"ok": True}))

    @routes.get("/favorites")
    async def get_favorites(request):
        username, err = require_user(request)
        if err:
            return err
        u = find_user(load_users(), username)
        return cors(web.json_response({"favorites": u.get("favorites", [])}))

    @routes.post("/favorites")
    async def post_favorites(request):
        username, err = require_user(request)
        if err:
            return err
        body = json.loads(await request.text() or "{}")
        sound_id = str(body.get("sound_id") or "")
        action = body.get("action")
        if not sound_id or action not in ("add", "remove"):
            return cors(web.json_response({"error": "sound_id and action required"}, status=400))
        users = load_users()
        u = find_user(users, username)
        favs = set(u.get("favorites", []))
        favs.add(sound_id) if action == "add" else favs.discard(sound_id)
        u["favorites"] = sorted(favs)
        save_users(users)
        return cors(web.json_response({"ok": True, "favorites": u["favorites"]}))

    @routes.post("/channel-order")
    async def set_channel_order(request):
        username, err = require_user(request)
        if err:
            return err
        body = json.loads(await request.text() or "{}")
        order = body.get("order")
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
            return cors(web.json_response({"error": "order must be a list of strings"}, status=400))
        users = load_users()
        u = find_user(users, username)
        u["channel_order"] = order[:100]
        save_users(users)
        return cors(web.json_response({"ok": True, "channel_order": u["channel_order"]}))

    @routes.get("/history")
    async def get_history(request):
        _, err = require_user(request)
        if err:
            return err
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        rows = read_history()[-limit:][::-1]
        return cors(web.json_response({"history": rows}))

    @routes.get("/stats/top-sounds")
    async def top_sounds(request):
        _, err = require_user(request)
        if err:
            return err
        counts, names = {}, {}
        for row in read_history():
            sid = row["sound_id"]
            counts[sid] = counts.get(sid, 0) + 1
            names[sid] = row.get("name")
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:20]
        return cors(web.json_response({
            "top": [{"sound_id": sid, "name": names.get(sid), "count": c} for sid, c in top]
        }))

    @routes.get("/config")
    async def get_config(request):
        if (err := guard(request)):
            return err
        return cors(web.json_response({
            "max_local_sound_seconds": MAX_LOCAL_SOUND_SECONDS,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
        }))

    @routes.get("/local-sounds")
    async def list_local_sounds(request):
        _, err = require_user(request)
        if err:
            return err
        return cors(web.json_response({"sounds": load_local_sounds()}))

    @routes.post("/local-sounds")
    async def upload_local_sound(request):
        username, err = require_user(request)
        if err:
            return err
        name, emoji, start_sec, end_sec = None, None, 0.0, None
        tmp_path, size = None, 0
        try:
            reader = await request.multipart()
            async for field in reader:
                if field.name == "name":
                    name = (await field.text()).strip()
                elif field.name == "emoji":
                    emoji = (await field.text()).strip() or None
                elif field.name == "start_sec":
                    start_sec = float(await field.text())
                elif field.name == "end_sec":
                    end_sec = float(await field.text())
                elif field.name == "file":
                    os.makedirs(LOCAL_SOUNDS_DIR, exist_ok=True)
                    fd, tmp_path = tempfile.mkstemp(dir=LOCAL_SOUNDS_DIR, suffix=".upload")
                    with os.fdopen(fd, "wb") as out:
                        while True:
                            chunk = await field.read_chunk()
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > MAX_UPLOAD_BYTES:
                                raise ValueError("file too large")
                            out.write(chunk)
        except ValueError as e:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
            return cors(web.json_response({"error": str(e)}, status=413))

        if not tmp_path or not name or end_sec is None or end_sec <= start_sec:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
            return cors(web.json_response({"error": "file, name, start_sec and end_sec required"}, status=400))

        duration = min(end_sec - start_sec, MAX_LOCAL_SOUND_SECONDS)
        sound_id = secrets.token_hex(8)
        ok = await ffmpeg_trim(tmp_path, local_sound_path(sound_id), start_sec, duration)
        os.remove(tmp_path)
        if not ok:
            return cors(web.json_response({"error": "trim/transcode failed"}, status=500))

        sounds = load_local_sounds()
        entry = {
            "id": sound_id, "name": name, "emoji": emoji, "duration_sec": duration,
            "uploaded_by": username, "created_at": time.time(), "origin_sound_id": None,
        }
        sounds.append(entry)
        save_local_sounds(sounds)
        return cors(web.json_response({"ok": True, "sound": entry}))

    @routes.delete("/local-sounds/{id}")
    async def delete_local_sound(request):
        username, err = require_user(request)
        if err:
            return err
        sid = request.match_info["id"]
        sounds = load_local_sounds()
        entry = next((s for s in sounds if s["id"] == sid), None)
        if not entry:
            return cors(web.json_response({"error": "not found"}, status=404))
        u = find_user(load_users(), username)
        if entry["uploaded_by"] != username and (not u or u.get("role") != "admin"):
            return cors(web.json_response({"error": "not allowed"}, status=403))
        save_local_sounds([s for s in sounds if s["id"] != sid])
        path = local_sound_path(sid)
        if os.path.exists(path):
            os.remove(path)
        return cors(web.json_response({"ok": True}))

    @routes.get("/local-sounds/{id}/file")
    async def local_sound_file(request):
        _, err = require_user(request)
        if err:
            return err
        sid = request.match_info["id"]
        entry = next((s for s in load_local_sounds() if s["id"] == sid), None)
        if not entry:
            return cors(web.json_response({"error": "not found"}, status=404))
        path = local_sound_path(sid)
        if not os.path.exists(path):
            return cors(web.json_response({"error": "audio file missing"}, status=404))
        resp = cors(web.FileResponse(path))
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_filename(entry["name"], sid)}"'
        return resp

    @routes.post("/local-sounds/{id}/play")
    async def play_local_sound(request):
        username, err = require_user(request)
        if err:
            return err
        sid = request.match_info["id"]
        entry = next((s for s in load_local_sounds() if s["id"] == sid), None)
        if not entry:
            return cors(web.json_response({"error": "not found"}, status=404))
        path = local_sound_path(sid)
        if not os.path.exists(path):
            return cors(web.json_response({"error": "audio file missing"}, status=404))
        try:
            await agent.play_local(path)
        except Exception as e:
            return cors(web.json_response({"error": str(e)}, status=500))
        append_history(username, "local:" + sid, entry["name"], None)
        return cors(web.json_response({"ok": True}))

    @routes.post("/sounds/{sound_id}/transfer")
    async def transfer_sound(request):
        username, err = require_user(request)
        if err:
            return err
        sound_id = request.match_info["sound_id"]
        display_name, display_emoji = await find_sound_meta(agent, sound_id)
        try:
            data = await fetch_discord_sound_bytes(sound_id)
        except RuntimeError as e:
            return cors(web.json_response({"error": str(e)}, status=502))

        os.makedirs(LOCAL_SOUNDS_DIR, exist_ok=True)
        local_id = secrets.token_hex(8)
        raw_path = os.path.join(LOCAL_SOUNDS_DIR, f"{local_id}.src")
        with open(raw_path, "wb") as f:
            f.write(data)
        ok = await ffmpeg_trim(raw_path, local_sound_path(local_id), 0, MAX_LOCAL_SOUND_SECONDS)
        os.remove(raw_path)
        if not ok:
            return cors(web.json_response({"error": "transcode failed"}, status=500))

        sounds = load_local_sounds()
        entry = {
            "id": local_id, "name": display_name, "emoji": display_emoji, "duration_sec": None,
            "uploaded_by": username, "created_at": time.time(), "origin_sound_id": sound_id,
        }
        sounds.append(entry)
        save_local_sounds(sounds)
        return cors(web.json_response({"ok": True, "sound": entry}))

    @routes.get("/sounds/{sound_id}/download")
    async def download_sound(request):
        _, err = require_user(request)
        if err:
            return err
        sound_id = request.match_info["sound_id"]
        display_name, _ = await find_sound_meta(agent, sound_id)
        try:
            data = await fetch_discord_sound_bytes(sound_id)
        except RuntimeError as e:
            return cors(web.json_response({"error": str(e)}, status=502))
        resp = cors(web.Response(body=data, content_type="audio/mpeg"))
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_filename(display_name, sound_id)}"'
        return resp

    @routes.get("/guilds")
    async def guilds(request):
        if (err := guard(request)):
            return err
        sc = agent.status_channel()
        connected_id = sc.id if sc else None
        data = []
        for g in agent.guilds:
            channels = [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "connected": c.id == connected_id,
                }
                for c in g.channels
                if isinstance(c, discord.VoiceChannel)
                and c.permissions_for(g.me).connect
            ]
            data.append({"id": str(g.id), "name": g.name, "channels": channels})
        return cors(web.json_response({"guilds": data}))

    @routes.post("/join")
    async def join(request):
        if (err := guard(request)):
            return err
        try:
            body = json.loads(await request.text() or "{}")
            ch = await agent.join_channel(body.get("channel_id"))
            return cors(web.json_response({"ok": True, "channel": ch.name, "guild": ch.guild.name}))
        except Exception as e:
            return cors(web.json_response({"error": str(e)}, status=500))

    @routes.post("/leave")
    async def leave(request):
        if (err := guard(request)):
            return err
        try:
            await agent.leave()
            return cors(web.json_response({"ok": True}))
        except Exception as e:
            return cors(web.json_response({"error": str(e)}, status=500))

    @routes.get("/sounds")
    async def sounds(request):
        if (err := guard(request)):
            return err
        try:
            gid = request.query.get("guild_id")
            data = []
            if gid == "all":
                for g in agent.guilds:
                    try:
                        data += [
                            sound_to_dict(s, False, g.id, g.name)
                            for s in await agent.guild_sounds(g.id)
                            if getattr(s, "available", True)
                        ]
                    except Exception as e:
                        print(f"sounds fetch failed for {g.name}:", e)
            else:
                if not gid:
                    sc = agent.status_channel()
                    if not sc:
                        return cors(web.json_response({"sounds": [], "note": "not in voice"}))
                    gid = sc.guild.id
                g = agent.get_guild(int(gid))
                data = [
                    sound_to_dict(s, False, gid, g.name if g else None)
                    for s in await agent.guild_sounds(gid)
                    if getattr(s, "available", True)
                ]
            data += [sound_to_dict(s, True) for s in await agent.get_defaults()]
            return cors(web.json_response({"sounds": data}))
        except Exception as e:
            return cors(web.json_response({"error": str(e)}, status=500))

    @routes.get("/status")
    async def status(request):
        if (err := guard(request)):
            return err
        vc = agent.current_vc()
        sc = agent.status_channel()
        return cors(web.json_response({
            "ready": agent.is_ready(),
            "voice_connected": bool(sc),
            "audio_live": bool(vc),  # False = shown in channel but audio needs reconnect
            "channel": sc.name if sc else None,
            "guild": sc.guild.name if sc else None,
        }))

    @routes.post("/play")
    async def play(request):
        if (err := guard(request)):
            return err
        try:
            body = json.loads(await request.text() or "{}")
            sound = await agent.play(body.get("sound_id"))
            username = request.get("user")
            if username:
                guild = getattr(sound, "guild", None)
                append_history(username, str(sound.id), sound.name, guild.name if guild else None)
            return cors(web.json_response({"ok": True}))
        except Exception as e:
            return cors(web.json_response({"error": str(e)}, status=500))

    app = web.Application()
    app.add_routes(routes)
    return app


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN")
    bootstrap_admin()
    Agent().run(TOKEN)
