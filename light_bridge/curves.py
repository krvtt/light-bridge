"""Day programs ("profiles"): data model, interpolation, templates.

A profile is a list of phases in the HeliaLux style: each phase has a target
time (minutes after midnight) and the levels reached at that time. The first
phase always sits at 00:00. Between phases the light fades linearly; after the
last phase it fades towards the 00:00 phase of the next day.
"""
from __future__ import annotations

import copy

ALL_CHANNELS = ["w", "r", "g", "b"]
CHANNEL_NAMES = {"w": "White", "r": "Red", "g": "Green", "b": "Blue"}
MAX_SLOTS = 8


def clamp(v, lo=0, hi=100):
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def blank_levels(channels):
    return {c: 0 for c in channels}


def convert_levels(levels: dict, channels: list) -> dict:
    """Map levels onto another light's channels (missing channels become 0)."""
    return {c: clamp(levels.get(c, 0)) for c in channels}


def normalize_points(points, channels, max_points=None):
    """Sort, clamp, de-duplicate times and make sure a phase exists at 00:00."""
    out = []
    seen = set()
    for p in sorted(points or [], key=lambda x: clamp(x.get("t", 0), 0, 1439)):
        t = clamp(p.get("t", 0), 0, 1439)
        if t in seen:
            continue
        seen.add(t)
        out.append({
            "t": t,
            "v": convert_levels(p.get("v", {}), channels),
            "ci": clamp(p.get("ci", 0), 0, 3),
            "cm": clamp(p.get("cm", 0), 0, 3),
        })
    if not out:
        out = [{"t": 0, "v": blank_levels(channels), "ci": 0, "cm": 0}]
    if out[0]["t"] != 0:
        start = sample(out, 0, channels)
        out.insert(0, {"t": 0, "v": start, "ci": 0, "cm": 0})
    if max_points:
        out = out[:max_points]
    return out


def sample(points, minute, channels):
    """Levels at a minute of the day (linear fade, wrapping over midnight)."""
    pts = sorted(points, key=lambda p: p["t"])
    if not pts:
        return blank_levels(channels)
    if len(pts) == 1:
        return convert_levels(pts[0]["v"], channels)
    minute = minute % 1440
    prev = nxt = None
    for p in pts:
        if p["t"] <= minute:
            prev = p
        else:
            nxt = p
            break
    if prev is None:
        prev = pts[-1]
    if nxt is None:
        nxt = pts[0]
    span = (nxt["t"] - prev["t"]) % 1440 or 1440
    pos = (minute - prev["t"]) % 1440
    f = pos / span
    return {c: int(round(prev["v"].get(c, 0) + (nxt["v"].get(c, 0) - prev["v"].get(c, 0)) * f)) for c in channels}


def next_phase(points, minute):
    """Return the time of the next phase after `minute` (or None)."""
    times = sorted(p["t"] for p in points)
    for t in times:
        if t > minute:
            return t
    return times[0] if times else None


