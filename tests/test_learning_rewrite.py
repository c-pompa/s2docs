"""The deterministic prose -> keyword query rewriter (module + `s2docs learning rewrite`).

Why this exists, measured: ``db.fts.build_match_query`` extracts up to 12 word tokens and ANDs them,
so a real user question retrieves nothing. The literal, recorded inputs below are the evidence:
``http://10.88.15.208:8812/api/search?q=how+do+i+make+a+breakable+prop&limit=1`` -> ``total=0``, while
``q=breakable+prop`` -> 60 and ``q=prop_physics`` -> 64. Every token must match, so prose loses and
keywords win.

The rewriter is stdlib-only and model-free: drop stopwords/interrogatives, apply a SMALL curated
synonym map (each entry cites below why it is real), cap the output so the specificity budget is not
spent on filler, and detect the game only from explicit words or CS2-only vocabulary — never guessed.

These real user questions are literal recorded inputs, not invented: they are the five shapes the
loop logged. Each asserts the rewritten query is short, keeps the signal words, and drops the
stopwords.
"""

from __future__ import annotations

import json

from s2docs.learning.rewrite import STOPWORDS, Rewrite, rewrite

# --------------------------------------------------------------------- the five


def test_real_question_breakable_prop_drops_interrogatives():
    """'how do I make a breakable prop' -> the two content words. 0 hits before, 60 with the keyword
    form; the fix is to reduce prose to ``breakable prop`` and nothing else."""
    r = rewrite("how do I make a breakable prop")

    assert isinstance(r, Rewrite)
    assert r.query == "breakable prop"
    assert {"breakable", "prop"} <= set(r.query.split())
    for stop in ("how", "do", "i", "make", "a"):
        assert stop in r.dropped
        assert stop not in r.query.split()
    assert r.game is None, "prop exists in both games — do not guess"


def test_real_question_spectate_through_walls_keeps_the_domain_terms():
    """The long, hedge-every-option question. Signal words: spectators, xray (the SPECTATOR sense of
    'see through walls' — spec_show_xray: "you can see player outlines ... through walls"), and the
    game filter cs2 (in ``game``, not as a token). Prose words (can/you/how/create/research/allow) go.
    """
    q = ("can you research how we can create a mod for cs2, or map mod in cs2 to allow "
         "spectators to see through walls within a specific area of view")
    r = rewrite(q)

    words = set(r.query.split())
    assert len(r.query.split()) <= 6, "few tokens beat many — every token must match"
    assert {"spectators", "xray"} <= words
    assert "cs2" not in words, "the game name is a filter, not a query token"
    assert r.game == "cs2"
    for stop in ("can", "you", "how", "research", "create", "allow", "the"):
        assert stop not in words, stop
    assert any("see through walls -> xray" in n for n in r.notes)


def test_real_question_player_info_ui_maps_to_the_hud_panorama():
    """'i thought cs2 had a middle of the screen ui to show player info?' — 'middle of the screen' and
    'player info' are both the panorama HUD. cs2 settles ``game`` but is not emitted as a token;
    'thought'/'had'/'show' are filler."""
    r = rewrite("i thought cs2 had a middle of the screen ui to show player info?")

    words = set(r.query.split())
    assert {"panorama", "hud"} <= words
    assert "cs2" not in words, "the game name is a filter, not a query token"
    assert r.game == "cs2"
    for stop in ("i", "thought", "had", "the", "to", "show", "middle"):
        assert stop not in words, stop
    assert any("middle of the screen -> panorama hud" in n for n in r.notes)
    assert any("player info -> panorama hud" in n for n in r.notes)


def test_real_question_cs_script_stays_identifier_shaped():
    """'recompile the cs_script (the .vjs_c is baked into the vpk)' — the identifiers ARE the query.
    Stopwords (the/is/into) go; cs_script/vjs_c/vpk stay verbatim; cs_script implies cs2."""
    r = rewrite("recompile the cs_script (the .vjs_c is baked into the vpk)")

    words = set(r.query.split())
    assert {"cs_script", "vjs_c", "vpk"} <= words
    assert r.game == "cs2", "cs_script is a CS2-only script API"
    for stop in ("the", "is", "into"):
        assert stop in r.dropped
        assert stop not in words


def test_real_question_convar_passes_through_essentially_unchanged():
    """'what is mp_roundtime' -> the convar itself. SURGICAL: a keyword-shaped token is not mangled,
    and the CS2-only convar also settles the game."""
    r = rewrite("what is mp_roundtime")

    assert r.query == "mp_roundtime"
    assert r.game == "cs2"
    assert "what" in r.dropped and "is" in r.dropped


# ------------------------------------------------------------ surgical pass-through


