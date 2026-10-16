"""Deterministic prose -> keyword query rewriter for s2docs search.

Stdlib only, no model, no network. It exists because ``db.fts.build_match_query`` treats a query as a
MATCH expression whose tokens are ANDed — EVERY token must match — so a real user question (prose with
articles and auxiliaries) retrieves nothing. Measured: ``q=how do i make a breakable prop`` returns 0,
``q=breakable prop`` returns 60, ``q=prop_physics`` returns 64 (live index, /api/search).

The rewriter is the missing prose front-end: reduce a question to the few tokens that can actually
match, map the handful of phrasings users really use onto the engine's own vocabulary, and settle the
game only from explicit words or CS2-only vocabulary. ``review._subject`` already names the same
failure ("ANDs up to 12 query tokens ... the case scores nothing for a reason that has nothing to do
with the human's answer"); this module is the deterministic answer to it.

Design rules, each one load-bearing:

  1. Drop stopwords and interrogatives. A curated ``STOPWORDS`` set, not a dependency — the loop is
     offline and inspectable.
  2. Few tokens beat many. Every token must match, so the budget is spent on the most specific tokens
     (``_score``) and capped at ``_MAX_TOKENS``.
  3. A SMALL curated synonym map. A synonym that is wrong is worse than none; every entry is here
     because the engine's docs index the concept under a different term than users type.
  4. Game from evidence, never a guess. ``None`` when it is ambiguous — a wrong ``game=`` filter
     silently drops the right answer.
  5. Surgical. A keyword-shaped query (``mp_roundtime``) passes through unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Rewrite", "rewrite", "STOPWORDS", "PHRASES", "TOKEN_SYNONYMS"]


@dataclass(frozen=True)
class Rewrite:
    """The rewriter's decision, with its own provenance (what it dropped and why)."""

    query: str                 # the few high-signal tokens, space separated
    game: str | None           # 'cs2' | 'deadlock' | None when undetermined
    dropped: tuple[str, ...]   # tokens removed, for transparency
    notes: tuple[str, ...]     # e.g. 'synonym: see through walls -> occluder'


_MAX_TOKENS = 6

