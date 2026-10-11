"""Shared light model used by every adapter (Chihiros, HeliaLux, simulations)."""
from __future__ import annotations

import time
from datetime import datetime

from . import curves


def now_minute(ts=None) -> float:
    d = datetime.fromtimestamp(ts if ts is not None else time.time())
    return d.hour * 60 + d.minute + d.second / 60


def weekday_index(ts=None) -> int:
    """0 = Monday … 6 = Sunday."""
    return datetime.fromtimestamp(ts if ts is not None else time.time()).weekday()


class Light:
    kind = "base"
    kind_label = "Light"
    max_points = 16
    PREVIEW_SECONDS = 120

    def __init__(self, lid: str, name: str, room: str, channels: list):
        self.id = lid
        self.name = name
        self.room = room
        self.channels = list(channels)
        self.online = False
        self.connection = "offline"      # connected | connecting | offline
        self.status_text = "Not connected yet"
        self.levels = curves.blank_levels(self.channels)
        self.mode = "program"            # program | manual | maintenance
        self.manual_levels = None
        self.manual_until = None
        self.preview_levels = None
        self.preview_until = None
        self.profiles = curves.default_profiles(self.channels)
        self.weekdays = [0] * 7          # profile slot per weekday (Mon..Sun)
        self.last_error = None
        self.dirty = False               # needs saving to the store
        self.wake = None                 # asyncio.Event set by the engine's runner

    def poke(self):
        """Ask the engine to run this light's next tick right away."""
        if self.wake is not None:
            self.wake.set()

    # ------------------------------------------------------------ helpers
    def caps(self) -> dict:
        return {"clouds": False, "weekdays_editable": True, "max_points": self.max_points,
                "clock_sync": False, "safety_net": False, "profiles_on_device": False}

    def active_slot(self, ts=None) -> int:
        slot = self.weekdays[weekday_index(ts)]
        return slot if 0 <= slot < len(self.profiles) else 0

    def program_levels(self, ts=None) -> dict:
        prof = self.profiles[self.active_slot(ts)]
        return curves.sample(prof["points"], now_minute(ts), self.channels)

    def manual_active(self, ts=None) -> bool:
        ts = ts if ts is not None else time.time()
        return self.mode in ("manual", "maintenance") and self.manual_until is not None and ts < self.manual_until

    def preview_active(self, ts=None) -> bool:
        ts = ts if ts is not None else time.time()
        return self.preview_until is not None and ts < self.preview_until

    def expire_modes(self, ts=None):
        ts = ts if ts is not None else time.time()
        if self.mode in ("manual", "maintenance") and not self.manual_active(ts):
            self.mode, self.manual_levels, self.manual_until = "program", None, None
        if self.preview_until is not None and not self.preview_active(ts):
            self.preview_levels, self.preview_until = None, None

    def set_channels(self, channels):
        self.channels = list(channels)
        for p in self.profiles:
            p["points"] = curves.normalize_points(p["points"], self.channels, self.max_points)
        self.levels = curves.convert_levels(self.levels, self.channels)

    # ------------------------------------------------------------ actions (override)
    async def tick(self, ts):  # called every ~2 s by the engine
        pass

    async def start_manual(self, levels, minutes, mode="manual"):
        raise NotImplementedError

    async def back_to_program(self):
        raise NotImplementedError

    async def preview(self, levels):
        raise NotImplementedError

    async def end_preview(self):
        raise NotImplementedError

    async def save_profile(self, slot, profile) -> dict:
        self.profiles[slot] = {"name": str(profile.get("name") or "")[:24] or f"Program {slot + 1}",
                               "points": curves.normalize_points(profile["points"], self.channels, self.max_points)}
        self.dirty = True
        return {"ok": True, "verified": True, "message": "Saved"}

    async def set_weekdays(self, days):
        if not isinstance(days, list) or len(days) != 7:
            raise ValueError("Please choose a program for each of the 7 days.")
        self.weekdays = [max(0, min(len(self.profiles) - 1, int(d))) for d in days]
        self.dirty = True

    async def reload(self):
        pass

    async def sync_clock(self):
        pass

    async def shutdown(self, handover=True):
        pass

    def info(self) -> dict:
        return {}

    # ------------------------------------------------------------ state
    def state(self, ts=None) -> dict:
        ts = ts if ts is not None else time.time()
        slot = self.active_slot(ts)
        prof = self.profiles[slot]
        minute = now_minute(ts)
        nxt = curves.next_phase(prof["points"], int(minute))
        return {
            "id": self.id, "kind": self.kind, "kind_label": self.kind_label,
            "name": self.name, "room": self.room, "channels": self.channels,
            "online": self.online, "connection": self.connection, "status_text": self.status_text,
            "levels": self.levels, "mode": self.mode,
            "manual_until": self.manual_until if self.manual_active(ts) else None,
            "preview": self.preview_active(ts),
            "active_slot": slot, "active_name": prof["name"], "today_points": prof["points"],
            "next_change": nxt, "caps": self.caps(), "info": self.info(), "last_error": self.last_error,
        }

    def to_store(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "room": self.room,
                "channels": self.channels, "profiles": self.profiles, "weekdays": self.weekdays}

    def load_store(self, d: dict):
        self.name = d.get("name", self.name)
        self.room = d.get("room", self.room)
        if d.get("channels"):
            self.channels = list(d["channels"])
        if isinstance(d.get("profiles"), list) and d["profiles"]:
            profs = d["profiles"][: curves.MAX_SLOTS]
            while len(profs) < curves.MAX_SLOTS:
                profs.append({"name": f"Program {len(profs) + 1}", "points": []})
            self.profiles = [{"name": p.get("name", f"Program {i + 1}"),
                              "points": curves.normalize_points(p.get("points", []), self.channels, self.max_points)}
                             for i, p in enumerate(profs)]
        if isinstance(d.get("weekdays"), list) and len(d["weekdays"]) == 7:
            self.weekdays = [max(0, min(len(self.profiles) - 1, int(x))) for x in d["weekdays"]]