def test_already_keyword_like_input_is_not_transformed():
    """A query that is already keyword-like must pass through essentially unchanged (requirement 5)."""
    for text in ("prop_physics", "breakable prop", "mp_roundtime", "mp_roundvtime"):
        r = rewrite(text)
        assert r.query == text, text
        assert r.dropped == ()
        assert r.notes == ()


# --------------------------------------------------------------- curated synonyms
# Each entry is here for a reason; a synonym that is wrong is worse than none.
# The test names the reason. Kept deliberately small.


def test_synonym_see_through_walls_is_the_occluder_term():
    """Engine docs index the visibility system as 'occluder'; users say 'see through walls'."""
    r = rewrite("how do i see through walls")
    assert "occluder" in r.query.split()
    assert any("see through walls -> occluder" in n for n in r.notes)


def test_synonym_behind_a_wall_is_also_the_occluder():
    r = rewrite("hide an entity behind a wall")
    assert "occluder" in r.query.split()
    assert any("behind a wall -> occluder" in n for n in r.notes)


def test_synonym_player_info_maps_to_panorama_hud():
    """The HUD is the panorama UI; 'player info' is the user's name for it."""
    r = rewrite("player info")
    assert r.query == "panorama hud"


def test_synonym_players_info_ui_is_the_spectator_panel():
    r = rewrite("players info ui")
    assert "spectator" in r.query.split()
    assert any("players info ui -> spectator" in n for n in r.notes)


def test_synonym_red_block_texture_is_a_prop():
    """A user describing the red dev-block texture means the prop it is painted on."""
    r = rewrite("red block texture")
    assert "prop" in r.query.split()
    assert any("red block texture -> prop" in n for n in r.notes)


def test_synonym_see_through_walls_is_xray_in_spectator_context():
    """Row ``cs2-spectator-xray-walls``. The answering document is the convar ``spec_show_xray``,
    whose description reads: "If set to 1, you can see player outlines and name IDs through walls -
    who you can see depends on your team and mode." Its term is ``xray`` (the key ``spec_show_xray``),
    NOT the map-compile-time ``occluder`` — 'occluder' retrieves ``r_csgo_sky_occluder`` (a skybox
    convar: a miss). So a SPECTATOR/player/outline intent resolves the phrase to xray."""
    q = "How do I make spectators see enemy player outlines through walls when watching a CS2 match?"
    r = rewrite(q)

    words = set(r.query.split())
    assert "xray" in words, "spectator intent => the xray sense (spec_show_xray)"
    assert "occluder" not in words, "occluder is the map/vis sense, not the spectator one"
    assert r.game == "cs2"
    assert any("through walls -> xray" in n for n in r.notes)
    assert any("spec_show_xray" in n for n in r.notes), "sense decision must be auditable"


def test_see_through_walls_stays_occluder_for_the_map_vis_sense():
    """Rows ``cs2-prop-static-visoccluder`` / ``cs2-hammer-visibility-doc``: in a MAP/VIS context
    (visibility, geometry, compile) the phrase is the precomputed-visibility ``occluder`` — the
    "Vis Occluder" field on server:prop_static. The sense must not collapse onto xray."""
    r = rewrite("which geometry contributes to vis, can you see through walls and occlude")

    words = set(r.query.split())
    assert "occluder" in words, "map/vis intent => the occluder sense"
    assert "xray" not in words
    assert any("sense=map/vis" in n for n in r.notes)


def test_synonym_load_a_mod_and_dll_are_addons():
    """Source 2 ships mods as addons; a .dll is a code addon."""
    r1 = rewrite("how to load a mod")
    assert "addon" in r1.query.split()
    assert any("load a mod -> addon" in n for n in r1.notes)

    r2 = rewrite("dll")
    assert r2.query == "addon"
    assert any("dll -> addon" in n for n in r2.notes)


# ------------------------------------------------------------------- game detection


def test_game_detection_explicit_words():
    assert rewrite("deadlock hero ability").game == "deadlock"
    assert rewrite("citadel map format").game == "deadlock"
    assert rewrite("csgo smoke lineup").game == "cs2"
    assert rewrite("counter-strike spectator").game == "cs2"


def test_game_detection_cs2_only_vocabulary():
    assert rewrite("dust2 callouts").game == "cs2"
    assert rewrite("mp_roundvtime").game == "cs2"


def test_game_is_none_when_genuinely_ambiguous_or_absent():
    assert rewrite("how do i make a breakable prop").game is None
    assert rewrite("cs2 deadlock crossplay").game is None, "both named — do not guess"


# ------------------------------------------------- game name is a FILTER, not a query token
# Under FTS5 AND, every token must match, so a token that is a filter rather than a keyword
# actively destroys hits. The game NAME (cs2/csgo/deadlock/...) is already expressed by Rewrite.game,
# so emitting it as a query token is a pure loss. Measured on row cs2-spectators-max:
#   q='spectators allowed match'      game=cs2 -> total 1, rank 1 (mp_spectators_max)
#   q='spectators allowed cs2 match'  game=cs2 -> total 0   <-- the bare name kills it


