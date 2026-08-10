#!/usr/bin/env python3
"""soundctl - tiny CLI for the Discord Soundboard Agent.

Written strictly against API.md. Stdlib only.

Usage:
  python soundctl.py status
  python soundctl.py channels
  python soundctl.py join <channel_id>
  python soundctl.py sounds [all|<guild_id>]
  python soundctl.py play <sound_id>
  python soundctl.py leave

Config via env or flags:
  SOUND_AGENT_URL  (default http://localhost:8766)   or --base
  SOUND_AGENT_KEY  (default empty)                   or --key
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


def call(base, key, method, path, body=None, query=None):
    q = dict(query or {})
    if key:
        q["key"] = key
    url = base.rstrip("/") + path + ("?" + urllib.parse.urlencode(q) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"error": f"HTTP {e.code}"}
        sys.exit(f"error: {err.get('error', e)}")
    except Exception as e:
        sys.exit(f"error: cannot reach agent at {base} ({e})")


def icon(s):
    return s["emoji_name"] or ("[custom]" if s["emoji_id"] else "🔊")


def main():
    ap = argparse.ArgumentParser(prog="soundctl")
    ap.add_argument("--base", default=os.environ.get("SOUND_AGENT_URL", ""))
    ap.add_argument("--key", default=os.environ.get("SOUND_AGENT_KEY", ""))
    ap.add_argument("cmd", choices=["status", "channels", "join", "sounds", "play", "leave"])
    ap.add_argument("arg", nargs="?")
    a = ap.parse_args()

    if a.cmd == "status":
        s = call(a.base, a.key, "GET", "/status")
        if s["voice_connected"]:
            live = "" if s["audio_live"] else " (audio reconnects on next action)"
            print(f"connected: {s['guild']} / {s['channel']}{live}")
        else:
            print("not in voice")

    elif a.cmd == "channels":
        d = call(a.base, a.key, "GET", "/guilds")
        for g in d["guilds"]:
            print(g["name"])
            for c in g["channels"]:
                mark = "*" if c["connected"] else " "
                print(f"  {mark} {c['id']}  {c['name']}")

    elif a.cmd == "join":
        if not a.arg:
            sys.exit("usage: soundctl join <channel_id>")
        r = call(a.base, a.key, "POST", "/join", body={"channel_id": a.arg})
        print(f"joined {r['guild']} / {r['channel']}")

    elif a.cmd == "sounds":
        q = {"guild_id": a.arg} if a.arg else None
        d = call(a.base, a.key, "GET", "/sounds", query=q)
        if not d["sounds"]:
            print(d.get("note", "no sounds"))
        for s in d["sounds"]:
            src = "default" if s["default"] else (s["guild_name"] or "?")
            print(f"{s['sound_id']}  {icon(s)}  {s['name']}  ({src})")

    elif a.cmd == "play":
        if not a.arg:
            sys.exit("usage: soundctl play <sound_id>")
        call(a.base, a.key, "POST", "/play", body={"sound_id": a.arg})
        print("played")

    elif a.cmd == "leave":
        call(a.base, a.key, "POST", "/leave", body={})
        print("left")


if __name__ == "__main__":
    main()