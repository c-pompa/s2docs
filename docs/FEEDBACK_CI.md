# Reviewer feedback as CI

s2docs runs two CI pipelines that never check the same thing:

| Pipeline | Ids | Holds | Sources |
|---|---|---|---|
| `hermes-pr-feedback-ci` | `HPF-*` | every rule an upstream hermes-agent reviewer asked for | a review or comment URL, one rule per request |
| `cpompa-hermes-ci` | `CHC-*` | our house standards: lint, tests, security and secret scans, lockfile integrity, commit hygiene, packaging, the job environment | `house rule`, or the dated coordinator security finding the rule answers |

One registry, [`ci/feedback-rules.yaml`](../ci/feedback-rules.yaml), decides which rule belongs to
which pipeline, where it came from, and which script enforces it. One runner,
[`scripts/ci/run.sh`](../scripts/ci/run.sh), executes exactly the rules the registry assigns to the
pipeline it is asked for. `.gitlab-ci.yml` only names the pipelines; it never lists rules.

## The rules today

Review [5631127162](https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162)
on NousResearch/hermes-agent#143393 (teknium1, changes requested, read at `531a24c7`) produced the
nine `hermes-pr-feedback-ci` rules. The registry stores each rule's verbatim quote.

| Rule | Feedback | Enforces |
|---|---|---|
| HPF-001 | 1a "reject targets starting with `-` and allow only an ssh alias or `[user@]host[:port]` characters" | `register_host` (function, REST route, MCP tool) refuses 39 unsafe targets, stores nothing for them, and still accepts the valid forms |
| HPF-002 | 1b "put `--` before the destination in the two ssh calls" | every ssh process (probe, build, deploy verify) has `--` right before the destination and a port only as `-p N`; only `hosts.ssh_argv` builds ssh argv |
| HPF-003 | 1c "keep scp's `host:path` argument from starting with `-`" | the scp operand is destination plus path and never starts with `-`, even for rows written without validation; only `hosts.scp_argv` builds scp argv |
| HPF-004 | 1d "add a test that `register_host` with `-oProxyCommand=...` raises" | `test_register_host_rejects_unsafe_ssh_target` exists, has a `-oProxyCommand=` case, and every case passes, with none skipped |
| HPF-005 | 2a "add an upper bound below the next major for each" | every runtime dependency is `>=floor,<bound`, the locked version is inside it, and the bound is no later than the next breaking line (`<0.(y+1)` for 0.x) |
| HPF-006 | 2b "use the oldest compatible floors rather than the newest release" | at the snapshot, lowest-direct installs every floor and the full suite passes; the release just below each floor fails to resolve or fails the suite |
| HPF-007 | 2c "Adding `--exclude-newer` with a fixed date to the `uvx` args" | uvx gets `--exclude-newer` before the command, as one RFC 3339 instant with an offset, not in the future, at which the deps and the build backend resolve |
| HPF-008 | 3 "point the `uvx` git ref in `plugin/mcp.json` at the fixed commit too" | the uvx ref is a full sha of an ancestor of HEAD whose `git archive` passes every `pinned_server` rule |
| HPF-009 | 2d catalog rule 10, cited in item 2: "run their own release quarantine (`uv --exclude-newer` in their CI)" | `[tool.uv] exclude-newer` is a fixed instant, not in the future, and equals the plugin's `--exclude-newer` |

House rules: CHC-LINT (ruff, including the Python embedded in CI scripts), CHC-SHELL (`bash -n` and
ShellCheck 0.11.0), CHC-SAST (a flake8-bandit subset for exec/eval, unsafe deserialisation, local
shells and unverified TLS), CHC-TESTS (the full suite passes with no skips), CHC-SECRETS (gitleaks on
the shipped files and the reviewed commit range), CHC-IOC (the compromised-npm indicators from the
workspace security note), CHC-LOCKFILE (`uv lock --check`, PyPI-only sources, a sha256 on every
artifact), CHC-COMMITS (subject, body, no fixup or WIP commits, `git diff --check`, no file over
5 MiB), CHC-PACKAGING (sdist and wheel build; the installed wheel runs), and
CHC-RUNNER-NO-DOCKER-SOCK (the CI job has nothing at `/var/run/docker.sock` and no `DOCKER_HOST`).

