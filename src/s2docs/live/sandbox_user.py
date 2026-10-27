"""The contract a user cs_script lives under when it is bundled into the `hermes_sandbox` runtime.

A user's `.js` is never shipped as a point_script of its own: the engine only loads a script that a map's
`point_script` names, and `hermes_sandbox.vpk` carries no script at all — its `point_script` names
`maps/scripts/hermes_sandbox.vjs`, which the engine reads from the LOOSE
`<CS2>/game/csgo/maps/scripts/hermes_sandbox.vjs_c`. That file can be replaced without a `.vmap` compile
(measured 2026-10-27), which is the whole reason this route exists: `.vmap` compiles need an Active session
on desktop-pc, and pomps-pc — the host that actually plays — must never compile a map.

So the user's code is a MODULE bundled into that runtime (``sandbox_map.render_script(spawnables, user)``),
and this module is the contract it is checked against *before* anything is compiled:

* it defines ``function start(api) { ... }`` and may define ``function stop(api) { ... }``;
* it never touches ``Instance``: the ``api`` object the runtime builds is the entire surface
  (``api.msg``, ``api.command``, ``api.onChat``). That is not decoration — a second
  ``Instance.OnPlayerChat`` registration would overwrite the runtime's, so chat is fanned out from the
  single registration the runtime already owns;
* no imports/exports (a bundled module has no module system), no ``eval``/``Function``, and no
  ``globalThis``/``process``/``console``/``window``/``document`` we could not attribute;
* no template literals: ``${...}`` is code, and blanking it would let ``Instance`` hide inside a string.

The scan runs over the code with comments and string literals BLANKED, so ``"Instance"`` in a message is not
an access and ``/* Instance */`` is not code. A refusal is a :class:`UserScriptRefused` carrying one reason
per offending token, which the pipeline records as a ``failed`` validate.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

#: A name is a console tag (`[user:<name>]`) and a file name part: lowercase, short, no punctuation.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: Refuse a module that is bigger than this before scanning it: a script that needs more is not a module
#: this route can review, and an unbounded input is a way to make the checker itself the problem.
MAX_BYTES = 64 * 1024

#: ``(pattern, reason)`` applied to the blanked source. Order is the order the reasons are reported in.
FORBIDDEN: tuple[tuple[str, str], ...] = (
    (r"\bInstance\b",
     "Instance: the runtime owns it — use the `api` object (api.msg, api.command, api.onChat)"),
    (r"\bimport\b", "import: a bundled module has no module system; drop the import"),
    (r"\bexport\b", "export: a bundled module has no module system; drop the export"),
    (r"\brequire\b", "require: cs_script has no module loader"),
    (r"\beval\b", "eval: code that builds code cannot be reviewed"),
    (r"\bFunction\b", "Function: the Function constructor is eval in disguise"),
    (r"\bglobalThis\b", "globalThis: the runtime's globals are not the module's"),
    (r"\bprocess\b", "process: cs_script has no Node runtime"),
    (r"\bconsole\b", "console: write console output with api.msg(text)"),
    (r"\bwindow\b", "window: cs_script has no DOM"),
    (r"\bdocument\b", "document: cs_script has no DOM"),
)

#: The contract: `start(api)` is required, `stop(api)` is optional. `(?<![\w.$])` keeps `as.start(` and
#: `obj.function(` from matching a declaration.
START = re.compile(r"(?<![\w.$])function\s+start\s*\(\s*api\s*\)")
STOP = re.compile(r"(?<![\w.$])function\s+stop\s*\(\s*api\s*\)")


class UserScriptRefused(ValueError):
    """The module does not meet the contract. ``reasons`` is one line per offending token."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class UserModule:
    """A checked user module plus the per-build nonce its `ready` line is proved against."""

    name: str
    code: str
    nonce: str

    @property
    def ready_line(self) -> str:
        return f"[user:{self.name}] ready {self.nonce}"


def _blank(code: str) -> tuple[str, bool]:
    """`code` with comments and string literals replaced by spaces (newlines kept); also saw_template.

    Both directions matter: ``"Instance"`` inside a message must not be refused, and a name inside a comment
    must not pass as code. Backticks are not blanked — a template literal's ``${...}`` is code, so the
    checker refuses the literal instead of hiding whatever is inside it.
    """
    out: list[str] = []
    i, n = 0, len(code)
    saw_template = False
    while i < n:
        c = code[i]
        if c == "/" and code.startswith("//", i):
            end = code.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif c == "/" and code.startswith("/*", i):
            end = code.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in code[i:end]))
            i = end
        elif c in "'\"":
            j = i + 1
            while j < n:
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == c:
                    j += 1
                    break
                if code[j] == "\n":  # unterminated at a newline: stop, tsc will report it
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
        elif c == "`":
            saw_template = True
            j = i + 1
            while j < n:
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == "`":
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out), saw_template


def check_user_module(code: str, *, name: str, nonce: str | None = None) -> UserModule:
    """Return the checked module, or raise :class:`UserScriptRefused` with every reason found.

    ``nonce`` is minted here when the caller does not pass one: it goes into the bundle, so the
    ``[user:<name>] ready <nonce>`` line an in-game readback sees can only come from THIS build.
    """
    if not NAME_RE.match(name or ""):
        raise UserScriptRefused(
            [f"invalid name {name!r}: lowercase [a-z][a-z0-9_]*, max 32 — it is the `[user:<name>]` tag"]
        )
    if not isinstance(code, str) or not code.strip():
        raise UserScriptRefused(["the module is empty: it must define `function start(api) { ... }`"])
    size = len(code.encode("utf-8"))
    if size > MAX_BYTES:
        raise UserScriptRefused([f"the module is {size} B; the limit is {MAX_BYTES} B"])

    blanked, saw_template = _blank(code)
    reasons = [reason for pattern, reason in FORBIDDEN if re.search(pattern, blanked)]
    if saw_template:
        reasons.append("template literal: use 'a ' + b so the checker can read every line as code")
    if not START.search(blanked):
        reasons.append(
            "no `function start(api) { ... }`: the runtime builds the api object and calls start(api) with it"
        )
    if reasons:
        raise UserScriptRefused(reasons)
    return UserModule(name=name, code=code, nonce=nonce or secrets.token_hex(4))


def has_stop(module: UserModule) -> bool:
    """True when the module also defines `stop(api)`: the bundle then registers `hu_stop` for it."""
    return bool(STOP.search(_blank(module.code)[0]))
