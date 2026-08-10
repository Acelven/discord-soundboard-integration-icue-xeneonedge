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
import asyncio
import discord
from aiohttp import web

TOKEN = os.environ.get("DISCORD_TOKEN", "")
PORT = int(os.environ.get("PORT", "8766"))
API_KEY = os.environ.get("API_KEY", "")

intents = discord.Intents.default()


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


class Agent(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.sound_cache = {}  # guild_id -> (timestamp, [sounds])
        self.default_sounds = []
        self.last_activity = None  # monotonic time of last join/play
        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT_SEC", str(4 * 3600)))
        self.ghost_channel = None  # (guild_id, channel_id) bot is shown in but has no live audio client

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


def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # Chromium Private Network Access: pages fetching into private IP space
    # require this on the preflight or requests are silently blocked
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


def authed(request):
    return not API_KEY or request.query.get("key") == API_KEY


def build_app(agent: Agent):
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(request):
        if (err := guard(request)):
            return err
        return cors(web.Response(text=INDEX_HTML, content_type="text/html"))

    @routes.options("/{tail:.*}")
    async def options(_):
        return cors(web.Response())

    def guard(request):
        if not authed(request):
            return cors(web.json_response({"error": "bad api key"}, status=401))
        return None

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
            await agent.play(body.get("sound_id"))
            return cors(web.json_response({"ok": True}))
        except Exception as e:
            return cors(web.json_response({"error": str(e)}, status=500))

    app = web.Application()
    app.add_routes(routes)
    return app


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN")
    Agent().run(TOKEN)
