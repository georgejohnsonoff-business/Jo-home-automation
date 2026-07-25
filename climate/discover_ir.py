"""
discover_ir.py — one-off helper to list the learned IR keys for the heater
(and other DIY remotes), so you can fill config.yaml heater.code_on/off.

    python -m climate.discover_ir

Needs a working .env (TUYA_ACCESS_ID / TUYA_ACCESS_SECRET).
"""
from dotenv import load_dotenv
load_dotenv()

from .cloud import TuyaCloud
from .loop import load_config


def main():
    cfg = load_config()
    cloud = TuyaCloud()
    if not cloud.ready:
        print("Tuya Cloud not configured — add TUYA_ACCESS_ID/SECRET to .env")
        return

    ir_hub = cfg["devices"]["ir_hub"]["id"]
    for name in ("heater", "led", "projector"):
        remote = cfg["devices"][name]["remote_id"]
        print(f"\n=== {name}  (remote {remote}) ===")
        keys = cloud.ir_learned_keys(ir_hub, remote)
        if not keys:
            print("  (no learned keys returned — is it a DIY/learned remote?)")
        for k in keys:
            # shape varies; print whatever identifies each learned button
            print("  ", k)


if __name__ == "__main__":
    main()
