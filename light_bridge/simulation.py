"""Demo mode: lights that behave like the real ones, without any hardware.

The simulated HeliaLux answers with the same variables as the real controller,
so the real parsing and control code paths are exercised.
"""
from __future__ import annotations

import json
import threading
import time

from . import curves
from .base import now_minute
from .helialux import CH_ORDER


class SimHeliaLuxHttp:
    def __init__(self, ip="192.168.0.50", lamp="4Ch"):
        self.ip = ip
        self.lamp = lamp
        self.channels = ["w", "b"] if lamp.startswith("2") else ["w", "r", "g", "b"]
        self.programs = [curves.template_profile("community-nobreak", self.channels),
                         curves.template_profile("amazon", self.channels)] + [
            {"name": f"Profile {i + 1}", "points": curves.normalize_points([], self.channels)} for i in range(2, 8)]
        self.programs[0]["name"] = "std_13-21_mond"
        self.weekdays = [0, 0, 0, 0, 0, 1, 1]
        self.manual_until = 0
        self.manual_levels = {c: 0 for c in self.channels}
        self.selected = 0
        self._lock = threading.Lock()
        self.requests = []

    def _today(self):
        return self.programs[self.weekdays[time.localtime().tm_wday]]

    def status(self):
        with self._lock:
            self.requests.append(("GET", "/statusvars.js"))
            manual = time.time() < self.manual_until
            lv = self.manual_levels if manual else curves.sample(self._today()["points"], now_minute(), self.channels)
            return {"lang": 0, "lamp": self.lamp, "profNum": self.weekdays[time.localtime().tm_wday],
                    "profile": self._today()["name"], "tsimtime": int(now_minute()), "tsimact": 0,
                    "csimact": 1 if manual else 0,
                    "brightness": [lv.get(k, 0) for k in CH_ORDER if k in self.channels]}

    def start_manual(self, minutes):
        with self._lock:
            self.requests.append(("POST", f"/stat manual {minutes}"))
            self.manual_until = time.time() + minutes * 60
            self.manual_levels = curves.sample(self._today()["points"], now_minute(), self.channels)

    def set_levels(self, levels, channels):
        with self._lock:
            self.requests.append(("POST", "/stat levels"))
            if time.time() < self.manual_until:
                self.manual_levels = {c: int(levels.get(c, 0)) for c in self.channels}

    def stop_manual(self):
        with self._lock:
            self.requests.append(("POST", "/stat stop"))
            self.manual_until = 0

    def read_overview(self):
        return {"profiles": [p["name"] for p in self.programs], "profsel": list(self.weekdays)}

    def read_program(self, slot):
        p = self.programs[slot]
        out = {"profNum": slot, "profile": p["name"], "times": [x["t"] for x in p["points"]],
               "cI": [x.get("ci", 0) for x in p["points"]], "cM": [x.get("cm", 0) for x in p["points"]]}
        for i, k in enumerate(CH_ORDER):
            if k in self.channels:
                out[f"CH{i + 1}"] = [x["v"].get(k, 0) for x in p["points"]]
        return out

    def write_program(self, slot, fields):
        times = json.loads(fields["TIMES"])
        pts = []
        for i, t in enumerate(times):
            v = {k: json.loads(fields[f"CH{j + 1}"])[i] for j, k in enumerate(CH_ORDER) if f"CH{j + 1}" in fields}
            pts.append({"t": t, "v": v, "ci": json.loads(fields["CINT"])[i], "cm": json.loads(fields["CMOT"])[i]})
        self.programs[slot] = {"name": fields["PNAME"], "points": curves.normalize_points(pts, self.channels, 16)}
