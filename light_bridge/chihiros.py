"""Chihiros Universal WRGB over Bluetooth Low Energy.

Protocol (Nordic UART service) as documented by TheMicDiet/chihiros-led-control
and caleb-venner/AquaBle. Lamp-first: the day program is stored IN the light, which
runs it on its own; the PC may leave at any time (see ChihirosLight).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime

from . import curves
from .base import Light

log = logging.getLogger("light_bridge.chihiros")

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # write (app -> light)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # notify (light -> app)
NAME_PREFIXES = ("DYU",)                           # Universal WRGB 550 … 1500
CHANNEL_ID = {"r": 0, "g": 1, "b": 2, "w": 3}
CHANNELS = ["r", "g", "b", "w"]
WEEKDAY_BITS = [0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01]  # Mon..Sun
AUTO_ON, RESET_AUTO, CUSTOM_INIT = 18, 5, 3


class Encoder:
    """Frame builder. Verified against the documented example frames."""

    def __init__(self):
        self.hi, self.lo = 0, 0

    def _next_id(self):
        self.lo += 1
        if self.lo == 0x5A:
            self.lo += 1
        if self.lo > 0xFF:
            self.lo = 0
            self.hi += 1
            if self.hi == 0x5A:
                self.hi += 1
            if self.hi > 0xFF:
                self.hi, self.lo = 0, 1
        return self.hi, self.lo

    def frame(self, family: int, mode: int, params) -> bytes:
        p = [max(0, min(255, int(round(v)))) for v in params]
        p = [0x59 if v == 0x5A else v for v in p]
        for _ in range(8):
            hi, lo = self._next_id()
            body = [family, 0x01, len(p) + 5, hi, lo, mode, *p]
            chk = 0
            for b in body[1:]:
                chk ^= b
            if chk == 0x5A:
                continue
            return bytes(body + [chk])
        raise RuntimeError("could not encode frame")

    def set_time(self, d: datetime) -> bytes:
        return self.frame(0x5A, 0x09, [d.year - 2000, d.month, d.isoweekday(), d.hour, d.minute, d.second])

    def status(self) -> bytes:
        return self.frame(0x5A, 0x04, [0x01])

    def channel(self, ch: int, level: int) -> bytes:
        return self.frame(0x5A, 0x07, [ch, max(0, min(100, int(level)))])

    def mode(self, action: int) -> bytes:
        return self.frame(0x5A, 0x05, [action, 0xFF, 0xFF])

    def curve_point(self, ch: int, hour: int, minute: int, level: int) -> bytes:
        """Step-mode point (format captured from the official app on a Chihiros A2)."""
        return self.frame(0x5A, 0x06, [ch, hour, minute, max(0, min(100, int(level)))])

    def basic_schedule(self, on: int, off: int, ramp: int, mask: int, lv: dict) -> bytes:
        return self.frame(0xA5, 0x19, [on // 60, on % 60, off // 60, off % 60, max(0, min(150, ramp)), mask & 0x7F,
                                       lv.get("r", 0), lv.get("g", 0), lv.get("b", 0), lv.get("w", 0),
                                       0xFF, 0xFF, 0xFF, 0xFF])


# ------------------------------------------------------------------ transports
class BleTransport:
    """Bluetooth link via bleak (Windows, macOS, Linux)."""

    radio_lock = None  # shared with scanning: one radio operation at a time (set by the engine)

    FRESH_SECONDS = 90  # a device object from a recent scan can be used directly

    def __init__(self, address: str):
        self.address = address
        self.client = None
        self._with_response = True
        self.cached_device = None   # BLEDevice from the engine's last scan
        self.cached_at = 0.0

    @property
    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def connect(self, on_disconnect, allow_search=True) -> None:
        from bleak import BleakClient, BleakScanner  # imported lazily: demo mode needs no Bluetooth
        fresh = self.cached_device is not None and time.time() - self.cached_at < self.FRESH_SECONDS
        if not fresh and not allow_search:
            # Not seen in the latest search. Don't block the radio looking for it;
            # the engine's regular search will report it as soon as it is back.
            raise ConnectionError("not found in the last Bluetooth search (switched off or out of range?)")
        lock = BleTransport.radio_lock
        if lock:
            await lock.acquire()
        try:
            if fresh:
                device = self.cached_device          # seen in a scan moments ago: no extra search
            else:
                device = await BleakScanner.find_device_by_address(self.address, timeout=6.0)
            if device is None:
                raise ConnectionError("light not found nearby (switched off or out of range?)")
            client = BleakClient(device, disconnected_callback=lambda _c: on_disconnect())
            await asyncio.wait_for(client.connect(), timeout=35)
        finally:
            if lock:
                lock.release()
        self.client = client
        try:
            char = client.services.get_characteristic(NUS_RX)
            props = getattr(char, "properties", []) or []
            self._with_response = "write" in props
        except Exception:  # noqa: BLE001 - property lookup is best effort
            self._with_response = True
        try:
            await client.start_notify(NUS_TX, lambda _s, data: log.debug("rx %s", bytes(data).hex(" ")))
        except Exception as e:  # noqa: BLE001
            log.debug("notifications unavailable: %s", e)

    async def write(self, data: bytes) -> None:
        if not self.is_connected:
            raise ConnectionError("not connected")
        await asyncio.wait_for(self.client.write_gatt_char(NUS_RX, data, response=self._with_response), timeout=6)

    async def disconnect(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass


class SimTransport:
    """In-memory light for demo mode and tests."""

    def __init__(self, address: str = "SIM-CHIHIROS"):
        self.address = address
        self.connected = False
        self.frames: list[bytes] = []
        self.output = {c: 0 for c in CHANNELS}

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self, on_disconnect, allow_search=True) -> None:
        await asyncio.sleep(0.2)
        self.connected = True

    async def write(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("not connected")
        self.frames.append(bytes(data))
        if data[0] == 0x5A and data[5] == 0x07:
            ch = {v: k for k, v in CHANNEL_ID.items()}[data[6]]
            self.output[ch] = data[7]

    async def disconnect(self) -> None:
        self.connected = False


# ------------------------------------------------------------------ light
class ChihirosLight(Light):
    """Lamp-first design: the day program is STORED IN THE LIGHT, which runs it on its own.

    The bridge only (1) writes the program and the clock, (2) handles short, temporary
    manual light and previews, and (3) puts the light back into its own program afterwards.
    If the PC leaves (laptop taken to work, lid closed, out of range), the light keeps
    running its stored program. Only a manual light that was active at that moment stays
    until the bridge is back; the bridge then switches the light back to its program.

    Two ways to store a program in the light:
    * "simple" (default, documented): one bright block per weekday group, with a fade.
    * "curve" (only after the on-light test confirmed it): the full curve with up to
      20 phases, same every day. The format was captured on another Chihiros model.
    """
    kind = "chihiros"
    kind_label = "Chihiros Universal WRGB"
    max_points = 20                 # the light's own limit for stored curves
    FRAME_GAP = 0.06
    CLOCK_SYNC_SECONDS = 6 * 3600
    STABLE_SECONDS = 8              # a program is only rewritten after the link has been up this long
    AWAY_TEXT = {None: "Out of range. The light runs its own program.",
                 "manual": "Out of range. The light still shows the manual light until the PC is near again.",
                 "test": "Out of range during the curve test. Your program is stored again when the PC is near.",
                 "erased": "Out of range while storing the program. It is stored again when the PC is near."}
    TEST_W, TEST_B = 30, 60         # on-light curve test: soft white, then blue

    def __init__(self, lid, name, room, address, transport=None):
        super().__init__(lid, name, room, CHANNELS)
        self.address = address
        self.transport = transport or BleTransport(address)
        self.enc = Encoder()
        self._lock = asyncio.Lock()
        self._sent: dict = {}
        self._last_clock = 0.0
        self._next_try = 0.0
        self._backoff = 2.0
        self._connecting = False
        self._attempts = 0
        self.scan_assisted = False   # set by the engine: connect mainly to lights its search has seen
        self.rssi = None             # signal strength from the last search (dBm)
        self.seen_at = None
        # what is stored in / running on the light
        self.lamp_mode = "simple"    # simple | curve
        self.curve_variant = None    # 1 | 2 once the curve test succeeded
        self.curve_tested = None     # {"variant", "ok", "at"} of the last test
        self.program_on_light = None # hash of the program last written successfully
        self.lamp_state = "unknown"  # what we last put the light into: program | manual | test | unknown
        self.test = None             # running curve test
        self.adopted = True          # False for lights found nearby but not yet confirmed by the user
        self.lamp_intact = True      # False from the reset of a program write until it is complete
        self.left_in = None          # what the light was left in when the link dropped: manual | test | erased
        self._connected_at = 0.0
        self._utc_offset = None
        self.persist = None          # set by the engine: save the settings file at once
        self.status_text = "Looking for the light…"

    # ---------------------------------------------------------- what the light stores
    def caps(self):
        c = super().caps()
        c.update({"clock_sync": True, "lamp_mode": self.lamp_mode, "curve_variant": self.curve_variant,
                  "curve_tested": self.curve_tested, "weekdays_editable": self.lamp_mode == "simple",
                  "program_on_device": True})
        return c

    def simple_blocks(self):
        """One bright block per group of weekdays that share a program (documented auto mode)."""
        groups = {}
        for day, slot in enumerate(self.weekdays):
            groups[slot] = groups.get(slot, 0) | WEEKDAY_BITS[day]
        out = []
        for slot, mask in sorted(groups.items()):
            basic = curves.derive_basic_schedule(self.profiles[slot]["points"], self.channels)
            if basic:
                out.append({**basic, "mask": mask, "slot": slot})
        return out

    def curve_points(self):
        """Curve mode stores one curve for every day: the program used on Mondays."""
        return curves.normalize_points(self.profiles[self.weekdays[0]]["points"], self.channels, self.max_points)

    def lamp_program(self):
        if self.lamp_mode == "curve":
            return {"mode": "curve", "variant": self.curve_variant, "points": self.curve_points()}
        return {"mode": "simple", "blocks": self.simple_blocks()}

    def program_hash(self):
        return hashlib.sha1(json.dumps(self.lamp_program(), sort_keys=True).encode()).hexdigest()

    def lamp_points(self, ts=None):
        """What the light itself runs today (for display)."""
        if self.lamp_mode == "curve":
            return self.curve_points()
        day = datetime.fromtimestamp(ts if ts is not None else time.time()).weekday()
        for b in self.simple_blocks():
            if b["mask"] & WEEKDAY_BITS[day]:
                return curves.simple_points(b, self.channels)
        return curves.normalize_points([], self.channels)

    def expected_levels(self, ts):
        if self.test:
            return curves.sample(self.test["points"], _minute(ts), self.channels)
        if self.preview_active(ts):
            return dict(self.preview_levels)
        if self.manual_active(ts):
            return dict(self.manual_levels)
        return curves.sample(self.lamp_points(ts), _minute(ts), self.channels)

    def info(self):
        blocks = self.simple_blocks()
        today = datetime.now().weekday()
        block = next((b for b in blocks if b["mask"] & WEEKDAY_BITS[today]), None)
        return {"address": self.address, "rssi": self.rssi, "seen_at": self.seen_at,
                "program_pending": self.program_on_light != self.program_hash(),
                "simple_today": ({k: block[k] for k in ("on", "off", "ramp", "cut_at_midnight")} if block else None),
                "left_in": self.left_in,
                "test": ({k: self.test[k] for k in ("variant", "blue_at", "back_at", "until")} if self.test else None)}

    def active_slot(self, ts=None):
        if self.lamp_mode == "curve":
            return self.weekdays[0] if 0 <= self.weekdays[0] < len(self.profiles) else 0
        return super().active_slot(ts)

    def state(self, ts=None):
        d = super().state(ts)
        pts = self.lamp_points(ts)
        d["lamp_points"] = pts
        d["next_change"] = curves.next_phase(pts, int(_minute(ts if ts is not None else time.time())))
        d["adopted"] = self.adopted
        return d

    def seen(self, rssi=None, device=None):
        """Called by the engine when a search sees this light."""
        self.seen_at = time.time()
        if rssi is not None:
            self.rssi = int(rssi)
        if device is not None and hasattr(self.transport, "cached_device"):
            self.transport.cached_device, self.transport.cached_at = device, self.seen_at
        if not self.transport.is_connected:
            self._next_try = time.time()     # it is (back) in range: try right away
            self.poke()

    # ---------------------------------------------------------- link handling
    def _on_disconnect(self):
        if not self.lamp_intact:
            self.left_in = "erased"
        elif self.lamp_state in ("manual", "test"):
            self.left_in = self.lamp_state
        elif self.lamp_state == "program":
            self.left_in = None
        self.online = False
        self.connection = "offline"
        self.status_text = self.AWAY_TEXT.get(self.left_in, self.AWAY_TEXT[None])
        self._sent = {}
        self.lamp_state = "unknown"
        self.dirty = True
        self._next_try = time.time() + 1

    def _persist(self):
        self.dirty = True
        if self.persist:
            try:
                self.persist()
            except Exception:  # noqa: BLE001 - saving must never break the light
                pass

    async def _ensure_connected(self, ts) -> bool:
        if self.transport.is_connected:
            return True
        if self._connecting or ts < self._next_try:
            return False
        self._connecting = True
        self.connection = "connecting"
        allow_search = not self.scan_assisted or self._attempts % 4 == 3
        self._attempts += 1
        try:
            await self.transport.connect(self._on_disconnect, allow_search=allow_search)
            self.online, self.connection = True, "connected"
            self.status_text = "Connected via Bluetooth"
            self.last_error = None
            self._backoff = 2.0
            self._attempts = 0
            self._sent = {}
            self._last_clock = 0.0
            self._connected_at = time.time()
            self.lamp_state = "unknown"
            log.info("%s connected", self.name)
            return True
        except Exception as e:  # noqa: BLE001
            self.online, self.connection = False, "offline"
            self.last_error = str(e) or e.__class__.__name__
            self.status_text = self.AWAY_TEXT.get(self.left_in, self.AWAY_TEXT[None])
            self._next_try = ts + self._backoff
            self._backoff = min(60.0, self._backoff * 2)
            log.debug("%s connect failed: %s", self.name, self.last_error)
            return False
        finally:
            self._connecting = False

    async def _send(self, frame: bytes):
        await self.transport.write(frame)
        await asyncio.sleep(self.FRAME_GAP)

    # ---------------------------------------------------------- writing to the light
    async def _write_program(self):
        """Store the program in the light and let the light run it."""
        prog = self.lamp_program()
        digest = self.program_hash()
        self.program_on_light = None          # if we are interrupted, write again next time
        self.lamp_intact = False              # from the reset on, the light's old program is gone
        self._persist()
        await self._send(self.enc.mode(RESET_AUTO))
        if prog["mode"] == "curve":
            await self._send(self.enc.mode(CUSTOM_INIT))
            for ch in CHANNELS:
                for p in prog["points"]:
                    await self._send(self.enc.curve_point(CHANNEL_ID[ch], p["t"] // 60, p["t"] % 60, p["v"].get(ch, 0)))
            if prog["variant"] == 2:
                await self._send(self.enc.mode(AUTO_ON))
        else:
            for b in prog["blocks"][:7]:
                await self._send(self.enc.basic_schedule(b["on"], b["off"], b["ramp"], b["mask"], b["levels"]))
            await self._send(self.enc.mode(AUTO_ON))
        self.program_on_light = digest
        self.lamp_intact = True
        self.lamp_state = "program"
        self.left_in = None
        self._sent = {}
        self._persist()
        log.info("%s: program stored in the light (%s)", self.name, prog["mode"])

    async def _return_to_program(self):
        if self.lamp_mode == "curve" and self.curve_variant == 1:
            await self._write_program()       # no lighter command known for this variant
        else:
            await self._send(self.enc.mode(AUTO_ON))
            self.lamp_state = "program"
            self.left_in = None
            self._sent = {}

    def _stable(self, ts):
        """Only start a program write (which begins by erasing the light's program) on a link
        that has been up for a while - not while the laptop is just passing by at the edge of range.
        A light whose program is already erased is repaired at once."""
        return not self.lamp_intact or time.time() - self._connected_at >= self.STABLE_SECONDS

    async def _sync_light(self, ts):
        """Bring the light into the state it should be in right now (lock must be held)."""
        offset = datetime.fromtimestamp(ts).astimezone().utcoffset()
        if ts - self._last_clock > self.CLOCK_SYNC_SECONDS or offset != self._utc_offset:
            await self._send(self.enc.set_time(datetime.fromtimestamp(ts)))   # also after summer/winter time
            self._last_clock, self._utc_offset = ts, offset
        if self.test:
            return                            # the test curve is already in the light
        if self.preview_active(ts) or self.manual_active(ts):
            target = self.preview_levels if self.preview_active(ts) else self.manual_levels
            for ch in CHANNELS:
                v = int(target.get(ch, 0))
                if self._sent.get(ch) != v:
                    await self._send(self.enc.channel(CHANNEL_ID[ch], v))
                    self._sent[ch] = v
            self.lamp_state = "manual"
            return
        needs_rewrite = self.program_on_light != self.program_hash() or (
            self.lamp_state != "program" and self.lamp_mode == "curve" and self.curve_variant == 1)
        if needs_rewrite and self._stable(ts):
            await self._write_program()
        elif self.lamp_state != "program" and not (self.lamp_mode == "curve" and self.curve_variant == 1):
            await self._return_to_program()   # meanwhile: back to the (old, complete) program in the light

    async def tick(self, ts):
        if not self.adopted:              # never write to a light the user has not confirmed
            self.status_text = "New light nearby. Confirm that it is yours."
            return
        self.expire_modes(ts)
        if self.test and ts > self.test["until"]:
            log.info("%s: curve test timed out, keeping the simple program", self.name)
            self.test = None
            self.program_on_light = None      # restore the real program
            self._persist()
        self.levels = {c: int(v) for c, v in self.expected_levels(ts).items()}
        if not await self._ensure_connected(ts):
            return
        try:
            await self._locked(self._sync_light, ts)
        except ConnectionError:
            pass                              # already marked offline; retried on the next search

    async def _locked(self, fn, *args):
        async with self._lock:
            try:
                return await fn(*args)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e) or e.__class__.__name__
                log.info("%s write failed: %s", self.name, self.last_error)
                await self.transport.disconnect()
                self._on_disconnect()
                raise ConnectionError("The light did not answer. Move the PC closer and try again.") from e

    async def _apply_now(self):
        ts = time.time()
        self.levels = {c: int(v) for c, v in self.expected_levels(ts).items()}
        if self.transport.is_connected:
            try:
                await self._locked(self._sync_light, ts)
            except ConnectionError:
                pass

    # ---------------------------------------------------------- user actions
    async def start_manual(self, levels, minutes, mode="manual"):
        self.mode = mode
        self.manual_levels = curves.convert_levels(levels, self.channels)
        self.manual_until = time.time() + max(1, min(1440, int(minutes))) * 60
        self.preview_levels = self.preview_until = None
        await self._apply_now()

    async def back_to_program(self):
        self.mode, self.manual_levels, self.manual_until = "program", None, None
        self.preview_levels = self.preview_until = None
        await self._apply_now()

    async def preview(self, levels):
        self.preview_levels = curves.convert_levels(levels, self.channels)
        self.preview_until = time.time() + self.PREVIEW_SECONDS
        await self._apply_now()

    async def end_preview(self):
        self.preview_levels = self.preview_until = None
        await self._apply_now()

    async def sync_clock(self):
        if not self.transport.is_connected:
            raise ConnectionError("The light is out of range right now.")
        async with self._lock:
            await self._send(self.enc.set_time(datetime.now()))
            self._last_clock = time.time()

    async def save_profile(self, slot, profile):
        await super().save_profile(slot, profile)
        await self._apply_now()
        return self._send_result("Saved")

    async def set_weekdays(self, days):
        if self.lamp_mode == "curve" and len(set(int(d) for d in days)) > 1:
            raise ValueError("With full curves, the light runs the same program every day.")
        await super().set_weekdays(days)
        await self._apply_now()

    def _send_result(self, what):
        if self.program_on_light == self.program_hash():
            return {"ok": True, "verified": True, "message": f"{what} and stored in the light."}
        return {"ok": True, "verified": True, "pending": True,
                "message": f"{what}. It is sent to the light as soon as the light is in range."}

    async def set_lamp_mode(self, mode):
        if mode == "curve" and not self.curve_variant:
            raise ValueError("Run the curve test first.")
        self.lamp_mode = mode if mode in ("simple", "curve") else "simple"
        self.test = None                      # also used to cancel a running test
        self.program_on_light = None          # store the program again
        self.dirty = True
        await self._apply_now()
        return self._send_result("Switched")

    async def curve_test(self, variant):
        """Store a short test curve. The light should turn blue at `blue_at` for two minutes."""
        if variant not in (1, 2):
            raise ValueError("Unknown test")
        if not self.transport.is_connected:
            raise ConnectionError("The light must be connected. Take the PC near the light.")
        ts_now = time.time()
        now = datetime.fromtimestamp(ts_now)
        t0 = now.hour * 60 + now.minute + 2           # at least one full minute ahead
        if t0 + 6 >= 1440:
            raise ValueError("Please start the test after midnight (00:10).")
        minute_start = ts_now - now.second - now.microsecond / 1e6
        slot = Light.active_slot(self, ts_now)         # the program that runs today
        w, b = {"r": 0, "g": 0, "b": 0, "w": self.TEST_W}, {"r": 0, "g": 0, "b": self.TEST_B, "w": 0}
        pts = curves.normalize_points([{"t": 0, "v": w}, {"t": t0, "v": w}, {"t": t0 + 1, "v": b},
                                       {"t": t0 + 3, "v": b}, {"t": t0 + 4, "v": w}], self.channels)
        self.mode, self.manual_levels, self.manual_until = "program", None, None
        self.preview_levels = self.preview_until = None

        async def upload():
            await self._send(self.enc.set_time(datetime.now()))
            self._last_clock = time.time()
            await self._send(self.enc.mode(RESET_AUTO))
            await self._send(self.enc.mode(CUSTOM_INIT))
            for ch in CHANNELS:
                for p in pts:
                    await self._send(self.enc.curve_point(CHANNEL_ID[ch], p["t"] // 60, p["t"] % 60, p["v"][ch]))
            if variant == 2:
                await self._send(self.enc.mode(AUTO_ON))
        self.program_on_light = None
        self.lamp_intact = False
        self._persist()
        await self._locked(upload)
        self.lamp_intact = True               # the light holds a complete (test) program
        self.lamp_state = "test"
        self.test = {"variant": variant, "points": pts, "slot": slot, "blue_at": minute_start + 3 * 60,
                     "back_at": minute_start + 5 * 60, "until": minute_start + 10 * 60}
        self.levels = {c: int(v) for c, v in self.expected_levels(time.time()).items()}
        return {k: self.test[k] for k in ("variant", "blue_at", "back_at")}

    async def curve_result(self, variant, worked):
        slot = self.test["slot"] if self.test else Light.active_slot(self)
        self.test = None
        self.curve_tested = {"variant": variant, "ok": bool(worked), "at": time.time()}
        if worked:
            self.lamp_mode, self.curve_variant = "curve", variant
            self.weekdays = [slot] * 7            # a stored curve runs every day: today's program
        self.program_on_light = None              # store the real program again
        self._persist()
        await self._apply_now()
        res = self._send_result("Done")
        if worked:
            res["message"] = f"Full curves on. “{self.profiles[slot]['name']}” now runs every day. " + res["message"]
        return res

    def adopt_curve_result(self, other):
        """Copy a successful test result from an identical light."""
        if other.curve_variant:
            self.weekdays = [Light.active_slot(self)] * 7     # this light keeps its own program of today
            self.curve_variant, self.lamp_mode = other.curve_variant, "curve"
            self.curve_tested = dict(other.curve_tested or {}, copied_from=other.name)
            self.program_on_light = None
            self.dirty = True
            self.poke()

    async def identify(self):
        """Let the light blink blue three times so the user can see which one it is."""
        if not self.transport.is_connected:
            self._connecting = False
            await self.transport.connect(self._on_disconnect, allow_search=True)
            self.online, self.connection = True, "connected"

        async def blink():
            for _ in range(3):
                for ch in CHANNELS:
                    await self._send(self.enc.channel(CHANNEL_ID[ch], 100 if ch == "b" else 0))
                await asyncio.sleep(0.6)
                for ch in CHANNELS:
                    await self._send(self.enc.channel(CHANNEL_ID[ch], 0))
                await asyncio.sleep(0.4)
            await self._send(self.enc.mode(AUTO_ON))    # back to the program stored in the light
            self._sent = {}
            self.lamp_state = "unknown"
        try:
            await self._locked(blink)
        except ConnectionError:
            # do not leave the light blue or dark: one more try to switch it back to its own program
            try:
                await self.transport.connect(self._on_disconnect, allow_search=True)
                await self._send(self.enc.mode(AUTO_ON))
                if self.adopted:
                    self.online, self.connection, self._connected_at = True, "connected", time.time()
            except Exception:  # noqa: BLE001
                pass
            if not self.adopted:
                await self.transport.disconnect()
                self.online, self.connection = False, "offline"
            raise
        if not self.adopted:
            await self.transport.disconnect()
            self.online, self.connection = False, "offline"

    async def handover(self):
        """Fast path before sleep or shutdown: make sure the light runs its own program.

        Only the one short "run your program" command is sent. A full program write is
        never started here: it begins by erasing the light's program, and the PC may be
        gone before it is complete. A change that is not yet stored waits for the next start."""
        if not self.transport.is_connected or not self.adopted:
            return  # never write to a light the user has not confirmed as theirs
        self.mode, self.manual_levels, self.manual_until = "program", None, None
        self.preview_levels = self.preview_until = None
        if self.lamp_state == "program" or not self.lamp_intact or self.test:
            return  # already running it, or only a full write could help (done on the next start)
        if self.lamp_mode == "curve" and self.curve_variant == 1:
            return  # this variant can only go back by a full write; done on the next start
        async with self._lock:
            await self._send(self.enc.mode(AUTO_ON))
            self.lamp_state = "program"
            self.left_in = None
            self._sent = {}

    async def shutdown(self, handover=True):
        if handover:
            try:
                await self.handover()
            except Exception:  # noqa: BLE001
                pass
        await self.transport.disconnect()

    def to_store(self):
        d = super().to_store()
        d.update({"address": self.address, "adopted": self.adopted, "program_on_light": self.program_on_light,
                  "lamp_intact": self.lamp_intact, "left_in": self.left_in, "lamp_mode": self.lamp_mode,
                  "curve_variant": self.curve_variant, "curve_tested": self.curve_tested})
        return d

    def load_store(self, d):
        super().load_store(d)
        self.program_on_light = d.get("program_on_light")
        self.curve_variant = d.get("curve_variant")
        self.lamp_mode = d.get("lamp_mode") if d.get("lamp_mode") == "curve" and self.curve_variant else "simple"
        self.curve_tested = d.get("curve_tested")
        self.adopted = d.get("adopted", True)
        self.lamp_intact = d.get("lamp_intact", True)
        self.left_in = d.get("left_in") if d.get("left_in") in ("manual", "test", "erased") else None
        if self.left_in:
            self.status_text = self.AWAY_TEXT[self.left_in]


def _minute(ts):
    d = datetime.fromtimestamp(ts)
    return d.hour * 60 + d.minute + d.second / 60


async def scan(timeout=6.0):
    """Return [(address, name, rssi, device)] of Chihiros Universal lights nearby."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for address, (device, adv) in found.items():
        name = (getattr(adv, "local_name", None) or getattr(device, "name", None) or "").strip()
        if name.upper().startswith(NAME_PREFIXES):
            out.append((address, name, getattr(adv, "rssi", None), device))
    return out
