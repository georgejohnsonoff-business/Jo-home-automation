"""
nl.py — natural language -> structured automation, via the Groq API.

Runs only when you CREATE a rule (occasional + cheap), never in the control
loop. Uses openai/gpt-oss-120b on Groq (OpenAI-compatible endpoint) with JSON
mode + Pydantic validation — no brittle string parsing.

Routing is the model's job:
  * fan / heater / led / projector  -> target "app"  (runs in our loop)
  * cooler / coffee / Havells bulb  -> target "google_home" (generates a script
    to paste into Google Home, because the app can't reach those)

Needs GROQ_API_KEY in .env.
"""
from __future__ import annotations
import json
from typing import List, Literal

from pydantic import BaseModel, ValidationError

from .rules import Rule, Action as RuleAction

MODEL = "openai/gpt-oss-120b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class Action(BaseModel):
    device: Literal["fan", "heater", "led", "projector"]
    command: Literal["set_speed", "on", "off", "power"]
    value: str          # for set_speed: an int "1".."5" or expression like "tier(heat_index)"; else ""


class ParsedRule(BaseModel):
    target: Literal["app", "google_home"]
    name: str
    # app fields
    when: str           # boolean expression over sensor vars, or "manual" for a button-only scene, or "" if google_home
    actions: List[Action]
    # google_home fields
    gh_yaml: str        # full Google Home Script Editor YAML, or "" if app
    gh_summary: str     # one-line plain-English summary of the GH automation, or ""
    note: str           # short note back to the user (assumptions, thresholds chosen, caveats)


SYSTEM = """You convert a person's plain-English home-automation request into ONE structured rule for a smart-home controller in a Bangalore living room.

## Sensor variables (available to `when` expressions, evaluated every 30s)
- temp        : air temperature, °C
- rh          : relative humidity, %
- dew_point   : °C
- spread      : temp - dew_point (evaporative headroom; >9 dry, <5 muggy)
- heat_index  : "feels like" °C
- hour, minute: local clock (0-23, 0-59)
- weekday     : 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Saturday, 6=Sunday
Helper functions allowed in expressions: tier(heat_index) -> fan speed 1-5, min, max, abs, round.
Operators: and or not, < <= > >= == !=, + - * / %. Example: `heat_index > 27 and spread > 7 and weekday <= 3`
(weekday <= 3 means Mon-Thu; combine with "and" for "on these weekdays only" requests).

## Devices the APP controls directly (target = "app")
- fan      : commands set_speed (value "1".."5" or an expression like "tier(heat_index)") | off
- heater   : on | off        (a space heater)
- led      : on | off (LED strip) | color (value: one of "red","green","blue" — only these 3 are
             learned so far) | mode (value: one of "smooth","fade","strobe")
- projector: on | off | power
For app rules: set `when` to the trigger expression, or "manual" for a scene the user runs with a button (movie mode, etc.). Fill `actions`. Leave gh_yaml/gh_summary "".
If a request needs multiple LED actions (e.g. "on and blue"), include multiple Action entries for
the led device — one with command "on", another with command "color" and the color name.

## Devices only GOOGLE HOME can control (target = "google_home")
- Cooler - Living Room        (evaporative air cooler; on/off)
- Coffee Maker - Living Room  (on/off)
- Lights - Living Room        (Havells bulb; on/off)
- Temperature Sensor - Living Room (reads temperatureAmbient °C and humidityAmbientPercent)
If the request involves ANY of these, target = "google_home": generate a complete Google Home Script Editor YAML in `gh_yaml`, a one-line `gh_summary`, leave `when`/`actions` empty.

### Google Home YAML schema (this exact shape works in the user's account)
metadata:
  name: <name>
  description: <desc>
automations:
  - starters:
      - type: device.state.TemperatureControl
        device: Temperature Sensor - Living Room
        state: temperatureAmbient
        greaterThan: 29C            # or lessThan
      - type: device.state.HumiditySetting
        device: Temperature Sensor - Living Room
        state: humidityAmbientPercent
        lessThan: 55                # or greaterThan
    condition:                      # optional; use type: and / or with conditions:[...] to require BOTH
      type: and
      conditions: [ ...same shape as starters... ]
    actions:
      - type: device.command.OnOff
        devices: Cooler - Living Room
        on: true                    # or false
For a time schedule use a starter: `- type: time.schedule` with `at: "7:00 AM"` and optional `weekdays: [MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY]`.

## Evaporative-cooler physics (apply when the request is about the cooler)
The cooler only helps when air is hot AND dry. Turn it ON when hot and spread is high / humidity low; turn it OFF when muggy (high humidity / low spread) — never cool into humid air.

## Rules
- Choose sensible thresholds if the user is vague, and state them in `note`.
- Exactly one rule per request. Prefer the simplest automation that satisfies the request.
- `note` is a short friendly confirmation of what you built and any assumption.

## Output format
Respond with ONLY a single JSON object, no markdown fences, no commentary, matching exactly this shape:
{
  "target": "app" | "google_home",
  "name": string,
  "when": string,
  "actions": [{"device": "fan"|"heater"|"led"|"projector", "command": "set_speed"|"on"|"off"|"power", "value": string}],
  "gh_yaml": string,
  "gh_summary": string,
  "note": string
}"""


def translate(text: str, api_key: str | None = None) -> tuple[Rule | None, str]:
    """Return (Rule, note). Rule is None with an error note if the API isn't set up."""
    try:
        from groq import Groq
    except ImportError:
        return None, "groq SDK not installed (pip install groq)."

    import os
    key = api_key or os.getenv("GROQ_API_KEY")
    if not key:
        return None, "GROQ_API_KEY not set in .env."

    try:
        client = Groq(api_key=key)
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        raw = resp.choices[0].message.content
    except Exception as e:
        return None, f"Couldn't reach Groq: {e}"

    try:
        p = ParsedRule.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        return None, f"Model returned something I couldn't parse: {e}"

    rule = Rule(
        id="",                                  # assigned by the store
        name=p.name,
        source_text=text,
        target=p.target,
        when=p.when,
        actions=[RuleAction(a.device, a.command, a.value) for a in p.actions],
        gh_yaml=p.gh_yaml,
        gh_summary=p.gh_summary,
    )
    return rule, p.note
