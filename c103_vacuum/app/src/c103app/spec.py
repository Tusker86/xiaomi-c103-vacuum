"""MIoT map for the Xiaomi Mijia 3C Enhanced robot, xiaomi.vacuum.c103 (and nothing else).

Derived from the c101/c103 profile of xiaomi-vac (MIT, (c) 2026 letitbe-dull); see
third_party/LICENSE-xiaomi-vac.txt. Values marked "verified" were read or sent on the
real robots in this project; the rest come from the published MIoT spec.
"""
from __future__ import annotations

import re

MODEL = "xiaomi.vacuum.c103"

# --- properties: (siid, piid) -------------------------------------------------
FIRMWARE = (1, 4)        # read-only, e.g. "4.3.5_0046"
SERIAL = (1, 5)          # read-only; the same serial the map key is built from
STATUS = (2, 1)         # verified: 5/6/7 = cleaning, 10 = docked and full
FAULT = (2, 2)           # verified: 2105 is the normal "docked, full" code
MODE = (2, 4)            # 0 sweep, 1 sweep+mop, 2 mop, 3 sweep then mop
SWEEP_TYPE = (2, 8)      # READ-ONLY in practice: writes get no reply and do not apply (tested)
BATTERY = (3, 1)         # verified
ALARM = (4, 1)           # verified: find-me beep
VOLUME = (4, 2)          # verified
REPEAT = (7, 1)
FAN = (7, 5)             # 0 silent, 1 standard, 2 medium, 3 turbo
WATER = (7, 6)           # 0 low, 1 mid, 2 high
BOX = (7, 3)             # read-only: 0 none, 1 dust box, 2 water box, 3 combined box
CLOTH = (7, 4)           # read-only: 0 no mop cloth, 1 cloth fitted
MOP_ROUTE = (7, 7)      # 0 S (parallel rows), 1 Y (cross-hatch); read/write per the published spec
CLEANING_TIME = (7, 22)  # minutes in the current/last run
CLEANING_AREA = (7, 23)  # m2 in the current/last run

# Consumables. Every service has a life percentage and hours left, but the piid order
# differs between services (mop is the odd one out).
CONSUMABLES = {
    "filter":     {"life_pct": (15, 1), "hours_left": (15, 2)},
    "main_brush": {"life_pct": (16, 2), "hours_left": (16, 1)},
    "side_brush": {"life_pct": (17, 2), "hours_left": (17, 1)},
    "mop":        {"life_pct": (18, 1), "hours_left": (18, 2)},
}

# reset-consumable actions (no inputs): zero the wear counter after the part was replaced.
RESET_CONSUMABLE = {"filter": (15, 1), "main_brush": (16, 1), "side_brush": (17, 1), "mop": (18, 1)}

DND = {"enable": (12, 1), "start_hour": (12, 2), "start_minute": (12, 3),
       "end_hour": (12, 4), "end_minute": (12, 5)}

# Map service. current_path is verified readable locally: a rolling tail of <=15 points.
MAP_ID = (10, 2)
MAP_LIST_HAS_NEW = (10, 19)
CURRENT_PATH = (10, 5)

# --- actions: (siid, aiid) ----------------------------------------------------
START = (2, 1)
STOP = (2, 2)            # c103 has no separate pause action; the integration uses stop
CHARGE = (3, 1)          # return to dock
SET_ROOM_CLEAN = (7, 3)  # verified; piid 24 room ids (CSV *string*), 25 mode, 26 oper
GET_MAP_LIST = (10, 1)   # verified; out piid 4 = JSON list
UPLOAD_BY_MAPID = (10, 2)
UPLOAD_BY_MAPID_II = (10, 14)
SET_CUR_MAP = (10, 3)    # in piid 6 = map id
DEL_MAP = (10, 4)
RENAME_ROOM = (10, 7)    # in: piid 6 = map id, 9 = room id, 10 = name. Verified: applied without an ack; shows in the cloud map one upload later
RENAME_MAP = (10, 5)     # in piid 6 = map id, piid 8 = name
MERGE_ROOMS = (10, 8)    # arrange-name; CLOUD ONLY (verified 2026-10-06). in: map id, room ids as a CSV string, language
GET_CUR_PATH = (10, 12)  # only returns points after the current block; cannot select a range
# Zone clean (VERIFIED on the 1f robot, Living Room, 2026-10-05): WRITE the property
# ZONE_POINTS = "x1,y1,x2,y2,x3,y3,x4,y4" (four corners, METRES, map frame), then call START_ZONE_CLEAN.
# The set-zone-point action (9/8) with mm values and the "[x0,y0,x1,y1,1]" format did NOT work.
ZONE_POINTS = (9, 2)
START_ZONE_CLEAN = (9, 3)

# --- enums --------------------------------------------------------------------
# status -> activity. 10 observed on both docked robots (the integration leaves it unmapped).
ACTIVITY = {0: "idle", 1: "idle", 2: "paused", 3: "returning", 4: "docked",
            5: "cleaning", 6: "cleaning", 7: "cleaning", 8: "idle", 10: "docked"}
CLEANING_STATES = {5, 6, 7}

FAN_SPEEDS = {"silent": 0, "standard": 1, "medium": 2, "turbo": 3}
WATER_LEVELS = {"low": 0, "mid": 1, "high": 2}
MOP_ROUTES = {"s": 0, "y": 1}
BOXES = {0: "none", 1: "dust box", 2: "water box", 3: "combined box"}
MODES = {"sweep": 0, "sweep_and_mop": 1, "mop": 2, "sweep_then_mop": 3}
SWEEP_TYPES = {"global": 0, "mop": 1, "edge": 2, "area": 3, "point": 4,
               "remote": 5, "explore": 6, "room": 7, "floor": 8}

# The map's name for an unnamed room is "房间3" (Chinese "room 3"); also accept "Room 3".
_PLACEHOLDER = re.compile(r"^(\u623f\u95f4|room ?)\d+$", re.I)


def is_placeholder(name) -> bool:
    return not name or bool(_PLACEHOLDER.match(str(name).strip()))
