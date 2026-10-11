# Changelog

## 0.11.0 — 2026-10-11

Focus: two identical Chihiros lights in two rooms, and a laptop that is used as a normal laptop.

### Changed
- **Lamp-first:** the day program is stored in each light and runs there on its own. The laptop
  only writes changes, sets the clock and handles temporary manual light.
- Default test order for full curves: the "auto" variant first (it needs no full rewrite later).
- The simple version ignores dim moonlight and is cut at midnight instead of running all night.
- HeliaLux is optional (beta); no automatic network scans any more.

### Added
- New lights found nearby must be confirmed ("This is mine"); *Flash* and *Not mine*.
- Access key for every device except the PC itself (carried by the QR code).
- The app shows when a light was left in manual light, in a test or with an unfinished program.
- Program writes only start on a steady link; handover before sleep/shutdown never erases.
- One bridge per PC; settings saved with fsync; damaged settings file is kept and reported.
- Clock re-set after summer/winter time changes.
- Confirmation before discarding unsaved edits and before restoring a backup.
- Tests with a model of the light (including "laptop leaves after every frame").

## 0.9.0 — 2026-10-10
- First prototype: shared web app for Chihiros (Bluetooth) and HeliaLux (Wi-Fi), templates,
  phase editor with live preview, iPhone via QR code, Windows start scripts.
