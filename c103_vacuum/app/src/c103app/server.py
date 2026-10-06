"""Web server: live API, controls and the map page.  Run:  python -m c103app.server

GET endpoints are read-only. POST endpoints send commands to a robot over the LAN. They
need the header `X-Requested-With: vac` (so another website cannot make your browser send commands).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

from aiohttp import web

from . import account, cloudmap, config, mqttbridge
from .commands import Commander, Refused
from .live import RobotLive
from .robot import RobotError

ROOT = Path(__file__).resolve().parents[2]  # .../vacuum_app
WEB = ROOT / "web"
DATA = Path(os.environ.get("C103_DATA", ROOT / "data"))
STATE = Path(os.environ.get("C103_STATE", DATA.parent))   # Xiaomi session + robot list
NO_CACHE = {"Cache-Control": "no-cache"}


def make_app() -> web.Application:
    opts = config.load_options()
    acct = account.Account(STATE, opts)
    found = acct.devices()                        # the c103 robots on the Xiaomi account
    robots, names = config.robots(opts, found)
    if not robots and acct.cloud is None:
        print("nothing to control yet: add robots on the Configuration tab, or fill in the Xiaomi login "
              "so the app can find them (Documentation tab).", flush=True)
    live = {rid: RobotLive(r, names[rid]) for rid, r in robots.items()}
    for t in live.values():
        t.start()

    maps = None                                   # the map picture needs the Xiaomi login and a matching device
    info = cloudmap.match_all(robots, found) if acct.cloud else {}
    if info:
        maps = cloudmap.CloudMaps({rid: robots[rid] for rid in info}, DATA, acct, info)
        cloudmap.Scheduler(maps, {rid: live[rid] for rid in info}).start()

    def robot_or_404(request) -> RobotLive:
        rid = request.match_info["rid"]
        if rid not in live:
            raise web.HTTPNotFound(text="unknown robot")
        return live[rid]

    # --- reads ------------------------------------------------------------------
    async def index(_):
        return web.FileResponse(WEB / "index.html", headers=NO_CACHE)

    def login_state(rid):
        return maps.session[rid]["state"] if maps and rid in maps.session else "off"

    async def state(_):
        return web.json_response({rid: {**t.snapshot(), "map_login": login_state(rid)} for rid, t in live.items()},
                                 headers=NO_CACHE)

    async def trail(request):
        t = robot_or_404(request)
        try:
            since = int(request.query.get("since", 0))
        except ValueError:
            since = 0
        run, pts = t.trail_since(since)
        return web.json_response({"run": run, "since": since, "points": pts}, headers=NO_CACHE)

    async def cloud_status(_):
        return web.json_response({"enabled": maps is not None, "robots": maps.status if maps else {},
                                  "login": maps.session if maps else {}},
                                 headers=NO_CACHE)

    async def robot_maps(request):
        rid = robot_or_404(request).robot.id
        try:
            options = await asyncio.get_running_loop().run_in_executor(None, commander.map_options, rid)
        except Exception as ex:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"{type(ex).__name__}: {ex}"}, status=502)
        return web.json_response({"ok": True, "maps": options}, headers=NO_CACHE)

    async def map_file(request):
        robot_or_404(request)
        name = request.match_info["name"]
        if name not in ("meta", "base.png"):
            raise web.HTTPNotFound()
        path = DATA / "maps" / request.match_info["rid"] / ("meta.json" if name == "meta" else "base.png")
        if not path.exists():
            raise web.HTTPNotFound(text="no map imported for this robot")
        return web.FileResponse(path, headers=NO_CACHE)

    # --- commands (validation lives in commands.py, shared with the MQTT bridge) ---
    commander = Commander(robots, DATA)
    commander.poke = lambda rid: live[rid].poke()
    commander.map_login = login_state
    if maps is not None:
        def _after_rename(rid):                    # a rename shows up in the cloud map one upload later
            for _ in range(5):
                if maps.refresh(rid).get("changed"):
                    return

        def _later(target):                        # run a map refresh in the background (robots without a map picture: nothing)
            def hook(rid):
                if rid in maps.robots:
                    threading.Thread(target=target, args=(rid,), daemon=True, name="map-refresh").start()
            return hook

        commander.on_room_renamed = _later(_after_rename)
        commander.on_map_changed = _later(maps.refresh)      # a map switch: pull the new map right away
    mqtt_cfg = config.load_mqtt(opts)              # optional: the MQTT settings (Configuration tab)
    if mqtt_cfg and robots:
        mqttbridge.MqttBridge(mqtt_cfg, names, live, commander).start()
    elif robots:
        print("mqtt: no username set, the Home Assistant entities are off", flush=True)

    async def command(request):
        rid = robot_or_404(request).robot.id
        if request.headers.get("X-Requested-With") != "vac":
            raise web.HTTPForbidden(text="missing X-Requested-With header")
        try:
            body = await request.json() if request.can_read_body else {}
            if not isinstance(body, dict):
                raise Refused("body must be a JSON object")
            action = request.match_info["action"]
            print(f"{time.strftime('%H:%M:%S')} cmd {rid} {action} {json.dumps(body)}", flush=True)
            res = await asyncio.get_running_loop().run_in_executor(None, commander.run, rid, action, body)
            live[rid].poke()
            return web.json_response({"ok": True, **res})
        except Refused as ex:
            return web.json_response({"ok": False, "error": str(ex)}, status=ex.status)
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        except RobotError as ex:
            return web.json_response({"ok": False, "error": str(ex)}, status=502)
        except Exception as ex:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"{type(ex).__name__}: {ex}"}, status=502)

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", state),
        web.get("/api/trail/{rid}", trail),
        web.get("/api/cloudmap", cloud_status),
        web.get("/api/maps/{rid}", robot_maps),
        web.get("/api/map/{rid}/{name}", map_file),
        web.post("/api/{rid}/{action}", command),
    ])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=int(os.environ.get("C103_PORT", "8099")))
