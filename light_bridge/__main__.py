"""Start the Light Bridge:  python -m light_bridge  [--demo] [--port 8080]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
import webbrowser
from pathlib import Path

from .engine import VERSION, Engine, default_data_dir
from .helialux import local_ipv4_addresses
from .server import load_or_create_key, start_server


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="light_bridge", description="One interface for Chihiros and HeliaLux lights.")
    ap.add_argument("--demo", action="store_true", help="run with simulated lights (no hardware needed)")
    ap.add_argument("--demo-helialux", action="store_true", help="demo: also simulate a HeliaLux")
    ap.add_argument("--port", type=int, default=8080, help="web port (default 8080)")
    ap.add_argument("--open-browser", action="store_true", help="open the interface in the browser after start")
    ap.add_argument("--data-dir", type=Path, default=None, help="where settings are stored")
    ap.add_argument("--verbose", action="store_true", help="show technical details in the window")
    return ap.parse_args(argv)


async def amain(args):
    engine = Engine(args.data_dir or default_data_dir(), demo=args.demo or args.demo_helialux, port=args.port,
                    demo_helialux=args.demo_helialux)
    lock = _single_instance(engine.data_dir)
    if lock is None:
        print("Light Bridge is already running on this PC. Opening it in the browser…", flush=True)
        webbrowser.open(f"http://localhost:{args.port}")
        return
    _log_to_file(engine.data_dir)
    engine.load()
    loop = asyncio.get_running_loop()
    engine.loop = loop
    stop = asyncio.Event()

    def request_stop(*_):
        loop.call_soon_threadsafe(stop.set)

    if hasattr(signal, "SIGBREAK"):          # Windows: Ctrl+Break
        signal.signal(signal.SIGBREAK, request_stop)
    try:                                     # macOS / Linux: "kill", logout, systemd stop
        loop.add_signal_handler(signal.SIGTERM, request_stop)
    except (NotImplementedError, RuntimeError, AttributeError):
        pass
    _install_windows_close_handler(request_stop)
    _install_windows_sleep_handler(engine, loop)

    key = engine.access_key = load_or_create_key(engine.data_dir)
    server, port = start_server(engine, args.port, request_stop, key)
    engine.port = port
    urls = [f"http://{ip}:{port}/?k={key}" for ip in local_ipv4_addresses()]
    line = "=" * 64
    print(line)
    print(f"  Light Bridge {VERSION} is running{'  (DEMO - simulated lights)' if args.demo else ''}")
    print(f"  On this computer:  http://localhost:{port}")
    for u in urls:
        print(f"  On your phone:     {u}   (same Wi-Fi)")
    print("  Keep this window open. To stop: use 'Stop bridge' in the app,")
    print("  close this window, or press Ctrl+C.")
    print(line, flush=True)
    if args.open_browser:
        loop.call_later(1.0, webbrowser.open, f"http://localhost:{port}")

    runner = asyncio.ensure_future(engine.run())
    waiter = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if runner.done() and runner.exception():
            raise runner.exception()
    finally:
        print("Stopping… handing the lights back to their own programs.", flush=True)
        await engine.shutdown()
        runner.cancel()
        server.shutdown()
        server.server_close()
        print("Light Bridge stopped. You can close this window.", flush=True)


_win_handler = None
_win_sleep = None


def _single_instance(folder):
    """Only one bridge per PC: two would fight over the Bluetooth link and the settings file.
    Returns an open lock file (keep it open while running) or None if another bridge runs."""
    f = open(Path(folder) / "bridge.lock", "a+")  # noqa: SIM115 - kept open on purpose
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def _log_to_file(folder):
    """Keep a small log file next to the settings (helps when something needs to be checked)."""
    from logging.handlers import RotatingFileHandler
    try:
        h = RotatingFileHandler(Path(folder) / "light-bridge.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        h.setLevel(logging.INFO)
        logging.getLogger().addHandler(h)
    except OSError:
        pass


def _install_windows_sleep_handler(engine, loop):
    """Windows: just before the PC goes to sleep (laptop lid closed), make sure every
    Chihiros runs its own program. Matters only if a manual light was active. Best effort."""
    global _win_sleep
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        callback_type = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p)

        class Params(ctypes.Structure):
            _fields_ = [("Callback", callback_type), ("Context", ctypes.c_void_p)]

        PBT_APMSUSPEND = 4

        def on_power(_ctx, kind, _setting):
            if kind == PBT_APMSUSPEND:
                try:
                    asyncio.run_coroutine_threadsafe(engine.handover_all(), loop).result(timeout=1.8)
                except Exception:  # noqa: BLE001
                    pass
            return 0

        cb = callback_type(on_power)
        params = Params(cb, None)
        handle = ctypes.c_void_p()
        DEVICE_NOTIFY_CALLBACK = 2
        res = ctypes.windll.powrprof.PowerRegisterSuspendResumeNotification(
            DEVICE_NOTIFY_CALLBACK, ctypes.byref(params), ctypes.byref(handle))
        if res == 0:
            _win_sleep = (cb, params, handle)          # keep references alive
    except Exception:  # noqa: BLE001
        pass


def _install_windows_close_handler(request_stop):
    """Windows: closing the console window, logging off or shutting down gives us ~5 s.
    Used to switch every Chihiros back to the program stored in it."""
    global _win_handler
    if os.name != "nt":
        return
    try:
        import ctypes
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def handler(ctrl_type):
            if ctrl_type in (0, 1, 2, 5, 6):   # Ctrl+C, Ctrl+Break, close, logoff, shutdown
                request_stop()
                if ctrl_type in (2, 5, 6):
                    time.sleep(4.5)            # keep the process alive while the loop hands over
                return True
            return False

        _win_handler = handler_type(handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_win_handler, True)
    except Exception:  # noqa: BLE001 - best effort only
        pass


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    if not args.verbose:
        logging.getLogger("bleak").setLevel(logging.WARNING)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
