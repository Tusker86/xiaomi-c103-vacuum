"""Keep the saved map fresh from the Xiaomi cloud while the robots report their position locally.

The cloud owns the static map (walls, rooms, zones); the robot owns the live pose and trail. This
module only rewrites the static map files, so a refresh can never move or erase the robot.

Test CLI (no files written):   python -m c103app.cloudmap fetch <robot id>
Refresh once, writing files:   python -m c103app.cloudmap refresh <robot id>
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import config, maprender, spec
from .account import Account
from .mapfetch import FetchResult, MapFetcher
from .robot import Robot

LIVE_KEYS = ("path", "vacuum", "vacuum_room", "vacuum_room_name", "goto")  # live data: never stored with the map

IDLE_EVERY_S = 3600        # docked/idle: pull the map at least once an hour (it only changes through this app)
CLEANING_EVERY_S = 60      # while cleaning the map grows; refresh about once a minute
AFTER_RUN_DELAYS_S = (60,)      # when a run ends, fetch the final map once, a minute later
UPLOAD_WAIT_S = 6
RETRY_S = 90               # after a failure (often Xiaomi being slow or busy) try again soon, then ease off:
MAX_RETRY_S = 1800         # 90 s, 3 min, 6 min, 12 min, 24 min, then every 30 min
FAILING_AFTER = 6          # this many failures in a row (about 45 minutes) count as "login problem"


def retry_after(fails: int) -> float:
    """Seconds until the next try after `fails` failed refreshes in a row."""
    return min(MAX_RETRY_S, RETRY_S * 2 ** max(fails - 1, 0))


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def match_all(robots: dict[str, Robot], found: list[dict]) -> dict[str, dict]:
    """The Xiaomi device of each robot (by its MAC, else by its IP). A robot with none gets no map picture."""
    out = {}
    for rid, robot in robots.items():
        mac = (robot.mac() or "").lower()
        d = next((d for d in found if d["mac"].lower() == mac or d["host"] == robot.host), None)
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

    def mark_failing(self) -> None:
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

    def merge_rooms(self, rid: str, map_id: int, rooms: list[int]) -> str:
        """Merge rooms through the Xiaomi cloud (the robot ignores the same call over the LAN). Copies the
        saved map first, since a merge cannot be undone. Returns the backup folder name."""
        src, stamp = self.data / "maps" / rid, time.strftime("%Y%m%d-%H%M%S")
        name = f"pre-merge-{rid}-{stamp}"
        dest = self.data / "backups" / name
        dest.mkdir(parents=True, exist_ok=True)
        for f in ("vector.json", "base.png", "meta.json"):
            if (src / f).exists():
                shutil.copy2(src / f, dest / f)
        d = self.info[rid]
        with self.acct.lock:
            resp = self.acct.cloud.action(d["region"], d["did"], *spec.MERGE_ROOMS,
                                          [int(map_id), ",".join(str(r) for r in rooms), "en"])
        code = (resp or {}).get("result", {}).get("code") if isinstance(resp, dict) else None
        if resp is None or (resp.get("code") not in (0, None)) or code not in (0, None):
            raise RuntimeError(f"Xiaomi did not accept the merge (reply: {json.dumps(resp)[:200]})")
        return name

    def refresh(self, rid: str, request_upload: bool = True) -> dict:
        """One refresh. Never raises: an error is returned in the status (a crash here would end the scheduler)."""
        with self._busy[rid]:
            try:
                return self._refresh(rid, request_upload)
            except Exception as ex:  # noqa: BLE001
                self.status[rid].update(ok=False, error=f"{type(ex).__name__}: {ex}")
                return self.status[rid]

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
        if res.vector is None:
            st["error"] = "no readable map: " + ", ".join(f"slot {s}: {o}" for s, o in res.outcomes.items())
            return st
        self.acct.set_state("ok")                # a map came down, so the login works
        out = self.data / "maps" / rid
        meta_path = out / "meta.json"
        old = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        # Room names are stored outside the pixel data the hash covers, so compare them separately.
        names_sig = json.dumps(sorted((r["id"], r["name"]) for r in res.vector.get("rooms", [])), ensure_ascii=False)
        if (old.get("content_hash") == res.content_hash and old.get("map_id") == res.vector["map_id"]
                and old.get("names_sig") == names_sig):
            st["changed"] = False
            return st
        if (old.get("map_id") == res.vector["map_id"] and not res.vector.get("rooms")
                and json.loads(old.get("names_sig") or "[]")):
            # Xiaomi's second storage slot is an older copy without room names. When the first slot could
            # not be read for a moment, the fallback must not wipe the names of the same map.
            st["changed"] = False
            return st
        vec = {k: v for k, v in res.vector.items() if k not in LIVE_KEYS}
        png, meta = maprender.render(vec)
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
        self.wake = threading.Event()             # a robot's status changed: look again now
        for live in lives.values():
            live.notify = self.wake.set

    def _sleep_s(self) -> float:
        """Until the next pull is due (never a busy loop, never longer than an hour)."""
        nxt = min([*self.due.values(), *(t for ts in self.after_run.values() for t in ts)], default=time.time() + 3600)
        return min(3600.0, max(0.05, nxt - time.time()))

    def run(self) -> None:
        while True:
            self.wake.clear()                      # before looking: a change that comes later is not lost
            now = time.time()
            for rid, live in self.lives.items():
                act = live.snapshot().get("activity", "unknown")
                if self.prev[rid] in ("cleaning", "returning") and act in ("docked", "idle"):
                    self.after_run[rid] = [now + d for d in AFTER_RUN_DELAYS_S]
                if act == "cleaning" and self.prev[rid] != "cleaning":
                    self.due[rid] = min(self.due[rid], now + CLEANING_EVERY_S)   # the map grows: pull every minute
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
                        if self.fails[rid] >= FAILING_AFTER:
                            self.maps.mark_failing()
                        self.due[rid] = time.time() + retry_after(self.fails[rid])
            self.wake.wait(self._sleep_s())


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
