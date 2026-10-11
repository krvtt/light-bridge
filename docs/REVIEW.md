# Review notes — Light Bridge 0.11

Scope: is the solution consistent, and safe for people without technical knowledge, given that
the laptop is used as a normal laptop (taken to work, lid closed, walked between the two rooms)
and that **a light must keep working in its intended mode after any unexpected disconnect**?

Method: two passes. (1) own review against the scenarios below, (2) an independent adversarial
review of the code and tests. Every finding was fixed and covered by a test, or is listed as an
open limit. All tests run without hardware (`python -m unittest discover -s tests`).

## Scenarios checked

| Scenario | Result |
|---|---|
| Laptop leaves while the light runs its program | Light keeps running its stored program. |
| Laptop leaves during manual / maintenance light | Light keeps the manual colours until the laptop is back; the app says *still in manual light*. Windows sleep hook tries to switch back first (best effort). |
| Link drops in the middle of a program write | App shows *program only partly stored*; repaired at once on the next contact. Tested by cutting the link after every single frame. |
| Walking past a light at the edge of range | No program write is started unless the link has been up for 8 s. |
| Lid closed / Windows sleep / console closed | Only the short "run your program" command is sent, for all lights at the same time. Never a write that erases first. |
| Crash or power loss of the laptop during a write or test | The unfinished state is saved to disk before the write starts and repaired on the next start. |
| Two identical lights | Separate state, lock and encoder per light; one unplugged light does not block the other; radio access is shared fairly. |
| A neighbour's Chihiros nearby | Listed as *new light nearby*; nothing is stored in it unless the user confirms. *Flash* only blinks it and then switches it to its own stored program. |
| Laptop in an office network | Other devices need the access key; no automatic network scans. |
| Summer/winter time | Clock is set again after the change; the curve test is timed from "now". |
| Damaged settings file, bad backup file | Kept as a copy and reported; bad entries are skipped; restore asks first and sends the programs again. |
| Double start (autostart + double-click) | Second copy opens the browser on the running one and exits. |

## Findings and status

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Bridge drove the light minute by minute: when the laptop left, the light froze | high | fixed — lamp-first redesign |
| 2 | Simple version could be "on all day" (moonlight, light over midnight) | high | fixed — brightness by sum of colours, circular day, cut at midnight with a note |
| 3 | Program writes (which erase first) could start at the edge of range or before sleep | high | fixed — steady-link rule, handover never writes, auto variant tested first |
| 4 | App said "runs its own program" while a light was stuck in manual light | high | fixed — *left in* state shown and saved; "keep the PC near" hint |
| 5 | Curve test / write leftovers not saved before a crash | medium | fixed — saved before the write starts |
| 6 | Restore marked old programs as "already in the light" | medium | fixed — programs are sent again; confirmation |
| 7 | Flash text promised "nothing is changed" | medium | fixed — text corrected; one retry to switch the light back |
| 8 | API open to every device in the network | medium | fixed — access key via QR code; frame and body checks |
| 9 | Two bridges could run at the same time | medium | fixed — lock file; no port sharing on Windows |
| 10 | Handover ran light by light (too slow before sleep) | medium | fixed — all lights at the same time |
| 11 | Curve test result changed the weekday plan differently per light | medium | fixed — each light keeps its program of today; option off by default |
| 12 | Tests checked frames, not what the light does | medium | fixed — light model in the tests |
| 13 | Summer/winter time: curve test timing and clock | low | fixed |
| 14 | One broken light could hide all lights in the app | low | fixed |
| 15 | Settings file robustness (fsync, damaged file) | low | fixed |
| 16 | HeliaLux auto-search in foreign networks | low | fixed — only on request |
| 17 | Unsaved edits lost when leaving the editor | low | fixed — asks first |
| 18 | Setup ages badly (unpinned library, Python update) | low | fixed — `bleak<4`, self-check in the start script |

## Open limits (not solvable in software alone, or need the real lights)

1. **Tested with simulated lights only.** The protocol is community-documented; confirm on the two
   real lights first (see the test plan in the guide).
2. **Full curves unverified** on the Universal WRGB. Without them: one colour mix per day.
3. **Fade semantics** of the light's own program (does the fade end at the "off" time or start there?)
   are assumed; check in the field test.
4. **Manual light needs the laptop to end it.** Only a light-side timer would avoid this; no
   documented command is known.
5. **Clock after a power cut** while the laptop is away stays wrong until the laptop is back.
6. **Windows parts untested** on real Windows: start scripts, close hook, sleep hook
   (Modern Standby laptops may not report sleep in time).
7. **Bluetooth range** in the real flat; the laptop must be near each light now and then.
8. **HeliaLux** is beta (unofficial interface).

## Disclaimer

Light Bridge is a private hobby project, provided "as is" without warranty (MIT licence). It uses
unofficial, community-documented interfaces of Chihiros and Juwel products; firmware updates may
change them. Using it may affect the light program of your aquarium — check the tank after changes
and keep a backup. Not affiliated with, endorsed or supported by Chihiros or Juwel.
