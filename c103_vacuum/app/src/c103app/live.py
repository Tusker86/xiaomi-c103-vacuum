"""Live state of the robots: one polling thread per robot, entirely over the LAN.

Each thread keeps the latest status, the robot's pose and a trail. The robot only reports its
last ~15 path points, so the trail is built here by collecting new points (keyed by point index).
Point numbering restarting means a new run began, so the old line is dropped.
"""
from __future__ import annotations

import threading
import time

from .robot import Robot, RobotError

ACTIVE = {"cleaning", "returning"}
MAX_TRAIL = 6000
# The path property is a stream of <=15-point blocks: each read returns the next block (a backlog
# is handed over 15 points per read), and at the live front the block grows until it is full.
# Driving makes ~7 points/s, so reading every second keeps up with room to spare.
ACTIVE_TICK_S = 1.0
IDLE_TICK_S = 4.0
STATUS_EVERY_ACTIVE_S = 3.0


class RobotLive(threading.Thread):
    def __init__(self, robot: Robot, name: str):
        super().__init__(daemon=True, name=f"live-{robot.id}")
        self.robot = robot
        self.name_ = name
        self._lock = threading.Lock()
        self.status: dict = {}
        self.pose: dict | None = None
        self.trail: list[list[float]] = []
        self.run_id = 0           # bumps whenever the trail is restarted
        self.error: str | None = None
        self.updated = 0.0
        self._last_idx: int | None = None
        self._activity = "unknown"
        self._poke = threading.Event()   # set by poke(): re-read the robot right now

    # --- polling loop ---------------------------------------------------------
    def run(self) -> None:
        next_status = 0.0
        while True:
            now = time.time()
            active = self._activity in ACTIVE
            try:
                if now >= next_status:
                    self._take_status(self.robot.status())
                    next_status = now + (STATUS_EVERY_ACTIVE_S if self._activity in ACTIVE else IDLE_TICK_S)
                self._merge(self.robot.path_tail())
                with self._lock:
                    if self.error:
                        print(f"robot {self.robot.id}: reachable again", flush=True)
                    self.error = None
                    self.updated = time.time()
            except RobotError as ex:
                self._fail(str(ex))
            except Exception as ex:  # noqa: BLE001  (a bad reply must not kill the thread)
                self._fail(f"{type(ex).__name__}: {ex}")
            if self._poke.wait(ACTIVE_TICK_S if active else IDLE_TICK_S):
                self._poke.clear()
                next_status = 0.0     # a command just ran: read the status again immediately

    def _fail(self, msg: str) -> None:
        with self._lock:
            if self.error is None:                  # say it once, not on every poll
                print(f"robot {self.robot.id} ({self.robot.host}): {msg}. Wrong IP or token, or the robot is off "
                      "or on another network (reserve its IP in the router).", flush=True)
            self.error = msg

    def _take_status(self, st) -> None:
        was = self._activity
        with self._lock:
            self.status = {
                "activity": st.activity, "raw_status": st.raw_status, "fault": st.fault,
                "battery": st.battery, "mode": st.mode, "sweep_type": st.sweep_type,
                "fan": st.fan, "water": st.water, "repeat": st.repeat, "alarm": st.alarm,
                "volume": st.volume, "cleaning_time_min": st.cleaning_time_min,
                "cleaning_area_m2": st.cleaning_area_m2,
                "consumables": st.consumables, "dnd": st.dnd}
            self._activity = st.activity
            if was in ("docked", "idle", "unknown") and st.activity == "cleaning" and was != "unknown":
                self.trail = []         # a fresh run starts: show only the new line
                self.run_id += 1

    def _merge(self, tail) -> None:
        pts = tail.points
        if not pts:
            return
        with self._lock:
            if self._last_idx is not None and pts[-1][0] < self._last_idx - 5:
                self.trail = []          # numbering restarted: a new run
                self.run_id += 1
                self._last_idx = None
            for idx, x, y, _phi in pts:
                if self._last_idx is None or idx > self._last_idx:
                    self.trail.append([round(x, 3), round(y, 3)])
            self._last_idx = pts[-1][0]
            if len(self.trail) > MAX_TRAIL:
                del self.trail[: len(self.trail) - MAX_TRAIL]
            self.pose = {"x": pts[-1][1], "y": pts[-1][2], "phi": pts[-1][3]}

    def poke(self) -> None:
        """Ask the polling loop to re-read the robot immediately (call after a command)."""
        self._poke.set()

    # --- snapshots for the API ------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {"id": self.robot.id, "name": self.name_, **self.status, "pose": self.pose,
                    "run": self.run_id, "trail_len": len(self.trail), "error": self.error,
                    "updated": self.updated}

    def trail_since(self, since: int) -> tuple[int, list[list[float]]]:
        with self._lock:
            return self.run_id, self.trail[max(since, 0):]
