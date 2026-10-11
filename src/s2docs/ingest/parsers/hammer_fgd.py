"""Parser for Valve's hand-written Hammer FGD files (game/core/base.fgd, game/csgo/csgo.fgd, ...).

These carry the labels, help text, defaults and choice lists Hammer shows, which
the DumpSource2 runtime FGDs lack. Grammar handled (token-based, whitespace-insensitive):

    @include "base.fgd"
    @exclude some_class
    @PointClass base(A, B) iconsprite("x.vmat") metadata { entity_tool_name = "X" } = classname :
        "Description" + " continued"
    [
        key(type) [ group="Misc" ] : "Label" : default : "Description" =
        [
            0 : "No"
            1 : "Yes"
        ]
        input Name(type) : "Description"
        output Name(type) : "Description"
    ]
    @OverrideClass = classname [ ...members merged into the existing class... ]

Other directives (@VisGroupFilter, @EntityGroup, @mapsize, ...) are skipped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

_TOKEN = re.compile(
    r'\s+|//[^\n]*|"(?:[^"\\]|\\.)*"|[@()\[\]{}:=,+]|[^\s"()\[\]{}:=,+@]+',
    re.S,
)
_PUNCT = set("@()[]{}:=,+")


@dataclass(slots=True)
class HammerMember:
    kind: str  # keyvalue | input | output
    name: str
    type: str
    label: str | None = None
    default: str | None = None
    description: str | None = None
    choices: list[tuple[str, str]] | None = None
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class HammerClass:
    classname: str
    class_type: str
    source_file: str
    bases: list[str] = field(default_factory=list)
    description: str | None = None
    tool_name: str | None = None
    tool_tip: str | None = None
    tool_group: str | None = None
    members: list[HammerMember] = field(default_factory=list)

    def merge(self, other: HammerClass) -> None:
        """Apply an @OverrideClass: replace same-named members, append new ones."""
        index = {(m.kind, m.name): i for i, m in enumerate(self.members)}
        for m in other.members:
            key = (m.kind, m.name)
            if key in index:
                self.members[index[key]] = m
            else:
                index[key] = len(self.members)
                self.members.append(m)
        for attr in ("description", "tool_name", "tool_tip", "tool_group"):
            if getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
        if other.bases:
            self.bases = other.bases


@dataclass(slots=True)
class _Tok:
    kind: str  # str | word | punct
    value: str


class FgdSyntaxError(ValueError):
    pass


def _tokenize(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    for m in _TOKEN.finditer(text):
        s = m.group(0)
        if s.isspace() or s.startswith("//"):
            continue
        if s.startswith('"'):
            out.append(_Tok("str", s[1:-1].replace('\\"', '"')))
        elif s in _PUNCT:
            out.append(_Tok("punct", s))
        else:
            out.append(_Tok("word", s))
    return out


class _Parser:
    def __init__(self, tokens: list[_Tok], source_file: str):
        self.t = tokens
        self.i = 0
        self.source_file = source_file

    # -- token helpers
    def peek(self, offset: int = 0) -> _Tok | None:
        j = self.i + offset
        return self.t[j] if j < len(self.t) else None

    def next(self) -> _Tok:
        tok = self.peek()
        if tok is None:
            raise FgdSyntaxError(f"{self.source_file}: unexpected end of file")
        self.i += 1
        return tok

    def at(self, value: str) -> bool:
        tok = self.peek()
        return tok is not None and tok.kind == "punct" and tok.value == value

    def expect(self, value: str) -> None:
        tok = self.next()
        if tok.kind != "punct" or tok.value != value:
            raise FgdSyntaxError(f"{self.source_file}: expected {value!r}, got {tok.value!r}")

    def skip_balanced(self, open_: str, close: str) -> list[_Tok]:
        """Consume a balanced block starting at `open_`; return the inner tokens."""
        self.expect(open_)
        depth, inner = 1, []
        while True:
            tok = self.next()
            if tok.kind == "punct" and tok.value == open_:
                depth += 1
            elif tok.kind == "punct" and tok.value == close:
                depth -= 1
                if depth == 0:
                    return inner
            inner.append(tok)

    def concat_string(self) -> str | None:
        """Read `"a" + "b" + ...`; returns None if no string is present."""
        tok = self.peek()
        if tok is None or tok.kind != "str":
            return None
        parts = [self.next().value]
        while self.at("+") and (nxt := self.peek(1)) is not None and nxt.kind == "str":
            self.next()
            parts.append(self.next().value)
        return "".join(parts)

    def scalar(self) -> str | None:
        """A default value: string, word/number, or nothing (`: :`)."""
        tok = self.peek()
        if tok is None or tok.kind == "punct":
            return None
        if tok.kind == "str":
            return self.concat_string()
        return self.next().value

    # -- grammar
    def directives(self) -> list[tuple[str, object]]:
        out: list[tuple[str, object]] = []
        while self.peek() is not None:
            if not self.at("@"):
                self.next()  # stray token; resync on the next '@'
                continue
            self.next()
            name = self.next().value
            low = name.lower()
            if low == "include":
                out.append(("include", self.next().value))
            elif low == "exclude":
                out.append(("exclude", self.next().value))
            elif low.endswith("class"):
                out.append(("class", self.class_decl(name)))
            else:
                # @VisGroupFilter { } / @EntityGroup "x" { } / @mapsize(a, b): skip to next '@'
                while self.peek() is not None and not self.at("@"):
                    if self.at("{"):
                        self.skip_balanced("{", "}")
                    else:
                        self.next()
        return out

    def class_decl(self, class_type: str) -> HammerClass:
        cls = HammerClass(classname="", class_type=class_type, source_file=self.source_file)
        # Header helpers up to '='
        while not self.at("="):
            tok = self.next()
            if tok.kind == "word" and self.at("("):
                args = self.skip_balanced("(", ")")
                if tok.value.lower() == "base":
                    cls.bases = [a.value for a in args if a.kind in ("word", "str")]
            elif tok.kind == "word" and tok.value.lower() == "metadata" and self.at("{"):
                meta = _kv_block(self.skip_balanced("{", "}"))
                cls.tool_name = meta.get("entity_tool_name")
                cls.tool_tip = meta.get("entity_tool_tip")
                cls.tool_group = meta.get("entity_tool_group")
            elif self.at("{"):
                self.skip_balanced("{", "}")
            elif tok.kind == "punct" and tok.value == "@":
                raise FgdSyntaxError(f"{self.source_file}: class header without '='")
        self.expect("=")
        cls.classname = self.next().value
        if self.at(":"):
            self.next()
            cls.description = self.concat_string() or None
        if self.at("["):
            self.members(cls)
        return cls

    def members(self, cls: HammerClass) -> None:
        self.expect("[")
        while not self.at("]"):
            tok = self.next()
            if tok.kind != "word":
                continue
            kind = "keyvalue"
            name = tok.value
            if tok.value in ("input", "output") and (nxt := self.peek()) is not None and nxt.kind == "word":
                kind, name = tok.value, self.next().value
            if not self.at("("):
                continue  # not a member declaration
            type_tokens = self.skip_balanced("(", ")")
            member = HammerMember(kind=kind, name=name, type="".join(t.value for t in type_tokens) or "void")
            # Optional metadata block: [ group="Misc" ] or { sort_priority = 90 }
            if self.at("[") or self.at("{"):
                pair = ("[", "]") if self.at("[") else ("{", "}")
                member.meta = _kv_block(self.skip_balanced(*pair))
            fields: list[str | None] = []
            while self.at(":") and len(fields) < 4:
                self.next()
                fields.append(self.scalar())
            if kind == "keyvalue":
                member.label = fields[0] if len(fields) > 0 else None
                member.default = fields[1] if len(fields) > 1 else None
                member.description = fields[2] if len(fields) > 2 else None
            else:
                member.description = fields[0] if fields else None
            if self.at("="):
                self.next()
                if self.at("["):
                    member.choices = _choices(self.skip_balanced("[", "]"))
            member.label = member.label or None
            member.description = member.description or None
            member.default = member.default if member.default not in ("", None) else None
            cls.members.append(member)
        self.expect("]")


def _kv_block(tokens: list[_Tok]) -> dict[str, str]:
    """`key = value key2 = "value"` (commas optional) -> dict; nested blocks ignored."""
    out: dict[str, str] = {}
    for i in range(len(tokens) - 2):
        if tokens[i].kind == "word" and tokens[i + 1].kind == "punct" and tokens[i + 1].value == "=":
            out[tokens[i].value] = tokens[i + 2].value
    return out


def _choices(tokens: list[_Tok]) -> list[tuple[str, str]]:
    """`value : "label" [: flag_default]` entries."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind in ("word", "str") and i + 2 < len(tokens) and tokens[i + 1].value == ":":
            label = tokens[i + 2]
            out.append((tok.value, label.value))
            i += 3
            # flags have a trailing ': 0/1' default
            if i + 1 < len(tokens) and tokens[i].value == ":":
                i += 2
        else:
            i += 1
    return out


def parse_hammer_fgd(text: str, source_file: str) -> list[tuple[str, object]]:
    """Parse one file into ordered directives: ('include', path), ('exclude', name), ('class', HammerClass)."""
    return _Parser(_tokenize(text), source_file).directives()


def resolve_hammer_fgds(root: str, read: Callable[[str], str | None]) -> dict[str, HammerClass]:
    """Load `root` and its @includes (depth-first, each once) and apply excludes/overrides.

    `read(name)` returns the text of an included file name (or None if missing).
    """
    classes: dict[str, HammerClass] = {}
    seen: set[str] = set()

    def load(name: str) -> None:
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        text = read(name)
        if text is None:
            return
        for kind, value in parse_hammer_fgd(text, name):
            if kind == "include":
                load(str(value))
            elif kind == "exclude":
                classes.pop(str(value), None)
            elif kind == "class":
                cls = value
                assert isinstance(cls, HammerClass)
                existing = classes.get(cls.classname)
                if cls.class_type.lower() == "overrideclass":
                    if existing:
                        existing.merge(cls)
                else:
                    classes[cls.classname] = cls

    load(root)
    return classes
