"""The app's settings: the add-on Configuration tab (/data/options.json)."""
from __future__ import annotations

import json
import os
import re

from .account import slug
from .robot import Robot

OPTIONS_PATH = os.environ.get("C103_OPTIONS", "/data/options.json")

_HOST = re.compile(r"^[A-Za-z0-9.\-]+$")
_TOKEN = re.compile(r"^[0-9a-fA-F]{32}$")


def load_options() -> dict:
    """{} when there is no settings file (not running as the add-on)."""
    try:
        with open(OPTIONS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_mqtt(opts: dict) -> dict | None:
    """None = the Home Assistant link is off (no broker or no username)."""
    m = opts.get("mqtt") or {}
    if m.get("host") and m.get("username"):
        return {"host": m["host"], "port": int(m.get("port") or 1883),
                "username": m["username"], "password": m.get("password") or ""}
    return None


def robots(opts: dict, found: list[dict]) -> tuple[dict[str, Robot], dict[str, str]]:
    """({id: Robot}, {id: name}). A filled Robots list is used as it is; with an empty one, every
    robot found on the Xiaomi account. A robot with a bad entry is skipped and explained in the log."""
    out: dict[str, Robot] = {}
    names: dict[str, str] = {}
    for r in opts.get("robots") or found:
        name = str(r.get("name") or "").strip()
        rid = str(r.get("id") or slug(name))
        host, token = str(r.get("host") or ""), str(r.get("token") or "")
        if rid in out:
            problem = "its id is used twice"
        elif not _HOST.match(host):
            problem = f"the IP address looks wrong ({host!r})"
        elif not _TOKEN.match(token):
            problem = "the token must be 32 hexadecimal characters"
        else:
            out[rid], names[rid] = Robot(rid, host, token), name or rid
            continue
        print(f"robot {name or rid}: skipped, {problem}. Fix it on the Configuration tab.", flush=True)
    return out, names
