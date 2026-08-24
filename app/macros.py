"""SillyTavern-style prompt macros.

Supports:
  {{user}}, {{User}}, {{USER}}, {{user_name}}    → user persona display name
  {{user.<field>}}                                → arbitrary user persona field
  {{char}}, {{char_name}}                         → active character display name
  {{random:a,b,c}}                                → uniform random pick
  {{roll:dN}} / {{roll:N}}                        → integer in [1, N]

All matches are case-insensitive.
"""
from __future__ import annotations

import random
import re
from typing import Any


_RANDOM = re.compile(r"\{\{random:([^}]+)\}\}", re.IGNORECASE)
_ROLL = re.compile(r"\{\{roll:?d?(\d+)\}\}", re.IGNORECASE)
_USER_FIELD = re.compile(r"\{\{user\.([a-z0-9_]+)\}\}", re.IGNORECASE)
_USER = re.compile(r"\{\{(?:user|user_name)\}\}", re.IGNORECASE)
_CHAR = re.compile(r"\{\{(?:char|char_name)\}\}", re.IGNORECASE)


def apply(text: str | None, ctx: dict[str, Any]) -> str:
    if not text:
        return text or ""
    user_name = str(ctx.get("user_name") or "User")
    char_name = str(ctx.get("char_name") or "")
    user_persona = ctx.get("user_persona") or {}
    out = str(text)
    out = _USER_FIELD.sub(lambda m: str(user_persona.get(m.group(1).lower(), "")), out)
    out = _USER.sub(user_name, out)
    out = _CHAR.sub(char_name, out)
    out = _RANDOM.sub(_pick, out)
    out = _ROLL.sub(_roll, out)
    return out


def _pick(m: re.Match[str]) -> str:
    parts = [s.strip() for s in m.group(1).split(",") if s.strip()]
    return random.choice(parts) if parts else ""


def _roll(m: re.Match[str]) -> str:
    n = max(1, int(m.group(1)))
    return str(random.randint(1, n))
