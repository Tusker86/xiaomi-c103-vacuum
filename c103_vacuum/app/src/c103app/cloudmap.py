"""Keep the saved map fresh from the Xiaomi cloud while the robots report their position locally.

The cloud owns the static map (walls, rooms, zones); the robot owns the live pose and trail. This
module only rewrites the static map files, so a refresh can never move or erase the robot.

Test CLI (no files written):   python -m c103app.cloudmap fetch <robot id>
Refresh once, writing files:   python -m c103app.cloudmap refresh <robot id>
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from . import config, maprender
from .account import Account
from .mapfetch import FetchResult, MapFetcher
from .robot import Robot

LIVE_KEYS = ("path", "vacuum", "vacuum_room", "vacuum_room_name", "goto")  # live data: never stored with the map

IDLE_EVERY_S = 300         # docked/idle: look for map changes every 5 minutes
CLEANING_EVERY_S = 60      # while cleaning the map grows; refresh about once a minute
AFTER_RUN_DELAYS_S = (20, 90)   # when a run ends, fetch the final map twice
UPLOAD_WAIT_S = 6
RETRY_S = 90               # after a failure (often one slow download) try again soon ...
FAIL_BACKOFF_S = 1800      # ... and only back off for long after this many failures in a row
MAX_QUICK_RETRIES = 4


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def match_all(robots: dict[str, Robot], found: list[dict]) -> dict[str, dict]:
    """The Xiaomi device of each robot (by its MAC, else by its IP). A robot with none gets no map picture."""
    out = {}
    for rid, robot in robots.items():
        mac = (robot.mac() or "").lower()
        d = next((d for d in found if d["mac"] == mac or d["host"] == robot.host), None)
        if d:
            out[rid] = d
        elif found:
            print(f"map {rid}: no robot on the Xiaomi account matches {robot.host}; no map picture for it.", flush=True)
    return out


class CloudMaps:
    """Per-robot cloud fetcher on the account's one session, plus the code that turns a fetched map
    into the files the web page shows."""

    def __init__(self, robots: dict[str, Robot], data_dir: Path, acct: Account, info: dict[str, dict]):
        self.robots, self.data, self.acct, self.info = robots, Path(data_dir), acct, info
        self._fetchers: dict[str, MapFetcher] = {}
        self._busy = {rid: threading.Lock() for rid in robots}     # one refresh per robot at a time
        self.status: dict[str, dict] = {rid: {} for rid in robots}
        self.session = {rid: acct.health for rid in robots}        # Xiaomi login health, shared

    def _fetcher(self, rid: str) -> MapFetcher:
        if rid not in self._fetchers:
            robot, d = self.robots[rid], self.info[rid]
            mac, sn = robot.mac() or d["mac"], robot.wifi_sn()
            if not mac or not sn:
                raise RuntimeError("could not read the robot's MAC / wifi serial (needed for the map key)")
            self._fetchers[rid] = MapFetcher(self.acct.cloud, server=d["region"], device_id=d["did"],
                                             mac=mac, wifi_sn=sn)
        return self._fetchers[rid]

    def mark_failing(self, rid: str) -> None:
        """Map downloads keep failing (a dead Xiaomi login, or Xiaomi's storage being down)."""
        if self.acct.health.get("state") not in ("failing", "paste_rejected"):
            self.acct.set_state("failing")
            print("xiaomi: map downloads keep failing. The login may have expired "
                  "(Documentation tab, 'Renewing the Xiaomi login'), or Xiaomi's storage is down.", flush=True)

    def fetch(self, rid: str) -> FetchResult:
        """One fetch; if the session looks expired, renew it with the passToken and try again."""
        with self.acct.lock:
            res = self._fetcher(rid).fetch()
            if res.vector is None and any(o == "no_url" for o in res.outcomes.values()) and self.acct.renew():
                res = self._fetcher(rid).fetch()
        return res

    def refresh(self, rid: str, request_upload: bool = True) -> dict:
        with self._busy[rid]:
            return self._refresh(rid, request_upload)

    def _refresh(self, rid: str, request_upload: bool) -> dict:
        """Ask the robot for a fresh upload, fetch it and, if the map changed, rewrite its files."""
        st = self.status[rid]
        st["checked_at"] = int(time.time())
        try:
            if request_upload:
                cur = next((m["id"] for m in self.robots[rid].map_list() if m.get("cur")), None)
                if cur is not None:
                    self.robots[rid].request_map_upload(cur)
                    time.sleep(UPLOAD_WAIT_S)
            res = self.fetch(rid)
        except Exception as ex:  # noqa: BLE001
            st.update(ok=False, error=f"{type(ex).__name__}: {ex}")
            return st
        st.update(outcomes=res.outcomes, ok=res.vector is not None, error=None)
        if res.vector is not None:               # a map came down, so the login works
            self.acct.set_state("ok")
        if res.vector is None:
            st["error"] = "no readable map: " + ", ".join(f"slot {s}: {o}" for s, o in res.outcomes.items())
            return st
        out = self.data / "maps" / rid
        meta_path = out / "meta.json"
        old = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        # Room names are stored outside the pixel data the hash covers, so compare them separately.
        names_sig = json.dumps(sorted((r["id"], r["name"]) for r in res.vector.get("rooms", [])), ensure_ascii=False)
        if (old.get("content_hash") == res.content_hash and old.get("map_id") == res.vector["map_id"]
                and old.get("names_sig") == names_sig):
            st["changed"] = False
            return st
        vec = {k: v for k, v in res.vector.items() if k not in LIVE_KEYS}
        png, meta = maprender.render(vec, rid)
        meta.update(imported_at=int(time.time()), source="cloud", content_hash=res.content_hash, names_sig=names_sig)
        out.mkdir(parents=True, exist_ok=True)
        _write_json(out / "vector.json", vec)
        (out / "base.png.tmp").write_bytes(png)
        os.replace(out / "base.png.tmp", out / "base.png")
        _write_json(meta_path, meta)           # last: the page watches meta.imported_at
        st.update(changed=True, changed_at=meta["imported_at"], map_id=meta["map_id"])
        if not any(r.get("name") for r in meta.get("rooms", [])):
            print(f"map {rid}: saved, but no room has a name yet. Select a room on the page and press Rename.",
                  flush=True)
        return st


class Scheduler(threading.Thread):
    """Decides when each robot's map is refreshed (needs the RobotLive objects for the activity)."""

    def __init__(self, maps: CloudMaps, lives: dict):
        super().__init__(daemon=True, name="cloudmap")
        self.maps, self.lives = maps, lives
        now = time.time()
        self.due = {rid: now + 10 + 15 * i for i, rid in enumerate(maps.robots)}   # staggered start
        self.prev = {rid: "unknown" for rid in maps.robots}
        self.after_run: dict[str, list[float]] = {rid: [] for rid in maps.robots}
        self.fails = {rid: 0 for rid in maps.robots}
        self.said: dict[str, str | None] = {rid: None for rid in maps.robots}

    def run(self) -> None:
        while True:
            now = time.time()
            for rid, live in self.lives.items():
                act = live.snapshot().get("activity", "unknown")
                if self.prev[rid] in ("cleaning", "returning") and act in ("docked", "idle"):
                    self.after_run[rid] = [now + d for d in AFTER_RUN_DELAYS_S]
                self.prev[rid] = act
                due = self.due[rid]
                pending = [t for t in self.after_run[rid] if t <= now]
                if pending or now >= due:
                    self.after_run[rid] = [t for t in self.after_run[rid] if t > now]
                    st = self.maps.refresh(rid)
                    every = CLEANING_EVERY_S if act == "cleaning" else IDLE_EVERY_S
                    if st.get("error") != self.said[rid]:
                        self.said[rid] = st.get("error")
                        if self.said[rid]:
                            print(f"map {rid}: {self.said[rid]}", flush=True)
                    if st.get("ok"):
                        self.fails[rid] = 0
                        self.due[rid] = time.time() + every
                    else:
                        self.fails[rid] += 1
                        if self.fails[rid] > MAX_QUICK_RETRIES:     # about 8 minutes of failures in a row
                            self.maps.mark_failing(rid)
                        self.due[rid] = time.time() + (RETRY_S if self.fails[rid] <= MAX_QUICK_RETRIES else FAIL_BACKOFF_S)
            time.sleep(5)


def _main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("fetch", "refresh"):
        print(__doc__)
        return 2
    rid = argv[1]
    opts = config.load_options()
    data = Path(os.environ.get("C103_DATA", Path(__file__).resolve().parents[2] / "data"))
    acct = Account(os.environ.get("C103_STATE", data.parent), opts)
    found = acct.devices()
    robots, _ = config.robots(opts, found)
    maps = CloudMaps(robots, data, acct, match_all(robots, found))
    if argv[0] == "fetch":
        res = maps.fetch(rid)
        v = res.vector
        print("outcomes:", res.outcomes)
        if v:
            print("map_id:", v["map_id"], "| grid", v["size"], "| rooms:", [(r["id"], r["name"]) for r in v["rooms"]],
                  "| path pts:", len(v.get("path", [])), "| hash:", res.content_hash[:12])
    else:
        print(json.dumps(maps.refresh(rid), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
