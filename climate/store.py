"""
store.py — SQLite persistence. Backs three things:
  1. Activity log (readings + events) — survives restarts, unlike the old deque.
  2. Learned actuator-effectiveness rates (for self-tuning).
  3. Auto-tuned threshold overrides (persisted so a restart doesn't lose them).

One file, climate/climate.db, created on first run.
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts REAL, temp REAL, rh REAL, dew_point REAL, spread REAL, heat_index REAL,
    mode TEXT, fan INTEGER, heater INTEGER, cooler_inferred INTEGER
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS events (
    ts REAL, type TEXT, message TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS learned_rates (
    combo TEXT PRIMARY KEY,   -- e.g. "fan3_heaterF_coolerT"
    temp_slope REAL,          -- °C per minute (negative = cooling), EMA
    rh_slope REAL,
    samples INTEGER
);

CREATE TABLE IF NOT EXISTS threshold_overrides (
    name TEXT PRIMARY KEY,
    value REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path="climate/climate.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- readings -------------------------------------------------------------
    def log_reading(self, temp, rh, dew_point, spread, heat_index, mode,
                    fan, heater, cooler_inferred):
        self.conn.execute(
            "INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), temp, rh, dew_point, spread, heat_index, mode,
             fan, int(heater), int(cooler_inferred)))
        self.conn.commit()

    def readings_since(self, seconds_ago: float) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM readings WHERE ts >= ? ORDER BY ts", (time.time() - seconds_ago,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # -- events -----------------------------------------------------------------
    def log_event(self, type_: str, message: str, details: dict | None = None):
        self.conn.execute(
            "INSERT INTO events VALUES (?,?,?,?)",
            (time.time(), type_, message, json.dumps(details or {})))
        self.conn.commit()

    def recent_events(self, limit=100) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["details"] = json.loads(d["details"] or "{}")
            out.append(d)
        return out

    # -- learned rates ------------------------------------------------------
    def update_rate(self, combo: str, temp_slope: float, rh_slope: float, alpha=0.3):
        row = self.conn.execute(
            "SELECT temp_slope, rh_slope, samples FROM learned_rates WHERE combo=?",
            (combo,)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO learned_rates VALUES (?,?,?,1)",
                             (combo, temp_slope, rh_slope))
        else:
            prev_t, prev_r, n = row
            new_t = alpha * temp_slope + (1 - alpha) * prev_t
            new_r = alpha * rh_slope + (1 - alpha) * prev_r
            self.conn.execute(
                "UPDATE learned_rates SET temp_slope=?, rh_slope=?, samples=? WHERE combo=?",
                (new_t, new_r, n + 1, combo))
        self.conn.commit()

    def get_rate(self, combo: str):
        row = self.conn.execute(
            "SELECT temp_slope, rh_slope, samples FROM learned_rates WHERE combo=?",
            (combo,)).fetchone()
        return {"temp_slope": row[0], "rh_slope": row[1], "samples": row[2]} if row else None

    def all_rates(self) -> dict:
        cur = self.conn.execute("SELECT combo, temp_slope, rh_slope, samples FROM learned_rates")
        return {r[0]: {"temp_slope": r[1], "rh_slope": r[2], "samples": r[3]} for r in cur.fetchall()}

    # -- threshold overrides --------------------------------------------------
    def set_threshold(self, name: str, value: float):
        self.conn.execute(
            "INSERT INTO threshold_overrides VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (name, value, time.time()))
        self.conn.commit()

    def get_thresholds(self) -> dict:
        cur = self.conn.execute("SELECT name, value FROM threshold_overrides")
        return {r[0]: r[1] for r in cur.fetchall()}

    # -- small key/value store (last-autotune-date, etc.) ---------------------
    def kv_get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        self.conn.commit()
