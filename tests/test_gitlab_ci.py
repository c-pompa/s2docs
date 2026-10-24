"""`.gitlab-ci.yml` places every job deterministically.

Only runners carrying the `s2docs` tag may take an s2docs job, so a verdict never depends on which
shared runner happened to be free. The tag comes from the `default:` block; a job that sets its own
`tags:` (directly or through `extends:`) would override it, which these tests refuse.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RESERVED = {"default", "include", "stages", "variables", "workflow", "image", "services", "cache",
            "before_script", "after_script"}


def _config() -> dict:
    return yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))


def _jobs(config: dict) -> dict[str, dict]:
    return {name: spec for name, spec in config.items()
            if name not in RESERVED and not name.startswith(".") and isinstance(spec, dict)}


def resolve(config: dict, name: str) -> dict:
    """The job's keys after `extends:` (GitLab's deep merge, later wins), without `default:`."""
    spec = config[name]
    parents = spec.get("extends", [])
    merged: dict = {}
    for parent in [parents] if isinstance(parents, str) else parents:
        merged = _merge(merged, resolve(config, parent))
    return _merge(merged, {k: v for k, v in spec.items() if k != "extends"})


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def effective(config: dict, name: str, key: str):
    job = resolve(config, name)
    return job[key] if key in job else config.get("default", {}).get(key)


def test_the_pipeline_has_jobs():
    assert _jobs(_config()), ".gitlab-ci.yml defines no jobs"


@pytest.mark.parametrize("job", sorted(_jobs(_config())))
def test_every_job_runs_only_on_s2docs_runners(job):
    config = _config()
    assert effective(config, job, "tags") == ["s2docs"], f"{job} would not be placed on an s2docs runner"


def test_the_tag_comes_from_the_default_block():
    config = _config()
    assert config["default"]["tags"] == ["s2docs"]
    overriding = [job for job in _jobs(config) if "tags" in resolve(config, job)]
    assert not overriding, f"jobs overriding default tags: {overriding}"


# ------------------------------------------------------------------ where falsify runs
# A small evaluator for the subset of GitLab `rules:` this file uses: `if:` built from $VAR, `==`,
# `!=`, `&&`, `||`, plus `changes:` and `when:`. Anything else raises, so a rule this evaluator does
# not understand fails the test instead of being read as "runs".
_TOKEN = re.compile(r'\s*(?:(\|\||&&)|(==|!=)|\$([A-Za-z_][A-Za-z0-9_]*)|"([^"]*)")')


def _atoms(expr: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(expr.rstrip()):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise ValueError(f"unsupported rules:if expression {expr!r} at {expr[pos:]!r}")
        kind = "op" if m[1] else "cmp" if m[2] else "var" if m[3] else "str"
        out.append((kind, m[1] or m[2] or m[3] or m[4] or ""))
        pos = m.end()
    return out


def _truth(expr: str, env: dict[str, str]) -> bool:
    toks = _atoms(expr)
    ors: list[list[list[tuple[str, str]]]] = [[[]]]
    for kind, val in toks:
        if kind == "op" and val == "||":
            ors.append([[]])
        elif kind == "op":
            ors[-1].append([])
        else:
            ors[-1][-1].append((kind, val))

    def term(t: list[tuple[str, str]]) -> bool:
        if len(t) == 1 and t[0][0] == "var":
            return bool(env.get(t[0][1]))
        if len(t) == 3 and t[0][0] == "var" and t[1][0] == "cmp" and t[2][0] == "str":
            equal = env.get(t[0][1]) == t[2][1]
            return equal if t[1][1] == "==" else not equal
        raise ValueError(f"unsupported term {t!r} in {expr!r}")

    return any(all(term(t) for t in ands) for ands in ors)


def _glob(pattern: str) -> re.Pattern:
    out = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*", "[^/]*")
    return re.compile(out + r"\Z")


def runs(rules: list[dict] | None, env: dict[str, str], changed: list[str]) -> bool:
    """First matching rule decides; no `rules:` means the job always runs."""
    if rules is None:
        return True
    for rule in rules:
        unknown = set(rule) - {"if", "changes", "when"}
        if unknown:
            raise ValueError(f"unsupported rule keys {unknown}")
        if "if" in rule and not _truth(rule["if"], env):
            continue
        if "changes" in rule:
            paths = rule["changes"]["paths"] if isinstance(rule["changes"], dict) else rule["changes"]
            if env.get("CI_PIPELINE_SOURCE") != "merge_request_event":
                raise ValueError("changes: outside a merge request pipeline always matches; keep it MR-only")
            if not any(_glob(p).match(f) for p in paths for f in changed):
                continue
        return rule.get("when", "on_success") != "never"
    return False


PUSH_DEV = {"CI_PIPELINE_SOURCE": "push", "CI_COMMIT_BRANCH": "dev"}
PUSH_DEV_WITH_MR = {**PUSH_DEV, "CI_OPEN_MERGE_REQUESTS": "s2docs/valve-mcp-api-project!1"}
MR_FROM_DEV = {"CI_PIPELINE_SOURCE": "merge_request_event", "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "dev",
               "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "main"}
SCHEDULE_DEV = {"CI_PIPELINE_SOURCE": "schedule", "CI_COMMIT_BRANCH": "dev"}
WEB_DEV = {"CI_PIPELINE_SOURCE": "web", "CI_COMMIT_BRANCH": "dev"}
MR_FEATURE = {"CI_PIPELINE_SOURCE": "merge_request_event",
              "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME": "feature/dev/x", "CI_MERGE_REQUEST_TARGET_BRANCH_NAME": "dev"}
PUSH_FEATURE = {"CI_PIPELINE_SOURCE": "push", "CI_COMMIT_BRANCH": "feature/dev/x"}
APP_CHANGE = ["src/s2docs/hosts.py"]


def jobs_in(env: dict[str, str], changed: list[str]) -> set[str]:
    """The jobs a pipeline with these predefined variables contains ({} when workflow refuses it)."""
    config = _config()
    if not runs(config["workflow"]["rules"], env, changed):
        return set()
    return {job for job in _jobs(config) if runs(resolve(config, job).get("rules"), env, changed)}


def _falsify_jobs_for(rule_id: str, pipeline: str) -> set[str]:
    """Jobs whose script runs `run.sh falsify <pipeline>` over all rules or naming RULE_ID."""
    config, out = _config(), set()
    for job in _jobs(config):
        for line in resolve(config, job).get("script", []):
            words = line.split()
            is_falsify = words[:1] and words[0].endswith("scripts/ci/run.sh") and words[1:3] == ["falsify", pipeline]
            if is_falsify and (not words[3:] or rule_id in words[3:]):
                out.add(job)
    return out


def _registry():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("ci_registry_gl", ROOT / "scripts" / "ci" / "registry.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.load()


DEV_PIPELINES = {
    "push-to-dev": PUSH_DEV,
    "dev-as-mr-source": MR_FROM_DEV,  # an open dev->main MR replaces dev's branch pipeline
    "nightly-on-dev": SCHEDULE_DEV,
}


@pytest.mark.parametrize("scenario", sorted(DEV_PIPELINES))
@pytest.mark.parametrize("rule_id", [r.id for r in _registry().rules])
def test_every_rule_is_falsified_on_dev(rule_id, scenario):
    rule = _registry().get(rule_id)
    candidates = _falsify_jobs_for(rule_id, rule.pipeline)
    assert candidates, f"no job runs `scripts/ci/run.sh falsify {rule.pipeline}` covering {rule_id}"
    present = jobs_in(DEV_PIPELINES[scenario], changed=APP_CHANGE)  # no CI file touched
    assert candidates & present, f"{rule_id} is never falsified in a {scenario} pipeline (falsify jobs {candidates})"


def test_an_open_dev_mr_does_replace_the_dev_branch_pipeline():
    # why dev-as-mr-source is a scenario at all: the workflow drops the branch pipeline then
    assert jobs_in(PUSH_DEV_WITH_MR, changed=APP_CHANGE) == set()


@pytest.mark.parametrize("env", [PUSH_DEV, MR_FEATURE, PUSH_FEATURE, SCHEDULE_DEV, WEB_DEV, MR_FROM_DEV],
                         ids=["push-dev", "mr-feature", "push-feature", "schedule", "web", "mr-from-dev"])
def test_enforce_runs_in_every_pipeline(env):
    present = jobs_in(env, changed=APP_CHANGE)
    assert {"registry:validate", "hermes-pr-feedback-ci:enforce", "cpompa-hermes-ci:enforce"} <= present, present


FALSIFY = {"hermes-pr-feedback-ci:falsify", "cpompa-hermes-ci:falsify"}


@pytest.mark.parametrize(
    ("env", "changed", "expected"),
    [
        pytest.param(MR_FEATURE, APP_CHANGE, False, id="feature-mr-app-only"),
        pytest.param(MR_FEATURE, ["ci/feedback-rules.yaml"], True, id="feature-mr-registry"),
        pytest.param(MR_FEATURE, ["ci/fixtures/HPF-008/reject-no-from.sh"], True, id="feature-mr-fixture"),
        pytest.param(MR_FEATURE, ["scripts/ci/run.sh"], True, id="feature-mr-runner"),
        pytest.param(MR_FEATURE, [".gitlab-ci.yml"], True, id="feature-mr-gitlab-ci"),
        pytest.param(MR_FEATURE, ["scripts/release.sh"], False, id="feature-mr-other-script"),
        pytest.param(PUSH_FEATURE, APP_CHANGE, False, id="feature-push"),
        pytest.param({**PUSH_DEV, "CI_COMMIT_BRANCH": "main"}, APP_CHANGE, True, id="push-main"),
        pytest.param(WEB_DEV, APP_CHANGE, True, id="web-run-on-dev"),
        pytest.param({**WEB_DEV, "CI_COMMIT_BRANCH": "feature/dev/x"}, APP_CHANGE, False, id="web-run-on-feature"),
    ],
)
def test_falsify_runs_only_where_it_pays_for_itself(env, changed, expected):
    present = jobs_in(env, changed)
    assert (FALSIFY & present) == (FALSIFY if expected else set()), present


def test_manual_runs_stay_allowed():
    for source in ("web", "api"):
        assert jobs_in({"CI_PIPELINE_SOURCE": source, "CI_COMMIT_BRANCH": "feature/dev/x"}, APP_CHANGE)
