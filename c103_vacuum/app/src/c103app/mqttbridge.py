"""MQTT bridge: publishes each robot to Home Assistant (MQTT discovery) and accepts commands.

Topics (prefix c103app):
  c103app/bridge                   online/offline (last-will), retained
  c103app/<rid>/availability       online/offline per robot, retained
  c103app/<rid>/state              JSON with everything the sensors/selects/switch/number read, retained
  c103app/<rid>/vacuum             JSON for the vacuum entity (state, battery_level, fan_speed), retained
  c103app/<rid>/vacuum/{cmd,fan,send}   vacuum entity commands (start/pause/stop/return_to_base/locate; fan; send_command)
  c103app/<rid>/set/<key>          select/switch/number commands (fan, water, mode, repeat, volume)
  c103app/<rid>/btn/<name>         button presses (locate, dock, stop, start, all_rooms, room_<id>, reset_<part>)
  c103app/<rid>/last_result        JSON result of the last command (for debugging)
Every command goes through commands.Commander: the same checks as the web page.
"""
from __future__ import annotations

import json
import threading
import time

import paho.mqtt.client as mqtt

from . import spec
from .commands import Commander, Refused
from .robot import RobotError

P = "c103app"
DISCOVERY = "homeassistant"
HEARTBEAT_S = 60
MAPS_EVERY_S = 60      # how often the "Active map" select looks at the shared saved-map list (memory only) ...
MAPS_ROBOT_EVERY_S = 600   # ... and the robot itself is only asked this often; a map switch refreshes the list at once
STALE_S = 30           # a robot whose live data is older than this is reported unavailable

INV = lambda d: {v: k for k, v in d.items()}  # noqa: E731
FAN_NAMES, WATER_NAMES = INV(spec.FAN_SPEEDS), INV(spec.WATER_LEVELS)
MODE_NAMES, SWEEP_NAMES = INV(spec.MODES), INV(spec.SWEEP_TYPES)
ROUTE_NAMES = INV(spec.MOP_ROUTES)


def dnd_window(dnd: dict) -> str | None:
    """The do-not-disturb hours as "22:00-08:00", or None when the robot did not report them."""
    parts = [dnd.get(k) for k in ("start_hour", "start_minute", "end_hour", "end_minute")]
    if None in parts:
        return None
    return "{:02d}:{:02d}-{:02d}:{:02d}".format(*parts)
HA_STATES = {"cleaning", "returning", "docked", "idle", "paused"}
CONS_LABELS = {"main_brush": "Main brush", "side_brush": "Side brush", "filter": "Filter", "mop": "Mop"}