CHC-RUNNER-NO-DOCKER-SOCK answers the coordinator security finding of 2026-10-23: a runner mounted
`/var/run/docker.sock` into job containers, which gives code under test the runner host's Docker
daemon, and with it root on that host. It inspects the environment it runs in, not the tree, so it runs
in `enforce` and fails the job on any runner that still mounts the socket. On a workstation with
Docker Desktop it fails as well; that is the same finding, about the machine. `CHC_DOCKER_SOCKETS`
(`:`-separated, default `/var/run/docker.sock`) redirects the inspected paths for its fixtures. Inside
a GitLab job (`GITLAB_CI=true`), a redirect counts as a failure, because it would hide a real socket.

## Why the pipelines cannot overlap

`scripts/ci/registry.py` validates the registry before every run and refuses it, so the runner exits 2,
when any of these is true:

- a rule id, check script, fixture set or **concern** is claimed by more than one rule. The `concern`
  is a slug naming what a rule is about. Two rules about the same thing, in either pipeline, are
  refused, which means one pipeline can never re-implement the other's check.
- an id lacks its pipeline's prefix, two pipelines share a prefix, or a script sits outside
  `scripts/ci/checks/<its pipeline>/`;
- a reviewer rule has no review URL, `feedback_id`, reviewer or quote, or its `feedback_id` names a
  different review than its URL;
- a house rule carries reviewer keys, or its source is anything other than exactly `house rule` or
  `coordinator security finding <YYYY-MM-DD>: <finding>`;
- a script's header (`# rule:`, `# pipeline:`, `# source:`, `# feedback:`) contradicts its row;
- a check script or fixture set on disk is not registered, or a script is not executable;
- a rule has no `reject-*` fixture, or uses `clean: fixture` with no `accept-*` fixture.

[`tests/test_ci_registry.py`](../tests/test_ci_registry.py) checks these invariants twice:

- On the real registry. No id, script, fixture set or concern is claimed by both pipelines, and every
  item of review 5631127162 maps to exactly one rule.
- On planted bad registries. The same id in both pipelines, one script or one concern claimed by both,
  plus every schema violation above; each must be refused with a message that names the conflict.

It also runs the falsification harness against planted checks. A check that always passes, one that
errors instead of rejecting, and one that rejects everything are all reported `BROKEN`.

Some pairs look similar but stay separate because each enforces a different concern:

| Pair | Concern of each |
|---|---|
| HPF-004 and CHC-TESTS | HPF-004 requires one named regression test the reviewer asked for. CHC-TESTS requires the whole suite to pass. |
| HPF-009 and CHC-LOCKFILE | HPF-009 requires the release snapshot to match the users' one. CHC-LOCKFILE requires `uv.lock` to be current for `pyproject.toml`. |
| HPF-005/006 and CHC-LOCKFILE | HPF-005/006 cover the declared ranges. CHC-LOCKFILE covers the lock's integrity. |

## The contract every check follows

`scripts/ci/checks/<pipeline>/<RULE>-<slug>.sh <tree>` exits:

- `0`: the tree satisfies the rule.
- `1`: the tree violates it; each violation is printed as `FAIL <RULE>: ...`.
- `2`: the rule could not be evaluated. That covers bad usage, an unreadable input, a missing tool,
  an unreachable index, or a crash. The message names the input or tool (`missing tool: gitleaks`,
  `unreadable input: ...`). Python checks go through `cilib.run_check`, which maps any unexpected
  exception to 2, never to 1, so a broken check cannot pass itself off as a detection.

No check uses `|| true`, and no check skips silently. A rule whose premise is gone (for example,
mcp.json no longer launching with uvx) exits 2 and says so. It never reports a pass.

## Falsification: a check that cannot fail is not a check

Every rule has a fixture set in `ci/fixtures/<RULE>/`:

- `reject-<slug>.sh` plants one violation. The check must exit exactly **1**. Exit 0 means it cannot
  detect the violation; exit 2 means it errored instead of detecting it. Both count as `BROKEN`.
- `accept-<slug>.sh` plants a legitimate variation. The check must exit **0**, so a rule cannot
  "work" by refusing everything.
