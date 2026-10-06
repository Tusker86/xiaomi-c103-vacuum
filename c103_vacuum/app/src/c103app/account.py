"""The Xiaomi account: one saved session, renewal from a pasted browser login, and the robots on it.

Files in the state folder (0600): `session.json` (the one session of the account, the region and a
fingerprint of the last pasted login) and `robots.json` (the c103 robots found on the account, with
the ids the app gave them). Local control starts from `robots.json`, so it never waits for Xiaomi.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import spec
from .cloud import XiaomiCloud

REGIONS = ("cn", "de", "us", "ru", "tw", "sg", "in", "i2")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "robot"


def _log(msg: str) -> None:
    print(f"xiaomi: {msg}", flush=True)


def _write(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class Account:
    def __init__(self, state_dir: str | Path, opts: dict):
        self.dir = Path(state_dir)
        self.lock = threading.RLock()                    # one conversation with Xiaomi at a time
        self.health = {"state": "unknown"}               # unknown | ok | failing | paste_rejected (shared, mutated in place)
        self.region_opt = str(opts.get("xiaomi_region") or "auto")
        saved = _read(self.dir / "session.json")
        if saved is None:                                # the session the app used before this file existed
            raw = _read(self.dir / "cloud.json") or {}
            old = next(iter(raw.get("robots", {}).values()), None)
            if old:
                saved = {"session": {k: old.get(k) for k in ("user_id", "ssecurity", "service_token", "pass_token")},
                         "region": old.get("server"), "applied_paste": raw.get("applied_paste")}
        saved = saved or {}
        self.region = saved.get("region")
        self._applied = saved.get("applied_paste")
        s = saved.get("session") or {}
        self.cloud = (XiaomiCloud(s["user_id"], s.get("ssecurity", ""), s.get("service_token", ""), s.get("pass_token"))
                      if s.get("user_id") else None)
        self._apply_paste(opts)

    def set_state(self, state: str) -> None:
        self.health.update(state=state, checked_at=int(time.time()))

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        _write(self.dir / "session.json", {"session": self.cloud.session(), "region": self.region,
                                           "applied_paste": self._applied})

    # --- login ------------------------------------------------------------------
    def _apply_paste(self, opts: dict) -> None:
        """A user id + pass token pasted into the settings replace the saved session once."""
        uid = str(opts.get("xiaomi_user_id") or "").strip()
        tok = str(opts.get("xiaomi_pass_token") or "").strip()
        if not tok:
            if uid:
                _log("a user id is set but the pass token is empty; see the Documentation tab, 'Renewing the Xiaomi login'.")
            return
        uid = uid or (str(self.cloud.user_id) if self.cloud else "")
        if not uid.isdigit():
            _log("the user id must be the number in the browser cookie 'userId' (about 10 digits), not 'cUserId'.")
            self.set_state("paste_rejected")
            return
        digest = hashlib.sha256(f"{uid}:{tok}".encode()).hexdigest()
        if digest == self._applied:
            return
        fresh = XiaomiCloud(uid, "", "", tok)
        if not fresh.refresh():
            _log("Xiaomi did not accept the pasted login; the saved one (if any) is kept. "
                 "Copy the whole passToken value right after logging in at account.xiaomi.com.")
            self.set_state("paste_rejected")
            return
        with self.lock:
            self.cloud, self._applied = fresh, digest
            self._save()
        self.set_state("ok")
        _log("pasted login applied.")

    def renew(self) -> bool:
        """New short-lived keys from the long-lived pass token."""
        with self.lock:
            if self.cloud and self.cloud.refresh():
                self._save()
                return True
        return False

    # --- robots on the account --------------------------------------------------
    def devices(self) -> list[dict]:
        """The c103 robots: [{id, name, host, token, did, mac, region}]. Without a saved list they are
        asked from Xiaomi now; afterwards the saved list is used and refreshed in the background."""
        if self.cloud is None:
            return []
        saved = (_read(self.dir / "robots.json") or {}).get("devices")
        if isinstance(saved, list):
            threading.Thread(target=self._refresh_list, args=(saved,), daemon=True, name="robot-list").start()
            return saved
        found = self._ask()
        if found:
            self._store(found)
        return found or []

    def _refresh_list(self, old: list[dict]) -> None:
        new = self._ask(old)
        if new and new != old:
            self._store(new)
            _log("the robots on the account changed (IP, token or a new robot); restart the app to apply it.")

    def _store(self, devices: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        _write(self.dir / "robots.json", {"devices": devices})

    def _regions(self) -> list[str]:
        if self.region_opt != "auto":
            return [self.region_opt]
        return [self.region] if self.region else list(REGIONS)

    def _ask(self, old: list[dict] | None = None) -> list[dict] | None:
        """Ask Xiaomi for the account's c103 robots; None = could not ask, [] = there are none."""
        with self.lock:
            regions = self._regions()
            answers = self._list_regions(regions)
            if all(a is None for _, a in answers) and self.renew():      # an expired key looks like "no answer"
                answers = self._list_regions(regions)
            for region, devs in answers:
                robots = [d for d in devs or [] if d.get("model") == spec.MODEL]
                if robots:
                    if region != self.region:
                        self.region = region
                        self._save()
                    return self._describe(robots, region, old or [])
            if any(a is not None for _, a in answers):
                _log(f"no {spec.MODEL} robot found on this Xiaomi account (regions checked: {', '.join(regions)}).")
                return []
            _log("could not read the robot list from Xiaomi: the login may have expired "
                 "(see the Documentation tab, 'Renewing the Xiaomi login').")
            return None

    def _list_regions(self, regions: list[str]) -> list[tuple[str, list[dict] | None]]:
        with ThreadPoolExecutor(len(regions)) as pool:
            return list(pool.map(self._list_region, regions))

    def _list_region(self, region: str):
        try:                                             # its own connection: the regions are asked in parallel
            return region, XiaomiCloud(**self.cloud.session()).devices(region)
        except Exception:  # noqa: BLE001  (a region that cannot be read counts as "no answer")
            return region, None

    @staticmethod
    def _describe(robots: list[dict], region: str, old: list[dict]) -> list[dict]:
        ids = {d["did"]: d["id"] for d in old}
        taken = set(ids.values())
        out = []
        for r in robots:
            did = str(r["did"])
            if did not in ids:                           # a new robot: its id never changes afterwards
                base = n = slug(r.get("name", ""))
                i = 1
                while n in taken:
                    i += 1
                    n = f"{base}-{i}"
                ids[did] = n
                taken.add(n)
            out.append({"id": ids[did], "name": r.get("name") or ids[did], "host": r.get("localip"),
                        "token": r.get("token"), "did": did, "mac": str(r.get("mac", "")),
                        "region": region})
        return out
