"""Juwel HeliaLux SmartControl over the home network (HTTP).

The SmartControl has no official API. The endpoints below were documented by
the community (ioBroker forum 2021/22, pyHelialux). Firmware updates may change
them, so every write to a day program is read back and verified.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import curves
from .base import Light, now_minute

log = logging.getLogger("light_bridge.helialux")

# SmartControl channel order: CH1 white, CH2 blue, CH3 green, CH4 red (Spectrum).
# Two-colour HeliaLux LED lights only use CH1 (white) and CH2 (blue).
CH_ORDER = ["w", "b", "g", "r"]
CHANNELS_4 = ["w", "r", "g", "b"]
CHANNELS_2 = ["w", "b"]

_VAR_RE = re.compile(r"(?:var\s+)?([A-Za-z_]\w*)\s*=\s*(.*?);", re.S)


def parse_js_vars(text: str) -> dict:
    """Parse `name=value;` assignments from the SmartControl's *.js status files."""
    out = {}
    for name, raw in _VAR_RE.findall(text):
        raw = raw.strip()
        val = raw
        try:
            if raw.startswith("["):
                val = json.loads(raw.replace("'", '"'))
            elif raw[:1] in "'\"" and raw[-1:] == raw[:1]:
                val = raw[1:-1]
            elif re.fullmatch(r"-?\d+", raw):
                val = int(raw)
            elif re.fullmatch(r"-?\d+\.\d+", raw):
                val = float(raw)
        except ValueError:
            val = raw
        out[name] = val
    return out


def channels_for_lamp(lamp_value) -> list:
    s = str(lamp_value or "").lower()
    return CHANNELS_2 if s.startswith("2") or "2ch" in s else CHANNELS_4


class HeliaLuxHttp:
    """Blocking HTTP client for one SmartControl (run it in a worker thread)."""

    def __init__(self, ip: str, timeout: float = 4.0):
        self.ip = ip
        self.timeout = timeout

    @property
    def base(self):
        return f"http://{self.ip}"

    def _get(self, path: str) -> str:
        with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "replace")

    def _post(self, path: str, data: dict) -> str:
        body = urllib.parse.urlencode({k: str(v) for k, v in data.items()}).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "replace")

    # status and manual control
    def status(self) -> dict:
        return parse_js_vars(self._get("/statusvars.js"))

    def start_manual(self, minutes: int):
        minutes = max(1, min(1439, int(minutes)))
        self._post("/stat", {"action": 14, "cswi": "true", "ctime": f"{minutes // 60:02d}:{minutes % 60:02d}"})

    def set_levels(self, levels: dict, channels: list):
        data = {"action": 10}
        for i, key in enumerate(CH_ORDER):
            if key in channels:
                data[f"ch{i + 1}"] = max(0, min(100, int(levels.get(key, 0))))
        self._post("/stat", data)

    def stop_manual(self):
        self._post("/stat", {"action": 14, "cswi": "false"})
        self._post("/stat", {"action": 10})

    # day programs
    def read_overview(self) -> dict:
        return parse_js_vars(self._get("/wpvars.js"))

    def read_program(self, slot: int) -> dict:
        self._get(f"/profedit.html?profNum={slot}")
        return parse_js_vars(self._get("/profeditvars.js"))

    def write_program(self, slot: int, fields: dict):
        self._get(f"/profedit.html?profNum={slot}")
        data = {"action": 30, **fields}
        try:
            self._post("/pedit", data)
        except Exception:  # noqa: BLE001 - older firmware: same parameters as query string
            self._get("/pedit?" + urllib.parse.urlencode({k: str(v) for k, v in data.items()}))


def program_from_vars(v: dict, channels: list) -> dict:
    times = v.get("times") or [0]
    points = []
    for i, t in enumerate(times):
        lv = {}
        for idx, key in enumerate(CH_ORDER):
            arr = v.get(f"CH{idx + 1}")
            if key in channels and isinstance(arr, list) and i < len(arr):
                lv[key] = arr[i]
        ci = v.get("cI") or v.get("CINT") or []
        cm = v.get("cM") or v.get("CMOT") or []
        points.append({"t": int(t), "v": lv, "ci": ci[i] if i < len(ci) else 0, "cm": cm[i] if i < len(cm) else 0})
    return {"name": str(v.get("profile") or v.get("PNAME") or ""), "points": curves.normalize_points(points, channels, 16)}


