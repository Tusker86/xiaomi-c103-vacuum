"""Xiaomi cloud client: reuse a saved session, renew it with the passToken, mint map download URLs.

Adapted from xiaomi-vac's cloud/connector.py (MIT, (c) 2026 letitbe-dull; see
third_party/LICENSE-xiaomi-vac.txt). Only the parts the app needs are kept: no password login, no
captcha/2FA, no device discovery. The session (user id, ssecurity, serviceToken, passToken) is read
from the app's cloud.json on the Pi. Synchronous; call from a worker thread.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import time

import requests

try:
    from Crypto.Cipher import ARC4
except ModuleNotFoundError:  # pragma: no cover
    from Cryptodome.Cipher import ARC4

_LOGGER = logging.getLogger(__name__)


class XiaomiCloud:
    def __init__(self, user_id: str, ssecurity: str, service_token: str, pass_token: str | None = None):
        self._s = requests.session()
        self._agent = _agent()
        self.user_id = user_id
        self.ssecurity = ssecurity
        self.service_token = service_token
        self.pass_token = pass_token

    def session(self) -> dict:
        return {"user_id": self.user_id, "ssecurity": self.ssecurity,
                "service_token": self.service_token, "pass_token": self.pass_token}

    def refresh(self) -> bool:
        """Renew ssecurity + serviceToken with the long-lived passToken (no password needed)."""
        if not self.pass_token or not self.user_id:
            return False
        h = {"User-Agent": self._agent}
        self._s.cookies.set("userId", str(self.user_id), domain="xiaomi.com")
        self._s.cookies.set("passToken", self.pass_token, domain="xiaomi.com")
        try:
            r = self._s.get("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true",
                            headers=h, cookies={"userId": str(self.user_id)}, timeout=10)
            j = _to_json(r.text)
            if "ssecurity" not in j or not j.get("location"):
                return False
            self.ssecurity = j["ssecurity"]
            self.pass_token = j.get("passToken", self.pass_token)
            token = self._s.get(j["location"], headers=h, timeout=10).cookies.get("serviceToken")
        except Exception:  # noqa: BLE001
            return False
        if token:
            self.service_token = token
            return True
        return False

    # --- API ------------------------------------------------------------------
    @staticmethod
    def _api_url(server: str) -> str:
        return "https://" + ("" if server == "cn" else server + ".") + "api.io.mi.com/app"

    def _call(self, url: str, params: dict) -> dict | None:
        h = {"Accept-Encoding": "identity", "User-Agent": self._agent,
             "Content-Type": "application/x-www-form-urlencoded",
             "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2", "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4"}
        ck = {"userId": str(self.user_id), "serviceToken": str(self.service_token),
              "yetAnotherServiceToken": str(self.service_token), "locale": "en_GB", "channel": "MI_APP_STORE"}
        nonce = base64.b64encode(os.urandom(8) + int(time.time() * 1000 / 60000).to_bytes(4, "big")).decode()
        sn = self._signed_nonce(nonce)
        params["rc4_hash__"] = _enc_sig(url, sn, params)
        for k, v in params.items():
            params[k] = _enc_rc4(sn, v)
        params.update({"signature": _enc_sig(url, sn, params), "ssecurity": self.ssecurity, "_nonce": nonce})
        for attempt in (1, 2):                  # the first request after a renewal sometimes fails to connect
            try:
                r = self._s.post(url, headers=h, cookies=ck, params=params, timeout=10)
                break
            except requests.exceptions.RequestException as ex:
                _LOGGER.debug("cloud request to %s failed (attempt %d): %s", url, attempt, ex)
        else:
            return None
        if r.status_code != 200:
            return None
        return json.loads(_dec_rc4(self._signed_nonce(params["_nonce"]), r.text))

    def _signed_nonce(self, nonce: str) -> str:
        h = hashlib.sha256(base64.b64decode(self.ssecurity) + base64.b64decode(nonce))
        return base64.b64encode(h.digest()).decode()

    def devices(self, server: str) -> list[dict] | None:
        """Every device on the account in that region (name, model, localip, did, mac, token ...)."""
        resp = self._call(self._api_url(server) + "/home/device_list",
                          {"data": json.dumps({"getVirtualModel": False, "getHuamiDevices": 0})})
        try:
            return resp["result"]["list"]
        except (TypeError, KeyError):
            return None

    def map_url(self, server: str, did: str, slot: str = "0") -> str | None:
        """Signed download URL for one map object (the c103 is an ijai-engine robot: `_pro` endpoint
        first, the plain one as fallback)."""
        obj = f"{self.user_id}/{did}/{slot}"
        for endpoint in ("get_interim_file_url_pro", "get_interim_file_url"):
            resp = self._call(self._api_url(server) + f"/v2/home/{endpoint}",
                              {"data": f'{{"obj_name":"{obj}"}}'})
            try:
                return resp["result"]["url"]
            except (TypeError, KeyError):
                continue
        return None

    def download(self, url: str) -> bytes | None:
        """The map blob sits on a Beijing storage server that is sometimes slow: allow 30 s, try twice."""
        for attempt in (1, 2):
            try:
                r = self._s.get(url, timeout=30)
                return r.content if r.status_code == 200 else None
            except requests.exceptions.RequestException as ex:
                _LOGGER.debug("map download attempt %d failed: %s", attempt, ex)
        return None


# --- crypto/util helpers --------------------------------------------------------
def _agent() -> str:
    aid = "".join(chr(random.randint(65, 69)) for _ in range(13))
    rt = "".join(chr(random.randint(97, 122)) for _ in range(18))
    return f"{rt}-{aid} APP/com.xiaomi.mihome APPV/10.5.201"


def _to_json(text: str) -> dict:
    return json.loads(text.replace("&&&START&&&", ""))


def _enc_rc4(pw: str, payload: str) -> str:
    r = ARC4.new(base64.b64decode(pw))
    r.encrypt(bytes(1024))
    return base64.b64encode(r.encrypt(payload.encode())).decode()


def _dec_rc4(pw: str, payload: str) -> bytes:
    r = ARC4.new(base64.b64decode(pw))
    r.encrypt(bytes(1024))
    return r.encrypt(base64.b64decode(payload))


def _enc_sig(url: str, signed_nonce: str, params: dict) -> str:
    sp = ["POST", url.split("com")[1].replace("/app/", "/")]
    sp += [f"{k}={v}" for k, v in params.items()]
    sp.append(signed_nonce)
    return base64.b64encode(hashlib.sha1("&".join(sp).encode()).digest()).decode()
