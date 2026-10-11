"""The bridge core: finds lights, keeps them running, stores settings."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from . import chihiros, curves, helialux
from .base import Light

log = logging.getLogger("light_bridge")
VERSION = "0.11.0"


def default_data_dir() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "LightBridge"
    return Path.home() / ".light-bridge"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())[-12:] or "x"


class Engine:
    TICK_SECONDS = 2.0
    BLE_SCAN_EVERY = 30
    LAN_SCAN_EVERY = 120

    def __init__(self, data_dir: Path, demo: bool = False, port: int = 8080, demo_helialux: bool = False):
        self.demo = demo
        self.demo_helialux = demo_helialux
        self.port = port
        self.data_dir = Path(data_dir) / ("demo" if demo else "")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store_path = self.data_dir / "lights.json"
        self.lights: dict[str, Light] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.ble = {"available": None, "scanning": False, "last_scan": None, "message": ""}
        self.lan = {"scanning": False, "last_scan": None, "message": ""}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._scan_event: asyncio.Event | None = None
        self._lan_event: asyncio.Event | None = None
        self.stopping = False
        self._dirty = False
        self.ignored: set = set()           # Bluetooth addresses the user said are not theirs
        self.notice = ""                    # shown once in the app, e.g. after a damaged settings file
        self.access_key = None              # set by the server; goes into the phone link (QR code)
        self._ips = (0.0, [])

    # ------------------------------------------------------------ persistence
    def _make_light(self, d: dict) -> Light | None:
        kind = d.get("kind")
        if kind == "chihiros":
            transport = chihiros.SimTransport(d.get("address", "SIM")) if self.demo else None
            light = chihiros.ChihirosLight(d["id"], d.get("name", "Chihiros"), d.get("room", ""),
                                           d.get("address", ""), transport=transport)
            light.scan_assisted = not self.demo
        elif kind == "helialux":
            client = None
            if self.demo:
                from .simulation import SimHeliaLuxHttp
                client = SimHeliaLuxHttp(d.get("ip", "192.168.0.50"),
                                         "2Ch" if len(d.get("channels") or []) == 2 else "4Ch")
            light = helialux.HeliaLuxLight(d["id"], d.get("name", "HeliaLux"), d.get("room", ""),
                                           d.get("ip", ""), channels=d.get("channels"), client=client)
        else:
            return None
        light.load_store(d)
        return light

    def load(self):
        data = {}
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                # keep the damaged file for rescue instead of silently overwriting it
                broken = self.store_path.with_name(f"lights.broken-{int(time.time())}.json")
                try:
                    os.replace(self.store_path, broken)
                except OSError:
                    pass
                log.warning("could not read %s (%s); kept a copy as %s", self.store_path, e, broken.name)
                self.notice = (f"The settings file was damaged and could not be read. A copy was kept as "
                               f"{broken.name} in the settings folder. Restore a backup if you have one.")
        if not isinstance(data, dict):
            data = {}
        self.ignored = set(x for x in data.get("ignored", []) if isinstance(x, str))
        for d in data.get("lights", []) if isinstance(data.get("lights"), list) else []:
            try:
                light = self._make_light(d)
            except Exception as e:  # noqa: BLE001 - one bad entry must not stop the bridge
                log.warning("skipped a damaged light entry: %s", e)
                continue
            if light:
                self.lights[light.id] = light
        if self.demo and not self.lights:
            demo = [{"id": "chihiros-demo-1", "kind": "chihiros", "name": "Main tank", "room": "Living room",
                     "address": "SIM-DYU700"},
                    {"id": "chihiros-demo-2", "kind": "chihiros", "name": "Second tank", "room": "Bedroom",
                     "address": "SIM-DYU700-2"}]
            if self.demo_helialux:
                demo.append({"id": "helialux-demo", "kind": "helialux", "name": "HeliaLux Spectrum", "room": "Office",
                             "ip": "192.168.0.50"})
            for d in demo:
                self.lights[d["id"]] = self._make_light(d)
            self._dirty = True
        if self.demo:
            for l in self.lights.values():          # plausible signal values for the simulated lights
                if l.kind == "chihiros" and l.rssi is None:
                    l.rssi = -58 if l.id.endswith("1") else -66
            # a third, "foreign" light nearby, so the add / ignore steps can be tried safely
            addr = "SIM-DYU700-NEARBY"
            if addr not in self.ignored and not any(getattr(l, "address", "") == addr for l in self.lights.values()):
                light = self._make_light({"id": "chihiros-demo-nearby", "kind": "chihiros",
                                          "name": "DYU700E4F1", "address": addr})
                light.adopted = False
                light.rssi = -74
                self.lights[light.id] = light

    def save(self, force=False):
        if not force and not self._dirty and not any(l.dirty for l in self.lights.values()):
            return
        # lights that were only seen nearby (not confirmed) are not saved; they are found again next time
        data = {"version": 1, "lights": [l.to_store() for l in self.lights.values() if getattr(l, "adopted", True)],
                "ignored": sorted(self.ignored)}
        tmp = self.store_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=1))
            f.flush()
            os.fsync(f.fileno())                # on disk before it replaces the old file
        os.replace(tmp, self.store_path)
        self._dirty = False
        for l in self.lights.values():
            l.dirty = False

    # ------------------------------------------------------------ lifecycle
    async def run(self):
        self.loop = asyncio.get_running_loop()
        self._scan_event = asyncio.Event()
        self._lan_event = asyncio.Event()
        if not self.demo:
            chihiros.BleTransport.radio_lock = asyncio.Lock()
        for light in self.lights.values():
            self._start_light(light)
        await asyncio.gather(self._ble_loop(), self._lan_loop(), self._save_loop())

    def _start_light(self, light: Light):
        if hasattr(light, "persist"):
            light.persist = self._save_now
        async def runner():
            light.wake = asyncio.Event()
            while not self.stopping:
                try:
                    await light.tick(time.time())
                except Exception as e:  # noqa: BLE001
                    light.last_error = str(e)
                    log.exception("tick %s", light.name)
                try:
                    await asyncio.wait_for(light.wake.wait(), timeout=self.TICK_SECONDS)
                except asyncio.TimeoutError:
                    pass
                light.wake.clear()
        self._tasks[light.id] = asyncio.ensure_future(runner())

    def _save_now(self):
        """Save at once after decisions the user would not want to repeat (add, ignore, remove)."""
        try:
            self.save(force=True)
        except OSError as e:
            log.warning("save failed: %s", e)

    async def _save_loop(self):
        while not self.stopping:
            await asyncio.sleep(5)
            try:
                self.save()
            except OSError as e:
                log.warning("save failed: %s", e)

    async def _wait_scan(self, seconds):
        try:
            await asyncio.wait_for(self._scan_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        forced = self._scan_event.is_set()
        return forced

    async def _ble_loop(self):
        if self.demo:
            self.ble.update(available=True, message="Demo mode: simulated light")
            return
        try:
            import bleak  # noqa: F401
            self.ble["available"] = True
        except ImportError:
            self.ble.update(available=False, message="Bluetooth support is not installed. Start the bridge with the start file.")
            return
        forced = True
        while not self.stopping:
            known = [l for l in self.lights.values() if l.kind == "chihiros"]
            mine = [l for l in known if l.adopted]
            if forced or not mine or any(not l.online for l in mine):
                self.ble["scanning"] = True
                try:
                    async with chihiros.BleTransport.radio_lock:
                        found = await chihiros.scan(6.0)
                    self.ble["message"] = ""
                    for address, name, rssi, device in found:
                        if address in self.ignored:
                            continue
                        light = next((l for l in known if getattr(l, "address", None) == address), None)
                        if light is None:
                            # Found nearby: listed as "new", but NOTHING is written to it until the user
                            # confirms it is theirs (it could be a neighbour's light).
                            lid = f"chihiros-{slug(address)}"
                            light = chihiros.ChihirosLight(lid, name or "Chihiros", "", address)
                            light.scan_assisted = True
                            light.adopted = False
                            self.lights[lid] = light
                            known.append(light)
                            self._dirty = True
                            self._start_light(light)
                            log.info("found new Chihiros light %s (%s)", name, address)
                        light.seen(rssi, device)
                except Exception as e:  # noqa: BLE001
                    self.ble["message"] = f"Bluetooth problem: {e}. Is Bluetooth switched on?"
                finally:
                    self.ble["scanning"] = False
                    self.ble["last_scan"] = time.time()
            recently = any(getattr(l, "seen_at", None) and time.time() - l.seen_at < 600 for l in known)
            forced = await self._wait_scan(self.BLE_SCAN_EVERY if recently or not known else 90)

    async def _lan_loop(self):
        """HeliaLux search in the local network.

        Only on request (button "Search this home network"). Never automatically: the laptop
        is also used in other networks (office, hotel), where scanning is not appropriate.
        """
        if self.demo:
            return
        last = 0.0
        while not self.stopping:
            try:
                await asyncio.wait_for(self._lan_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            forced = self._lan_event.is_set()
            self._lan_event.clear()
            known = [l for l in self.lights.values() if l.kind == "helialux"]
            if not forced:
                continue
            self.lan["scanning"] = True
            try:
                online_ips = [l.ip for l in known if l.online]
                found = await self.loop.run_in_executor(None, helialux.scan_network, online_ips)
                for ip, st in found:
                    self._adopt_helialux(ip, st)
                self.lan["message"] = "" if found or not forced else "No HeliaLux found in this network."
            except Exception as e:  # noqa: BLE001
                self.lan["message"] = f"Network search failed: {e}"
            finally:
                self.lan["scanning"] = False
                self.lan["last_scan"] = last = time.time()

    def _adopt_helialux(self, ip, status):
        known = [l for l in self.lights.values() if l.kind == "helialux"]
        if any(l.ip == ip for l in known):
            return None
        offline = [l for l in known if not l.online]
        if len(offline) == 1:                       # same light, new address (e.g. after a power cut)
            offline[0].set_ip(ip)
            self._dirty = True
            return offline[0]
        n = 1
        while f"helialux-{n}" in self.lights:
            n += 1
        channels = helialux.channels_for_lamp(status.get("lamp"))
        name = "HeliaLux " + ("Spectrum" if len(channels) == 4 else "LED")
        light = helialux.HeliaLuxLight(f"helialux-{n}", name, "", ip, channels=channels)
        self.lights[light.id] = light
        self._dirty = True
        self._start_light(light)
        log.info("found HeliaLux at %s", ip)
        return light

    async def handover_all(self, timeout=1.5):
        """Before sleep: every light back to its own program, all lights at the same time (fast)."""
        lights = [l for l in self.lights.values() if hasattr(l, "handover")]

        async def one(light):
            try:
                await asyncio.wait_for(light.handover(), timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
        await asyncio.gather(*(one(l) for l in lights))
        self._save_now()

    async def shutdown(self):
        self.stopping = True
        for task in self._tasks.values():
            task.cancel()
        await self.handover_all(timeout=4)

        async def close(light):
            try:
                await asyncio.wait_for(light.shutdown(handover=False), timeout=5)
            except Exception:  # noqa: BLE001
                pass
        await asyncio.gather(*(close(l) for l in list(self.lights.values())))
        self.save(force=True)
        chihiros.BleTransport.radio_lock = None

    # ------------------------------------------------------------ API
    def _get(self, lid, new_ok=False) -> Light:
        if lid not in self.lights:
            raise KeyError("Unknown light")
        light = self.lights[lid]
        if not new_ok and getattr(light, "adopted", True) is False:
            # safety: nothing may be changed on a light that could belong to a neighbour
            raise ValueError("First confirm that this light is yours.")
        return light

    def local_ips(self):
        """This PC's addresses, looked up at most once a minute (the lookup can be slow on VPNs)."""
        at, ips = self._ips
        if time.time() - at > 60:
            ips = helialux.local_ipv4_addresses()
            self._ips = (time.time(), ips)
        return ips

    async def api_state(self):
        now = time.time()
        lights = []
        for l in self.lights.values():
            try:
                lights.append(l.state(now))
            except Exception as e:  # noqa: BLE001 - one broken light must not hide the others
                log.exception("state of %s", l.name)
                lights.append({"id": l.id, "kind": l.kind, "kind_label": l.kind_label, "name": l.name,
                               "room": l.room, "channels": l.channels, "online": False, "connection": "offline",
                               "status_text": f"Problem: {e}", "levels": {c: 0 for c in l.channels},
                               "mode": "program", "manual_until": None, "preview": False, "active_slot": 0,
                               "active_name": "", "today_points": [], "next_change": None,
                               "caps": {}, "info": {}, "last_error": str(e), "broken": True})
        ips = await self.loop.run_in_executor(None, self.local_ips) if self.loop else self.local_ips()
        return {
            "bridge": {"version": VERSION, "demo": self.demo, "now": now,
                       "urls": [f"http://{ip}:{self.port}/" + (f"?k={self.access_key}" if self.access_key else "")
                                for ip in ips],
                       "ble": self.ble, "lan": self.lan, "data_dir": str(self.data_dir), "notice": self.notice,
                       "hidden": len(self.ignored)},
            "lights": lights,
        }

    async def api_scan(self):
        self._scan_event.set()
        await asyncio.sleep(0.05)
        self._scan_event.clear()
        return {"ok": True}

    async def api_adopt(self, lid, name=None, room=None):
        light = self._get(lid, new_ok=True)
        n = sum(1 for l in self.lights.values() if l.kind == "chihiros" and getattr(l, "adopted", True))
        light.adopted = True
        light.name = (name or "").strip()[:40] or ("Chihiros" if n == 0 else f"Chihiros {n + 1}")
        light.room = (room or "").strip()[:40]
        light.dirty = True
        light.poke()
        self._save_now()
        return {"ok": True}

    async def api_identify(self, lid):
        light = self._get(lid, new_ok=True)
        if not hasattr(light, "identify"):
            raise ValueError("Not available for this light")
        await light.identify()
        return {"ok": True}

    async def api_ignore(self, lid):
        light = self._get(lid, new_ok=True)
        self.ignored.add(getattr(light, "address", lid))
        await self.api_forget(lid)
        return {"ok": True}

    async def api_unignore(self):
        """Show lights again that were marked "Not mine" (in case of a mistake)."""
        self.ignored.clear()
        self._save_now()
        return await self.api_scan()

    async def api_find_helialux(self):
        self._lan_event.set()
        return {"ok": True}

    async def api_curve_test(self, lid, variant):
        light = self._get(lid)
        if not hasattr(light, "curve_test"):
            raise ValueError("Not available for this light")
        return await light.curve_test(int(variant))

    async def api_curve_result(self, lid, variant, worked, apply_to_all=False):
        light = self._get(lid)
        res = await light.curve_result(int(variant), bool(worked))
        if worked and apply_to_all:
            for other in self.lights.values():
                if other is not light and other.kind == "chihiros" and other.adopted:
                    other.adopt_curve_result(light)
        return res

    async def api_lamp_mode(self, lid, mode):
        light = self._get(lid)
        if not hasattr(light, "set_lamp_mode"):
            raise ValueError("Not available for this light")
        return await light.set_lamp_mode(mode)

    async def api_add_ip(self, ip):
        ip = (ip or "").strip()
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
            raise ValueError("Please enter an address like 192.168.178.40")
        st = await self.loop.run_in_executor(None, helialux.probe, ip, 3.0)
        if not st:
            raise ValueError(f"No HeliaLux SmartControl answered at {ip}.")
        light = self._adopt_helialux(ip, st)
        return {"ok": True, "id": light.id if light else None}

    async def api_settings(self, lid, body):
        light = self._get(lid)
        if "name" in body:
            light.name = str(body["name"]).strip()[:40] or light.name
        if "room" in body:
            light.room = str(body["room"]).strip()[:40]
        if light.kind == "helialux":
            if body.get("colours") in (2, 4):
                light.set_channels(helialux.CHANNELS_2 if body["colours"] == 2 else helialux.CHANNELS_4)
                light.channels_locked = True
                light.programs_loaded = False   # read the programs again with the new colour set
                light._next_poll = 0
            if body.get("ip"):
                light.set_ip(str(body["ip"]).strip())
        light.dirty = True
        return {"ok": True}

    async def api_forget(self, lid):
        light = self._get(lid, new_ok=True)
        task = self._tasks.pop(lid, None)
        if task:
            task.cancel()
        await light.shutdown(handover=True)
        del self.lights[lid]
        self._dirty = True
        self._save_now()
        return {"ok": True}

    async def api_manual(self, lid, levels, minutes):
        await self._get(lid).start_manual(levels, minutes)
        return {"ok": True}

    async def api_maintenance(self, lid, minutes=60):
        light = self._get(lid)
        await light.start_manual({c: 100 for c in light.channels}, minutes, mode="maintenance")
        return {"ok": True}

    async def api_program(self, lid):
        await self._get(lid).back_to_program()
        return {"ok": True}

    async def api_preview(self, lid, levels=None, stop=False):
        light = self._get(lid)
        if stop:
            await light.end_preview()
        else:
            await light.preview(levels or {})
        return {"ok": True}

    async def api_profiles(self, lid):
        light = self._get(lid)
        return {"profiles": light.profiles, "weekdays": light.weekdays, "channels": light.channels,
                "caps": light.caps(), "active_slot": light.active_slot()}

    async def api_save_profile(self, lid, slot, profile):
        light = self._get(lid)
        if not 0 <= slot < curves.MAX_SLOTS:
            raise ValueError("Unknown program slot")
        return await light.save_profile(slot, profile)

    async def api_weekdays(self, lid, days):
        await self._get(lid).set_weekdays(days)
        return {"ok": True}

    async def api_reload(self, lid):
        light = self._get(lid)
        await light.reload()
        return {"ok": True}

    async def api_sync_clock(self, lid):
        await self._get(lid).sync_clock()
        return {"ok": True}

    async def api_backup(self):
        return {"app": "light-bridge", "version": VERSION, "exported": time.time(),
                "lights": [l.to_store() for l in self.lights.values() if getattr(l, "adopted", True)]}

    async def api_restore(self, data):
        items = data.get("lights") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError("This is not a Light Bridge backup file.")
        restored = 0
        for d in items:
            if not isinstance(d, dict):
                continue
            light = self.lights.get(d.get("id"))
            if not light or light.kind != d.get("kind"):
                continue
            # what is stored IN the light is not taken from the file: the restored program is sent again
            d = {k: v for k, v in d.items() if k not in ("program_on_light", "lamp_intact", "left_in")}
            try:
                light.load_store(d)               # same Bluetooth address = the user's own light
            except (TypeError, ValueError, KeyError) as e:
                log.warning("restore skipped %s: %s", light.name, e)
                continue
            if hasattr(light, "program_on_light"):
                light.program_on_light = None
                light.poke()
            light.dirty = True
            restored += 1
        self._save_now()
        return {"ok": True, "restored": restored}

    def templates(self):
        return [{"id": t["id"], "name": t["name"], "description": t["description"]} for t in curves.TEMPLATES]

    async def api_template(self, lid, tid):
        light = self._get(lid)
        prof = curves.template_profile(tid, light.channels)
        prof["points"] = prof["points"][: light.max_points]
        return prof