_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Curated English function words + interrogatives + a short list of conversational verbs that carry no
# retrieval signal in this corpus. No dependency: the set is meant to be read. Domain words are
# deliberately absent (see test_stopword_set_has_no_domain_terms) — dropping 'prop' or 'addon' would be
# catastrophic, dropping 'please' is free.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but if then than so as of to in on at by for with from into about over under
    up down out off within without inside outside near around between among during through across
    along behind below above beneath beside onto upon via per
    this that these those there here it its is are was were be been being am
    do does did doing done have has had having will would shall should can could may might must
    i me my mine we us our ours you your yours he she they them their theirs him her his hers
    who whom whose what which when where why how
    not no nor yes very just also too any some all more most much many own same such only
    please want wanted need needed like get got gets use uses used using make makes made
    think thought know knows look looks help helps show shows shown tell tells say says said
    research create creates created make also allow allows specific certain general thing things work
    """.split()
)

# Multi-word phrasings -> the engine's own term. Longest first so 'players info ui' wins over
# 'player info'. Each entry cites (in tests/test_learning_rewrite.py) why it is real.
# The 'see through walls' family is NOT here: it is sense-dependent (see _WALL_SENSE_PHRASES).
_PHRASE_LIST: tuple[tuple[str, str], ...] = (
    ("behind a wall", "occluder"),
    ("middle of the screen", "panorama hud"),
    ("players info ui", "spectator"),
    ("player info", "panorama hud"),
    ("red block texture", "prop"),
    ("load a mod", "addon"),
)
# Pre-sorted longest-first, deterministic.
PHRASES: tuple[tuple[str, str], ...] = tuple(sorted(_PHRASE_LIST, key=lambda kv: (-len(kv[0]), kv[0])))

# What a user means by 'see through walls' depends on the SENSE, and the corpus indexes the two senses
# under different terms:
#   * SPECTATOR/player/outline intent -> xray  (spec_show_xray: "you can see player outlines ... through
#     walls"). The map-compile term 'occluder' here retrieves r_csgo_sky_occluder (a skybox convar) — a miss.
#   * MAP/VIS intent (visibility, compile, geometry) -> occluder (server:prop_static's "Vis Occluder",
#     the precomputed-visibility system).
# Longest first so 'see players through walls' wins over 'through walls'.
_WALL_SENSE_PHRASES: tuple[str, ...] = (
    "see players through walls", "see through walls", "through walls",
)
_SPECTATOR_SENSE: frozenset[str] = frozenset(
    {"spectator", "spectators", "spec", "player", "players", "outline", "outlines", "xray", "x_ray",
     "enemy", "teammate", "teammates", "silhouette", "glow", "hud"}
)

# Single-token synonyms: a .dll is a code addon in Source 2, and the corpus names the VIS system's
# contributors ("all meshes are VIS contributors") where users write the verb 'contributes'.
TOKEN_SYNONYMS: dict[str, str] = {
    "dll": "addon",
    "contributes": "contributor",
    "contribute": "contributor",
}

# Explicit in-text game names. 'counter-strike'/'counter strike' are handled as substrings.
_CS2_WORDS: frozenset[str] = frozenset({"cs2", "csgo", "counterstrike"})
_CS2_SUBSTRINGS: tuple[str, ...] = ("counter-strike", "counter strike")
# CS2-only vocabulary: identifiers that only exist in the CS2 toolchain. A query using one of these is
# a CS2 question even when it never says 'cs2'.
_CS2_VOCAB: frozenset[str] = frozenset(
    {"dust2", "mp_roundtime", "mp_roundvtime", "cs_script", "vjs", "vjs_c", "vsmartprop", "vmap"}
)
_DEADLOCK_WORDS: frozenset[str] = frozenset({"deadlock", "citadel"})

# Domain vocabulary used only for SPECIFICITY scoring (never for rewriting) — an identifier or a known
# engine term outranks a generic word when the cap has to choose.
_DOMAIN_VOCAB: frozenset[str] = frozenset(
    {
        "occluder", "panorama", "hud", "spectator", "spectators", "addon", "vmap", "vpk", "vjs",
        "vjs_c", "vscript", "prop", "physics", "entity", "convar", "schema", "gameevent", "netprop",
        "spawn", "trigger", "nav", "cs_script", "weapon", "item", "particle", "material", "texture",
        "model", "map", "sound", "keyvalues", "hammer", "cs2", "deadlock", "citadel", "ability",
        "hero", "patron", "urn", "mp_roundtime", "mp_roundvtime", "dust2",
        "xray", "vis", "visibility", "occluder", "contributor", "contributors",
    }
)


def _game_filter_tokens(game: str) -> frozenset[str]:
    """The bare game NAMES that merely establish ``game`` — a filter, not a keyword.

    Identifiers that contain a game name (``cs_script``, ``mp_roundtime``,
    ``citadel_spectator_mode``) are NOT here: they are real content tokens and are never stripped.
    """
    if game == "cs2":
        return _CS2_WORDS | {"counter", "strike"}  # 'counter-strike' tokenises to counter + strike
    if game == "deadlock":
        return _DEADLOCK_WORDS
    return frozenset()


def _wall_sense(raw: str) -> tuple[str, str]:
    """Resolve the 'see through walls' sense from context: (corpus term, sense label).

    Spectator/player/outline intent is the xray sense (spec_show_xray), everything else is the
    map/vis occluder sense.
    """
    if set(_TOKEN_RE.findall(raw)) & _SPECTATOR_SENSE:
        return "xray", "spectator/xray; spec_show_xray: see player outlines through walls"
    return "occluder", "map/vis"


def _score(tok: str) -> int:
    """Specificity: identifiers and engine terms first, long content words next, generic words last."""
    if "_" in tok or any(c.isdigit() for c in tok):
        return 3
    if tok in _DOMAIN_VOCAB:
        return 3
    if len(tok) >= 9:
        return 2
    return 1


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Word-boundary match so 'wall' does not fire inside 'wallet'."""
    return re.compile(r"(?<![a-z0-9_])" + re.escape(phrase) + r"(?![a-z0-9_])")


