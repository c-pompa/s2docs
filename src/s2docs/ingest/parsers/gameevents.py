"""Parser for KeyValues1 `.gameevents` files.

Trailing ``//`` comments on the same line as an event or field name are kept
as their descriptions, since that is the only documentation Valve ships.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field


@dataclass(slots=True)
class GameEventField:
    name: str
    type: str
    description: str | None = None


@dataclass(slots=True)
class GameEvent:
    name: str
    source_file: str
    description: str | None = None
    fields: list[GameEventField] = field(default_factory=list)


@dataclass(slots=True)
class _Tok:
    value: str
    line: int
    quoted: bool


def _tokenize(text: str) -> tuple[list[_Tok], dict[int, str]]:
    tokens: list[_Tok] = []
    comments: dict[int, str] = {}
    i, n, line = 0, len(text), 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            comment = text[i + 2 : end].strip()
            if comment and line not in comments:
                comments[line] = comment
            i = end
        elif ch in "{}":
            tokens.append(_Tok(ch, line, False))
            i += 1
        elif ch == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                if text[j] == "\n":
                    line += 1
                buf.append(text[j])
                j += 1
            tokens.append(_Tok("".join(buf), line, True))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            tokens.append(_Tok(text[i:j], line, False))
            i = j
    return tokens, comments


def parse_gameevents(text: str, source_file: str) -> Iterator[GameEvent]:
    tokens, comments = _tokenize(text)
    i = 0
    # Skip root key ("gameevents" / "ModEvents" / ...) and its opening brace.
    while i < len(tokens) and tokens[i].value != "{":
        i += 1
    i += 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.value == "}" and not tok.quoted:
            break
        if i + 1 < len(tokens) and tokens[i + 1].value == "{" and not tokens[i + 1].quoted:
            event = GameEvent(tok.value, source_file, comments.get(tok.line))
            i += 2
            while i < len(tokens) and not (tokens[i].value == "}" and not tokens[i].quoted):
                key = tokens[i]
                if i + 1 < len(tokens):
                    val = tokens[i + 1]
                    event.fields.append(
                        GameEventField(
                            key.value,
                            val.value,
                            comments.get(val.line) or comments.get(key.line),
                        )
                    )
                i += 2
            i += 1
            yield event
        else:
            i += 1