- With `clean: tree` (the default), the unmodified checkout is also an accept case.
- A rule about the job environment instead of the tree (CHC-RUNNER-NO-DOCKER-SOCK) cannot be planted
  as a file change, because the harness strips the environment. Its fixtures call `plant_env KEY VALUE`,
  which appends to the clone's `.git/ci-case.env`, and `run.sh` adds those variables to that one
  case's check run. Files the case needs outside the repository (a fake socket) go in `plant_scratch`,
  also under `.git`. Because neither lives in the tree, a commit cannot carry them into `enforce`.

`run.sh falsify <pipeline> [RULE...]` exports the checkout's shippable files into a one-commit git
repo, clones it per case, runs the fixture in the clone, then runs the check against the clone. It
uses `env -i` with a short allowlist (PATH, HOME, TMPDIR, uv/SSL settings, `CI_PYTHON`, `S2DOCS_TSC`
and a fixed git identity), so a verdict cannot depend on the machine or the CI job. It prints `HOLDS`
or `BROKEN` per case, writes JUnit and TSV to `$CI_REPORT_DIR` (default `./ci-reports`), and exits 1
if any case is broken.

Fixtures source [`scripts/ci/plant.sh`](../scripts/ci/plant.sh) and edit through anchors. Each edit
names the exact text it changes (`plant_replace`, `plant_sub`, `plant_pin`) and exits 3 when that
text is missing or ambiguous. A stale fixture is therefore reported as "fixture could not be planted"
(`ERROR`, and the job fails). It never becomes a quiet pass. Secrets and indicators that fixtures need
are assembled at run time (`plant fake-token`, `plant ioc`), so no file in the repository contains
one and the scans never flag their own fixtures.

`ci/fixtures/as-reviewed-531a24c7.patch` restores `hosts.py`, `deploys.py` and `builds.py` to the
reviewed code. It is the reviewer's own counterexample, and HPF-001 to HPF-004 and HPF-008 reject it.

If those files change and the patch stops applying, do **not** regenerate it with a whole-file
`git diff HEAD 531a24c7 -- <files>`: those files have gained code the rest of the tree depends on
(detached build launches and the pid column in `builds.py`, `powershell_command`/`powershell_argv`
and `scp_argv(pull=)` in `hosts.py`), so a wholesale revert plants a tree that cannot even import,
and the checks would report ERROR instead of the exit 1 the falsification needs. Re-plant the
counterexample and re-emit the patch instead:

```sh
uv run python scripts/ci/refresh_as_reviewed.py
```

It applies the current patch with fuzz, rewrites the blocks that drifted (keeping the newer
constructs the counterexample does not target), re-emits the patch with today's context, and fails
unless `git apply --check` then passes. Verify with
`bash scripts/ci/run.sh falsify hermes-pr-feedback-ci HPF-001 HPF-002 HPF-003 HPF-004 HPF-008`:
every `reject-as-reviewed` case must report HOLDS.

Two fixtures depend on PyPI as of the snapshot (`[tool.uv] exclude-newer`). HPF-006
`reject-floor-fails-suite` assumes fastapi 0.110.0 fails the suite, and `reject-floor-cannot-install`
assumes mcp needs pydantic above 2.0.0. When the snapshot moves, re-run `falsify` and update them if
they report `BROKEN`. Update the fixture only when the planted case itself stopped being a violation.
A check that accepts a real violation gets fixed in the check.

## Running it locally

```sh
uv sync --all-groups                                  # .venv: the project, pytest, ruff, pyyaml
npm ci --prefix tools/tsc --ignore-scripts            # the pinned tsc, so no cs_script test skips
.venv/bin/python scripts/ci/registry.py validate      # the registry and its invariants
scripts/ci/run.sh enforce hermes-pr-feedback-ci       # every reviewer rule against this checkout
scripts/ci/run.sh enforce cpompa-hermes-ci            # every house rule (needs gitleaks on PATH)
scripts/ci/run.sh falsify hermes-pr-feedback-ci       # every planted violation rejected, clean cases accepted
scripts/ci/run.sh falsify cpompa-hermes-ci HPF-005    # error: HPF-005 is not a rule of cpompa-hermes-ci
scripts/ci/run.sh check HPF-001 /path/to/other/tree   # one rule against any tree
env CHC_COMMITS_BASE=531a24c scripts/ci/run.sh check CHC-COMMITS   # pick the commit range explicitly
```

Set environment variables with `env VAR=... scripts/ci/run.sh ...`. In zsh, `VAR=... time cmd` is a
parse error.

