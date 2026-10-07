"""Local (LAN-only) client for one xiaomi.vacuum.c103.

Adapted from xiaomi-vac's IjaiVacuumDevice (MIT, (c) 2026 letitbe-dull); see
third_party/LICENSE-xiaomi-vac.txt. Synchronous; call it from a worker thread.
Everything goes over MIoT/UDP 54321 with the robot's IP and token. No cloud.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

from miio import MiotDevice

from . import spec


class RobotError(Exception):
    """A read the app depends on failed (network, token or protocol)."""


@dataclass
class Status:
    activity: str
    raw_status: int
    fault: int | None
    battery: int | None
    mode: int | None
    sweep_type: int | None
    fan: int | None
    water: int | None
    repeat: bool | None
    alarm: bool | None
    volume: int | None
    cleaning_time_min: int | None
    cleaning_area_m2: int | None
    consumables: dict = field(default_factory=dict)  # name -> {life_pct, hours_left}
    dnd: dict = field(default_factory=dict)
    mop_route: int | None = None
    box: int | None = None
    cloth: int | None = None
    new_map: bool | None = None
    firmware: str | None = None
    serial: str | None = None


@dataclass
class PathTail:
    """Recent trail points as (index, x_m, y_m, heading_rad); the last one is the robot."""
    points: list[tuple[int, float, float, float]]
    timestamp: int | None


# What changes while a robot works (6 properties = one request). Everything else is the slow set.
FAST_PROPS = (spec.STATUS, spec.FAULT, spec.BATTERY, spec.ALARM, spec.CLEANING_TIME, spec.CLEANING_AREA)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _text(v):
    return v if isinstance(v, str) and v else None


class Robot:
    def __init__(self, rid: str, host: str, token: str, timeout: int = 4):
        self.id = rid
        self.host = host
        self._dev = MiotDevice(host, token, mapping={}, timeout=timeout)
        self._lock = threading.Lock()  # one conversation with the robot at a time

    # --- low level ------------------------------------------------------------
    def _get(self, props: list[tuple[int, int]]) -> dict:
        """Read props in chunks. Returns {(siid, piid): value}; the robot silently drops
        props it will not answer, so replies are matched by siid/piid, never by position."""
        out: dict = {}
        with self._lock:
            for i in range(0, len(props), 8):
                chunk = props[i:i + 8]
                try:
                    res = self._dev.send("get_properties", [
                        {"did": f"{s}-{p}", "siid": s, "piid": p} for s, p in chunk])
                except Exception as ex:  # noqa: BLE001
                    raise RobotError(f"{self.id}: read failed: {ex}") from ex
                for r in res or []:
                    if isinstance(r, dict) and r.get("code") == 0:
                        out[(r["siid"], r["piid"])] = r.get("value")
        return out

    def _set(self, prop: tuple[int, int], value) -> None:
        with self._lock:
            res = self._dev.send("set_properties", [
                {"did": f"{prop[0]}-{prop[1]}", "siid": prop[0], "piid": prop[1], "value": value}])
        code = res[0].get("code") if res and isinstance(res[0], dict) else None
        if code != 0:
            raise RobotError(f"{self.id}: write {prop}={value!r} rejected (code {code})")

    def _action(self, act: tuple[int, int], inputs: list[tuple[int, object]] | None = None) -> dict:
        with self._lock:
            return self._dev.send("action", {
                "did": f"call-{act[0]}-{act[1]}", "siid": act[0], "aiid": act[1],
                "in": [{"piid": p, "value": v} for p, v in (inputs or [])]})

    def _action_lenient(self, act, inputs=None) -> str | None:
        """The robot often applies a command but never acknowledges it (-9999). Return the
        error text instead of raising; callers verify by reading state."""
        try:
            self._action(act, inputs)
            return None
        except Exception as ex:  # noqa: BLE001
            return f"{type(ex).__name__}: {ex}"

    # --- telemetry ------------------------------------------------------------
    def status(self, full: bool = True) -> Status:
        """full=False reads only the few values that change during a run (one request); the
        other fields of the result are then None/empty and the caller keeps what it had."""
        want = list(FAST_PROPS)
        if full:
            want += [spec.MODE, spec.SWEEP_TYPE, spec.VOLUME, spec.REPEAT, spec.FAN, spec.WATER,
                     spec.MOP_ROUTE, spec.BOX, spec.CLOTH, spec.MAP_LIST_HAS_NEW,
                     spec.FIRMWARE, spec.SERIAL, *spec.DND.values()]
            for c in spec.CONSUMABLES.values():
                want += [c["life_pct"], c["hours_left"]]
        v = self._get(want)
        raw = _int(v.get(spec.STATUS))
        if raw is None:
            raise RobotError(f"{self.id}: no status in reply")
        return Status(
            activity=spec.ACTIVITY.get(raw, "idle"), raw_status=raw,
            fault=_int(v.get(spec.FAULT)), battery=_int(v.get(spec.BATTERY)),
            mode=_int(v.get(spec.MODE)), sweep_type=_int(v.get(spec.SWEEP_TYPE)),
            fan=_int(v.get(spec.FAN)), water=_int(v.get(spec.WATER)),
            repeat=None if v.get(spec.REPEAT) is None else bool(_int(v.get(spec.REPEAT))),
            alarm=v.get(spec.ALARM) if isinstance(v.get(spec.ALARM), bool) else None,
            volume=_int(v.get(spec.VOLUME)),
            cleaning_time_min=_int(v.get(spec.CLEANING_TIME)),
            cleaning_area_m2=_int(v.get(spec.CLEANING_AREA)),
            consumables={name: {k: _int(v.get(p)) for k, p in c.items()}
                         for name, c in spec.CONSUMABLES.items()} if full else {},
            dnd={k: _int(v.get(p)) for k, p in spec.DND.items()} if full else {},
            firmware=_text(v.get(spec.FIRMWARE)), serial=_text(v.get(spec.SERIAL)),
            mop_route=_int(v.get(spec.MOP_ROUTE)), box=_int(v.get(spec.BOX)), cloth=_int(v.get(spec.CLOTH)),
            new_map=None if v.get(spec.MAP_LIST_HAS_NEW) is None else bool(_int(v.get(spec.MAP_LIST_HAS_NEW))),
        )

    def path_tail(self) -> PathTail:
        """The robot's recent trail. Value is a JSON list of 5-tuples
        [index, x, y, heading, flag] with a trailing Unix timestamp; x/y are metres in the
        map frame (dock is near the origin). Holds at most ~15 points: poll <= 1.5 s."""
        raw = self._get([spec.CURRENT_PATH]).get(spec.CURRENT_PATH)
        try:
            a = json.loads(raw) if raw else []
        except ValueError:
            a = []
        pts = [(int(a[i]), float(a[i + 1]), float(a[i + 2]), float(a[i + 3]))
               for i in range(0, len(a) - 4, 5)]
        ts = int(a[-1]) if len(a) % 5 == 1 else None
        return PathTail(pts, ts)

    # --- control --------------------------------------------------------------
    def is_cleaning(self) -> bool:
        return (_int(self._get([spec.STATUS]).get(spec.STATUS)) or 0) in spec.CLEANING_STATES

    def clean_rooms(self, room_ids: list[int], verify_s: float = 8.0) -> dict:
        """Start a room clean. Room ids go as a CSV *string*: an integer is read as 'no rooms',
        i.e. a full clean. Returns {started, ack_error}; verified by reading status."""
        csv = ",".join(str(int(r)) for r in room_ids)
        if not csv:
            raise ValueError("no room ids")
        err = self._action_lenient(spec.SET_ROOM_CLEAN, [(24, csv), (25, 0), (26, 1)])
        return {"started": self._wait_cleaning(verify_s), "ack_error": err}

    def clean_zone(self, x0: float, y0: float, x1: float, y1: float, verify_s: float = 12.0) -> dict:
        """Clean a rectangle given by two opposite corners (metres, map frame). The c103 wants the
        four corners as one comma string written to zone-points, then a separate start action."""
        xa, xb = sorted((x0, x1))
        ya, yb = sorted((y0, y1))
        corners = [(xa, yb), (xb, yb), (xb, ya), (xa, ya)]  # top-left, top-right, bottom-right, bottom-left
        value = ",".join(f"{v:g}" for c in corners for v in c)
        self._set(spec.ZONE_POINTS, value)
        err = self._action_lenient(spec.START_ZONE_CLEAN)
        return {"zone": value, "started": self._wait_cleaning(verify_s), "ack_error": err}

    def _wait_cleaning(self, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(1.5)
            if self.is_cleaning():
                return True
        return False

    def start(self) -> str | None:
        return self._action_lenient(spec.START)

    def stop(self) -> str | None:  # ends the run; pause() keeps it
        return self._action_lenient(spec.STOP)

    def is_paused(self) -> bool:
        return _int(self._get([spec.STATUS]).get(spec.STATUS)) == spec.PAUSED_STATE

    def _oper(self, oper: int) -> str | None:
        return self._action_lenient(spec.SET_ROOM_CLEAN, [(24, ""), (25, 0), (26, oper)])

    def pause(self) -> str | None:
        return self._oper(spec.OPER_PAUSE)

    def resume(self) -> str | None:
        return self._oper(spec.OPER_START)

    def dock(self) -> str | None:
        return self._action_lenient(spec.CHARGE)

    def locate(self, on: bool = True) -> None:
        self._set(spec.ALARM, on)

    def set_fan(self, name: str) -> None:
        self._set(spec.FAN, spec.FAN_SPEEDS[name])

    def set_water(self, name: str) -> None:
        self._set(spec.WATER, spec.WATER_LEVELS[name])

    def set_mode(self, name: str) -> None:
        self._set(spec.MODE, spec.MODES[name])

    def set_mop_route(self, name: str) -> None:
        self._set(spec.MOP_ROUTE, spec.MOP_ROUTES[name])

    def set_repeat(self, on: bool) -> None:
        self._set(spec.REPEAT, 1 if on else 0)

    def set_volume(self, level: int) -> None:
        self._set(spec.VOLUME, int(level))

    def reset_consumable(self, name: str, verify_s: float = 3.0) -> dict:
        """Zero one wear counter (after the part was replaced). The robot may not acknowledge, so
        read the life percentage back."""
        ack = self._action_lenient(spec.RESET_CONSUMABLE[name])
        time.sleep(verify_s)
        pct = spec.CONSUMABLES[name]["life_pct"]
        return {"ack_error": ack, "life_pct": _int(self._get([pct]).get(pct))}

    # --- maps -----------------------------------------------------------------
    def map_list(self) -> list[dict]:
        """[{'name', 'id', 'cur'}, ...] straight from the robot."""
        res = self._action(spec.GET_MAP_LIST)
        for out in res.get("out", []):
            if out.get("piid") == 4:
                try:
                    data = json.loads(out["value"])
                except (ValueError, KeyError):
                    return []
                return data if isinstance(data, list) else []
        return []

    def request_map_upload(self, map_id: int) -> str | None:
        """Ask the robot to push a fresh copy of this map to Xiaomi's storage."""
        return self._action_lenient(spec.UPLOAD_BY_MAPID_II, [(6, int(map_id))])

    def rename_room(self, map_id: int, room_id: int, name: str) -> str | None:
        """Store a room name in the robot's own map (it applies this without acknowledging)."""
        return self._action_lenient(spec.RENAME_ROOM, [(6, int(map_id)), (9, int(room_id)), (10, name)])

    def set_current_map(self, map_id: int) -> str | None:
        return self._action_lenient(spec.SET_CUR_MAP, [(6, int(map_id))])

    def mac(self) -> str | None:
        try:
            return self.info().mac_address
        except Exception:  # noqa: BLE001
            return None

    def wifi_sn(self) -> str | None:
        """Serial that seeds the map AES key: siid 1 / piid 5 (piid 3 on some firmware); 16-24
        upper-case alphanumerics."""
        for piid in (5, 3):
            v = self._get([(1, piid)]).get((1, piid))
            if isinstance(v, str) and 16 <= len(v) <= 24 and v.isalnum() and v == v.upper():
                return v
        return None

    def info(self):
        """miIO info (model, firmware, MAC); no secrets."""
        with self._lock:
            return self._dev.info()
