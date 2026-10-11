"""Tests with fake hardware: a fake `bleak` module and a fake HeliaLux web server.

Run:  python -m unittest discover -s tests -v
"""
import asyncio
import json
import sys
import threading
import time
import types
import unittest
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ------------------------------------------------------------------ fake bleak
FAKE = types.SimpleNamespace(frames=[], devices={"AA:BB:CC:DD:EE:01": "DYU700A1B2", "11:22:33:44:55:66": "SomeSpeaker"},
                             fail_connect=0, clients=[], missing_wait=0.01, finds=[], fail_after=None, lamps={})


def lamp(addr):
    """A simple model of what the real light does with the frames it receives."""
    return FAKE.lamps.setdefault(addr, {"schedule": [], "curve": [], "auto": False, "manual": {}, "custom": False})


def lamp_apply(addr, data):
    m, fam, cmd, par = lamp(addr), data[0], data[5], list(data[6:-1])
    if fam == 0xA5 and cmd == 0x19:
        m["schedule"].append(par)
    elif fam == 0x5A and cmd == 0x05 and par[0] == 5:        # reset: stored program erased
        m.update(schedule=[], curve=[], auto=False, manual={}, custom=False)
    elif fam == 0x5A and cmd == 0x05 and par[0] == 3:
        m.update(curve=[], custom=True, manual={})
    elif fam == 0x5A and cmd == 0x05 and par[0] == 18:
        m.update(auto=True, manual={})
    elif fam == 0x5A and cmd == 0x06:
        m["curve"].append(par)
    elif fam == 0x5A and cmd == 0x07:
        m["manual"][par[0]] = par[1]
        m["auto"] = False


def lamp_does(addr):
    """What the light does on its own right now (e.g. when the PC has just left)."""
    m = lamp(addr)
    if m["manual"]:
        return "manual"
    if m["auto"] and (m["schedule"] or m["curve"]):
        return "program"
    if m["custom"] and m["curve"]:
        return "program"
    return "nothing"          # erased or empty: the tank stays dark / undefined


class _Char:
    properties = ["write", "write-without-response"]


class _Services:
    def get_characteristic(self, uuid):
        return _Char()


class BLEDevice:
    def __init__(self, address, name):
        self.address, self.name = address, name


class AdvertisementData:
    def __init__(self, name, rssi=-60):
        self.local_name, self.rssi = name, rssi


class BleakScanner:
    @staticmethod
    async def discover(timeout=5.0, return_adv=False):
        await asyncio.sleep(0.01)
        return {a: (BLEDevice(a, n), AdvertisementData(n)) for a, n in FAKE.devices.items()}

    @staticmethod
    async def find_device_by_address(address, timeout=10.0):
        FAKE.finds.append(address)
        if address not in FAKE.devices:
            await asyncio.sleep(min(timeout, FAKE.missing_wait))   # real bleak waits the full timeout
            return None
        await asyncio.sleep(0.01)
        return BLEDevice(address, FAKE.devices.get(address))


class BleakClient:
    def __init__(self, device, disconnected_callback=None, **kw):
        self.device, self.cb, self.is_connected = device, disconnected_callback, False
        self.services = _Services()
        FAKE.clients.append(self)

    async def connect(self, **kw):
        if self.device.address not in FAKE.devices:
            raise OSError("device gone")
        if FAKE.fail_connect > 0:
            FAKE.fail_connect -= 1
            raise OSError("simulated connection failure")
        self.is_connected = True

    async def start_notify(self, uuid, cb):
        self.notify_cb = cb

    async def write_gatt_char(self, uuid, data, response=None):
        if not self.is_connected:
            raise OSError("not connected")
        if FAKE.fail_after is not None:
            if FAKE.fail_after <= 0:
                self.drop()
                raise OSError("out of range mid-write")
            FAKE.fail_after -= 1
        FAKE.frames.append((uuid, bytes(data), response, self.device.address))
        lamp_apply(self.device.address, bytes(data))

    async def disconnect(self):
        self.is_connected = False

    def drop(self):  # simulate the light going out of range
        self.is_connected = False
        if self.cb:
            self.cb(self)


sys.modules["bleak"] = types.SimpleNamespace(BleakScanner=BleakScanner, BleakClient=BleakClient)

from light_bridge import chihiros, curves, helialux  # noqa: E402
from light_bridge.base import Light  # noqa: E402
from light_bridge.engine import Engine  # noqa: E402

chihiros.ChihirosLight.STABLE_SECONDS = 0     # tests run fast; the waiting time has its own test