def _j(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


class MqttBridge(threading.Thread):
    def __init__(self, cfg: dict, names: dict[str, str], lives: dict, commander: Commander):
        super().__init__(daemon=True, name="mqtt-bridge")
        self.names, self.lives, self.cmd = names, lives, commander
        self._last: dict[str, str] = {}
        self._beat: dict[str, float] = {}
        self._maps: dict[str, list[dict]] = {}       # per robot: Commander.map_options()
        self._maps_at = 0.0
        self._room_names: dict[str, dict[int, str]] = {}
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="c103app")
        self.client.username_pw_set(cfg.get("username"), cfg.get("password"))
        self.client.will_set(f"{P}/bridge", "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect_async(cfg["host"], int(cfg.get("port", 1883)), keepalive=30)
        self.client.reconnect_delay_set(2, 30)

    # --- connection ---------------------------------------------------------------
    def run(self) -> None:
        self.client.loop_start()
        while True:
            try:
                self._refresh_maps()
                self._check_room_names()
                self._publish_states()
            except Exception as ex:  # noqa: BLE001  (never let the loop die)
                print("mqtt publish error:", type(ex).__name__, ex, flush=True)
            time.sleep(1.0)

    def _check_room_names(self) -> None:
        """A room was renamed: re-announce its "Clean <room>" button under the new name."""
        for rid in self.names:
            new = self.cmd.room_names(rid)
            old = self._room_names.get(rid)
            if old is not None and old != new:
                for kind, obj, cfg in self._entities(rid):
                    if obj.startswith("room_"):
                        self.client.publish(f"{DISCOVERY}/{kind}/c103app_{rid}/{obj}/config", _j(cfg), retain=True)
                for room_id in set(old) - set(new):          # a room lost its name: remove its button
                    self.client.publish(f"{DISCOVERY}/button/c103app_{rid}/room_{room_id}/config", "", retain=True)
            self._room_names[rid] = new

    def _refresh_maps(self) -> None:
        """Keep the 'Active map' select's options current; re-announce it when the list changes."""
        if time.time() - self._maps_at < MAPS_EVERY_S:
            return
        self._maps_at = time.time()
        for rid in self.names:
            try:
                new = self.cmd.map_options(rid, max_age=MAPS_ROBOT_EVERY_S)
            except Exception as ex:  # noqa: BLE001
                print(f"mqtt: map list for {rid} failed: {type(ex).__name__}: {ex}", flush=True)
                continue
            old = self._maps.get(rid)
            self._maps[rid] = new
            if old is None or [m["label"] for m in old] != [m["label"] for m in new]:
                for kind, obj, cfg in self._entities(rid):
                    if obj == "active_map":
                        self.client.publish(f"{DISCOVERY}/{kind}/c103app_{rid}/{obj}/config", _j(cfg), retain=True)

    def _on_connect(self, client, _u, _f, rc, _p):
        print("mqtt connected:", rc, flush=True)
        if rc != 0:
            return
        client.publish(f"{P}/bridge", "online", retain=True)
        for t in (f"{P}/+/vacuum/cmd", f"{P}/+/vacuum/fan", f"{P}/+/vacuum/send", f"{P}/+/set/+",
                  f"{P}/+/btn/+", f"{DISCOVERY}/status"):
            client.subscribe(t)
        self._last.clear()                       # republish everything after a (re)connect
        self._discover()

    # --- discovery ----------------------------------------------------------------
    def _discover(self) -> None:
        for rid in self.names:
            for kind, obj, cfg in self._entities(rid):
                self.client.publish(f"{DISCOVERY}/{kind}/c103app_{rid}/{obj}/config", _j(cfg), retain=True)

    def _entities(self, rid: str):
        dev = {"identifiers": [f"c103app_{rid}"], "name": f"Vacuum App {self.names[rid]}",
               "manufacturer": "Xiaomi", "model": "Mijia 3C Enhanced (xiaomi.vacuum.c103) via c103app"}
        avail = [{"topic": f"{P}/bridge"}, {"topic": f"{P}/{rid}/availability"}]
        base = {"device": dev, "availability": avail, "availability_mode": "all", "has_entity_name": True}
        st = f"{P}/{rid}/state"

        def ent(kind, obj, name, **kw):
            return kind, obj, {**base, "unique_id": f"c103app_{rid}_{obj}", "name": name, **kw}

        yield ent("vacuum", "vacuum", None, state_topic=f"{P}/{rid}/vacuum",
                  command_topic=f"{P}/{rid}/vacuum/cmd", set_fan_speed_topic=f"{P}/{rid}/vacuum/fan",
                  send_command_topic=f"{P}/{rid}/vacuum/send", fan_speed_list=list(spec.FAN_SPEEDS),
                  supported_features=["start", "pause", "stop", "return_home", "status", "locate",
                                      "fan_speed", "send_command"])  # "battery" is no longer accepted
        yield ent("sensor", "battery", "Battery", state_topic=st, value_template="{{ value_json.battery }}",
                  device_class="battery", unit_of_measurement="%", state_class="measurement")
        yield ent("sensor", "status", "Status", state_topic=st, value_template="{{ value_json.activity }}")
        yield ent("sensor", "fault", "Robot code", state_topic=st, value_template="{{ value_json.fault }}",
                  entity_category="diagnostic")
        yield ent("sensor", "sweep_type", "Sweep type", state_topic=st, entity_category="diagnostic",
                  value_template="{{ value_json.sweep_type }}")
        yield ent("sensor", "box", "Box fitted", state_topic=st, entity_category="diagnostic",
                  value_template="{{ value_json.box }}")
        yield ent("binary_sensor", "cloth", "Mop cloth fitted", state_topic=st, entity_category="diagnostic",
                  value_template="{{ 'ON' if value_json.cloth else 'OFF' }}", payload_on="ON", payload_off="OFF")
        yield ent("binary_sensor", "new_map", "New map waiting", state_topic=st, entity_category="diagnostic",
                  value_template="{{ 'ON' if value_json.new_map else 'OFF' }}", payload_on="ON", payload_off="OFF")
        yield ent("binary_sensor", "dnd", "Do not disturb", state_topic=st, entity_category="diagnostic",
                  value_template="{{ 'ON' if value_json.dnd_on else 'OFF' }}", payload_on="ON", payload_off="OFF")
        yield ent("sensor", "dnd_window", "Do not disturb hours", state_topic=st, entity_category="diagnostic",
                  value_template="{{ value_json.dnd_window }}")
        yield ent("sensor", "firmware", "Firmware", state_topic=st, entity_category="diagnostic",
                  value_template="{{ value_json.firmware }}")
        yield ent("sensor", "serial", "Serial number", state_topic=st, entity_category="diagnostic",
                  value_template="{{ value_json.serial }}")
        yield ent("sensor", "cleaning_time", "Cleaning time", state_topic=st, unit_of_measurement="min",
                  value_template="{{ value_json.cleaning_time_min }}", device_class="duration",
                  state_class="measurement")
        yield ent("sensor", "cleaning_area", "Cleaning area", state_topic=st, unit_of_measurement="m²",
                  value_template="{{ value_json.cleaning_area_m2 }}", state_class="measurement")
        yield ent("binary_sensor", "map_login", "Xiaomi map login problem", state_topic=st, device_class="problem",
                  value_template="{{ 'ON' if value_json.map_login in ['failing', 'paste_rejected'] else 'OFF' }}",
                  payload_on="ON", payload_off="OFF", entity_category="diagnostic")
        for key, label in CONS_LABELS.items():
            yield ent("sensor", f"{key}_life", f"{label} life", state_topic=st, unit_of_measurement="%",
                      value_template=f"{{{{ value_json.consumables.{key}.life_pct }}}}", state_class="measurement",
                      entity_category="diagnostic")
            yield ent("sensor", f"{key}_hours_left", f"{label} hours left", state_topic=st,
                      unit_of_measurement="h", device_class="duration",
                      value_template=f"{{{{ value_json.consumables.{key}.hours_left }}}}",
                      entity_category="diagnostic")
        yield ent("select", "fan_speed", "Suction", state_topic=st, value_template="{{ value_json.fan }}",
                  command_topic=f"{P}/{rid}/set/fan", options=list(spec.FAN_SPEEDS))
        yield ent("select", "water_level", "Water level", state_topic=st, value_template="{{ value_json.water }}",
                  command_topic=f"{P}/{rid}/set/water", options=list(spec.WATER_LEVELS))
        yield ent("select", "cleaning_mode", "Cleaning mode", state_topic=st, value_template="{{ value_json.mode }}",
                  command_topic=f"{P}/{rid}/set/mode", options=list(spec.MODES))
        yield ent("select", "mop_route", "Mop route", state_topic=st, value_template="{{ value_json.mop_route }}",
                  command_topic=f"{P}/{rid}/set/mop_route", options=list(spec.MOP_ROUTES))
        yield ent("switch", "repeat", "Repeat clean", state_topic=st, command_topic=f"{P}/{rid}/set/repeat",
                  value_template="{{ 'ON' if value_json.repeat else 'OFF' }}", payload_on="ON", payload_off="OFF")
        yield ent("number", "volume", "Volume", state_topic=st, value_template="{{ value_json.volume }}",
                  command_topic=f"{P}/{rid}/set/volume", min=0, max=10, step=1, mode="slider")
        yield ent("switch", "find", "Find robot (beep)", state_topic=st, command_topic=f"{P}/{rid}/set/find",
                  value_template="{{ 'ON' if value_json.alarm else 'OFF' }}", payload_on="ON", payload_off="OFF",
                  icon="mdi:bell-ring")           # ON beeps for 3 s and then switches itself off
        for name, label in (("dock", "Return to dock"), ("stop", "Stop"),
                            ("start", "Start full clean")):
            yield ent("button", name, label, command_topic=f"{P}/{rid}/btn/{name}", payload_press="PRESS")
        yield ent("button", "all_rooms", "Clean all rooms", command_topic=f"{P}/{rid}/btn/all_rooms",
                  payload_press="PRESS")
        for part, label in CONS_LABELS.items():
            yield ent("button", f"reset_{part}", f"Reset {label.lower()} counter", entity_category="config",
                      command_topic=f"{P}/{rid}/btn/reset_{part}", payload_press="PRESS")
        if self._maps.get(rid):
            yield ent("select", "active_map", "Active map", state_topic=st, entity_category="config",
                      value_template="{{ value_json.active_map }}", command_topic=f"{P}/{rid}/set/active_map",
                      options=[m["label"] for m in self._maps[rid]])
        for room_id, room in self.cmd.room_names(rid).items():
            yield ent("button", f"room_{room_id}", f"Clean {room}", command_topic=f"{P}/{rid}/btn/room_{room_id}",
                      payload_press="PRESS")

    # --- state ----------------------------------------------------------------------
    def _publish_states(self) -> None:
        now = time.time()
        for rid, live in self.lives.items():
            s = live.snapshot()
            if "activity" not in s:                       # nothing read from the robot yet
                continue
            online = s["error"] is None and now - s["updated"] < STALE_S
            self._pub(rid, "availability", "online" if online else "offline")
            self._pub(rid, "state", _j({
                "activity": s["activity"], "battery": s["battery"], "fault": s["fault"],
                "fan": FAN_NAMES.get(s["fan"]), "water": WATER_NAMES.get(s["water"]),
                "mode": MODE_NAMES.get(s["mode"]), "sweep_type": SWEEP_NAMES.get(s["sweep_type"]),
                "mop_route": ROUTE_NAMES.get(s["mop_route"]),
                "box": spec.BOXES.get(s["box"]), "cloth": bool(s["cloth"]),
                "new_map": bool(s["new_map"]), "firmware": s["firmware"], "serial": s["serial"],                "dnd_on": bool(s["dnd"].get("enable")), "dnd_window": dnd_window(s["dnd"]),
                "repeat": bool(s["repeat"]), "alarm": bool(s["alarm"]), "volume": s["volume"],
                "cleaning_time_min": s["cleaning_time_min"], "cleaning_area_m2": s["cleaning_area_m2"],
                "consumables": s["consumables"],
                "map_login": self.cmd.map_login(rid) if self.cmd.map_login else "off",
                "active_map": next((m["label"] for m in self._maps.get(rid, []) if m["cur"]), None)}))
            self._pub(rid, "vacuum", _j({
                "state": s["activity"] if s["activity"] in HA_STATES else "idle",
                "battery_level": s["battery"], "fan_speed": FAN_NAMES.get(s["fan"])}))

    def _pub(self, rid: str, topic: str, payload: str) -> None:
        key = f"{rid}/{topic}"
        now = time.time()
        if self._last.get(key) == payload and now - self._beat.get(key, 0) < HEARTBEAT_S:
            return
        self._last[key], self._beat[key] = payload, now
        self.client.publish(f"{P}/{key}", payload, retain=True)

    # --- commands -------------------------------------------------------------------
    def _on_message(self, _c, _u, msg):
        payload = msg.payload.decode("utf-8", "replace").strip()
        if msg.topic == f"{DISCOVERY}/status":
            if payload == "online":                      # Home Assistant (re)started: announce again
                self._last.clear()
                self._discover()
            return
        threading.Thread(target=self._handle, args=(msg.topic, payload), daemon=True).start()

    def _handle(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        if len(parts) < 4 or parts[0] != P or parts[1] not in self.names:
            return
        rid, kind, name = parts[1], parts[2], parts[3]
        action, body = None, {}
        try:
            if kind == "vacuum" and name == "cmd":
                action = {"start": "start", "pause": "stop", "stop": "stop", "return_to_base": "dock",
                          "locate": "locate"}.get(payload)
            elif kind == "vacuum" and name == "fan":
                action, body = "set", {"key": "fan", "value": payload}
            elif kind == "vacuum" and name == "send":
                action, body = self._send_command(rid, payload)
            elif kind == "set" and name == "find":
                action = "locate" if payload.upper() in ("ON", "TRUE", "1") else "locate-off"
            elif kind == "set" and name == "active_map":
                action, body = "set-map", {"label": payload}
            elif kind == "set":
                action, body = "set", {"key": name, "value": self._setting_value(name, payload)}
            elif kind == "btn":
                action, body = self._button(rid, name)
            if action is None:
                return self._result(rid, topic, False, "unsupported command")
            print(f"{time.strftime('%H:%M:%S')} mqtt cmd {rid} {action} {_j(body)}", flush=True)
            self._result(rid, action, True, self.cmd.run(rid, action, body))
            self.lives[rid].poke()
        except (Refused, RobotError, ValueError) as ex:
            self._result(rid, action or topic, False, str(ex))
        except Exception as ex:  # noqa: BLE001
            self._result(rid, action or topic, False, f"{type(ex).__name__}: {ex}")

    @staticmethod
    def _setting_value(key: str, payload: str):
        if key == "repeat":
            return payload.upper() in ("ON", "TRUE", "1")
        if key == "volume":
            return int(float(payload))
        return payload

    def _button(self, rid: str, name: str):
        if name in ("locate", "dock", "stop", "start"):
            return name, {}
        if name.startswith("reset_") and name[6:] in spec.RESET_CONSUMABLE:
            return "reset-consumable", {"name": name[6:]}
        if name == "all_rooms":
            return "clean-rooms", {"rooms": sorted(self.cmd.room_names(rid))}
        if name.startswith("room_"):
            return "clean-rooms", {"rooms": [int(name[5:])]}
        raise ValueError("unknown button")

    @staticmethod
    def _send_command(rid: str, payload: str):
        """vacuum.send_command: payload is JSON {"command": ..., "params": ...} or a bare command."""
        try:
            obj = json.loads(payload)
        except ValueError:
            obj = {"command": payload}
        # Home Assistant flattens the service's params into the message: {"command": ..., "rooms": [...]};
        # a nested {"command": ..., "params": {...}} is accepted too.
        command = obj.get("command")
        params = obj.get("params") or {k: v for k, v in obj.items() if k != "command"}
        if command in ("clean_rooms", "clean_segment", "clean_segments"):
            rooms = params.get("rooms", params.get("segments")) if isinstance(params, dict) else params
            if isinstance(rooms, str):
                rooms = [int(x) for x in rooms.replace(" ", "").split(",") if x]
            return "clean-rooms", {"rooms": rooms}
        if command == "clean_zone" and isinstance(params, dict):
            return "clean-zone", params
        raise ValueError(f"unsupported send_command: {command!r}")

    def _result(self, rid: str, action, ok: bool, detail) -> None:
        if rid in self.names:
            self.client.publish(f"{P}/{rid}/last_result", _j({"time": int(time.time()), "action": action,
                                                              "ok": ok, "detail": detail}))
        if not ok:
            print(f"mqtt command failed: {action}: {detail}", flush=True)
