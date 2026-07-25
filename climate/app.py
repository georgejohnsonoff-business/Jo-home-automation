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


# --- conversational automations --------------------------------------------
@app.post("/api/rules")
def create_rule(text: str = Body(..., embed=True)):
    """Turn plain English into a rule. App rules go live immediately; Google
    Home rules come back with a script to paste."""
    rule, note = translate(text)
    if rule is None:
        return JSONResponse({"ok": False, "note": note}, status_code=400)
    rule.id = controller.store.next_id()
    controller.store.add(rule)
    return {"ok": True, "note": note, "rule": rule.to_dict()}


@app.post("/api/rules/{rid}/toggle")
def toggle_rule(rid: str, enabled: bool = Body(..., embed=True)):
    controller.store.toggle(rid, enabled)
    return {"ok": True}


@app.post("/api/rules/{rid}/run")
def run_rule(rid: str):
    return {"ok": controller.run_rule(rid)}


@app.delete("/api/rules/{rid}")
def delete_rule(rid: str):
    controller.store.delete(rid)
    return {"ok": True}
