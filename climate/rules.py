"""
rules.py — the conversational automation layer.

A Rule is what the Claude translator (nl.py) produces from plain English.
Two kinds, by `target`:

  app          — runs HERE, every tick, against the live sensor. Executes on the
                 Tuya devices (fan / heater / led / projector) the app controls.
  google_home  — the app can't reach the Qubo cooler / coffee / Havells bulb, so
                 these carry a generated Google Home script for you to paste once.

The `when` expression is evaluated with a SAFE ast walker (no eval of arbitrary
code) over the sensor variables. Actions are idempotent — the device wrappers
already dedupe, so re-firing a matching rule every tick is free.
"""
from __future__ import annotations
import ast
import json
import logging
import operator
from dataclasses import dataclass, field, asdict
from pathlib import Path

log = logging.getLogger("rules")

# ---- safe expression evaluation --------------------------------------------
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
        ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}


def _fan_tier(hi):
    if hi < 24: return 1
    if hi < 26: return 2
    if hi < 28: return 3
    if hi < 30: return 4
    return 5


_FUNCS = {"tier": _fan_tier, "min": min, "max": max, "abs": abs, "round": round}


def safe_eval(expr, ctx: dict):
    """Evaluate a boolean/arithmetic expression over ctx. Raises on anything
    outside the whitelist (calls to unknown funcs, attribute access, etc.)."""
    if not expr or expr.strip().lower() in ("manual", "false"):
        return False
    if expr.strip().lower() == "true":
        return True

    def ev(node):
        if isinstance(node, ast.Expression): return ev(node.body)
        if isinstance(node, ast.BoolOp):
            vals = [ev(v) for v in node.values]
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            return (not v) if isinstance(node.op, ast.Not) else -v
        if isinstance(node, ast.BinOp):
            return _BIN[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.Compare):
            left = ev(node.left); out = True
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                out = out and _CMP[type(op)](left, right); left = right
            return out
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
                raise ValueError("unknown function")
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        if isinstance(node, ast.Name):
            if node.id not in ctx: raise ValueError(f"unknown var {node.id}")
            return ctx[node.id]
        if isinstance(node, ast.Constant):
            return node.value
        raise ValueError(f"disallowed: {type(node).__name__}")

    return bool(ev(ast.parse(expr, mode="eval")))


# ---- data model -------------------------------------------------------------
@dataclass
class Action:
    device: str          # fan | heater | led | projector
    command: str         # set_speed | on | off | power | color | mode
    value: str = ""      # set_speed: int/expr; color/mode: the name (e.g. "blue")


@dataclass
class Rule:
    id: str
    name: str
    source_text: str          # the original English
    target: str               # app | google_home
    enabled: bool = True
    # app:
    when: str = ""            # expression; "manual" = only via Run button
    actions: list = field(default_factory=list)   # list[Action]
    # google_home:
    gh_yaml: str = ""
    gh_summary: str = ""

    @property
    def manual(self) -> bool:
        return self.target == "app" and (not self.when or self.when.strip().lower() == "manual")

    def to_dict(self):
        d = asdict(self); return d

    @classmethod
    def from_dict(cls, d):
        acts = [Action(**a) for a in d.get("actions", [])]
        return cls(**{**d, "actions": acts})


# ---- execution --------------------------------------------------------------
def run_actions(actions, actuators, ctx):
    """actuators: dict with .fan (set_speed/off), .heater (set), .ir (send LED/projector)."""
    for a in actions:
        try:
            if a.device == "fan":
                if a.command == "off":
                    actuators["fan"].set_speed(0)
                else:
                    spd = int(safe_eval(a.value, ctx)) if a.value and not a.value.isdigit() else int(a.value or 0)
                    actuators["fan"].set_speed(max(0, min(5, spd)))
            elif a.device == "heater":
                actuators["heater"].set(a.command == "on")
            elif a.device in ("led", "projector"):
                actuators["ir_scene"](a.device, a.command, a.value)   # callback fires the learned code
        except Exception as e:
            log.error("action %s/%s failed: %s", a.device, a.command, e)


# ---- store ------------------------------------------------------------------
class RuleStore:
    def __init__(self, path="climate/rules.json"):
        self.path = Path(path)
        self.rules: list[Rule] = []
        self.load()

    def load(self):
        if self.path.exists():
            self.rules = [Rule.from_dict(d) for d in json.loads(self.path.read_text())]

    def save(self):
        self.path.write_text(json.dumps([r.to_dict() for r in self.rules], indent=2))

    def add(self, rule: Rule):
        self.rules.append(rule); self.save()

    def get(self, rid):
        return next((r for r in self.rules if r.id == rid), None)

    def delete(self, rid):
        self.rules = [r for r in self.rules if r.id != rid]; self.save()

    def toggle(self, rid, enabled):
        r = self.get(rid)
        if r: r.enabled = enabled; self.save()

    def next_id(self):
        return f"r{1 + max([int(r.id[1:]) for r in self.rules if r.id[1:].isdigit()] + [0])}"
