"""Web server: live API, controls and the map page.  Run:  python -m c103app.server

GET endpoints are read-only. POST endpoints send commands to a robot over the LAN. They
need the header `X-Requested-With: vac` (so another website cannot make your browser send commands).
"""
from __future__ import annotations

import asyncio
import json
import logging
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
MERGE_POLLS, MERGE_POLL_S = 12, 15        # after a merge: look at the map every 15 s, for 3 minutes


def map_stamp(data: Path, rid: str) -> int:
    """When this robot's saved map was last rewritten (meta.json is written last), 0 if there is none.
    The page compares it with the one it knows, so it never has to ask for the map files to find out."""
    try:
        return int((data / "maps" / rid / "meta.json").stat().st_mtime)
    except OSError:
        return 0


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

    async def robot_image(_):
        return web.FileResponse(WEB / "robot.png", headers=NO_CACHE)

    def login_state(rid):
        return maps.session[rid]["state"] if maps and rid in maps.session else "off"

    def all_state() -> dict:
        return {rid: {**t.snapshot(), "map_login": login_state(rid), "map_at": map_stamp(DATA, rid)}
                for rid, t in live.items()}

    async def state(_):
        return web.json_response(all_state(), headers=NO_CACHE)

    async def live_view(request):
        """The page's one request: every robot's state and, when it asks (?floor=&since=), that floor's trail."""
        out = {"state": all_state(), "trail": None}
        rid = request.query.get("floor")
        if rid in live:
            try:
                since = int(request.query.get("since", 0))
            except ValueError:
                since = 0
            run, pts = live[rid].trail_since(since)
            out["trail"] = {"run": run, "since": since, "points": pts}
        return web.json_response(out, headers=NO_CACHE)

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

    async def merge_state(request):
        rid = robot_or_404(request).robot.id
        return web.json_response(commander.merge_status.get(rid, {"state": "idle", "message": ""}), headers=NO_CACHE)

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

        def _merge(rid, map_id, rooms, name):
            """Send the merge to Xiaomi, then watch the saved map in the background: the cloud applies it
            a little later, and the merged room comes back without its name."""
            if rid not in maps.robots:
                raise Refused("this robot has no map from the Xiaomi cloud, so it cannot be merged", 409)
            try:
                backup = maps.merge_rooms(rid, map_id, rooms)
            except Exception as ex:  # noqa: BLE001
                raise Refused(str(ex), 502)
            commander.merge_status[rid] = {"state": "working", "message": "Merging. This takes about a minute."}
            threading.Thread(target=_finish_merge, args=(rid, rooms, name), daemon=True, name="merge").start()
            return {"backup": backup}

        def _finish_merge(rid, rooms, name):
            st = commander.merge_status[rid]
            try:
                for _ in range(MERGE_POLLS):
                    time.sleep(MERGE_POLL_S)
                    maps.refresh(rid)
                    left = [r for r in rooms if r in commander.map_rooms(rid)]
                    if len(left) < len(rooms):
                        break
                else:
                    st.update(state="unchanged", message="Xiaomi accepted it, but the map has not changed. "
                                                         "The rooms may not touch each other.")
                    return
                commander.run(rid, "rename-room", {"room": left[0], "name": name})
                st.update(state="done", message=f'Merged. The room is now "{name}"; the map shows it within a minute.')
            except Exception as ex:  # noqa: BLE001
                st.update(state="error", message=f"The merge went through, but naming the room failed: {ex}. Rename it yourself.")

        commander.merge_cloud = _merge
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
        web.get("/robot.png", robot_image),
        web.get("/api/state", state),
        web.get("/api/live", live_view),
        web.get("/api/trail/{rid}", trail),
        web.get("/api/cloudmap", cloud_status),
        web.get("/api/maps/{rid}", robot_maps),
        web.get("/api/map/{rid}/{name}", map_file),
        web.get("/api/merge/{rid}", merge_state),
        web.post("/api/{rid}/{action}", command),
    ])
    return app


if __name__ == "__main__":
    # The map parser warns about pixel types it does not know on every decode; the maps draw fine, so keep it out of the log.
    logging.getLogger("vacuum_map_parser_ijai").setLevel(logging.ERROR)
    web.run_app(make_app(), host="0.0.0.0", port=int(os.environ.get("C103_PORT", "8099")))