def _detect_game(raw: str, tokens: list[str]) -> str | None:
    """The game, from explicit words or CS2-only vocabulary — never guessed.

    Both signalled = genuinely ambiguous = ``None``: a wrong ``game`` filter silently hides the right
    document, which is worse than an unfiltered search.
    """
    toks = set(tokens)
    cs2 = bool(toks & _CS2_WORDS) or bool(toks & _CS2_VOCAB) or any(s in raw for s in _CS2_SUBSTRINGS)
    deadlock = bool(toks & _DEADLOCK_WORDS)
    if cs2 and deadlock:
        return None
    if cs2:
        return "cs2"
    if deadlock:
        return "deadlock"
    return None


def rewrite(text: str) -> Rewrite:
    """Reduce free text to a short, high-signal FTS5 keyword query + a game decision.

    Deterministic: the same input always yields the same ``Rewrite``.
    """
    raw = " ".join((text or "").lower().split())
    if not raw:
        return Rewrite(query="", game=None, dropped=(), notes=())

    notes: list[str] = []
    # 1. Phrase synonyms, on the raw text (before stopword removal, so multi-word phrases that contain
    #    stopwords — 'middle of the screen' — still match).
    work = raw
    # 1a. The sense-dependent 'see through walls' family: resolve against the whole raw text first.
    for phrase in _WALL_SENSE_PHRASES:
        pat = _phrase_pattern(phrase)
        if pat.search(work):
            target, sense = _wall_sense(raw)
            work = pat.sub(target, work)
            notes.append(f"synonym: {phrase} -> {target} (sense={sense})")
    for phrase, target in PHRASES:
        pat = _phrase_pattern(phrase)
        if pat.search(work):
            work = pat.sub(target, work)
            notes.append(f"synonym: {phrase} -> {target}")

    # 2. Tokenise, then single-token synonyms.
    mapped: list[str] = []
    for tok in _TOKEN_RE.findall(work):
        if tok in TOKEN_SYNONYMS:
            target = TOKEN_SYNONYMS[tok]
            notes.append(f"synonym: {tok} -> {target}")
            tok = target
        mapped.append(tok)

    # 3. Drop stopwords and duplicates (dedupe keeps token budget for distinct signal).
    dropped: list[str] = []
    kept: list[str] = []
    seen: set[str] = set()
    for tok in mapped:
        if tok in STOPWORDS or tok in seen:
            dropped.append(tok)
            continue
        seen.add(tok)
        kept.append(tok)

    # 3.5 The game NAME is a FILTER, not a keyword: it is carried by Rewrite.game. Under the
    #     engine's AND semantics a bare 'cs2' costs real hits ('spectators allowed match' hits,
    #     '... cs2 ...' scores nothing), so strip it — before the cap, so budget goes to real tokens.
    #     Only when a game was actually established; identifiers (cs_script, citadel_*_mode) are untouched.
    game = _detect_game(raw, _TOKEN_RE.findall(raw))
    if game:
        names = _game_filter_tokens(game)
        stripped = [tok for tok in kept if tok in names]
        if stripped:
            kept = [tok for tok in kept if tok not in names]
            dropped.extend(stripped)
            notes.append(f"filter: dropped game name(s) {', '.join(stripped)} (game={game})")

    # 4. Cap, preferring the most specific tokens; preserve original order in the output.
    if len(kept) > _MAX_TOKENS:
        chosen = set(sorted(kept, key=_score, reverse=True)[:_MAX_TOKENS])
        final: list[str] = []
        for tok in kept:
            if tok in chosen:
                final.append(tok)
            else:
                dropped.append(tok)
        kept = final

    query = " ".join(kept)
    return Rewrite(query=query, game=game, dropped=tuple(dropped), notes=tuple(notes))