# ------------------------------------------------------------------ fake HeliaLux
class FakeHeliaLux:
    def __init__(self, lamp="4Ch"):
        self.lamp = lamp
        self.nch = 2 if lamp.startswith("2") else 4
        self.programs = [{"name": "std_13-21_mond" if i == 0 else f"Profile {i + 1}",
                          "times": [0, 780, 840, 1260, 1290], "ch": [[0, 0, 60, 60, 0][:] for _ in range(self.nch)],
                          "ci": [0] * 5, "cm": [0] * 5} for i in range(8)]
        self.selected = 0
        self.csim = 0
        self.manual = [0] * self.nch
        self.requests = []

    def js(self, d):
        out = []
        for k, v in d.items():
            if isinstance(v, str):
                out.append(f"{k}='{v}';")
            else:
                out.append(f"{k}={json.dumps(v, separators=(',', ':'))};")
        return "\n".join(out)

    def handle(self, method, path, form):
        self.requests.append((method, path, form))
        if path == "/statusvars.js":
            return self.js({"lang": 0, "lamp": self.lamp, "profNum": 0, "profile": self.programs[0]["name"],
                            "tsimtime": datetime.now().hour * 60 + datetime.now().minute, "tsimact": 0, "csimact": self.csim,
                            "brightness": self.manual if self.csim else [11, 22, 33, 44][: self.nch],
                            "times": self.programs[0]["times"]})
        if path == "/wpvars.js":
            return self.js({"profiles": [p["name"] for p in self.programs], "profsel": [0, 0, 0, 0, 0, 1, 1]})
        if path.startswith("/profedit.html"):
            self.selected = int(urllib.parse.parse_qs(path.split("?", 1)[1])["profNum"][0])
            return "<html></html>"
        if path == "/profeditvars.js":
            p = self.programs[self.selected]
            d = {"live": 0, "profNum": self.selected, "profile": p["name"], "times": p["times"], "cM": p["cm"], "cI": p["ci"]}
            for i in range(self.nch):
                d[f"CH{i + 1}"] = p["ch"][i]
            return self.js(d)
        if path == "/stat":
            if form.get("action") == "14":
                self.csim = 1 if form.get("cswi") == "true" else 0
            if form.get("action") == "10":
                for i in range(self.nch):
                    if f"ch{i + 1}" in form:
                        self.manual[i] = int(form[f"ch{i + 1}"])
            return "ok"
        if path == "/pedit" and form.get("action") == "30":
            p = self.programs[self.selected]
            p["name"] = form["PNAME"]
            p["times"] = json.loads(form["TIMES"])
            p["ch"] = [json.loads(form[f"CH{i + 1}"]) for i in range(self.nch)]
            p["ci"] = json.loads(form["CINT"])
            p["cm"] = json.loads(form["CMOT"])
            return "ok"
        return None


def serve(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, body):
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            b = body.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._reply(fake.handle("GET", self.path, {}))

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
            self._reply(fake.handle("POST", self.path, form))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"127.0.0.1:{srv.server_address[1]}"


# ------------------------------------------------------------------ tests
class ProtocolTests(unittest.TestCase):
    def test_documented_frames(self):
        e = chihiros.Encoder()
        e.hi, e.lo = 0x00, 0x1F
        self.assertEqual(e.channel(0, 100).hex(" "), "5a 01 07 00 20 07 00 64 45")
        e.hi, e.lo = 0x00, 0x09
        self.assertEqual(e.set_time(datetime(2025, 9, 2, 10, 0, 25)).hex(" "), "5a 01 0b 00 0a 09 19 09 02 0a 00 19 08")
        e.hi, e.lo = 0x00, 0x16
        self.assertEqual(e.frame(0xA5, 0x19, [2, 0x1E, 5, 0x0A, 1, 0x7F] + [0xFF] * 8).hex(" "),
                         "a5 01 13 00 17 19 02 1e 05 0a 01 7f ff ff ff ff ff ff ff ff 71")
        e.hi, e.lo = 0x00, 0xCF
        self.assertEqual(e.mode(5).hex(" "), "5a 01 08 00 d0 05 05 ff ff d9")

    def test_schedule_frame_fields(self):
        e = chihiros.Encoder()
        f = e.basic_schedule(8 * 60 + 30, 21 * 60 + 15, 200, chihiros.WEEKDAY_BITS[0] | chihiros.WEEKDAY_BITS[6],
                             {"r": 1, "g": 2, "b": 3, "w": 4})
        self.assertEqual((f[0], f[5]), (0xA5, 0x19))
        self.assertEqual(list(f[6:-1]), [8, 30, 21, 15, 150, 0x41, 1, 2, 3, 4, 0xFF, 0xFF, 0xFF, 0xFF])  # ramp max 150
        self.assertEqual(chihiros.WEEKDAY_BITS, [0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01])            # Mon..Sun
        t = e.set_time(datetime(2026, 10, 11, 9, 5, 7))                                                 # a Sunday
        self.assertEqual(list(t[6:-1]), [26, 10, 7, 9, 5, 7])

    def test_reserved_byte_never_sent(self):
        e = chihiros.Encoder()
        for i in range(70000):
            f = e.channel(i % 4, i % 101)
            self.assertNotIn(0x5A, f[1:])