def test_game_name_is_a_filter_not_a_query_token():
    """Row ``cs2-spectators-max``: the oracle 'max spectators allowed match' (game=cs2) hits rank 1
    while the rewriter's 'spectators allowed cs2 match' misses. The fix: drop the bare game name from
    the query, keep setting ``game``."""
    r = rewrite("How many spectators are allowed in a CS2 match?")

    assert r.game == "cs2", "the game must still be set"
    assert "cs2" not in r.query.split(), "the game name is a filter, not a keyword"
    assert "cs2" in r.dropped
    assert {"spectators", "allowed", "match"} <= set(r.query.split())


def test_game_name_filter_is_stripped_for_every_detected_variant():
    for prose, game, names in (
        ("switch the spectator camera mode in Deadlock", "deadlock", ("deadlock",)),
        ("counter-strike spectator list", "cs2", ("counter", "strike")),
        ("csgo smoke lineup", "cs2", ("csgo",)),
        ("citadel spectator tools", "deadlock", ("citadel",)),
    ):
        r = rewrite(prose)
        assert r.game == game, prose
        for name in names:
            assert name not in r.query.split(), (prose, name)
            assert name in r.dropped, (prose, name)


def test_ambiguous_game_names_are_not_stripped():
    """When BOTH games are named the value is undetermined, so neither name established it — stripping
    would itself be a guess. Leave the tokens (the search is simply unscoped)."""
    r = rewrite("cs2 deadlock crossplay")
    assert r.game is None
    assert {"cs2", "deadlock"} <= set(r.query.split())


def test_identifiers_containing_a_game_name_are_kept():
    """An identifier the user typed is not the bare game name and must survive verbatim."""
    r1 = rewrite("citadel_spectator_mode")
    assert r1.query == "citadel_spectator_mode"

    r2 = rewrite("recompile the cs_script")
    assert "cs_script" in r2.query.split()
    assert r2.game == "cs2"


# --------------------------------------------------------------------------- shape


def test_output_is_capped_and_prefers_specific_tokens():
    r = rewrite("breakable occluder panorama spectators addon entity trigger prop convar")
    assert len(r.query.split()) <= 6
    assert {"occluder", "panorama"} <= set(r.query.split())


def test_stopword_set_has_no_domain_terms():
    for term in ("prop", "vmap", "occluder", "addon", "panorama", "spectator", "entity"):
        assert term not in STOPWORDS, term


def test_vis_question_keeps_the_domain_vocabulary():
    """Row ``cs2-hammer-visibility-doc``: 'How does VIS work in Source 2 and which geometry
    contributes to it?' The corpus keeps this under the terms visible on
    s2wiki:/EngineTools/HammerEditor/visibility — "all meshes are VIS contributors" — so keep
    ``vis``/``geometry`` and resolve ``contributes`` to the corpus noun ``contributor``. Measured:
    q='vis source 2 geometry' game=None -> rank 1; q='vis contributor geometry' -> rank 1;
    the raw 'vis work source 2 geometry contributes' -> miss (no contributing document)."""
    r = rewrite("How does VIS work in Source 2 and which geometry contributes to it?")

    words = set(r.query.split())
    assert "vis" in words
    assert "geometry" in words
    assert "contributor" in words, "the corpus noun is 'VIS contributors', not the verb"
    assert "work" not in words, "'how does X work' is filler"
    assert "work" in r.dropped
    assert any("contributes -> contributor" in n for n in r.notes)


def test_empty_input_is_safe():
    r = rewrite("")
    assert r.query == "" and r.game is None and r.dropped == () and r.notes == ()


# ---------------------------------------------------------------------- the CLI verb


def _run_cli(monkeypatch, tmp_path, argv):
    from s2docs import cli
    from s2docs.config import get_settings

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("S2DOCS_DB", str(tmp_path / "data" / "s2docs.db"))
    get_settings.cache_clear()
    return cli.main(argv)


def test_cli_learning_rewrite_human(tmp_path, monkeypatch, capsys):
    rc = _run_cli(monkeypatch, tmp_path, ["learning", "rewrite", "how do I make a breakable prop"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "breakable prop" in out
    assert "game:" in out


def test_cli_learning_rewrite_json(tmp_path, monkeypatch, capsys):
    rc = _run_cli(monkeypatch, tmp_path,
                  ["learning", "rewrite", "can you see through walls in cs2", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["schema"] == "s2docs/v1"
    assert out["rewrite"]["game"] == "cs2"
    assert "occluder" in out["rewrite"]["query"].split()