What the checks need:

- HPF-006 and HPF-007 need network access to PyPI.
- HPF-006 runs the suite once per dependency plus once at the floors, about 3 minutes in total.
- HPF-008 needs full history, which is why CI sets `GIT_DEPTH: 0`.
- CHC-SHELL fetches the pinned ShellCheck wheel through `uvx` on first use.
- The commit range for CHC-COMMITS and CHC-SECRETS comes from `CHC_COMMITS_BASE`, the merge request's
  diff base, the branch's upstream, `CI_COMMIT_BEFORE_SHA`, or the default branch, in that order.
  When none of them gives a base, the check exits 2.

## How a reviewer comment becomes a rule

1. **Quote it.** Copy the comment verbatim. Split it into the separate things it asks for, give each
   one an item id, and form the feedback id `hermes-agent#<pr>/review-<review id>/<item>`. Use
   `comment-<id>` for an inline or issue comment.
2. **Decide the pipeline.** A reviewer asked for it, so it goes in `hermes-pr-feedback-ci`, even when
   a house rule already covers something nearby. If an existing rule already enforces exactly this
   concern, the registry refuses a second one. Cite the new feedback id in that rule's row instead.
3. **Fix the code first: the rule is not the fix.** The change the reviewer asked for lands as code
   and tests in the product, with its own test (TDD: red against the reviewed code, then green). The
   CI rule only keeps it fixed. A rule added without its fix makes `enforce` fail, by design. For
   review 5631127162, this is what landed:
   - **Item 1** is in `src/s2docs/hosts.py`. `parse_ssh_target` validates at registration and again
     at use. `ssh_argv`/`scp_argv` are the only argv builders and put `--` before the destination;
     the scp local operand is absolute and the port is `-p`/`-P`. `deploys.py` and `builds.py` use
     them and validate before taking a lease. `tests/test_ssh_hardening.py` covers it, including the
     reviewer's parametrised `-oProxyCommand=` test.
   - **Item 2** is in `pyproject.toml`: `fastapi>=0.110.1,<0.144`, `mcp[cli]>=2.0.0,<3`,
     `pydantic>=2.12.0,<3`, `uvicorn[standard]>=0.31.1,<0.55`. Each floor was found by running the
     suite on older releases and is proven by HPF-006. The release before each one fails:
     - fastapi 0.110.0 fails the suite;
     - mcp 1.30.0 fails it;
     - pydantic 2.11.10 cannot resolve beside mcp 2.0.0;
     - uvicorn 0.31.0 cannot resolve beside mcp 2.0.0.

     It also has `[tool.uv] exclude-newer = "2026-10-10T00:00:00Z"`, with `uv.lock` regenerated, and
     the same instant as `--exclude-newer` in `plugin/mcp.json`.
   - **Item 3**: `plugin/mcp.json`'s uvx ref pins the commit that carries all of the above.
4. **Add the registry row.** Fill in `id` (next free `HPF-NNN`), `pipeline`, `title`, a new `concern`
   slug, `source` (the comment URL), `feedback_id`, `reviewer`, `quote` (verbatim), `reviewed_at`
   (the commit the reviewer read), and `pinned_server`. Set `pinned_server: true` when the rule is
   about the server code or its declared dependencies, because users run the commit mcp.json pins.
   Add `enforces`, `script`, `fixtures`, and `clean` when needed.
