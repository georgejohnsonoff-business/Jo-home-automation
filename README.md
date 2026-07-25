# Climate Brain

A humidity-aware room climate controller. One always-on service reads a
temp/humidity sensor and drives an air cooler, a 5-speed fan, and an IR heater
to hold an optimum "feels-like" temperature — using real evaporative-cooling
physics, not a naive thermostat.

It runs **in simulation mode out of the box** (no hardware, no keys) so you can
see the whole thing work, then you wire in real devices one at a time.

```
climate/
  brain.py        pure decision engine — dew point, heat index, state machine
  devices.py      tinytuya I/O: HomeMate sensor, fan regulator, IR blaster
  cooler.py       the cooler behind one interface (Qubo bridge lives here)
  loop.py         controller: hysteresis, dwell timer, failsafes, runtime cap
  app.py          FastAPI + live phone dashboard
  config.yaml     all device keys + tunables
  test_brain.py   verify the logic before buying anything
  web/dashboard.html
```

## Why these boundaries

- **`brain.py` is pure.** No hardware, fully unit-tested. The logic you can
  verify is separated from the I/O that can flake.
- **The cooler is behind an interface.** Qubo has *no public API*, so its bridge
  is fragile by nature. It's isolated in `cooler.py` — when it breaks, or you
  swap the plug to Tuya, nothing else changes.

## Quick start (simulation — no hardware)

```bash
cd climate-brain
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

pytest climate/test_brain.py -v          # prove the brain
uvicorn climate.app:app --port 8000      # open http://localhost:8000
```

The dashboard runs a synthetic day (cool+humid dawn → hot+dry afternoon) so you
watch the mode change and the actuators respond. `cooler_backend` shows
`SimulatedCooler` and the sensor dot is amber until you add real devices.

## Wiring real devices

### 1. Tuya devices (HomeMate sensor, fan regulator, IR blaster)

Pull the local keys **once**:

```bash
python -m tinytuya wizard      # links your Smart Life account, writes devices.json
```

Copy each `id` + `key` (+ LAN `ip` from `python -m tinytuya scan`) into
`config.yaml`. DP indices (`dp_temp`, `dp_speed`, …) vary by model — read them
off `python -m tinytuya scan` while you toggle the device, and adjust.

**Learned IR codes.** Put the blaster in study mode and capture each remote key
(heater on/off, LED, projector), then paste the code strings into
`config.yaml → tuya.ir.codes`. The blaster fires them exactly as Smart Life does
— which is precisely what Google Home can't do.

### 2. The cooler (Qubo) — pick a bridge in `config.yaml → cooler.backend`

- **`webhook` (recommended, robust).** Set `on_url` / `off_url` to anything that
  toggles the Qubo plug: an IFTTT webhook applet, a Home Assistant webhook, an
  Alexa routine trigger. Survives Qubo app updates because you only depend on the
  relay, not Qubo's private API.
- **`qubo_cloud` (direct, fragile).** Reconstruct Qubo's private API from your own
  app traffic: run **mitmproxy** / HTTP Toolkit on your phone, open the Qubo app,
  toggle the plug, and read the login + command requests. Fill `base_url`,
  `login_path`, `command_path`, and match the payloads in `cooler.py` (marked
  `TODO`). Expect to re-capture after app updates.
- **`tuya`.** If you ever swap the cooler onto a Tuya 16A plug, just fill the
  `tuya` block — cleanest of all, fully local.

## Run it for real (Raspberry Pi, always on)

```bash
uvicorn climate.app:app --host 0.0.0.0 --port 8000
```

`systemd` unit so it survives reboots — `/etc/systemd/system/climate.service`:

```ini
[Unit]
Description=Climate Brain
After=network-online.target

[Service]
WorkingDirectory=/home/pi/climate-brain
ExecStart=/home/pi/climate-brain/.venv/bin/uvicorn climate.app:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now climate
```

Open `http://<pi-ip>:8000` on your phone → **Add to Home Screen** for a PWA.

## Tuning

All thresholds live in `config.yaml → thresholds` and map 1:1 to
`brain.Thresholds`. Watch the room for a few days and nudge:
`cooler_on_hi/off_hi` (comfort band + hysteresis), `spread_muggy` (how humid
before the cooler is pointless), `min_dwell_seconds` (how twitchy it is).

## Roadmap

- PID duty-cycling instead of on/off cooler
- forecast pre-cool (met.no) before the afternoon peak
- occupancy gating (Tuya mmWave sensor)
- scenes (movie / coffee / night) + Google/Alexa voice bridge