class CurveTests(unittest.TestCase):
    def test_sample_and_wrap(self):
        pts = curves.normalize_points([{"t": 480, "v": {"w": 0}}, {"t": 600, "v": {"w": 100}}, {"t": 1320, "v": {"w": 20}}], ["w"])
        self.assertEqual(pts[0]["t"], 0)                       # 00:00 phase inserted
        self.assertEqual(curves.sample(pts, 540, ["w"])["w"], 50)
        self.assertEqual(curves.sample(pts, 1320, ["w"])["w"], 20)

    def test_safety_net(self):
        prof = curves.template_profile("aquascape", ["r", "g", "b", "w"])
        basic = curves.derive_basic_schedule(prof["points"], ["r", "g", "b", "w"])
        self.assertEqual((basic["on"] // 60, basic["off"] // 60), (8, 19))
        self.assertLessEqual(basic["ramp"], 150)

    def test_simple_version_ignores_moonlight_and_midnight(self):
        ch = ["r", "g", "b", "w"]
        day = {"w": 70, "r": 50, "g": 50, "b": 50}

        def P(t, **v):
            return {"t": t, "v": {c: v.get(c, 0) for c in ch}}
        moon = [P(0, b=20), P(480, b=20), P(600, **day), P(1200, **day), P(1320, b=20)]
        b = curves.derive_basic_schedule(moon, ch)
        self.assertTrue(480 <= b["on"] <= 600 and 1200 <= b["off"] <= 1320)   # NOT on all night
        late = [P(0, **day), P(60), P(780), P(840, **day)]                     # 14:00 .. 01:00
        b = curves.derive_basic_schedule(late, ch)
        self.assertTrue(b["cut_at_midnight"])
        self.assertTrue(780 <= b["on"] <= 840 and b["off"] == 1435)
        self.assertEqual(curves.derive_basic_schedule([P(0)], ch), None)
        for t in curves.TEMPLATES:                                             # every template: a daytime block
            b = curves.derive_basic_schedule(curves.normalize_points(t["points"], ch), ch)
            self.assertTrue(6 * 60 <= b["on"] < b["off"] <= 23 * 60, t["id"])

    def test_two_colour_conversion(self):
        prof = curves.template_profile("community", ["w", "b"])
        self.assertTrue(all(set(p["v"]) == {"w", "b"} for p in prof["points"]))


class ChihirosBleTests(unittest.TestCase):
    def setUp(self):
        FAKE.frames.clear()
        FAKE.clients.clear()
        FAKE.fail_connect = 0

    def test_scan_finds_only_universal(self):
        self.assertEqual([f[:3] for f in run(chihiros.scan(0.1))], [("AA:BB:CC:DD:EE:01", "DYU700A1B2", -60)])

    def kinds(self, frames=None):
        return [(f[1][0], f[1][5]) + ((f[1][6],) if f[1][5] == 0x05 else ()) for f in (frames or FAKE.frames)]

    def light(self):
        light = chihiros.ChihirosLight("c1", "Tank", "", "AA:BB:CC:DD:EE:01")
        light.FRAME_GAP = 0
        return light

    def test_connect_stores_program_in_the_light(self):
        async def go():
            light = self.light()
            await light.tick(time.time())
            return light
        light = run(go())
        self.assertTrue(light.online)
        k = self.kinds()
        self.assertEqual(k[0], (0x5A, 0x09))                      # clock first
        self.assertEqual(k[1], (0x5A, 0x05, 5))                   # reset stored program
        self.assertIn((0xA5, 0x19), k)                            # one bright block (documented)
        self.assertEqual(k[-1], (0x5A, 0x05, 18))                 # the light runs its own program
        self.assertNotIn((0x5A, 0x07), k)                         # no live driving any more
        self.assertTrue(all(f[2] is True for f in FAKE.frames))   # confirmed writes
        self.assertFalse(light.info()["program_pending"])

    def test_nothing_is_rewritten_when_unchanged(self):
        async def go():
            light = self.light()
            t = time.time()
            await light.tick(t)
            n = len(FAKE.frames)
            for i in range(5):
                await light.tick(t + 2 * (i + 1))
            self.assertEqual(len(FAKE.frames), n)
        run(go())

    def test_manual_then_laptop_leaves_then_returns(self):
        """Manual light active, laptop leaves: on return the light goes back to its program."""
        async def go():
            light = self.light()
            t = time.time()
            await light.tick(t)
            await light.start_manual({"r": 10, "g": 20, "b": 30, "w": 40}, 30)
            self.assertEqual(self.kinds()[-1], (0x5A, 0x07))
            FAKE.clients[-1].drop()                               # lid closed / out of range
            self.assertFalse(light.online)
            self.assertEqual(lamp_does(light.address), "manual")  # honest: the manual light stays …
            self.assertEqual(light.info()["left_in"], "manual")   # … and the app says so
            self.assertIn("manual", light.status_text)
            light.manual_until = time.time() - 1                  # the manual time ran out meanwhile
            n = len(FAKE.frames)
            light._next_try = 0
            await light.tick(time.time())
            k = self.kinds(FAKE.frames[n:])
            self.assertEqual(k, [(0x5A, 0x09), (0x5A, 0x05, 18)])  # clock + back to own program
            self.assertEqual(light.mode, "program")
            self.assertEqual(lamp_does(light.address), "program")
            self.assertIsNone(light.info()["left_in"])
        run(go())

    def test_change_while_away_is_sent_on_return(self):
        async def go():
            light = self.light()
            await light.tick(time.time())
            FAKE.clients[-1].drop()
            prof = curves.template_profile("aquascape", light.channels)
            res = await light.save_profile(0, prof)
            self.assertTrue(res.get("pending"))
            n = len(FAKE.frames)
            light._next_try = 0
            await light.tick(time.time())
            k = self.kinds(FAKE.frames[n:])
            self.assertIn((0xA5, 0x19), k)
            self.assertFalse(light.info()["program_pending"])
        run(go())

    def test_interrupted_write_is_repeated(self):
        async def go():
            light = self.light()
            FAKE.fail_after = 2                                   # clock + reset, then gone
            await light.tick(time.time())
            FAKE.fail_after = None
            self.assertFalse(light.online)
            self.assertTrue(light.info()["program_pending"])
            self.assertEqual(light.info()["left_in"], "erased")    # shown in the app
            light._next_try = 0
            await light.tick(time.time())
            self.assertFalse(light.info()["program_pending"])
            self.assertEqual(self.kinds()[-1], (0x5A, 0x05, 18))
        try:
            run(go())
        finally:
            FAKE.fail_after = None

    def test_reconnect_with_backoff(self):
        async def go():
            FAKE.fail_connect = 2
            light = self.light()
            t = time.time()
            await light.tick(t)
            await light.tick(t + 3)
            await light.tick(t + 10)
            self.assertTrue(light.online)
        run(go())

    def test_curve_test_and_curve_mode(self):
        async def go():
            light = self.light()
            await light.tick(time.time())
            n = len(FAKE.frames)
            info = await light.curve_test(1)
            k = self.kinds(FAKE.frames[n:])
            self.assertEqual(k[:4], [(0x5A, 0x09), (0x5A, 0x05, 5), (0x5A, 0x05, 3), (0x5A, 0x06)])
            self.assertEqual(sum(1 for x in k if x == (0x5A, 0x06)), 4 * 5)   # 4 colours x 5 points
            self.assertNotIn((0x5A, 0x05, 18), k)                  # variant 1: no "auto on"
            self.assertGreater(info["back_at"], info["blue_at"])
            self.assertEqual(light.expected_levels(info["blue_at"] + 30)["b"], light.TEST_B)
            n = len(FAKE.frames)
            await light.curve_result(1, True)
            self.assertEqual(light.lamp_mode, "curve")
            k = self.kinds(FAKE.frames[n:])
            pts = len(light.curve_points())
            self.assertEqual(sum(1 for x in k if x == (0x5A, 0x06)), 4 * pts)
            # manual and back: variant 1 stores the curve again
            await light.start_manual({"w": 50}, 10)
            n = len(FAKE.frames)
            await light.back_to_program()
            self.assertIn((0x5A, 0x05, 3), self.kinds(FAKE.frames[n:]))
            with self.assertRaises(ValueError):
                await light.set_weekdays([0, 1, 0, 0, 0, 0, 0])
            await light.set_lamp_mode("simple")
            self.assertIn((0xA5, 0x19), self.kinds(FAKE.frames[-10:]))
        run(go())

    def test_failed_curve_test_keeps_simple(self):
        async def go():
            light = self.light()
            await light.tick(time.time())
            await light.curve_test(2)
            self.assertIn((0x5A, 0x05, 18), self.kinds(FAKE.frames[-3:]))   # variant 2 ends with auto on
            n = len(FAKE.frames)
            await light.curve_result(2, False)
            self.assertEqual(light.lamp_mode, "simple")
            self.assertIn((0xA5, 0x19), self.kinds(FAKE.frames[n:]))
            self.assertFalse(light.curve_tested["ok"])
        run(go())


    def test_laptop_may_leave_at_any_moment(self):
        """Cut the link after every single frame of a program write: the light either still
        runs a program, or the app knows it is unfinished - and the next visit always repairs it."""
        async def first_frames():
            light = self.light()
            await light.tick(time.time())
            return len(FAKE.frames)
        total = run(first_frames())
        self.assertGreater(total, 3)
        for cut in range(total):
            FAKE.frames.clear(); FAKE.clients.clear(); FAKE.lamps.clear()

            async def go():
                light = self.light()
                FAKE.fail_after = cut
                await light.tick(time.time())
                FAKE.fail_after = None
                does = lamp_does(light.address)
                self.assertTrue(does == "program" or light.info()["left_in"] == "erased" or cut <= 1,
                                f"cut after {cut} frames: light does {does}, app shows {light.info()['left_in']}")
                light._next_try = 0
                await light.tick(time.time())                    # the PC is back
                self.assertEqual(lamp_does(light.address), "program", f"not repaired after cut {cut}")
                self.assertIsNone(light.info()["left_in"])
            try:
                run(go())
            finally:
                FAKE.fail_after = None

    def test_handover_never_starts_erasing(self):
        async def go():
            light = self.light()
            await light.tick(time.time())
            light.STABLE_SECONDS = 3600                           # the link is "not stable" from now on
            light._connected_at = time.time()
            await light.save_profile(0, curves.template_profile("aquascape", light.channels))
            await light.start_manual({"w": 90}, 60)
            n = len(FAKE.frames)
            await light.handover()                                # e.g. the lid is being closed
            self.assertEqual(self.kinds(FAKE.frames[n:]), [(0x5A, 0x05, 18)])   # only "run your program"
            self.assertEqual(lamp_does(light.address), "program")             # the old, complete program
            self.assertTrue(light.info()["program_pending"])                  # new one follows next time
        run(go())

    def test_program_is_only_written_on_a_steady_link(self):
        async def go():
            light = self.light()
            light.STABLE_SECONDS = 0.3
            await light.tick(time.time())                         # just connected (walking past)
            self.assertNotIn((0xA5, 0x19), self.kinds())
            self.assertEqual(self.kinds()[-1], (0x5A, 0x05, 18))  # it keeps running its own program
            await asyncio.sleep(0.35)
            await light.tick(time.time())
            self.assertIn((0xA5, 0x19), self.kinds())
            self.assertFalse(light.info()["program_pending"])
        run(go())

    def test_curve_test_timing_and_todays_program(self):
        async def go():
            light, other = self.light(), chihiros.ChihirosLight("c2", "Other", "", "AA:BB:CC:DD:EE:01")
            today = datetime.now().weekday()
            light.weekdays = [(d + 1) % 3 for d in range(7)]
            other.weekdays = [(d + 2) % 3 for d in range(7)]
            expect, expect_other = light.weekdays[today], other.weekdays[today]
            await light.tick(time.time())
            info = await light.curve_test(2)
            self.assertTrue(119 <= info["blue_at"] - time.time() <= 181)      # from "now", not from midnight
            await light.curve_result(2, True)
            self.assertEqual(light.weekdays, [expect] * 7)                    # today's program, every day
            other.adopt_curve_result(light)
            self.assertEqual(other.weekdays, [expect_other] * 7)              # the other keeps its own
            self.assertEqual(lamp_does(light.address), "program")
        run(go())

    def test_weekdays_need_seven_days(self):
        light = self.light()
        with self.assertRaises(ValueError):
            run(light.set_weekdays([]))
        light.load_store({"weekdays": [0, 99, -3, 0, 0, 0, 0]})
        self.assertEqual(light.weekdays, [0, 7, 0, 0, 0, 0, 0])
        light.simple_blocks()                                             # no crash


class HeliaLuxTests(unittest.TestCase):
    def _light(self, lamp="4Ch"):
        fake = FakeHeliaLux(lamp)
        srv, addr = serve(fake)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return fake, helialux.HeliaLuxLight("h1", "HL", "", addr)

    def test_parse_js_vars(self):
        v = helialux.parse_js_vars("var lamp='4Ch';tsimtime=678;brightness=[1,2,3,4];profiles=[\"a\",\"b c\"];")
        self.assertEqual(v, {"lamp": "4Ch", "tsimtime": 678, "brightness": [1, 2, 3, 4], "profiles": ["a", "b c"]})

    def test_probe(self):
        fake, light = self._light()
        self.assertIsNotNone(helialux.probe(light.ip))
        self.assertIsNone(helialux.probe("127.0.0.1:1"))

    def test_four_colour_flow(self):
        fake, light = self._light("4Ch")

        async def go():
            await light.refresh()
            self.assertTrue(light.online)
            self.assertEqual(light.levels, {"w": 11, "b": 22, "g": 33, "r": 44})
            self.assertTrue(light.programs_loaded)
            self.assertEqual(light.weekdays, [0, 0, 0, 0, 0, 1, 1])
            await light.start_manual({"w": 10, "r": 20, "g": 30, "b": 40}, 90)
            self.assertEqual(fake.csim, 1)
            self.assertEqual(fake.manual, [10, 40, 30, 20])                # CH1 w, CH2 b, CH3 g, CH4 r
            self.assertIn(("POST", "/stat", {"action": "14", "cswi": "true", "ctime": "01:30"}), fake.requests)
            await light.back_to_program()
            self.assertEqual(fake.csim, 0)
            await light.preview({"w": 50, "r": 0, "g": 0, "b": 5})
            self.assertEqual(fake.csim, 1)
            await light.end_preview()
            self.assertEqual(fake.csim, 0)
            prof = curves.template_profile("amazon", light.channels)
            res = await light.save_profile(3, prof)
            self.assertTrue(res["verified"], res)
            self.assertEqual(fake.programs[3]["name"], "Amazon, cloudy")
            self.assertIn(2, fake.programs[3]["ci"])
        run(go())

    def test_two_colour_flow(self):
        fake, light = self._light("2Ch")

        async def go():
            await light.refresh()
            self.assertEqual(light.channels, ["w", "b"])
            self.assertEqual(light.levels, {"w": 11, "b": 22})
            res = await light.save_profile(1, curves.template_profile("community", light.channels))
            self.assertTrue(res["verified"], res)
            self.assertEqual(len(fake.programs[1]["ch"]), 2)
        run(go())

    def test_offline_after_two_failures(self):
        light = helialux.HeliaLuxLight("h1", "HL", "", "127.0.0.1:1")

        async def go():
            await light.refresh()
            await light.refresh()
        run(go())
        self.assertFalse(light.online)


class EngineTests(unittest.TestCase):
    def test_discovers_both_lights_and_persists(self):
        import tempfile
        fake = FakeHeliaLux()
        srv, addr = serve(fake)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        orig = helialux.scan_network
        helialux.scan_network = lambda exclude=(): [(addr, helialux.probe(addr))]
        self.addCleanup(setattr, helialux, "scan_network", orig)
        tmp = tempfile.mkdtemp()

        async def go():
            eng = Engine(Path(tmp), demo=False)
            eng.load()
            task = asyncio.ensure_future(eng.run())
            await asyncio.sleep(0.2)
            await eng.api_find_helialux()          # HeliaLux search only on request
            await asyncio.sleep(1.5)
            st = await eng.api_state()
            kinds = sorted(l["kind"] for l in st["lights"])
            self.assertEqual(kinds, ["chihiros", "helialux"])
            for l in st["lights"]:
                if l["kind"] == "chihiros":
                    await eng.api_adopt(l["id"], "Tank", "Living room")   # confirm it is ours
            await eng.shutdown()
            task.cancel()
            saved = json.loads((Path(tmp) / "lights.json").read_text())
            self.assertEqual(len(saved["lights"]), 2)
        run(go())


class TwoChihirosTests(unittest.TestCase):
    """Two Chihiros lights driven by one bridge at the same time."""
    A, B = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"

    def setUp(self):
        import tempfile
        FAKE.frames.clear(); FAKE.clients.clear(); FAKE.finds.clear()
        FAKE.fail_connect = 0
        self._devices = dict(FAKE.devices)
        self.addCleanup(lambda: (FAKE.devices.clear(), FAKE.devices.update(self._devices)))
        FAKE.devices.clear()
        FAKE.devices.update({self.A: "DYU700A1B2", self.B: "DYU1200C3D4"})
        orig = helialux.scan_network
        helialux.scan_network = lambda exclude=(): []
        self.addCleanup(setattr, helialux, "scan_network", orig)
        self.tmp = Path(tempfile.mkdtemp())

    def frames_for(self, addr, kind=None):
        return [f for f in FAKE.frames if f[3] == addr and (kind is None or (f[1][0], f[1][5]) == kind)]

    def test_discovers_and_drives_both(self):
        async def go():
            eng = Engine(self.tmp)
            eng.load()
            task = asyncio.ensure_future(eng.run())
            await asyncio.sleep(1.5)
            lights = [l for l in eng.lights.values() if l.kind == "chihiros"]
            self.assertEqual(len(lights), 2)
            # found nearby, but nothing is written until the user confirms (could be a neighbour's light)
            self.assertTrue(all(l.adopted is False for l in lights))
            self.assertEqual(FAKE.frames, [])
            eng.save(force=True)
            self.assertEqual(json.loads((self.tmp / "lights.json").read_text())["lights"], [])
            st = await eng.api_state()
            self.assertTrue(all(x["adopted"] is False for x in st["lights"]))
            for l in sorted(lights, key=lambda l: l.address):
                await eng.api_adopt(l.id, "", "")
            for _ in range(60):
                if all(l.online for l in lights):
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(all(l.online for l in lights))
            self.assertEqual(sorted(l.name for l in lights), ["Chihiros", "Chihiros 2"])
            await asyncio.sleep(0.3)
            for addr in (self.A, self.B):
                self.assertEqual(len(self.frames_for(addr, (0x5A, 0x09))), 1)     # clock set on each
                self.assertEqual(len(self.frames_for(addr, (0xA5, 0x19))), 1)     # program stored in each
                self.assertEqual(self.frames_for(addr)[-1][1][5:7], bytes([0x05, 18]))
            self.assertTrue(all(l.rssi == -60 for l in lights))
            # different programs per light, manual on one only
            la = next(l for l in lights if l.address == self.A)
            lb = next(l for l in lights if l.address == self.B)
            n_b = len(self.frames_for(self.B))
            await eng.api_manual(la.id, {"r": 1, "g": 2, "b": 3, "w": 4}, 30)
            self.assertEqual(la.mode, "manual")
            self.assertEqual(lb.mode, "program")
            self.assertEqual(len(self.frames_for(self.B)), n_b)                    # B untouched
            # drop A: B keeps working, A reconnects by itself
            next(c for c in FAKE.clients if c.device.address == self.A and c.is_connected).drop()
            await eng.api_manual(lb.id, {"r": 9, "g": 9, "b": 9, "w": 9}, 30)
            self.assertEqual(self.frames_for(self.B)[-1][1][7], 9)
            await asyncio.sleep(3)
            self.assertTrue(la.online)
            await eng.shutdown()
            task.cancel()
            for addr in (self.A, self.B):                                         # both handed over
                self.assertEqual(self.frames_for(addr)[-1][1][5:7], bytes([0x05, 18]))
        run(go())

    def test_identify_and_ignore_a_foreign_light(self):
        async def go():
            eng = Engine(self.tmp)
            eng.load()
            task = asyncio.ensure_future(eng.run())
            await asyncio.sleep(1.5)
            la = next(l for l in eng.lights.values() if getattr(l, "address", "") == self.A)
            lb = next(l for l in eng.lights.values() if getattr(l, "address", "") == self.B)
            await eng.api_identify(lb.id)                    # blink so the user can see which one it is
            blinks = self.frames_for(self.B, (0x5A, 0x07))
            self.assertEqual(len(blinks), 24)                  # 3 x (on + off) x 4 channels
            self.assertEqual(self.frames_for(self.B)[-1][1][5:7], bytes([0x05, 18]))  # back to its own program
            self.assertFalse(lb.transport.is_connected)        # not kept: still not ours
            self.assertEqual(self.frames_for(self.B, (0xA5, 0x19)), [])  # no program written
            await eng.api_ignore(lb.id)                      # "not mine"
            self.assertNotIn(lb.id, eng.lights)
            await eng.api_adopt(la.id, "Tank", "Living room")
            await eng.api_scan()
            await asyncio.sleep(1.0)
            self.assertFalse(any(getattr(l, "address", "") == self.B for l in eng.lights.values()))
            await eng.shutdown()
            task.cancel()
            saved = json.loads((self.tmp / "lights.json").read_text())
            self.assertEqual([d["address"] for d in saved["lights"]], [self.A])
            self.assertEqual(saved["ignored"], [self.B])
            self.assertEqual(self.frames_for(self.B, (0xA5, 0x19)), [])  # B was never programmed
        run(go())

    def test_unplugged_light_does_not_block_the_other(self):
        (self.tmp / "lights.json").write_text(json.dumps({"version": 1, "lights": [
            {"id": "chihiros-b", "kind": "chihiros", "name": "Tank B", "room": "Bedroom", "address": self.B},
            {"id": "chihiros-a", "kind": "chihiros", "name": "Tank A", "room": "Living room", "address": self.A}]}))
        del FAKE.devices[self.B]           # B is unplugged
        FAKE.missing_wait = 2.0            # searching for a missing light takes long

        async def go():
            eng = Engine(self.tmp)
            eng.BLE_SCAN_EVERY = 1
            eng.load()
            t0 = asyncio.get_running_loop().time()
            task = asyncio.ensure_future(eng.run())
            la, lb = eng.lights["chihiros-a"], eng.lights["chihiros-b"]
            while not la.online and asyncio.get_running_loop().time() - t0 < 10:
                await asyncio.sleep(0.05)
            self.assertTrue(la.online)
            self.assertLess(asyncio.get_running_loop().time() - t0, 1.0)   # A did not wait for B
            self.assertFalse(lb.online)
            FAKE.devices[self.B] = "DYU1200C3D4"   # B plugged in again
            t1 = asyncio.get_running_loop().time()
            while not lb.online and asyncio.get_running_loop().time() - t1 < 15:
                await asyncio.sleep(0.05)
            self.assertTrue(lb.online)
            print(f"\n    B back online {asyncio.get_running_loop().time() - t1:.1f}s after plug-in; searches for B: {FAKE.finds.count(self.B)}")
            await eng.shutdown()
            task.cancel()
        try:
            run(go())
        finally:
            FAKE.missing_wait = 0.01


class ServerSecurityTests(unittest.TestCase):
    def test_host_names(self):
        from light_bridge.server import host_allowed
        for ok in ("192.168.178.23:8080", "localhost:8080", "127.0.0.1", "my-laptop:8080", "my-laptop.local:8080", "[::1]:8080"):
            self.assertTrue(host_allowed(ok), ok)
        for bad in ("evil.example.com:8080", "attacker.net", ""):
            self.assertFalse(host_allowed(bad), bad)

    def test_requests(self):
        import tempfile
        import urllib.error
        import urllib.request
        from light_bridge.server import start_server
        loop = asyncio.new_event_loop()
        th = threading.Thread(target=loop.run_forever, daemon=True)
        th.start()
        eng = Engine(Path(tempfile.mkdtemp()), demo=True)
        eng.load()
        eng.loop = loop
        srv, port = start_server(eng, 18411, lambda: None)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        base = f"http://127.0.0.1:{port}"

        def post(path, body=b"{}", headers=None):
            req = urllib.request.Request(base + path, data=body, method="POST", headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
            except (urllib.error.URLError, ConnectionError):
                return "closed"      # the server refused before the client had sent everything
        self.assertEqual(post("/api/scan", headers={"Content-Type": "text/plain"}), 415)       # plain form post
        self.assertEqual(post("/api/scan", headers={"Content-Type": "application/json",
                                                     "Origin": "http://evil.example.com"}), 403)
        self.assertEqual(post("/api/scan", headers={"Content-Type": "application/json", "Host": "evil.example.com"}), 403)
        self.assertIn(post("/api/lights/x/manual", b"x" * (600 * 1024), {"Content-Type": "application/json"}), (400, "closed"))
        with urllib.request.urlopen(base + "/api/state", timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        import socket as _s
        with _s.create_connection(("127.0.0.1", port), timeout=5) as c:          # negative length
            c.sendall(b"POST /api/scan HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                      b"Content-Length: -5\r\n\r\n")
            self.assertIn(b" 400 ", c.recv(200))
        loop.call_soon_threadsafe(loop.stop)
        th.join(5)
        loop.close()

    def test_other_devices_need_the_key(self):
        from light_bridge.server import client_allowed
        self.assertTrue(client_allowed("127.0.0.1", None, "k" * 22))
        self.assertTrue(client_allowed("::1", None, "k" * 22))
        self.assertFalse(client_allowed("192.168.1.20", None, "k" * 22))
        self.assertFalse(client_allowed("192.168.1.20", "wrong", "k" * 22))
        self.assertTrue(client_allowed("192.168.1.20", "k" * 22, "k" * 22))
        self.assertFalse(client_allowed("192.168.1.20", "", ""))

    def test_key_over_the_network(self):
        import tempfile
        import urllib.error
        import urllib.request
        from light_bridge.server import load_or_create_key, start_server
        ips = helialux.local_ipv4_addresses()
        if not ips:
            self.skipTest("no network address on this machine")
        loop = asyncio.new_event_loop()
        th = threading.Thread(target=loop.run_forever, daemon=True)
        th.start()
        tmp = Path(tempfile.mkdtemp())
        eng = Engine(tmp, demo=True)
        eng.load()
        eng.loop = loop
        key = eng.access_key = load_or_create_key(eng.data_dir)
        self.assertEqual(key, load_or_create_key(eng.data_dir))               # stays the same
        srv, port = start_server(eng, 18431, lambda: None, key)
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))

        def get(path, headers=None):
            try:
                with urllib.request.urlopen(urllib.request.Request(f"http://{ips[0]}:{port}{path}", headers=headers or {}),
                                            timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        self.assertEqual(get("/")[0], 200)                                   # the page itself loads
        self.assertEqual(get("/api/state")[0], 401)                          # but no data without the key
        code, body = get("/api/state", {"X-Light-Bridge-Key": key})
        self.assertEqual(code, 200)
        self.assertIn(f"?k={key}", json.loads(body)["bridge"]["urls"][0])    # the QR code carries the key
        loop.call_soon_threadsafe(loop.stop)
        th.join(5)
        loop.close()

    def test_only_one_bridge_per_pc(self):
        import tempfile
        from light_bridge.__main__ import _single_instance
        d = tempfile.mkdtemp()
        first = _single_instance(d)
        self.assertIsNotNone(first)
        self.assertIsNone(_single_instance(d))
        first.close()
        again = _single_instance(d)
        self.assertIsNotNone(again)
        again.close()


class EngineSafetyTests(unittest.TestCase):
    def test_one_broken_light_does_not_hide_the_others(self):
        import tempfile
        eng = Engine(Path(tempfile.mkdtemp()), demo=True)
        eng.load()
        lights = [l for l in eng.lights.values() if l.adopted]
        lights[0].state = lambda ts=None: 1 / 0
        st = run(eng.api_state())
        self.assertEqual(len(st["lights"]), len(eng.lights))
        self.assertTrue(any(x.get("broken") for x in st["lights"]))

    def test_restore_sends_the_program_again(self):
        import tempfile
        eng = Engine(Path(tempfile.mkdtemp()), demo=True)
        eng.load()
        l = eng.lights["chihiros-demo-1"]
        l.program_on_light = l.program_hash()
        backup = run(eng.api_backup())
        backup["lights"][0]["profiles"][0]["name"] = "From backup"
        res = run(eng.api_restore(backup))
        self.assertGreaterEqual(res["restored"], 1)
        self.assertEqual(l.profiles[0]["name"], "From backup")
        self.assertIsNone(l.program_on_light)                               # will be stored in the light again
        self.assertEqual(run(eng.api_restore({"lights": ["junk", {"id": "chihiros-demo-2", "kind": "chihiros",
                                                                   "weekdays": "bad"}]}))["restored"], 1)

    def test_damaged_settings_file_is_kept_and_reported(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "lights.json").write_text("{ this is not json")
        eng = Engine(d)
        eng.load()
        self.assertIn("damaged", eng.notice)
        self.assertTrue(list(d.glob("lights.broken-*.json")))
        (d / "lights.json").write_text(json.dumps({"lights": [{"kind": "chihiros"}, {"id": "c", "kind": "chihiros",
                                                                                     "address": "AA"}]}))
        eng2 = Engine(d)
        eng2.load()                                                        # bad entry skipped, good one kept
        self.assertEqual(list(eng2.lights), ["c"])


def run(coro):  # noqa: F811 - close loops to keep the test output clean
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