def program_to_fields(profile: dict, channels: list) -> dict:
    pts = curves.normalize_points(profile["points"], channels, 16)
    fields = {"PNAME": profile["name"][:20], "TIMES": json.dumps([p["t"] for p in pts], separators=(",", ":"))}
    for idx, key in enumerate(CH_ORDER):
        if key in channels:
            fields[f"CH{idx + 1}"] = json.dumps([p["v"].get(key, 0) for p in pts], separators=(",", ":"))
    fields["CINT"] = json.dumps([p.get("ci", 0) for p in pts], separators=(",", ":"))
    fields["CMOT"] = json.dumps([p.get("cm", 0) for p in pts], separators=(",", ":"))
    return fields


# ------------------------------------------------------------------ light
class HeliaLuxLight(Light):
    kind = "helialux"
    max_points = 16
    POLL_SECONDS = 15

    def __init__(self, lid, name, room, ip, channels=None, client=None):
        super().__init__(lid, name, room, channels or CHANNELS_4)
        self.ip = ip
        self.client = client or HeliaLuxHttp(ip)
        self._next_poll = 0.0
        self._fails = 0
        self.device_minute = None
        self.device_profile = None
        self.programs_loaded = False
        self.weekdays_known = False
        self.channels_locked = False   # user picked 2 or 4 colours manually
        self.status_text = "Connecting over Wi-Fi…"
        self._previewing = False
        self._preview_manual_since = 0.0

    @property
    def kind_label(self):
        return "Juwel HeliaLux " + ("Spectrum (4 colours)" if len(self.channels) == 4 else "LED (2 colours)")

    def caps(self):
        c = super().caps()
        c.update({"clouds": True, "weekdays_editable": False, "profiles_on_device": True})
        return c

    def info(self):
        offset = None
        if self.device_minute is not None:
            diff = (self.device_minute - int(now_minute())) % 1440
            offset = diff - 1440 if diff > 720 else diff
        return {"ip": self.ip, "device_time_offset": offset, "programs_loaded": self.programs_loaded,
                "colours": len(self.channels), "weekdays_known": self.weekdays_known}

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)

    def set_ip(self, ip):
        self.ip = ip
        self.client.ip = ip
        self._next_poll = 0

    # ---------------------------------------------------------- polling
    async def tick(self, ts):
        self.expire_modes(ts)
        if self._previewing and not self.preview_active(ts):
            self._previewing = False
            await self._restore_after_preview()
        if ts < self._next_poll:
            return
        self._next_poll = ts + self.POLL_SECONDS
        await self.refresh()

    async def refresh(self):
        try:
            st = await self._run(self.client.status)
            self._fails = 0
            self.online, self.connection = True, "connected"
            self.last_error = None
            if not self.channels_locked and "lamp" in st:
                detected = channels_for_lamp(st["lamp"])
                if detected != self.channels:
                    self.set_channels(detected)
                    self.dirty = True
            br = st.get("brightness")
            if isinstance(br, list):
                self.levels = {k: int(br[i]) for i, k in enumerate(CH_ORDER) if k in self.channels and i < len(br)}
            self.device_minute = st.get("tsimtime") if isinstance(st.get("tsimtime"), int) else None
            self.device_profile = st.get("profile")
            device_manual = st.get("csimact") == 1
            if not device_manual and self.mode in ("manual", "maintenance") and not self.preview_active():
                self.mode, self.manual_until, self.manual_levels = "program", None, None
            self.status_text = f"Online at {self.ip}"
            if not self.programs_loaded:
                await self.reload()
        except Exception as e:  # noqa: BLE001
            self._fails += 1
            self.last_error = str(e) or e.__class__.__name__
            if self._fails >= 2:
                self.online, self.connection = False, "offline"
                self.status_text = "Not reachable on the home network"

    async def reload(self):
        """Read all eight day programs and the weekday plan from the controller."""
        try:
            ov = await self._run(self.client.read_overview)
            names = ov.get("profiles") if isinstance(ov.get("profiles"), list) else []
            sel = ov.get("profsel")
            if isinstance(sel, list) and len(sel) == 7:
                self.weekdays = [int(x) for x in sel]
                self.weekdays_known = True
            profiles = []
            for slot in range(curves.MAX_SLOTS):
                v = await self._run(self.client.read_program, slot)
                prog = program_from_vars(v, self.channels)
                if slot < len(names) and names[slot]:
                    prog["name"] = names[slot]
                prog["name"] = prog["name"] or f"Program {slot + 1}"
                profiles.append(prog)
            self.profiles = profiles
            self.programs_loaded = True
            self.dirty = True
        except Exception as e:  # noqa: BLE001
            self.last_error = f"Could not read programs: {e}"
            log.info("%s: %s", self.name, self.last_error)

    def active_slot(self, ts=None):
        if self.device_profile and not self.weekdays_known:
            for i, p in enumerate(self.profiles):
                if p["name"] == self.device_profile:
                    return i
        return super().active_slot(ts)

    # ---------------------------------------------------------- actions
    async def start_manual(self, levels, minutes, mode="manual"):
        lv = curves.convert_levels(levels, self.channels)
        await self._run(self.client.start_manual, minutes)
        await self._run(self.client.set_levels, lv, self.channels)
        self.mode, self.manual_levels = mode, lv
        self.manual_until = time.time() + max(1, min(1439, int(minutes))) * 60
        self.preview_levels = self.preview_until = None
        self._previewing = False
        self.levels = lv

    async def back_to_program(self):
        await self._run(self.client.stop_manual)
        self.mode, self.manual_levels, self.manual_until = "program", None, None
        self.preview_levels = self.preview_until = None
        self._previewing = False
        self._next_poll = 0

    async def preview(self, levels):
        lv = curves.convert_levels(levels, self.channels)
        now = time.time()
        if not self.manual_active() and (not self._previewing or now - self._preview_manual_since > 240):
            await self._run(self.client.start_manual, 5)   # device falls back by itself after 5 min
            self._preview_manual_since = now
        self._previewing = True
        await self._run(self.client.set_levels, lv, self.channels)
        self.preview_levels, self.preview_until = lv, time.time() + self.PREVIEW_SECONDS
        self.levels = lv

    async def end_preview(self):
        self.preview_levels = self.preview_until = None
        if self._previewing:
            self._previewing = False
            await self._restore_after_preview()

    async def _restore_after_preview(self):
        try:
            if self.manual_active():
                await self._run(self.client.set_levels, self.manual_levels, self.channels)
            else:
                await self._run(self.client.stop_manual)
            self._next_poll = 0
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)

    async def save_profile(self, slot, profile):
        prog = {"name": (profile.get("name") or f"Program {slot + 1}")[:20],
                "points": curves.normalize_points(profile["points"], self.channels, self.max_points)}
        fields = program_to_fields(prog, self.channels)
        await self._run(self.client.write_program, slot, fields)
        verified = False
        try:
            back = program_from_vars(await self._run(self.client.read_program, slot), self.channels)
            verified = [(p["t"], p["v"]) for p in back["points"]] == [(p["t"], p["v"]) for p in prog["points"]]
        except Exception:  # noqa: BLE001
            pass
        self.profiles[slot] = prog
        self.dirty = True
        return {"ok": True, "verified": verified,
                "message": "Saved on the HeliaLux and checked." if verified else
                "Sent to the HeliaLux, but the check did not match. Open the program on the light to confirm."}

    async def set_weekdays(self, days):
        raise ValueError("Weekday plans are set on the HeliaLux itself (Weekdays menu).")

    def to_store(self):
        d = super().to_store()
        d.update({"ip": self.ip, "channels_locked": self.channels_locked})
        return d

    def load_store(self, d):
        super().load_store(d)
        self.channels_locked = bool(d.get("channels_locked"))


# ------------------------------------------------------------------ discovery
def local_ipv4_addresses() -> list:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 80))  # no packet is sent; picks the outgoing interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def probe(ip: str, timeout: float = 0.8):
    """Return the parsed status if a SmartControl answers at this address."""
    try:
        st = HeliaLuxHttp(ip, timeout=timeout).status()
        if "tsimtime" in st or ("brightness" in st and "profile" in st):
            return st
    except Exception:  # noqa: BLE001
        return None
    return None


def scan_network(exclude=()) -> list:
    """Probe every address of the local /24 networks. Returns [(ip, status)]."""
    targets = []
    for own in local_ipv4_addresses():
        prefix = own.rsplit(".", 1)[0]
        targets += [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" not in (own, *exclude)]
    found = []
    with ThreadPoolExecutor(max_workers=64) as pool:
        for ip, st in zip(targets, pool.map(probe, targets)):
            if st:
                found.append((ip, st))
    return found
