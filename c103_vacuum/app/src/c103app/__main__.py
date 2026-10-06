"""Test CLI: python -m c103app <status|path|watch|maps|info> [robot id] [seconds]

Read-only commands only; nothing here moves a robot.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

from . import account, config


def _dump(obj) -> None:
    print(json.dumps(dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj, indent=1))


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"status", "path", "watch", "maps", "info"}:
        print(__doc__)
        return 2
    cmd = argv[0]
    opts = config.load_options()
    data = Path(os.environ.get("C103_DATA", "data"))
    state = os.environ.get("C103_STATE", data.parent)     # the same folders as the server
    robots, _ = config.robots(opts, account.Account(state, opts).devices())
    ids = [argv[1]] if len(argv) > 1 and argv[1] in robots else list(robots)
    for rid in ids:
        r = robots[rid]
        print(f"== {rid} ({r.host})")
        if cmd == "status":
            _dump(r.status())
        elif cmd == "path":
            _dump(r.path_tail())
        elif cmd == "maps":
            _dump(r.map_list())
        elif cmd == "info":
            i = r.info()
            _dump({"model": i.model, "firmware": i.firmware_version, "mac": i.mac_address})
        elif cmd == "watch":
            secs = float(argv[2]) if len(argv) > 2 else 20
            end, last = time.time() + secs, None
            while time.time() < end:
                s, p = r.status(), r.path_tail()
                pos = p.points[-1] if p.points else None
                row = (s.activity, s.battery, pos)
                if row != last:
                    print(f"{time.strftime('%H:%M:%S')} {s.activity:9} bat={s.battery} pos={pos}")
                    last = row
                time.sleep(1.5)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
