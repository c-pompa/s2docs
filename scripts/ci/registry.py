"""The CI rule registry: load, validate and query ci/feedback-rules.yaml.

One registry decides, for every check, which pipeline owns it, where the rule came from and which
script enforces it. The runner (scripts/ci/run.sh) only ever executes what this module returns for
the pipeline it was asked to run, and tests/test_ci_registry.py asserts the invariants below on the
real registry and on planted bad ones — chief among them that no rule is claimed by both pipelines.

    registry.py validate
    registry.py rules <pipeline>          # TSV: id, script, fixtures, clean, title
    registry.py ids <pipeline>
    registry.py get <rule-id> <field>
    registry.py pinned-server             # ids of rules the server pin must also satisfy (HPF-008)
    registry.py junit <pipeline> <mode> <results.tsv> <out.xml>

Exit codes: 0 ok, 1 the registry is invalid (each problem printed), 2 usage / unreadable registry.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "ci" / "feedback-rules.yaml"

ORIGINS = {"reviewer", "house"}
HOUSE_SOURCE = "house rule"
RULE_ID_RE = re.compile(r"[A-Z]{3}-[A-Z0-9]+(?:-[A-Z0-9]+)*")
CONCERN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REVIEW_URL_RE = re.compile(
    r"https://github\.com/NousResearch/hermes-agent/(?:pull|issues)/(?P<pr>\d+)"
    r"#(?P<kind>pullrequestreview|discussion_r|issuecomment)-(?P<cid>\d+)"
)
FEEDBACK_ID_RE = re.compile(r"hermes-agent#(?P<pr>\d+)/(?P<kind>review|comment)-(?P<cid>\d+)/(?P<item>[0-9a-z.]+)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
CASE_RE = re.compile(r"(?P<expect>accept|reject)-[a-z0-9]+(?:-[a-z0-9]+)*\.(?:patch|sh)")

COMMON_KEYS = {"id", "pipeline", "title", "concern", "source", "enforces", "script", "fixtures"}
REVIEWER_KEYS = {"feedback_id", "reviewer", "quote", "reviewed_at", "pinned_server"}
OPTIONAL_KEYS = {"clean"}
CLEAN_MODES = {"tree", "fixture"}


class RegistryError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Rule:
    id: str
    pipeline: str
    title: str
    concern: str
    source: str
    enforces: str
    script: str
    fixtures: str
    clean: str = "tree"
    feedback_id: str | None = None
    reviewer: str | None = None
    quote: str | None = None
    reviewed_at: str | None = None
    pinned_server: bool = False


@dataclass
class Registry:
    pipelines: dict[str, dict]
    rules: list[Rule] = field(default_factory=list)

    def for_pipeline(self, pipeline: str) -> list[Rule]:
        if pipeline not in self.pipelines:
            raise RegistryError([f"unknown pipeline {pipeline!r}; known: {sorted(self.pipelines)}"])
        return [r for r in self.rules if r.pipeline == pipeline]

    def get(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise RegistryError([f"unknown rule {rule_id!r}"])


def _load_yaml(path: Path):
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - the dev group declares pyyaml
        raise OSError("PyYAML is not importable; run 'uv sync --all-groups'") from exc
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _header(script: Path) -> dict[str, str]:
    """`# key: value` lines at the top of a check script (until the first non-comment line)."""
    out: dict[str, str] = {}
    for line in script.read_text(encoding="utf-8").splitlines()[1:]:
        if not line.startswith("#"):
            break
        m = re.match(r"#\s*([a-z_]+):\s*(.*\S)\s*$", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2)
    return out


def _check_rule(raw: dict, pipelines: dict[str, dict], root: Path, problems: list[str]) -> Rule | None:
    where = f"rule {raw.get('id', '?')!r}"
    missing = sorted(COMMON_KEYS - raw.keys())
    if missing:
        problems.append(f"{where}: missing keys {missing}")
        return None
    pipeline = raw["pipeline"]
    if pipeline not in pipelines:
        problems.append(f"{where}: unknown pipeline {pipeline!r}")
        return None
    origin = pipelines[pipeline]["origin"]
    allowed = COMMON_KEYS | OPTIONAL_KEYS | (REVIEWER_KEYS if origin == "reviewer" else set())
    unknown = sorted(raw.keys() - allowed)
    if unknown:
        problems.append(f"{where}: keys not allowed for a {origin} rule: {unknown}")
    rid = str(raw["id"])
    if not RULE_ID_RE.fullmatch(rid) or not rid.startswith(pipelines[pipeline]["id_prefix"]):
        problems.append(f"{where}: id must match {RULE_ID_RE.pattern} and start with "
                        f"{pipelines[pipeline]['id_prefix']!r} (pipeline {pipeline})")
    if not CONCERN_RE.fullmatch(str(raw["concern"])):
        problems.append(f"{where}: concern must be a lowercase slug")
    for key in ("title", "enforces"):
        if not str(raw[key]).strip():
            problems.append(f"{where}: {key} is empty")

    source = str(raw["source"])
    if origin == "house":
        if source != HOUSE_SOURCE:
            problems.append(f"{where}: a house rule's source must be exactly {HOUSE_SOURCE!r}, not {source!r}")
    else:
        for key in ("feedback_id", "reviewer", "quote"):
            if not str(raw.get(key) or "").strip():
                problems.append(f"{where}: a reviewer rule needs {key}")
        url = REVIEW_URL_RE.fullmatch(source)
        fid = FEEDBACK_ID_RE.fullmatch(str(raw.get("feedback_id") or ""))
        if url is None:
            problems.append(f"{where}: source must link the upstream review/comment it came from, got {source!r}")
        if fid is None:
            problems.append(f"{where}: feedback_id must look like hermes-agent#<pr>/review-<id>/<item>")
        if url and fid and (url["pr"], url["cid"]) != (fid["pr"], fid["cid"]):
            problems.append(f"{where}: feedback_id {raw['feedback_id']!r} does not name the review in {source!r}")
        if raw.get("reviewed_at") is not None and not SHA_RE.fullmatch(str(raw["reviewed_at"])):
            problems.append(f"{where}: reviewed_at must be a full 40-hex commit sha")
        if "pinned_server" in raw and not isinstance(raw["pinned_server"], bool):
            problems.append(f"{where}: pinned_server must be true or false")

    clean = raw.get("clean", "tree")
    if clean not in CLEAN_MODES:
        problems.append(f"{where}: clean must be one of {sorted(CLEAN_MODES)}")

    script = root / str(raw["script"])
    expected_dir = root / "scripts" / "ci" / "checks" / pipeline
    if script.parent != expected_dir or not script.name.startswith(f"{rid}-") or script.suffix != ".sh":
        problems.append(f"{where}: script must be scripts/ci/checks/{pipeline}/{rid}-<slug>.sh, got {raw['script']!r}")
    if not script.is_file():
        problems.append(f"{where}: script {raw['script']!r} does not exist")
    else:
        if not script.stat().st_mode & 0o111:
            problems.append(f"{where}: script {raw['script']!r} is not executable")
        head = _header(script)
        expect = {"rule": rid, "pipeline": pipeline, "source": source}
        if origin == "reviewer":
            expect["feedback"] = str(raw.get("feedback_id"))
        for key, value in expect.items():
            if head.get(key) != value:
                problems.append(f"{where}: {raw['script']} header '# {key}:' is {head.get(key)!r}, "
                                f"registry says {value!r}")
        if not head.get("enforces"):
            problems.append(f"{where}: {raw['script']} header has no '# enforces:' line")

    fixtures = root / str(raw["fixtures"])
    if fixtures != root / "ci" / "fixtures" / rid:
        problems.append(f"{where}: fixtures must be ci/fixtures/{rid}, got {raw['fixtures']!r}")
    if not fixtures.is_dir():
        problems.append(f"{where}: fixtures dir {raw['fixtures']!r} does not exist")
    else:
        cases = sorted(p.name for p in fixtures.iterdir() if not p.name.startswith("."))
        bad = [c for c in cases if not CASE_RE.fullmatch(c) and c != "README.md"]
        if bad:
            problems.append(f"{where}: fixture files must be (accept|reject)-<slug>.(patch|sh): {bad}")
        if not any(c.startswith("reject-") for c in cases):
            problems.append(f"{where}: no reject-* fixture — a check with no planted violation is not tested")
        if clean == "fixture" and not any(c.startswith("accept-") for c in cases):
            problems.append(f"{where}: clean: fixture needs at least one accept-* fixture")

    return Rule(
        id=rid, pipeline=pipeline, title=str(raw["title"]), concern=str(raw["concern"]), source=source,
        enforces=" ".join(str(raw["enforces"]).split()), script=str(raw["script"]), fixtures=str(raw["fixtures"]),
        clean=str(clean), feedback_id=raw.get("feedback_id"), reviewer=raw.get("reviewer"), quote=raw.get("quote"),
        reviewed_at=raw.get("reviewed_at"), pinned_server=bool(raw.get("pinned_server", False)),
    )


def validate(data: object, root: Path = REPO_ROOT) -> Registry:
    """Validate parsed registry data; raise RegistryError listing every problem found."""
    problems: list[str] = []
    if not isinstance(data, dict) or data.get("version") != 1:
        raise RegistryError(["registry must be a mapping with version: 1"])
    pipelines = data.get("pipelines")
    if not isinstance(pipelines, dict) or not pipelines:
        raise RegistryError(["registry needs a non-empty 'pipelines' mapping"])
    prefixes: dict[str, str] = {}
    for name, spec in pipelines.items():
        if not isinstance(spec, dict) or spec.get("origin") not in ORIGINS or not spec.get("id_prefix"):
            problems.append(f"pipeline {name!r}: needs origin in {sorted(ORIGINS)} and an id_prefix")
            continue
        if spec["id_prefix"] in prefixes:
            problems.append(f"pipeline {name!r}: id_prefix {spec['id_prefix']!r} is already used by "
                            f"{prefixes[spec['id_prefix']]!r}")
        prefixes[spec["id_prefix"]] = name
    if problems:
        raise RegistryError(problems)
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RegistryError(["registry needs a non-empty 'rules' list"])

    checked = (_check_rule(raw, pipelines, root, problems) for raw in raw_rules if isinstance(raw, dict))
    rules = [rule for rule in checked if rule is not None]
    if len(rules) != len(raw_rules) and not problems:
        problems.append("every rule must be a mapping")

    # The non-duplication guarantee: an id, a script and a concern each belong to exactly one rule,
    # so no check is claimed by both pipelines and neither pipeline re-implements the other's rule.
    for attr in ("id", "script", "concern"):
        seen: dict[str, Rule] = {}
        for rule in rules:
            value = getattr(rule, attr)
            if value in seen:
                problems.append(f"{attr} {value!r} is claimed by rule {seen[value].id} ({seen[value].pipeline}) "
                                f"and rule {rule.id} ({rule.pipeline})")
            seen[value] = rule
    by_pipeline = {name: {r.id for r in rules if r.pipeline == name} for name in pipelines}
    names = sorted(by_pipeline)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = by_pipeline[a] & by_pipeline[b]
            if shared:
                problems.append(f"rule ids claimed by both {a} and {b}: {sorted(shared)}")
    for name, ids in by_pipeline.items():
        if not ids:
            problems.append(f"pipeline {name!r} has no rules")

    # Every check script on disk must be registered: an unregistered script is a check nobody runs.
    checks_root = root / "scripts" / "ci" / "checks"
    registered = {(root / r.script).resolve() for r in rules}
    if checks_root.is_dir():
        for script in sorted(checks_root.rglob("*.sh")):
            if script.resolve() not in registered:
                problems.append(f"{script.relative_to(root)} is not registered in ci/feedback-rules.yaml")
        for child in sorted(checks_root.iterdir()):
            if child.is_dir() and child.name not in pipelines:
                problems.append(f"scripts/ci/checks/{child.name}/ is not a registered pipeline")
    fixtures_root = root / "ci" / "fixtures"
    if fixtures_root.is_dir():
        known = {r.id for r in rules}
        for child in sorted(fixtures_root.iterdir()):
            if child.is_dir() and child.name not in known:
                problems.append(f"ci/fixtures/{child.name}/ belongs to no registered rule")

    if problems:
        raise RegistryError(problems)
    return Registry(pipelines=pipelines, rules=rules)


def load(path: Path = REGISTRY, root: Path = REPO_ROOT) -> Registry:
    return validate(_load_yaml(path), root)


def _junit(registry: Registry, pipeline: str, mode: str, results: Path, out: Path) -> None:
    """results.tsv rows: id, case, verdict, seconds, log path. FAIL/BROKEN -> failure, ERROR -> error."""
    rows = [line.split("\t") for line in results.read_text(encoding="utf-8").splitlines() if line.strip()]
    cases, failures, errors, total = [], 0, 0, 0.0
    for rid, case, verdict, seconds, log in rows:
        rule = registry.get(rid)
        total += float(seconds)
        name = f"{rid} {rule.title}" + (f" [{case}]" if case != "-" else "")
        body = ""
        log_text = Path(log).read_text(encoding="utf-8", errors="replace")[-20000:] if Path(log).is_file() else ""
        if verdict in ("FAIL", "BROKEN"):
            failures += 1
            body = f"<failure message={quoteattr(verdict)}>{escape(log_text)}</failure>"
        elif verdict == "ERROR":
            errors += 1
            body = f"<error message={quoteattr(verdict)}>{escape(log_text)}</error>"
        props = f"source: {rule.source}" + (f" | feedback: {rule.feedback_id}" if rule.feedback_id else "")
        cases.append(f'  <testcase classname={quoteattr(f"{pipeline}.{mode}")} name={quoteattr(name)} '
                     f'time="{float(seconds):.1f}">{body}<system-out>{escape(props)}</system-out></testcase>')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name={quoteattr(f"{pipeline}:{mode}")} tests="{len(rows)}" failures="{failures}" '
        f'errors="{errors}" time="{total:.1f}">\n' + "\n".join(cases) + "\n</testsuite>\n",
        encoding="utf-8",
    )


def main(argv: list[str]) -> int:
    try:
        registry = load()
    except RegistryError as exc:
        for problem in exc.problems:
            print(f"registry: {problem}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"registry: unreadable {REGISTRY}: {exc}", file=sys.stderr)
        return 2
    try:
        match argv[1:]:
            case ["validate"]:
                print(f"registry ok: {len(registry.rules)} rules in {len(registry.pipelines)} pipelines "
                      f"({', '.join(f'{p}={len(registry.for_pipeline(p))}' for p in registry.pipelines)})")
            case ["rules", pipeline]:
                for r in registry.for_pipeline(pipeline):
                    print("\t".join((r.id, r.script, r.fixtures, r.clean, r.title)))
            case ["ids", pipeline]:
                print("\n".join(r.id for r in registry.for_pipeline(pipeline)))
            case ["get", rule_id, key]:
                value = getattr(registry.get(rule_id), key, None)
                if value is None:
                    print(f"registry: rule {rule_id} has no {key!r}", file=sys.stderr)
                    return 2
                print(value)
            case ["pinned-server"]:
                print("\n".join(r.id for r in registry.rules if r.pinned_server))
            case ["junit", pipeline, mode, results, out]:
                registry.for_pipeline(pipeline)
                _junit(registry, pipeline, mode, Path(results), Path(out))
            case _:
                print(__doc__, file=sys.stderr)
                return 2
    except RegistryError as exc:
        for problem in exc.problems:
            print(f"registry: {problem}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
