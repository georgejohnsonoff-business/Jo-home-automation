"""
app.py — FastAPI front end. Serves the live dashboard, runs the control loop
in a background thread.

    uvicorn climate.app:app --host 0.0.0.0 --port 8000

Loads .env FIRST so Tuya Cloud credentials are present before the loop starts.
"""
from __future__ import annotations
import logging
import threading
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv

load_dotenv()   # read TUYA_ACCESS_ID / TUYA_ACCESS_SECRET from ./.env

from fastapi import FastAPI, Body                 # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

from .loop import Controller, load_config          # noqa: E402
from .nl import translate                          # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="Climate Brain")
controller = Controller(load_config())
WEB = Path(__file__).parent / "web"


@app.on_event("startup")
def _start_loop():
    threading.Thread(target=controller.run_forever, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(WEB / "dashboard.html")


@app.get("/api/state")
def state():
    return JSONResponse(controller.snapshot())


@app.post("/api/tick")
def force_tick():
    controller.tick()
    return controller.snapshot()


# --- activity log ------------------------------------------------------------
@app.get("/api/activity")
def activity(limit: int = 100):
    return {"events": controller.activity(limit)}


# --- manual device control -----------------------------------------------------
@app.post("/api/manual/{device}")
def manual_control(
    device: Literal["fan", "heater", "led", "projector"],
    on: Optional[bool] = Body(None),
    speed: Optional[int] = Body(None),
    delta: Optional[int] = Body(None),
    color: Optional[str] = Body(None),
):
    """Directly set a device from the dashboard. Blocks automation for this
    device for `manual_override_minutes` (config.yaml), then automation
    resumes regardless of mode."""
    ok = controller.set_manual(device, on=on, speed=speed, delta=delta, color=color)
    if not ok:
        return JSONResponse({"ok": False}, status_code=400)
    controller.tick()   # act on this immediately, don't wait for the next poll cycle
    return {"ok": True, "state": controller.snapshot()["devices"][device]}


@app.post("/api/confirm/{device}")
def confirm_state(
    device: Literal["fan", "heater", "led", "projector"],
    on: bool = Body(..., embed=True),
):
    """User reports the REAL, physically-observed state. Corrects our tracked
    belief only — sends no command itself. Use when you've looked at the
    device and it doesn't match what the dashboard shows (IR toggles have no
    feedback channel and can drift after cross-talk, a missed pulse, or a
    restart). Immediately ticks afterward so if the corrected belief now
    disagrees with what automation currently wants, the real corrective
    command fires right away instead of waiting for the next poll."""
    ok = controller.confirm_state(device, on)
    if not ok:
        return JSONResponse({"ok": False}, status_code=400)
    controller.tick()
    return {"ok": True, "state": controller.snapshot()["devices"][device]}


# --- thermostat override -----------------------------------------------------
@app.post("/api/thermostat")
def set_thermostat(target: float = Body(...), hours: float = Body(6.0)):
    """Set a single target temp — fan/heater work toward it instead of the
    normal comfort-band thresholds, for `hours` (default 6), then reverts
    automatically."""
    controller.set_thermostat(target, hours)
    controller.tick()   # apply the new target immediately
    return {"ok": True, "thermostat": controller.snapshot()["thermostat"]}


@app.delete("/api/thermostat")
def clear_thermostat():
    controller.clear_thermostat()
    return {"ok": True}


# --- conversational automations --------------------------------------------
@app.post("/api/rules")
def create_rule(text: str = Body(..., embed=True)):
    """Turn plain English into a rule. App rules go live immediately; Google
    Home rules come back with a script to paste."""
    rule, note = translate(text)
    if rule is None:
        return JSONResponse({"ok": False, "note": note}, status_code=400)
    rule.id = controller.rule_store.next_id()
    controller.rule_store.add(rule)
    return {"ok": True, "note": note, "rule": rule.to_dict()}


@app.post("/api/rules/{rid}/toggle")
def toggle_rule(rid: str, enabled: bool = Body(..., embed=True)):
    controller.rule_store.toggle(rid, enabled)
    return {"ok": True}


@app.post("/api/rules/{rid}/run")
def run_rule(rid: str):
    return {"ok": controller.run_rule(rid)}


@app.delete("/api/rules/{rid}")
def delete_rule(rid: str):
    controller.rule_store.delete(rid)
    return {"ok": True}
