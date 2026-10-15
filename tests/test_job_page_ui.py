"""Job page: the saved-verdict regression, the SETTING UP / CLEANING UP countdown, and screenshots
tied to steps/verdicts — executed against the shipped app.js with node, plus the wiring markers a
headless run cannot exercise.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from s2docs.api.app import create_app
from s2docs.db.connection import Database

NODE = shutil.which("node")


@pytest.fixture
def client(tmp_path):
    db = Database(tmp_path / "data" / "s2docs.db")
    with TestClient(create_app(db.path)) as c:
        yield c


def _js(client) -> str:
    return client.get("/app.js").text


# ------------------------------------------------------ wiring markers (no node)
def test_job_forms_are_delegated_and_native_submit_is_impossible(client):
    """The verdict/screenshot forms are bound by ONE delegated submit listener, never per-form, and
    each form also carries method/action so a native GET submit cannot navigate the page away."""
    js = _js(client)
    # one delegated submit listener, wired once in boot
    assert "function bindJobFormDelegation(" in js
    assert "bindJobFormDelegation();" in js
    assert 'document.addEventListener("submit"' in js
    assert 'form.matches("form[data-job-verdict]")' in js and 'form.matches("form[data-job-shot]")' in js
    # defense in depth: the forms cannot do a native submit
    assert 'method="post" action="javascript:void 0"' in js
    assert js.count('method="post" action="javascript:void 0"') >= 2
    # the old per-form binding is GONE from bindFilters
    filters = js.split("function bindFilters", 1)[1].split("\n}\n", 1)[0]
    assert "bindJobVerdict" not in filters and "bindJobShot" not in filters
    # handlers take (form, ev) and preventDefault themselves (no navigation even without a submitter)
    assert "async function bindJobVerdict(form, ev)" in js and "async function bindJobShot(form, ev)" in js
    # the stale ?verdict= tab is stripped (never auto-submitted) and a dismissible notice is shown
    assert "function stripStaleVerdictQuery(" in js and "function showStaleVerdictNotice(" in js
    assert "history.replaceState" in js and "staleVerdictNotice" in js
    assert "please re-send" in js


def test_job_page_organized_layout_and_shot_markers(client):
    """The reorganized hierarchy and the screenshot wiring all exist in app.js / style.css."""
    js = _js(client)
    css = client.get("/style.css").text
    # sections, in order
    for name in (
        "function jobHeader(",
        "function jobNow(",
        "function jobResultsTable(",
        "function jobSummaryBlocks(",
        "function jobIntake(",
        "function jobEvidence(",
        "function jobLinks(",
        "function jobTimelineGrouped(",
        "function loadJobDetail(",
    ):
        assert name in js, name
    # the header carries the job URL copy button; the lightbox + copy are delegated clicks
    assert "Copy job URL" in js and "function jobHeader(" in js
    assert "function openLightbox(" in js and "function closeLightbox(" in js and "function copyText(" in js
    assert 'closest("[data-copy]")' in js and 'closest("[data-shot]")' in js and 'closest("[data-vx-goto]")' in js
    # SETTING UP / CLEANING UP phases
    assert "function jobActivePhase(" in js and "function vxPhaseBanner(" in js
    assert "SETTING UP" in js and "CLEANING UP" in js and "data-vx-phase" in js
    assert "data-cd-sec" in js and "opens in" in js and "window closed" in js
    # screenshots tied to steps/verdicts
    assert "function jobScreenshots(" in js and "function shotThumb(" in js and "function shotCaption(" in js
    assert "screenshot at verdict" in js and "a screenshot will be saved with your verdict" in js
    assert "grouped by step" in js and "unassigned" in js
    # the screenshot-request control lives in the NOW card with a pending state
    assert "requested — waiting for the driver" in js and "data-job-shot" in js
    # the timeline folds routine lines and keeps important levels visible
    assert "TIMELINE_NOISE" in js and "isTimelineNoise" in js and "routine" in js
    assert "user-feedback" in js and "ledger-correction" in js
    # the layout CSS the reorg relies on
    for sel in (
        ".job-header",
        ".jh-meta",
        ".vx-phase",
        ".vx-phase-cd",
        ".shot-grid",
        ".shot-btn",
        ".lightbox",
        ".tl-group",
        ".sm-grid",
        ".ev-step",
        ".stale-verdict",
    ):
        assert sel in css, sel
    assert "@media (max-width: 1100px)" in css


# ------------------------------------------------- verdict-form regression (node)
@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_verdict_form_survives_live_rerender_in_node(client):
    """Reproduce the P0 bug: after a live re-render swaps the form's markup, a submit must still be
    handled (POST to /verdict) and must NOT navigate. The delegated listener is attached to the
    document, so a brand-new form node is handled just like the original."""
    assert NODE is not None
    js = _js(client)
    block = js.split("// job-form-delegation:start", 1)[1].split("// job-form-delegation:end", 1)[0]
    script = (
        """
