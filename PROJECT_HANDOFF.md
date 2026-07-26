# Climate Brain — Project Handoff / Resume Notes

This file exists so a future session (yours or an AI assistant's) can pick this project back up with full context. It does not change any app behavior — it's documentation only.

## What this is

An Android app ("JO's Home Automation") that embeds a full Python backend (FastAPI + Chaquopy) and runs it as a foreground service on George's daily-driver phone. It senses room temperature/humidity, decides fan/heater/LED/projector state every 10 seconds, actuates via Tuya Cloud + IR hub, self-learns, and displays (but does not control) a Google-Home-owned cooler.

Two Python trees exist and MUST be kept in sync after every change:
- Desktop: `/Volumes/CAP WORK/CURSOR DATA/climate-brain/climate/` (this repo, has the GitHub remote)
- Android: `/Volumes/CAP WORK/CURSOR DATA/climate-brain-android/app/src/main/python/climate/` (separate folder, no git remote of its own — it's the Chaquopy source tree bundled into the APK at build time)

**The one permanent, intentional divergence**: `loop.py`'s `load_config()`. Desktop defaults to a cwd-relative path (`"climate/config.yaml"`); Android resolves the path relative to `__file__` because Chaquopy's filesystem layout doesn't have a stable cwd. Never "fix" this to match — it's correct as-is on both sides.

Every other file (`app.py`, `devices.py`, `learn.py`, `loop.py` minus that one function, `nl.py`, `rules.py`, `config.yaml`, `web/dashboard.html`, etc.) should be byte-identical between the two trees. When resuming work, diff them first to confirm nothing drifted.

**Rebuilding**: Any Python-only change requires a full Android Studio rebuild + reinstall — Chaquopy bundles the Python source into the APK at build time, it does not hot-reload.

## Devices and how each is controlled

- **Sensor** — Tuya Cloud, temp (÷10) + humidity, read every `sensor_refresh_seconds`.
- **Fan** — Tuya Cloud, genuine bidirectional status (real speed readback). Controlled via `set_speed()`; drift-corrected via `sync_from_cloud()` every `fan_sync_seconds`.
- **Heater** ("Handy heater") — IR-only via the Tuya IR hub, learned remote codes. Real quirks that matter:
  - Power is a single toggle pulse; turning ON resets the physical unit to 25°C/HIGH-fan/timer-off — a hardware default, not ours to choose. Tracked state resyncs to that on every ON.
  - Temp is a real thermostat (15–32°C), one IR pulse per 1° via increase/decrease — we just tell it a setpoint, its own hardware does fine control.
  - Fan is a strict 2-state HIGH/LOW cycle, one pulse always flips it.
  - Timer is a 13-state one-directional cycle (0 off .. 12h); not used by automation, wired in for a possible future manual control only.
- **LED strip** — IR-only, 24-key learned remote (on/off/color/mode/brightness). Boots into a flash-demo mode after any power loss regardless of prior state — always force-resent during outage recovery, with a repeated color-fix every 4 min for 30 min (a single resend isn't reliable enough, confirmed live).
- **Projector** — IR-only, single power-toggle button. No feedback channel at all — riskiest device, tracked state must never drift or a toggle can fire backwards.
- **Cooler** — NOT controlled by this app. Owned by a separate Google Home automation script (Tuya has no API for it). This app only *infers and displays* whether the cooler is probably on, by replaying that GH script's own logic in `learn.py`'s `infer_cooler_state()`. See "Cooler display logic" below — keep this in sync with the real GH script whenever that script changes.

## Config (`climate/config.yaml`) — key sections

- `thresholds:` — comfort/fan-ramp/heater on-off temps. `heater_on_t`/`heater_off_t` are deliberately excluded from autotune's bounds (see Known Issues Fixed below) — never let autotune touch them again.
- `night:` — 23:00–06:00, fan cap 3.
- `cooldown:` — Mon–Thu 11:00–17:30 ("away at office"), rests fan/heater/LED to off regardless of climate.
- `fan_floor:` — Fri–Sun 10:30–19:15, minimum fan speed 3 (added on request; normal thresholds can still ramp it higher). Weekday convention throughout this file: `0=Monday ... 6=Sunday`.
- `learning:` — escalation after 20 min of an underperforming combo; autotune once/day, bounded step, needs 20+ samples.
- Tuya API budget block — `sensor_refresh_seconds`, `fan_sync_seconds`, `outage_check_seconds`, all currently 300s (5 min) each, landing at ~25,900 calls/month, just under Tuya's free-tier ~26,000/month cap. **Do not shrink these without recalculating the monthly total** — a previous per-tick (10s) cadence blew the budget by ~40x and got the fan unlinked/permission-denied repeatedly.

## Known issues fixed this build (don't reintroduce)

1. **Thermostat "Set" no-op** — input had no real `value`, just a placeholder; `parseFloat("")` → `NaN`, silently swallowed. Fixed with a real default value.
2. **Fan slider not moving the real device** — two stacked bugs: (a) the periodic dashboard poll rebuilt the slider DOM mid-drag, killing the pending event; (b) Android WebView doesn't reliably fire `change` from a touch release. Fixed via `pointerdown`/`pointerup` handlers and skipping slider re-render mid-drag.
3. **Device-state desync on command failure** — `Fan.set_speed()`, `ToggleIR.set()` (LED/projector), `HandyHeater.set()` all now return `bool` and only update their tracked/cached state on a *confirmed* successful command, never optimistically.
4. **Pydantic v1/v2 crash in `nl.py`, Android-only** — Android is pinned to `pydantic<2` (Chaquopy can't cross-compile v2's Rust core); desktop was on v2. Fixed by using `.parse_obj()` (works on both) instead of `.model_validate()` (v2-only), and old-style `@validator` instead of `@field_validator`.
5. **`nl.py` automation validation** — added per-device command whitelisting and `when`-expression range/syntax validation, plus multi-rule output (one natural-language request can now produce two separate rules with different trigger shapes).
6. **Manual-action UI lag** — dashboard buttons were blocking on a full synchronous `tick()` (a real Tuya network round-trip) before responding. Fixed via `tick_async()` + a `threading.Lock()` shared with the periodic loop.
7. **Self-learning autotune was silently ratcheting `heater_on_t` upward** toward a "too warm to need heat" threshold via a flawed heuristic. Fixed by excluding those two keys from autotune's bounds entirely and hard-pinning them from `config.yaml`.
8. **WebView caching stale dashboard.html after rebuild** — fixed via `Cache-Control: no-store` response header + `WebSettings.LOAD_NO_CACHE`.
9. **Outage detection — the big one, two rounds:**
   - Round 1: originally polled Tuya's `status()` on the fan as the reachability signal, gated on a slow interval to save API quota. This missed a real, physically-tested MCB power-cycle entirely, and separately had a bootstrap bug where restarting mid-outage silently adopted "offline" as the assumed normal baseline with no alert.
   - Round 2 (the actual root cause): confirmed via a live test — while the power was still physically off, Logcat showed Tuya's `/status` endpoint returning `success: true` with the last-cached reading for 20+ continuous minutes. **`status()` does not reflect real device reachability at all** — it's a cached-value read. The only real signal is `TuyaCloud.device_online()`, a separate endpoint. Outage detection now runs its own `outage_check_seconds` timer calling `device_online()`; `sync_from_cloud()`/`status()` is drift-correction only and must never again be used as an outage signal.
10. **Cooler display logic outdated** — `infer_cooler_state()` was replaying an old, simple three-rule GH script. Replaced with the real, current 10-rule "Cooler - Conflict Free" script's zone-based logic (Mon-Thu evening override, weekend-sensitive window, daytime dry/muggy bands, night mode, global swamp-safety cutoff that's disabled during the evening override). This is DISPLAY ONLY — the app never actuates the cooler. If the real GH script changes again, `infer_cooler_state()` in `learn.py` needs a matching update (verified against 12 hand-checked cases last time — see git history around this file for the test snippet if useful).

## Verification method used throughout (per explicit instruction)

At George's direction, **no live testing against real Tuya devices from the desktop** — all fixes were verified via: static syntax checks (`python3 -c "import ast; ast.parse(...)"`), isolated pure-Python logic simulations with a fake clock (no real `time.sleep`, no real devices), and George's own live phone testing (physical MCB power-cycles, Logcat pulls, manual dashboard operation) reported back for root-causing. Keep following this pattern unless George says otherwise.

## Pending / open items as of last session

- Confirm the new `fan_floor` window (Fri-Sun 10:30–19:15, min speed 3) behaves as expected once observed live for a full window.
- Confirm outage detection + recovery now actually fires correctly on a real MCB test with the `device_online()`-based fix (last known state: fix implemented and synced to both trees, not yet reconfirmed live after the `status()` root-cause fix).
- Self-learning escalation/autotune events were empty in the dashboard's "learning only" filter as of last check — expected (needs 20+ min stable combo, or 20+ samples for autotune), not a bug, but worth eyeballing again after a full day of real runtime.
- If the real Google Home cooler script changes again, `infer_cooler_state()` needs a matching manual update — it is not read from Google Home automatically, there is no API for that.

## Where things live

- Repo root (this file's location): `climate/` package — `app.py` (FastAPI routes), `loop.py` (Controller — the core tick loop), `devices.py` (Fan/HandyHeater/LED/Projector/Sensor classes), `brain.py` (pure decision function `decide()` + `Thresholds`), `cloud.py` (Tuya API wrapper — `status()` vs `device_online()` distinction lives here), `learn.py` (self-learning + cooler-state inference), `nl.py` (Groq-based natural-language rule creator), `rules.py` (rule storage/firing), `store.py` (SQLite-backed event/reading log + KV store), `web/dashboard.html` (the single-page dashboard UI, served locally and viewed via the app's WebView).
- Android-only native files (no desktop equivalent): `MainActivity.kt`, `ClimateBrainService.kt`, `WatchdogReceiver.kt`, `AndroidManifest.xml`, `build.gradle`, `strings.xml`, `activity_main.xml` — under `climate-brain-android/app/src/main/...`.
