"""FTS5 helpers: identifier splitting for indexing and safe MATCH-query building.

User input is never interpolated into a MATCH expression as-is. It is reduced
to word tokens, each emitted as a double-quoted FTS5 string (so operators like
AND/NEAR/column filters in user input are inert) and bound as a parameter.
"""

from __future__ import annotations

import re

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_MAX_TOKENS = 12


def split_identifier(name: str) -> str:
    """'m_flSimulationTime' -> 'm fl simulation time'; 'OnPlayerJump' -> 'on player jump'."""
    parts: list[str] = []
    for chunk in re.split(r"[_\W]+", name):
        if chunk:
            parts.extend(p for p in _CAMEL.split(chunk) if p)
    return " ".join(p.lower() for p in parts)


def _quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def build_match_query(user_query: str, *, prefix: bool = True) -> str | None:
    """Turn free text into an FTS5 query. Each token must match (implicit AND).

    A token matches either as-is (prefix) against all columns, or — for
    identifiers containing '_' — as its split parts, so `round_time` also
    finds `mp_roundtime` via name_parts.
    """
    tokens = _TOKEN.findall(user_query)[:_MAX_TOKENS]
    if not tokens:
        return None
    clauses = []
    for tok in tokens:
        star = "*" if prefix else ""
        alternatives = [f"{_quote(tok.lower())}{star}"]
        split = split_identifier(tok)
        if split and split != tok.lower():
            phrase = " + ".join(_quote(p) for p in split.split())
            alternatives.append(f"name_parts : ({phrase}{star})")
        clauses.append(alternatives[0] if len(alternatives) == 1 else "(" + " OR ".join(alternatives) + ")")
    return " AND ".join(clauses)