def derive_basic_schedule(points, channels):
    """Reduce a day program to the Chihiros built-in program (one bright block).

    "Bright" is measured as the sum of all colours, relative to the brightest moment,
    so a dim moonlight does not count as daylight. The day is treated as a circle;
    the light's own program cannot run over midnight, so a bright time that crosses
    midnight is cut there (the longer part is kept, `cut_at_midnight` is set).
    Returns dict(on, off, ramp, levels, cut_at_midnight) or None if always dark.
    """
    step = 5
    samples = [(m, sample(points, m, channels)) for m in range(0, 1440, step)]
    total = [sum(v.values()) for _, v in samples]
    peak = max(total, default=0)
    if peak <= 0:
        return None
    lit = [t >= peak * 0.25 for t in total]
    n = len(lit)
    cut = False
    if all(lit):
        first, last = 0, n - 1
    else:
        dark = lit.index(False)
        best_len, best_start, run_start, run_len = 0, 0, None, 0
        for k in range(1, n + 1):                      # walk once around the circle
            i = (dark + k) % n
            if lit[i]:
                if run_start is None:
                    run_start, run_len = i, 0
                run_len += 1
                if run_len > best_len:
                    best_len, best_start = run_len, run_start
            else:
                run_start = None
        first, last = best_start, (best_start + best_len - 1) % n
        if best_start + best_len > n:                  # crosses midnight: keep the longer part
            cut = True
            before, after = n - best_start, best_len - (n - best_start)
            first, last = (best_start, n - 1) if before >= after else (0, last)
    on, off = first * step, min(1435, last * step + step)
    window = [(m, t) for (m, _), t in zip(samples, total) if on <= m < off] or [(on, peak)]
    top = max(t for _, t in window)
    levels = next(v for (m, v), t in zip(samples, total) if on <= m < off and t == top) if top else samples[0][1]
    reach = next((m for m, t in window if t >= top * 0.9), on)
    ramp = max(0, min(150, reach - on, (off - on) // 2))
    return {"on": on, "off": off, "ramp": ramp, "levels": levels, "cut_at_midnight": cut}


def simple_points(block, channels):
    """Points that approximate the light's own simple program (on, fade, off) for display."""
    on, off, ramp = block["on"], block["off"], max(1, block["ramp"])
    lv = convert_levels(block["levels"], channels)
    full = min(on + ramp, off - 1)
    dim = max(off - ramp, full + 1)
    pts = [{"t": 0, "v": blank_levels(channels)}, {"t": on, "v": blank_levels(channels)},
           {"t": full, "v": lv}, {"t": min(dim, off - 1), "v": lv}, {"t": off, "v": blank_levels(channels)}]
    return normalize_points(pts, channels)


# ---------------------------------------------------------------- templates
# Levels are given for all four colours; two-colour lights use white and blue.
def _p(t, w=0, r=0, g=0, b=0, ci=0, cm=0):
    h, m = map(int, t.split(":"))
    return {"t": h * 60 + m, "v": {"w": w, "r": r, "g": g, "b": b}, "ci": ci, "cm": cm}


TEMPLATES = [
    {
        "id": "community",
        "name": "Community tank",
        "description": "Gentle sunrise, midday break, warm sunset, moonlight.",
        "points": [
            _p("00:00", b=3), _p("01:00"), _p("08:30"),
            _p("10:00", w=70, r=55, g=45, b=50), _p("13:00", w=70, r=55, g=45, b=50),
            _p("13:30", w=20, r=15, g=10, b=15), _p("15:30", w=20, r=15, g=10, b=15),
            _p("16:00", w=70, r=55, g=45, b=50), _p("20:00", w=70, r=55, g=45, b=50),
            _p("21:00", w=10, r=40, g=10, b=5), _p("21:45", b=6),
        ],
    },
    {
        "id": "community-nobreak",
        "name": "Community, no break",
        "description": "Long sunrise, full light all day, one-hour sunset.",
        "points": [
            _p("00:00", b=3), _p("01:00"), _p("09:00"),
            _p("11:00", w=75, r=55, g=50, b=55), _p("19:00", w=75, r=55, g=50, b=55),
            _p("20:00", w=10, r=35, g=10, b=5), _p("20:30", b=6),
        ],
    },
    {
        "id": "aquascape",
        "name": "Planted aquascape",
        "description": "Strong light for plants, 11 hours, one-hour fades.",
        "points": [
            _p("00:00"), _p("08:00"), _p("09:00", w=100, r=85, g=80, b=70),
            _p("19:00", w=100, r=85, g=80, b=70), _p("20:00"),
        ],
    },
    {
        "id": "rift-lake",
        "name": "Rift lake (bluish)",
        "description": "Lower intensity, more blue, quicker sunrise and sunset.",
        "points": [
            _p("00:00", b=4), _p("00:30"), _p("09:30"),
            _p("10:00", w=45, r=20, g=30, b=80), _p("19:30", w=45, r=20, g=30, b=80),
            _p("20:00", b=10), _p("21:00", b=4),
        ],
    },
    {
        "id": "amazon",
        "name": "Amazon, cloudy",
        "description": "Warm light with passing clouds at midday (clouds on HeliaLux only).",
        "points": [
            _p("00:00"), _p("09:00"), _p("09:45", w=55, r=70, g=40, b=25),
            _p("12:00", w=70, r=80, g=50, b=30, ci=2, cm=1), _p("15:00", w=70, r=80, g=50, b=30, ci=2, cm=1),
            _p("18:30", w=55, r=70, g=40, b=25), _p("19:30", w=5, r=30, g=5),
            _p("20:00"),
        ],
    },
    {
        "id": "simple",
        "name": "Simple day",
        "description": "On at 10:00, off at 20:00, half-hour fades.",
        "points": [_p("00:00"), _p("09:30"), _p("10:00", w=70, r=50, g=50, b=50),
                   _p("19:30", w=70, r=50, g=50, b=50), _p("20:00")],
    },
]


def template_profile(template_id, channels):
    tpl = next((t for t in TEMPLATES if t["id"] == template_id), TEMPLATES[0])
    return {"name": tpl["name"], "points": normalize_points(copy.deepcopy(tpl["points"]), channels)}


def default_profiles(channels):
    first = template_profile("community", channels)
    return [first] + [
        {"name": f"Program {i + 1}", "points": normalize_points([], channels)} for i in range(1, MAX_SLOTS)
    ]