5. **Write the check** as `scripts/ci/checks/hermes-pr-feedback-ci/HPF-NNN-<slug>.sh`, executable.
   - Its header repeats `rule`, `pipeline`, `feedback`, `source` and `enforces`.
   - Source `lib.sh`, call `ci_begin HPF-NNN "$@"`, and declare inputs and tools with `ci_need_file`
     and `ci_need_tool`.
   - Put the logic in a `ci_py` heredoc that returns violations through `cilib.run_check`.
   - Prefer behavioural checks (import the tree's code, fake the side effects) over text matching.
     Add a static scan when the rule is about where something may be built.
6. **Write the fixtures** in `ci/fixtures/HPF-NNN/`:
   - Plant at least the reviewer's own counterexample. For code, that is usually the code as reviewed.
   - Plant one `reject-*` per distinct way the fix could regress.
   - Add `accept-*` cases for legitimate variations the rule must not refuse.
7. **Prove it.** Run these three:
   - `scripts/ci/run.sh falsify hermes-pr-feedback-ci HPF-NNN`, where every case must be `HOLDS`;
   - `scripts/ci/run.sh check HPF-NNN`, which must pass on the fixed tree;
   - `scripts/ci/run.sh check HPF-NNN <export of the reviewed commit>`, which must exit 1.

   Run `env -u PYTHONPATH .venv/bin/pytest -q tests/test_ci_registry.py`. If the review has a coverage
   test there (like `test_review_5631127162_is_covered_item_by_item`), add the new item to it.
8. **Touch nothing else.** The new rule is the registry row, its script and its fixtures. No other
   check changes:
   - the runner picks the rule up from the registry;
   - HPF-008 re-verifies the pinned server against every `pinned_server` rule, new ones included;
   - the JUnit report carries the source and feedback id.

   If a new rule seems to need an edit to another check, the two rules share a concern. Fold the new
   feedback into the existing rule's row instead (step 2).

A house rule follows the same steps with `pipeline: cpompa-hermes-ci`, a `CHC-<NAME>` id,
`source: house rule` (or `coordinator security finding <YYYY-MM-DD>: <finding>` when a security
finding produced it), and no reviewer keys. It must not restate a reviewer rule. The shared `concern`
check enforces that.

## Commenting the delta on the upstream PR

After the fix is pushed and re-pinned, the PR gets a comment that says what changed. That comment
pastes the output of [`scripts/ci/pr_delta.py`](../scripts/ci/pr_delta.py) verbatim. Nobody types it
from memory, and nobody edits the values in it:

```sh
.venv/bin/python scripts/ci/pr_delta.py --from-sha <commit the reviewer read> --to-sha <commit pinned>
```

It reads every runtime range in `pyproject.toml` `[project].dependencies` exactly as written, the
`--from git+...@<sha>` ref and the `--exclude-newer` value from `plugin/mcp.json`, `version` from
`plugin/plugin.json`, and the commits of the range from `git log --oneline A..B`. It refuses (exit 2,
nothing on stdout) when a value is missing, the pin is not a full sha, or those files in the working
tree differ from `--to-sha`. A range or a sha retyped by hand is how a wrong number reaches a
reviewer, and once a reviewer has read it, it cannot be taken back. If the comment needs a value the
script does not print, extend the script; do not add the value by hand. `tests/test_pr_delta.py`
checks that the ranges come out byte-for-byte as they are written in the file.

## GitLab layout

`.gitlab-ci.yml` defines three stages. After `registry:validate`, the two pipeline stages run in
parallel, because each job only `needs:` the registry:

- `registry:validate`
- `hermes-pr-feedback-ci:enforce`, `hermes-pr-feedback-ci:falsify`
- `cpompa-hermes-ci:enforce`, `cpompa-hermes-ci:falsify`

Each job runs `scripts/ci/bootstrap.sh <components>`, then one `run.sh` command, and uploads
`ci-reports/*.xml` as JUnit.

Every job carries `tags: [s2docs]` through the `default:` block, so only a runner with that tag
(`mini-docker`) takes it and no verdict depends on which shared runner was free.

The `enforce` jobs run in every merge request and branch pipeline. The `falsify` jobs prove the
checks themselves, which takes minutes, so `.falsify-rules` runs them only:

- in scheduled pipelines;
- on `dev` and `main`, as a branch pipeline or as the source branch of an MR (an open dev→main MR
  replaces dev's branch pipelines, see `workflow:rules`);
- in merge requests whose `changes:` touch `ci/**/*`, `scripts/ci/**/*` or `.gitlab-ci.yml`.

Manual (web/API) runs stay allowed by `workflow:rules`; on a feature branch they get `enforce` only.
`tests/test_gitlab_ci.py` evaluates these rules for a push to dev, dev as an MR source, and a nightly
run, and fails if any registry rule is left without a `falsify` job in one of them. A check can
therefore not lose its falsification silently.

The image is `ghcr.io/astral-sh/uv:0.13.5-python3.11-trixie`, pinned by digest. `bootstrap.sh`
installs `uv sync --frozen`, node and npm plus `npm ci --ignore-scripts` for `tools/tsc`, and gitleaks
8.30.1 (sha256-verified). Each component either succeeds or exits non-zero and names itself.
