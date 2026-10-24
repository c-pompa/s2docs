"""The CI rule registry keeps the two pipelines apart and every check falsifiable.

ci/feedback-rules.yaml assigns each check to exactly one of two pipelines: hermes-pr-feedback-ci
(rules an upstream hermes-agent reviewer asked for) and cpompa-hermes-ci (house standards). These
tests run the registry validator (scripts/ci/registry.py) on the real registry and on planted bad
ones, and run the falsification harness (scripts/ci/run.sh falsify) on planted checks. The central
guarantee is non-duplication: no rule id, check script, fixture set or concern can be claimed by both
pipelines. See docs/FEEDBACK_CI.md.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI_DIR = ROOT / "scripts" / "ci"
REVIEW = "https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162"
REVIEW_ITEMS = ["1a", "1b", "1c", "1d", "2a", "2b", "2c", "2d", "3"]  # review 5631127162, as split in the registry
PIPELINES = {
    "hermes-pr-feedback-ci": {"origin": "reviewer", "id_prefix": "HPF-"},
    "cpompa-hermes-ci": {"origin": "house", "id_prefix": "CHC-"},
}


def _load_registry_module():
    spec = importlib.util.spec_from_file_location("ci_registry", CI_DIR / "registry.py")
    assert spec is not None and spec.loader is not None, "scripts/ci/registry.py is missing"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


registry = _load_registry_module()


@pytest.fixture(scope="module")
def real():
    return registry.load()


# ------------------------------------------------------------------ the real registry
def test_real_registry_is_valid(real):
    assert set(real.pipelines) == set(PIPELINES)
    for name, spec in PIPELINES.items():
        assert real.pipelines[name]["origin"] == spec["origin"]
        assert real.for_pipeline(name), f"{name} has no rules"


@pytest.mark.parametrize("attr", ["id", "script", "fixtures", "concern"])
def test_no_rule_is_claimed_by_both_pipelines(real, attr):
    owners: dict[str, set[str]] = {}
    for rule in real.rules:
        owners.setdefault(getattr(rule, attr), set()).add(rule.pipeline)
    shared = {value: sorted(p) for value, p in owners.items() if len(p) > 1}
    assert not shared, f"{attr} claimed by both pipelines: {shared}"
    values = [getattr(rule, attr) for rule in real.rules]
    assert len(values) == len(set(values)), f"{attr} claimed by more than one rule"


def test_each_rule_lives_in_its_own_pipeline(real):
    for rule in real.rules:
        assert rule.id.startswith(PIPELINES[rule.pipeline]["id_prefix"]), rule.id
        assert rule.script.startswith(f"scripts/ci/checks/{rule.pipeline}/{rule.id}-"), rule.script
        assert rule.fixtures == f"ci/fixtures/{rule.id}", rule.fixtures


def test_every_check_script_and_fixture_set_on_disk_is_registered(real):
    on_disk = {p.relative_to(ROOT).as_posix() for p in (ROOT / "scripts" / "ci" / "checks").rglob("*.sh")}
    assert on_disk == {rule.script for rule in real.rules}
    fixture_dirs = {p.name for p in (ROOT / "ci" / "fixtures").iterdir() if p.is_dir()}
    assert fixture_dirs == {rule.id for rule in real.rules}


def test_reviewer_rules_trace_to_their_review_and_house_rules_do_not(real):
    for rule in real.for_pipeline("hermes-pr-feedback-ci"):
        assert rule.source.startswith("https://github.com/NousResearch/hermes-agent/"), rule.id
        assert rule.feedback_id and rule.reviewer and rule.quote and rule.quote.strip(), rule.id
    for rule in real.for_pipeline("cpompa-hermes-ci"):
        assert rule.source == "house rule" or registry.HOUSE_FINDING_RE.fullmatch(rule.source), rule.id
        assert (rule.feedback_id, rule.reviewer, rule.quote, rule.pinned_server) == (None, None, None, False), rule.id


def test_review_5631127162_is_covered_item_by_item(real):
    items = sorted(
        r.feedback_id.rsplit("/", 1)[1] for r in real.rules if r.feedback_id and "/review-5631127162/" in r.feedback_id
    )
    assert items == sorted(REVIEW_ITEMS), "every item of the review maps to exactly one hermes-pr-feedback-ci rule"
    assert {r.source for r in real.rules if r.feedback_id and "/review-5631127162/" in r.feedback_id} == {REVIEW}


def test_pinned_server_rules_are_reviewer_rules_and_exclude_the_pin_check(real):
    pinned = [r for r in real.rules if r.pinned_server]
    assert pinned, "HPF-008 needs at least one pinned_server rule to verify the pin against"
    assert all(r.pipeline == "hermes-pr-feedback-ci" for r in pinned)
    assert "HPF-008" not in {r.id for r in pinned}, "the pin check cannot verify itself"


def test_registry_cli_validates_the_real_registry():
    proc = subprocess.run([sys.executable, str(CI_DIR / "registry.py"), "validate"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("registry ok: ")


@pytest.mark.parametrize("pipeline", sorted(PIPELINES))
def test_runner_runs_exactly_what_the_registry_assigns(real, pipeline):
    proc = subprocess.run(
        ["bash", str(CI_DIR / "run.sh"), "list", pipeline],
        capture_output=True, text=True, env={**os.environ, "CI_PYTHON": sys.executable},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == [r.id for r in real.for_pipeline(pipeline)]


# ------------------------------------------------------------------ planted bad registries
def _check(root: Path, pipeline: str, rid: str, *, source: str, feedback: str | None = None,
           header_pipeline: str | None = None) -> str:
    path = root / "scripts" / "ci" / "checks" / pipeline / f"{rid}-planted.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = ["#!/usr/bin/env bash", f"# rule: {rid}", f"# pipeline: {header_pipeline or pipeline}"]
    head += [f"# feedback: {feedback}"] if feedback else []
    head += [f"# source: {source}", "# enforces: planted", "exit 0", ""]
    path.write_text("\n".join(head), encoding="utf-8")
    path.chmod(0o755)
    return path.relative_to(root).as_posix()


def _fixtures(root: Path, rid: str, cases=("reject-planted.sh",)) -> str:
    folder = root / "ci" / "fixtures" / rid
    folder.mkdir(parents=True, exist_ok=True)
    for case in cases:
        (folder / case).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return folder.relative_to(root).as_posix()


def _reviewer(root: Path, rid: str = "HPF-901", concern: str = "planted-review-concern", **over) -> dict:
    fid = over.pop("feedback_id", "hermes-agent#143393/review-5631127162/9z")
    rule = {
        "id": rid, "pipeline": "hermes-pr-feedback-ci", "title": "planted", "concern": concern, "source": REVIEW,
        "feedback_id": fid, "reviewer": "teknium1", "quote": "planted quote", "enforces": "planted",
        "script": _check(root, "hermes-pr-feedback-ci", rid, source=REVIEW, feedback=fid),
        "fixtures": _fixtures(root, rid),
    }
    rule.update(over)
    return rule


def _house(root: Path, rid: str = "CHC-PLANTED", concern: str = "planted-house-concern", **over) -> dict:
    rule = {
        "id": rid, "pipeline": "cpompa-hermes-ci", "title": "planted", "concern": concern, "source": "house rule",
        "enforces": "planted", "script": _check(root, "cpompa-hermes-ci", rid, source="house rule"),
        "fixtures": _fixtures(root, rid),
    }
    rule.update(over)
    return rule


def _problems(data: dict, root: Path) -> list[str]:
    with pytest.raises(registry.RegistryError) as info:
        registry.validate(data, root)
    return info.value.problems


def _doc(*rules: dict, pipelines: dict | None = None) -> dict:
    return {"version": 1, "pipelines": pipelines or PIPELINES, "rules": list(rules)}


def test_planted_minimal_registry_is_valid(tmp_path):
    reg = registry.validate(_doc(_reviewer(tmp_path), _house(tmp_path)), tmp_path)
    assert [r.id for r in reg.rules] == ["HPF-901", "CHC-PLANTED"]


def test_a_house_rule_may_name_the_security_finding_it_answers(tmp_path):
    finding = "coordinator security finding 2026-10-23: a runner mounted /var/run/docker.sock into job containers"
    house = _house(tmp_path, source=finding)
    _check(tmp_path, "cpompa-hermes-ci", "CHC-PLANTED", source=finding)
    reg = registry.validate(_doc(_reviewer(tmp_path), house), tmp_path)
    assert reg.get("CHC-PLANTED").source == finding


def test_the_docker_socket_rule_answers_the_2026_10_23_finding(real):
    rule = real.get("CHC-RUNNER-NO-DOCKER-SOCK")
    assert rule.pipeline == "cpompa-hermes-ci"
    assert rule.source.startswith("coordinator security finding 2026-10-23: ")
    assert "/var/run/docker.sock" in rule.source


def test_the_same_rule_id_in_both_pipelines_is_refused(tmp_path):
    reviewer = _reviewer(tmp_path)
    copy = _house(tmp_path, rid="HPF-901", concern="another-concern")  # the reviewer rule's id, claimed by the house
    problems = _problems(_doc(reviewer, copy), tmp_path)
    assert any("rule ids claimed by both" in p and "HPF-901" in p for p in problems), problems
    assert any("id 'HPF-901' is claimed by rule HPF-901 (hermes-pr-feedback-ci)" in p for p in problems), problems


def test_one_script_claimed_by_both_pipelines_is_refused(tmp_path):
    reviewer = _reviewer(tmp_path)
    house = _house(tmp_path, script=reviewer["script"])
    problems = _problems(_doc(reviewer, house), tmp_path)
    assert any(f"script {reviewer['script']!r} is claimed by rule HPF-901" in p for p in problems), problems


def test_one_concern_claimed_by_both_pipelines_is_refused(tmp_path):
    # Different ids and scripts, same subject: the house pipeline would re-implement a reviewer rule.
    problems = _problems(_doc(_reviewer(tmp_path), _house(tmp_path, concern="planted-review-concern")), tmp_path)
    assert any("concern 'planted-review-concern' is claimed by rule HPF-901 (hermes-pr-feedback-ci) and rule "
               "CHC-PLANTED (cpompa-hermes-ci)" in p for p in problems), problems


def test_two_pipelines_cannot_share_an_id_prefix(tmp_path):
    pipelines = {**PIPELINES, "cpompa-hermes-ci": {"origin": "house", "id_prefix": "HPF-"}}
    problems = _problems(_doc(_reviewer(tmp_path), pipelines=pipelines), tmp_path)
    assert any("id_prefix 'HPF-' is already used" in p for p in problems), problems


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        pytest.param(lambda r: _house(r, feedback_id="hermes-agent#1/review-2/1", reviewer="x", quote="q"),
                     "keys not allowed for a house rule", id="house-rule-with-review-keys"),
        pytest.param(lambda r: _house(r, pinned_server=True), "keys not allowed for a house rule",
                     id="house-rule-marked-pinned-server"),
        pytest.param(lambda r: _house(r, source=REVIEW), "a house rule's source must be exactly 'house rule'",
                     id="house-rule-claiming-a-review"),
        pytest.param(lambda r: _house(r, source="coordinator security finding: socket mounted"),
                     "a house rule's source must be exactly 'house rule'", id="house-finding-without-date"),
        pytest.param(lambda r: _house(r, source="coordinator security finding 2026-10-23:"),
                     "a house rule's source must be exactly 'house rule'", id="house-finding-without-text"),
        pytest.param(lambda r: _reviewer(r, source="house rule"), "source must link the upstream review",
                     id="reviewer-rule-without-review-url"),
        pytest.param(lambda r: _reviewer(r, quote=" "), "a reviewer rule needs quote",
                     id="reviewer-rule-without-quote"),
        pytest.param(lambda r: _reviewer(r, feedback_id="hermes-agent#143393/review-1/1a"),
                     "does not name the review", id="feedback-id-names-another-review"),
        pytest.param(lambda r: _reviewer(r, id="CHC-901"), "start with 'HPF-'", id="wrong-id-prefix"),
        pytest.param(lambda r: _house(r, pipeline="nightly-ci"), "unknown pipeline", id="unknown-pipeline"),
        pytest.param(lambda r: _reviewer(r, fixtures=_fixtures(r, "HPF-901-x", cases=("accept-only.sh",))),
                     "fixtures must be ci/fixtures/HPF-901", id="fixtures-elsewhere"),
        pytest.param(lambda r: _reviewer(r, clean="sometimes"), "clean must be one of", id="bad-clean-mode"),
    ],
)
def test_a_rule_that_breaks_the_schema_is_refused(tmp_path, make, expected):
    problems = _problems(_doc(make(tmp_path)), tmp_path)
    assert any(expected in p for p in problems), problems


def test_a_rule_without_a_planted_violation_is_refused(tmp_path):
    rule = _reviewer(tmp_path)
    for case in (tmp_path / rule["fixtures"]).iterdir():
        case.unlink()
    (tmp_path / rule["fixtures"] / "accept-clean.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    problems = _problems(_doc(rule), tmp_path)
    assert any("no reject-* fixture" in p for p in problems), problems


def test_clean_fixture_mode_needs_an_accept_case(tmp_path):
    problems = _problems(_doc(_reviewer(tmp_path, clean="fixture")), tmp_path)
    assert any("clean: fixture needs at least one accept-* fixture" in p for p in problems), problems


def test_a_misnamed_fixture_case_is_refused(tmp_path):
    rule = _reviewer(tmp_path)
    (tmp_path / rule["fixtures"] / "planted-case.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    problems = _problems(_doc(rule), tmp_path)
    assert any("fixture files must be (accept|reject)-<slug>" in p for p in problems), problems


def test_an_unregistered_check_script_is_refused(tmp_path):
    _check(tmp_path, "cpompa-hermes-ci", "CHC-ORPHAN", source="house rule")
    problems = _problems(_doc(_reviewer(tmp_path), _house(tmp_path)), tmp_path)
    assert any("CHC-ORPHAN-planted.sh is not registered" in p for p in problems), problems


def test_a_fixture_set_without_a_rule_is_refused(tmp_path):
    _fixtures(tmp_path, "HPF-999")
    problems = _problems(_doc(_reviewer(tmp_path), _house(tmp_path)), tmp_path)
    assert any("ci/fixtures/HPF-999/ belongs to no registered rule" in p for p in problems), problems


def test_a_script_header_that_contradicts_the_registry_is_refused(tmp_path):
    rule = _house(tmp_path)
    _check(tmp_path, "cpompa-hermes-ci", "CHC-PLANTED", source="house rule", header_pipeline="hermes-pr-feedback-ci")
    problems = _problems(_doc(rule), tmp_path)
    assert any("header '# pipeline:' is 'hermes-pr-feedback-ci'" in p for p in problems), problems


def test_a_check_script_that_is_not_executable_is_refused(tmp_path):
    rule = _house(tmp_path)
    (tmp_path / rule["script"]).chmod(0o644)
    problems = _problems(_doc(rule), tmp_path)
    assert any("is not executable" in p for p in problems), problems


# ------------------------------------------------------------------ the falsification harness
HOLDS = 'if [ -e "$1/planted.txt" ]; then echo "FAIL CHC-PLANTED: planted.txt"; exit 1; fi'


@pytest.mark.parametrize(
    ("body", "expected_rc", "expected"),
    [
        pytest.param(HOLDS, 0, "HOLDS   CHC-PLANTED reject-planted.sh: exit 1 as expected", id="a-real-check-holds"),
        pytest.param("exit 0", 1, "the planted violation was ACCEPTED (exit 0): this check cannot fail on it",
                     id="a-check-that-cannot-fail-is-broken"),
        pytest.param('[ -e "$1/planted.txt" ] && exit 2; exit 0', 1,
                     "the planted violation made the check error (exit 2), not reject (exit 1)",
                     id="an-error-is-not-a-rejection"),
        pytest.param('[ -e "$1/planted.txt" ] && exit 1; exit 2', 1, "the clean case was not accepted (exit 2)",
                     id="a-check-that-rejects-everything-is-broken"),
    ],
)
def test_falsify_proves_a_check_can_fail_and_can_pass(tmp_path, body, expected_rc, expected):
    repo = tmp_path / "repo"
    shutil.copytree(CI_DIR, repo / "scripts" / "ci", ignore=shutil.ignore_patterns("checks", "__pycache__"))
    script = _check(repo, "cpompa-hermes-ci", "CHC-PLANTED", source="house rule")
    (repo / script).write_text((repo / script).read_text(encoding="utf-8").replace("exit 0\n", body + "\n"),
                               encoding="utf-8")
    reviewer = _reviewer(repo)
    fixtures = _fixtures(repo, "CHC-PLANTED", cases=())
    (repo / fixtures / "reject-planted.sh").write_text('#!/usr/bin/env bash\necho planted >"$1/planted.txt"\n',
                                                       encoding="utf-8")
    import yaml

    house = {"id": "CHC-PLANTED", "pipeline": "cpompa-hermes-ci", "title": "planted", "concern": "planted",
             "source": "house rule", "enforces": "planted", "script": script, "fixtures": fixtures}
    (repo / "ci" / "feedback-rules.yaml").write_text(yaml.safe_dump(_doc(reviewer, house)), encoding="utf-8")
    who = {"NAME": "t", "EMAIL": "t@example.invalid"}
    git = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           **{f"GIT_{role}_{key}": value for role in ("AUTHOR", "COMMITTER") for key, value in who.items()}}
    env = {**os.environ, **git, "CI_PYTHON": sys.executable, "CI_REPORT_DIR": str(tmp_path / "reports")}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)

    proc = subprocess.run(["bash", str(repo / "scripts" / "ci" / "run.sh"), "falsify", "cpompa-hermes-ci"],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == expected_rc, proc.stdout + proc.stderr
    assert expected in proc.stdout, proc.stdout
    report = (tmp_path / "reports" / "cpompa-hermes-ci-falsify.xml").read_text(encoding="utf-8")
    assert 'name="cpompa-hermes-ci:falsify" tests="2"' in report
