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
        "discord_user_id": None,
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


async def find_member_voice_channel(agent, discord_user_id):
    # Searches every guild the bot is in for a member's current voice channel.
    # Works without the privileged Members intent: guild.get_member() uses
    # whatever's cached, and fetch_member() is a targeted REST lookup for a
    # single known user id (unlike listing/searching all members, which does
    # need the privileged intent). Voice state itself comes from the
    # non-privileged voice_states intent already enabled by Intents.default().
    uid = int(discord_user_id)
    for g in agent.guilds:
        member = g.get_member(uid)
        if member is None:
            try:
                member = await g.fetch_member(uid)
            except discord.HTTPException:
                continue
        if member and member.voice and member.voice.channel:
            return member.voice.channel
    return None


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


TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def load_template(name):
    with open(os.path.join(TEMPLATES_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


INDEX_HTML = load_template("index.html")
LOGIN_HTML = load_template("login.html")
DASHBOARD_HTML = load_template("dashboard.html")


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


def guard_local(request):
    # Like guard(), but for local-sound read/play routes: accepts the shared
    # API_KEY (anonymous - request["user"] stays None) in addition to a
    # personal key or session cookie. Upload/delete/transfer stay on
    # require_user only, since those need a real username for attribution.
    key = request.query.get("key")
    if key and API_KEY and key == API_KEY:
        request["user"] = None
        return None
    username, err = require_user(request)
    if err:
        return err
    request["user"] = username
    return None


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
            "discord_user_id": u.get("discord_user_id"),
        }))

    @routes.post("/me/discord-id")
    async def set_discord_id(request):
        username, err = require_user(request)
        if err:
            return err
        body = json.loads(await request.text() or "{}")
        discord_id = str(body.get("discord_user_id") or "").strip()
        if discord_id and not discord_id.isdigit():
            return cors(web.json_response({"error": "discord_user_id must be numeric"}, status=400))
        users = load_users()
        u = find_user(users, username)
        u["discord_user_id"] = discord_id or None
        save_users(users)
        return cors(web.json_response({"ok": True, "discord_user_id": u["discord_user_id"]}))

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
            "favorites": [], "channel_order": [], "discord_user_id": None,
            "created_at": time.time(),
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
        if (err := guard_local(request)):
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
        if (err := guard_local(request)):
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
        if (err := guard_local(request)):
            return err
        username = request.get("user")
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
        if username:
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

    @routes.post("/me/join-mine")
    async def join_my_channel(request):
        username, err = require_user(request)
        if err:
            return err
        u = find_user(load_users(), username)
        discord_id = u.get("discord_user_id")
        if not discord_id:
            return cors(web.json_response(
                {"error": "link your Discord account first (drawer -> My Discord Account)"}, status=400))
        channel = await find_member_voice_channel(agent, discord_id)
        if not channel:
            return cors(web.json_response(
                {"error": "you don't appear to be in a voice channel the bot can see"}, status=404))
        try:
            ch = await agent.join_channel(str(channel.id))
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
