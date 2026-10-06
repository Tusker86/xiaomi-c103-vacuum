"""Fetch, decrypt and parse the c103 map from Xiaomi's cloud storage.

Adapted from xiaomi-vac's MapFetcher (MIT, (c) 2026 letitbe-dull; see
third_party/LICENSE-xiaomi-vac.txt), reduced to the c103 (ijai protobuf, AES-ECB key derived from
wifi_sn + user id + device id + model + MAC). The robot uploads each map to two cloud slots, "0"
and "1"; one of them is sometimes a blob nobody can decrypt, so both are tried.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from vacuum_map_parser_base.config.color import ColorsPalette
from vacuum_map_parser_base.config.drawable import Drawable
from vacuum_map_parser_base.config.image_config import ImageConfig
from vacuum_map_parser_base.config.size import Sizes
from vacuum_map_parser_ijai.map_data_parser import IjaiMapDataParser

from . import map_vector, spec
from .cloud import XiaomiCloud

_LOGGER = logging.getLogger(__name__)
SLOTS = ("0", "1")


def _patch_parse_rooms() -> None:
    """Work around an upstream crash on non-active maps: `_parse_rooms` looks up the active map's
    entry only to log its name and raises UnboundLocalError when it is missing."""
    parser_cls = IjaiMapDataParser

    @staticmethod
    def _parse_rooms(map_data_rooms: dict) -> None:
        for r in parser_cls.robot_map.roomDataInfo:
            if map_data_rooms is not None and r.roomId in map_data_rooms:
                map_data_rooms[r.roomId].name = r.roomName
                map_data_rooms[r.roomId].pos_x = r.roomNamePost.x
                map_data_rooms[r.roomId].pos_y = r.roomNamePost.y

    parser_cls._parse_rooms = _parse_rooms


_patch_parse_rooms()


@dataclass
class FetchResult:
    vector: dict | None      # grid + overlays in the map_vector format; None if nothing was readable
    content_hash: str | None  # sha256 of the decrypted blob: unchanged hash = unchanged map
    outcomes: dict           # slot -> "rendered" | "no_url" | "empty_download" | "undecryptable" | ...


class MapFetcher:
    def __init__(self, cloud: XiaomiCloud, *, server: str, device_id: str, mac: str, wifi_sn: str):
        self._cloud, self._server, self._did = cloud, server, str(device_id)
        self._mac, self._wifi_sn = mac, wifi_sn
        self._parser = IjaiMapDataParser(ColorsPalette(), Sizes(), [Drawable.CHARGER], ImageConfig(), [])

    def fetch(self) -> FetchResult:
        outcomes: dict[str, str] = {}
        for slot in SLOTS:
            res = self._fetch_slot(slot, outcomes)
            if res is not None:
                return FetchResult(res[0], res[1], outcomes)
        return FetchResult(None, None, outcomes)

    def _fetch_slot(self, slot: str, outcomes: dict) -> tuple[dict, str] | None:
        url = self._cloud.map_url(self._server, self._did, slot)
        if not url:
            outcomes[slot] = "no_url"      # usually: the cloud session has expired
            return None
        raw = self._cloud.download(url)
        if not raw:
            outcomes[slot] = "empty_download"
            return None
        try:
            unpacked = self._parser.unpack_map(
                raw, wifi_sn=self._wifi_sn, owner_id=str(self._cloud.user_id),
                device_id=self._did, model=spec.MODEL, device_mac=self._mac)
        except Exception as ex:  # noqa: BLE001  (wrong-key blobs fail here)
            outcomes[slot] = f"undecryptable ({type(ex).__name__})"
            return None
        try:
            md = self._parser.parse(unpacked)
            vector = map_vector.vector_map(md, unpacked)
        except Exception as ex:  # noqa: BLE001
            outcomes[slot] = f"parse_rejected ({type(ex).__name__})"
            return None
        if md.image is None or md.image.is_empty or not vector.get("grid_rle"):
            outcomes[slot] = "empty_map"
            return None
        outcomes[slot] = "rendered"
        return vector, hashlib.sha256(unpacked).hexdigest()