const esc = (v) => String(v ?? "");
const enc = encodeURIComponent;
let liveDirty = false;
const posted = [];
const refreshed = [];
let routed = 0;
async function postJson(url, body) { posted.push([url, body]); return { ok: true, body: {} }; }
async function refreshJobLive(id) { refreshed.push(id); }
function route() { routed += 1; }
const submitListeners = [];
const document = { addEventListener: (t, fn) => { if (t === "submit") submitListeners.push(fn); } };
"""
        + block
        + """
bindJobFormDelegation();
if (submitListeners.length !== 1) throw new Error("expected exactly one delegated submit listener");
function verdictForm() {
  const notes = { value: "looks good" };
  const btns = ["pass", "fail", "skip"].map((v) => ({ name: "verdict", value: v,
    innerHTML: "btn-" + v, disabled: false }));
  const result = { innerHTML: "" };
  return {
    tagName: "FORM", dataset: { jobVerdict: "j-x" }, elements: { notes },
    matches: (sel) => sel === "form[data-job-verdict]",
    querySelectorAll: (sel) => (sel.includes("button") ? btns : []),
    querySelector: (sel) => (sel === ".result" ? result : null),
    _btns: btns,
  };
}
function shotForm() {
  const note = { value: "capture the roof" };
  const result = { innerHTML: "" };
  return {
    tagName: "FORM", dataset: { jobShot: "j-x" }, elements: { note },
    matches: (sel) => sel === "form[data-job-shot]",
    querySelector: (sel) => (sel === ".result" ? result : null),
  };
}
(async () => {
  // The live re-render created a FRESH form node (the old per-form listener would be gone here).
  const vf = verdictForm();
  let prevented = false;
  submitListeners[0]({ target: vf, submitter: { value: "pass", innerHTML: "OK" },
    preventDefault: () => { prevented = true; } });
  const sf = shotForm();
  let preventedShot = false;
  submitListeners[0]({ target: sf, submitter: { value: "shot" }, preventDefault: () => { preventedShot = true; } });
  await new Promise((r) => setTimeout(r, 5));
  console.log(JSON.stringify({ posted, refreshed, routed, prevented, preventedShot,
    allDisabled: vf._btns.every((b) => b.disabled), notesCleared: vf.elements.notes.value === "" }));
})();
"""
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # the verdict POST happened (the bug lost it), to the right URL with the pressed value
    assert out["posted"][0] == ["/api/jobs/j-x/verdict", {"verdict": "pass", "notes": "looks good", "source": "web"}]
    # the screenshot request POST happened too
    assert out["posted"][1] == ["/api/jobs/j-x/screenshot-request", {"note": "capture the roof"}]
    # no native navigation: both submits were preventDefault'd
    assert out["prevented"] is True and out["preventedShot"] is True
    assert out["routed"] == 1  # only the screenshot handler re-routes; the verdict refreshes live
    assert out["refreshed"] == ["j-x"]
    assert out["allDisabled"] is True and out["notesCleared"] is True


# --------------------------------------------- SETTING UP / CLEANING UP (node)
@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_phase_countdown_renders_in_node(client):
    """jobActivePhase / vxPhaseBanner / nowTestingCard: a SETTING UP event shows the banner with a
    ticking countdown and disabled verdict buttons ('opens in …'); a CLEANING UP event shows the
    next-test line and 'window closed'; a later prompt/verdict supersedes the phase."""
    assert NODE is not None
    js = _js(client)
    block = "const VERDICT_SET" + js.split("const VERDICT_SET", 1)[1].split("function jobDetail", 1)[0]
    prologue = (
        'const esc = (v) => String(v ?? "");\n'
        'const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;\n'
        'const fmtTime = (iso) => String(iso ?? "—");\n'
        "const parseTs = (iso) => Date.parse(String(iso));\n"
        'const CT_ZONE = "America/Chicago";\n'
    )
    plan = {
        "id": 1,
        "level": "info",
        "phase": "plan",
        "message": "plan posted",
        "data": {
            "plan": [
                {"id": "s1", "index": 1, "title": "t1", "strip_name": "n1"},
                {"id": "s2", "index": 2, "title": "t2", "strip_name": "n2"},
            ]
        },
    }
    p1 = {
        "id": 10,
        "level": "info",
        "phase": "s1",
        "message": "STEP 1/2",
        "data": {
            "prompt": "p1",
            "look_for": "l1",
            "step_index": 1,
            "step_total": 2,
            "strip_name": "n1",
            "respond_hint": "!pass",
            "deadline_utc": "2026-10-15T23:00:00Z",
        },
    }
    v1 = {
        "id": 11,
        "level": "milestone",
        "phase": None,
        "message": "verdict pass",
        "data": {"verdict": "pass", "source": "chat", "notes": "ok", "step": "s1", "evidence_id": "e-1"},
    }
    setup = {
        "id": 12,
        "level": "info",
        "phase": "setup",
        "message": "SETTING UP TEST 2/2 area presets (10 s)",
        "data": {
            "countdown_s": 10,
            "until": "2026-10-15T22:00:10Z",
            "step": "s2",
            "step_index": 2,
            "step_total": 2,
            "name": "area presets",
            "next": "TEST 2 area presets",
        },
    }
    cleanup = {
        "id": 13,
        "level": "info",
        "phase": "cleanup",
        "message": "CLEANING UP TEST 1/2",
        "data": {
            "countdown_s": 6,
            "until": "2026-10-15T22:00:06Z",
            "step": "s1",
            "step_index": 1,
            "step_total": 2,
            "next": "TEST 2 area presets",
        },
    }
    v2 = {
        "id": 14,
        "level": "milestone",
        "phase": None,
        "message": "verdict fail",
        "data": {"verdict": "fail", "source": "web", "step": "s2"},
    }
    cs = {
        "phase": "s2",
        "step_index": 2,
        "step_total": 2,
        "prompt": "p2",
        "look_for": "l2",
        "deadline_utc": "2026-10-15T23:10:00Z",
        "asked_at": "2026-10-15T22:00:00Z",
        "respond_hint": "!pass",
    }

    def job(events):
        return {"id": "j", "status": "running", "current_step": cs, "events": events}

    jsetup = job([plan, p1, v1, setup])
    jclean = job([plan, p1, v1, cleanup])
    jnone = job([plan, p1, v1])
    jsuper = job([plan, p1, setup, v2])
    script = (
        prologue
        + block
        + "\nconst JSETUP = "
        + json.dumps(jsetup)
        + ";\n"
        + "const JCLEAN = "
        + json.dumps(jclean)
        + ";\n"
        + "const JNONE = "
        + json.dumps(jnone)
        + ";\n"
        + "const JSUPER = "
        + json.dumps(jsuper)
        + ";\n"
        + "console.log(JSON.stringify({\n"
        + "  setupBanner: vxPhaseBanner(JSETUP),\n"
        + "  setupCard: nowTestingCard(JSETUP),\n"
        + "  cleanBanner: vxPhaseBanner(JCLEAN),\n"
        + "  cleanCard: nowTestingCard(JCLEAN),\n"
        + "  noneBanner: vxPhaseBanner(JNONE),\n"
        + "  superBanner: vxPhaseBanner(JSUPER),\n"
        + "  superActive: jobActivePhase(JSUPER),\n"
        + "}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # SETTING UP: the banner names the test + a live countdown
    assert "SETTING UP" in out["setupBanner"] and "TEST 2/2" in out["setupBanner"]
    assert 'data-cd-sec="2026-10-15T22:00:10Z"' in out["setupBanner"] and "area presets" in out["setupBanner"]
    # the verdict buttons are disabled and read 'opens in'
    assert "disabled" in out["setupCard"] and "opens in" in out["setupCard"]
    assert "vx-gate" in out["setupCard"]
    # CLEANING UP: the next-test line + 'window closed'
    assert "CLEANING UP" in out["cleanBanner"] and "→ next: TEST 2 area presets" in out["cleanBanner"]
    assert "window closed" in out["cleanCard"]
    # no phase -> no banner; a verdict AFTER the setup event supersedes it
    assert out["noneBanner"] == ""
    assert out["superBanner"] == "" and out["superActive"] is None


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_countdown_ticker_renders_seconds_in_node(client):
    """updateCountdowns drives the [data-cd-sec] readouts as whole seconds ('8s' -> '0s')."""
    assert NODE is not None
    js = _js(client)
    block = "function updateCountdowns" + js.split("function updateCountdowns", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    script = (
        """
