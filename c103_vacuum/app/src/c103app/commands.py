"""Validated robot commands, shared by the web page and the MQTT bridge.

Everything that moves a robot or changes a setting goes through `Commander.run`, so both front ends
get the same rules: rooms must belong to the robot's active map, zones must be sensible and inside the map, no
clean is started while the robot is already cleaning, and only one command runs per robot at a time.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

from . import spec
from .robot import Robot

MIN_ZONE_M, MAX_ZONE_M, MAX_ZONE_AREA_M2 = 0.4, 30.0, 200.0
QUEUE_WAIT_S = 20


class Refused(Exception):
    """A command was rejected before reaching the robot (HTTP-style status in .status)."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _num(body: dict, key: str) -> float:
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise Refused(f"'{key}' must be a number")
    return float(v)


class Commander:
    def __init__(self, robots: dict[str, Robot], data_dir: Path):
        self.robots, self.data = robots, Path(data_dir)
        self._busy = {rid: threading.Lock() for rid in robots}
        self._maps: dict[str, tuple[float, list[dict]]] = {}
        self._metas: dict[str, tuple[int, dict]] = {}       # rid -> (file mtime, parsed meta.json)
        self.map_login = None               # callable(rid) -> Xiaomi login health, set by the server
        self.poke = None                    # callable(rid): ask the live reader for an immediate re-read
        self.on_room_renamed = None         # callable(rid): fetch the map again until the new name arrives
        self.on_map_changed = None          # callable(rid): set by the server to refresh the saved map

    def _bounds(self, rid: str) -> dict:
        try:
            return json.loads((self.data / "maps" / rid / "vector.json").read_text())["bounds"]
        except (OSError, ValueError, KeyError):
            raise Refused("no map for this robot, so zones cannot be checked", 409)

    def map_options(self, rid: str, max_age: float = 30.0) -> list[dict]:
        """[{'id', 'label', 'cur'}] for the robot's saved maps (cached: it is a robot round trip)."""
        at, cached = self._maps.get(rid, (0.0, []))
        if cached and time.time() - at < max_age:
            return cached
        raw = self.robots[rid].map_list()
        names = [m.get("name") or f"Map {m['id']}" for m in raw]
        out = [{"id": int(m["id"]), "cur": bool(m.get("cur")),
                "label": n if names.count(n) == 1 else f"{n} ({m['id']})"} for m, n in zip(raw, names)]
        self._maps[rid] = (time.time(), out)
        return out

    def run(self, rid: str, action: str, body: dict | None = None) -> dict:
        """Blocking. Validates, then talks to the robot. Raises Refused or RobotError."""
        body = body or {}
        robot = self.robots[rid]
        if action == "locate-off":                      # stop a beep early; never waits behind the 3 s beep
            robot.locate(False)
            return {}
        # Queue behind a running command (settings take <1 s, a clean start ~8 s) instead of dropping
        # it, so a burst from an automation (fan + water + mode ...) all gets through.
        if not self._busy[rid].acquire(timeout=QUEUE_WAIT_S):
            raise Refused("another command is still running on this robot", 409)
        try:
            if action in ("start", "clean-rooms", "clean-zone") and robot.is_cleaning():
                raise Refused("the robot is already cleaning", 409)
            if action == "start":                       # full clean, the robot's own plan
                return {"ack_error": robot.start()}
            if action == "clean-rooms":
                return self._clean_rooms(rid, body)
            if action == "clean-zone":
                return self._clean_zone(rid, body)
            if action == "stop":                        # also pause: the c103 has no pause state
                return {"ack_error": robot.stop()}
            if action == "dock":
                return {"ack_error": robot.dock()}
            if action == "locate":
                robot.locate(True)
                if self.poke:
                    self.poke(rid)                      # so the page can show the beep while it lasts
                time.sleep(3)
                robot.locate(False)
                return {}
            if action == "set":
                return self._setting(robot, body)
            if action == "reset-consumable":
                return self._reset_consumable(rid, robot, body)
            if action == "rename-room":
                return self._rename_room(rid, robot, body)
            if action == "set-map":
                return self._set_map(rid, robot, body)
            raise Refused("unknown action", 404)
        finally:
            self._busy[rid].release()

    def _reset_consumable(self, rid: str, robot: Robot, body: dict) -> dict:
        name = body.get("name")
        if name not in spec.RESET_CONSUMABLE:
            raise Refused(f"name must be one of {sorted(spec.RESET_CONSUMABLE)}")
        if robot.is_cleaning():
            raise Refused("the robot is cleaning", 409)
        return {"name": name, **robot.reset_consumable(name)}

    def _meta(self, rid: str) -> dict:
        """The saved map's meta.json, parsed again only when the file changed (the MQTT bridge asks every second)."""
        path = self.data / "maps" / rid / "meta.json"
        try:
            stamp = path.stat().st_mtime_ns
            cached = self._metas.get(rid)
            if cached and cached[0] == stamp:
                return cached[1]
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            raise Refused("no map for this robot yet, so its rooms are not known", 409)
        self._metas[rid] = (stamp, meta)
        return meta

    def map_rooms(self, rid: str) -> dict[int, str | None]:
        """Every room on the saved map -> the name stored on the robot (None = not named yet)."""
        return {r["id"]: r.get("name") for r in self._meta(rid).get("rooms", [])}

    def _active_map(self, rid: str, strict: bool) -> dict | None:
        """The robot's active map, read fresh, after checking that the saved map is that map: room ids
        belong to one map (a switch may also come from Mi Home), and after a switch the saved copy needs
        about a minute to follow. strict: a robot that cannot tell its active map is refused too."""
        try:
            cur = next((m for m in self.map_options(rid, max_age=0) if m["cur"]), None)
        except Exception:  # noqa: BLE001
            cur = None
        if cur is None:
            if strict:
                raise Refused("could not read the robot's active map", 502)
            return None
        if self._meta(rid).get("map_id") != cur["id"]:
            raise Refused("the saved map is not the robot's active map yet (it follows within about a minute "
                          "after a map switch)", 409)
        return cur

    def room_names(self, rid: str) -> dict[int, str]:
        """The named rooms only (the ones that get a 'Clean <room>' button and take part in 'all rooms')."""
        try:
            return {i: n for i, n in self.map_rooms(rid).items() if n}
        except Refused:
            return {}

    def _rename_room(self, rid: str, robot: Robot, body: dict) -> dict:
        room, name = body.get("room"), str(body.get("name", "")).strip()
        known = self.map_rooms(rid)
        if isinstance(room, bool) or room not in known:
            raise Refused(f"room must be one of {sorted(known)}")
        if not 1 <= len(name) <= 24 or not name.isprintable():
            raise Refused("the name must be 1-24 printable characters")
        if robot.is_cleaning():
            raise Refused("the robot is cleaning", 409)
        cur = self._active_map(rid, strict=True)
        ack = robot.rename_room(cur["id"], room, name)
        if self.on_room_renamed:
            self.on_room_renamed(rid)
        return {"room": room, "name": name, "ack_error": ack}

    def _set_map(self, rid: str, robot: Robot, body: dict) -> dict:
        maps = self.map_options(rid, max_age=0)
        target = next((m for m in maps if m["id"] == body.get("map_id") or m["label"] == body.get("label")), None)
        if target is None:
            raise Refused("unknown map")
        if robot.is_cleaning():
            raise Refused("the robot is cleaning", 409)
        if target["cur"]:
            return {"map_id": target["id"], "changed": False}
        ack = robot.set_current_map(target["id"])
        time.sleep(3)
        now = self.map_options(rid, max_age=0)
        ok = any(m["cur"] and m["id"] == target["id"] for m in now)
        if ok and self.on_map_changed:
            self.on_map_changed(rid)
        if not ok:
            raise Refused(f"the robot did not switch maps (ack: {ack})", 502)
        return {"map_id": target["id"], "changed": True, "ack_error": ack}

    def _clean_rooms(self, rid: str, body: dict) -> dict:
        rooms = body.get("rooms")
        allowed = set(self.map_rooms(rid))
        if (not isinstance(rooms, list) or not rooms
                or not all(isinstance(r, int) and not isinstance(r, bool) for r in rooms)
                or not set(rooms) <= allowed):
            raise Refused(f"rooms must be a non-empty list of ids from {sorted(allowed)}")
        self._active_map(rid, strict=False)     # (a robot that cannot tell is not blocked)
        return self.robots[rid].clean_rooms(sorted(set(rooms)))

    def _clean_zone(self, rid: str, body: dict) -> dict:
        x0, y0, x1, y1 = (_num(body, k) for k in ("x0", "y0", "x1", "y1"))
        w, h = abs(x1 - x0), abs(y1 - y0)
        b = self._bounds(rid)
        if not (b["minX"] <= min(x0, x1) and max(x0, x1) <= b["maxX"]
                and b["minY"] <= min(y0, y1) and max(y0, y1) <= b["maxY"]):
            raise Refused("the zone is outside the map")
        if not (MIN_ZONE_M <= w <= MAX_ZONE_M and MIN_ZONE_M <= h <= MAX_ZONE_M) or w * h > MAX_ZONE_AREA_M2:
            raise Refused(f"zone sides must be {MIN_ZONE_M}-{MAX_ZONE_M} m and under {MAX_ZONE_AREA_M2} m2")
        return self.robots[rid].clean_zone(x0, y0, x1, y1)

    @staticmethod
    def _setting(robot: Robot, body: dict) -> dict:
        key, val = body.get("key"), body.get("value")
        if key == "fan" and val in spec.FAN_SPEEDS:
            robot.set_fan(val)
        elif key == "water" and val in spec.WATER_LEVELS:
            robot.set_water(val)
        elif key == "mode" and val in spec.MODES:
            robot.set_mode(val)
        elif key == "repeat" and isinstance(val, bool):
            robot.set_repeat(val)
        elif key == "volume" and isinstance(val, int) and not isinstance(val, bool) and 0 <= val <= 10:
            robot.set_volume(val)
        else:
            raise Refused("unknown setting or value out of range")
        return {key: val}