const NOW = Date.parse("2026-10-15T22:00:00Z");
Date.now = () => NOW;
const els = [
  { dataset: { cdSec: "2026-10-15T22:00:08Z" }, textContent: "" },
  { dataset: { cdSec: "2026-10-15T21:59:59Z" }, textContent: "" },
];
const document = { querySelectorAll: (sel) => (sel === "[data-cd-sec]" ? els : []) };
"""
        + block
        + """
updateCountdowns();
console.log(JSON.stringify(els.map((e) => e.textContent)));
"""
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["8s", "0s"]


# ------------------------------------------- screenshots tied to steps (node)
@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_screenshot_step_verdict_rendering_in_node(client):
    """jobScreenshots folds the new + old event shapes; the RESULTS table and EVIDENCE grid group by
    step and mark the verdict shot; the NOW card shows the camera line and links the verdict shot."""
    assert NODE is not None
    js = _js(client)
    block = "const VERDICT_SET" + js.split("const VERDICT_SET", 1)[1].split("function jobDetail", 1)[0]
    prologue = (
        'const esc = (v) => String(v ?? "");\n'
        'const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;\n'
        'const fmtTime = (iso) => String(iso ?? "—");\n'
        "const parseTs = (iso) => Date.parse(String(iso));\n"
        'const CT_ZONE = "America/Chicago";\n'
    )
    plan = {
        "id": 1,
        "level": "info",
        "phase": "plan",
        "message": "plan",
        "data": {
            "plan": [
                {"id": "s1", "index": 1, "title": "t1", "strip_name": "n1", "strip_purpose": "p1", "strip_expect": "e1"}
            ]
        },
    }
    p1 = {
        "id": 10,
        "level": "info",
        "phase": "s1",
        "message": "STEP 1/1",
        "data": {
            "prompt": "p1",
            "look_for": "l1",
            "step_index": 1,
            "step_total": 1,
            "strip_name": "n1",
            "strip_purpose": "p1",
            "strip_expect": "e1",
            "deadline_utc": "2026-10-15T23:00:00Z",
            "respond_hint": "!pass",
        },
    }
    v1 = {
        "id": 11,
        "level": "milestone",
        "phase": None,
        "message": "verdict pass",
        "data": {"verdict": "pass", "source": "chat", "notes": "ok", "step": "s1", "evidence_id": "e-1"},
    }
    sh1 = {
        "id": 20,
        "level": "screenshot",
        "phase": "s1",
        "message": "shot",
        "created_at": "2026-10-15T22:00:05Z",
        "data": {
            "url": "/api/artifacts/j/1.png",
            "md5": "aa",
            "bytes": 10,
            "step": "s1",
            "step_index": 1,
            "step_total": 1,
            "reason": "ready",
            "seq": 1,
            "full_res": False,
            "width": 640,
            "height": 360,
        },
    }
    sh2 = {
        "id": 21,
        "level": "screenshot",
        "phase": "s1",
        "message": "shot",
        "created_at": "2026-10-15T22:00:09Z",
        "data": {
            "url": "/api/artifacts/j/2.png",
            "md5": "bb",
            "bytes": 20,
            "step": "s1",
            "step_index": 1,
            "step_total": 1,
            "reason": "verdict",
            "seq": 2,
            "full_res": True,
            "width": 1920,
            "height": 1080,
            "verdict": {"verdict": "pass", "source": "chat", "confirm_id": "e-1", "late": False},
        },
    }
    sh3 = {
        "id": 22,
        "level": "screenshot",
        "phase": None,
        "message": "shot",
        "created_at": "2026-10-15T21:59:00Z",
        "data": {"url": "/api/artifacts/j/old.jpg", "md5": "cc", "bytes": 5},
    }
    base = {
        "id": "j",
        "status": "passed",
        "current_step": None,
        "artifacts": [],
        "summary": None,
        "params": {},
        "links": [],
        "web_url": None,
    }
    jdone = {**base, "events": [plan, p1, v1, sh1, sh2, sh3]}
    cs = {
        "phase": "s1",
        "step_index": 1,
        "step_total": 1,
        "prompt": "p1",
        "look_for": "l1",
        "deadline_utc": "2026-10-15T23:00:00Z",
        "asked_at": "2026-10-15T22:00:00Z",
        "respond_hint": "!pass",
    }
    jrun = {**base, "status": "running", "current_step": cs, "events": [plan, p1, v1, sh1, sh2]}
    jrun_noverdictshot = {**base, "status": "running", "current_step": cs, "events": [plan, p1, v1, sh1]}
    script = (
        prologue
        + block
        + "\nconst JDONE = "
        + json.dumps(jdone)
        + ";\n"
        + "const JRUN = "
        + json.dumps(jrun)
        + ";\n"
        + "const JRUN2 = "
        + json.dumps(jrun_noverdictshot)
        + ";\n"
        + "const S = jobScreenshots(JDONE);\n"
        + "console.log(JSON.stringify({\n"
        + "  seq: S.map((s) => s.seq), step: S.map((s) => s.step), reason: S.map((s) => s.reason),\n"
        + "  vwidth: S[1].width,\n"
        + "  vfull: S[1].full_res,\n"
        + "  vconfirm: S[1].verdict && S[1].verdict.confirm_id,\n"
        + "  caption: shotCaption(S[1]),\n"
        + "  thumb: shotThumb(S[1]),\n"
        + "  results: jobResultsTable(JDONE),\n"
        + "  evidence: jobEvidence(JDONE),\n"
        + "  camRun: nowTestingCard(JRUN),\n"
        + "  camNoVerdictShot: nowTestingCard(JRUN2),\n"
        + "}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # folding the two event shapes: seq order, the old event unassigned
    assert out["seq"] == [1, 2, 3] and out["step"] == ["s1", "s1", None]
    assert out["reason"] == ["ready", "verdict", None]
    assert out["vwidth"] == 1920 and out["vfull"] is True and out["vconfirm"] == "e-1"
    # caption spec: '#seq · reason · HH:MM:SS · step' + verdict/source; thumb class flags the verdict
    import re as _re

    assert _re.fullmatch(r"#2 · verdict · \d\d:\d\d:\d\d · s1 · pass via chat", out["caption"]), out["caption"]
    assert 'class="shot shot-verdict"' in out["thumb"] and 'class="shot-btn"' in out["thumb"]
    # RESULTS: a Shots column, the verdict shot beside the badge, both step-s1 thumbs present
    assert "<th>Shots</th>" in out["results"]
    assert "screenshot at verdict" in out["results"]
    assert "/api/artifacts/j/1.png" in out["results"] and "/api/artifacts/j/2.png" in out["results"]
    # EVIDENCE: grouped by step, the old shot under 'unassigned'
    assert "grouped by step" in out["evidence"] and "ev-step" in out["evidence"]
    assert ">s1 " in out["evidence"] and ">unassigned " in out["evidence"]
    # NOW card: the camera line with the latest thumb + reason
    assert "vx-camera" in out["camRun"] and "shots this test" in out["camRun"]
    assert "screenshot at verdict" in out["camRun"]  # a matching verdict shot is linked
    assert "a screenshot will be saved with your verdict" in out["camNoVerdictShot"]
