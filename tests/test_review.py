"""`s2docs review` — the per-area swarm-review scoper (a pure git client).

Partitioner unit tests run against a throwaway git repo built in tmp_path:
a ``main`` base, a ``feat`` branch that diverges (modify / add / delete), and a
``main`` commit after the divergence that lands an identical copy of one file
(creating the stale-merge-base "already integrated" case).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from s2docs import cli, review


def _git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _write(repo, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Test")

    _write(r, "keep.txt", "a\nb\nc\n")
    _write(r, "docs/STATUS.md", "# status\n")
    _write(r, "tools/build/compile_map.py", "print(1)\n")
    _write(r, "dell.txt", "x\ny\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")

    _git(r, "checkout", "-q", "-b", "feat")
    _write(r, "tools/build/compile_map.py", "print(2)\nprint(3)\n")
    _write(r, "tools/build/extra.py", "x = 1\n")
    _write(r, "docs/STATUS.md", "# status\n\n- new\n")
    (r / "dell.txt").unlink()
    _write(r, "shared.txt", "identical\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "feat work")

    # main advances after the divergence: the same shared.txt content lands on
    # base via another path (stale merge-base → "already integrated").
    _git(r, "checkout", "-q", "main")
    _write(r, "shared.txt", "identical\n")
    _write(r, "main_only.txt", "landed\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "main advances")
    return r


def test_default_grouping_and_stats(repo):
    scope = review.build_scope(repo=repo, base="main", branch="feat")
    assert set(scope.areas) == {"tools/build", "docs", "root"}
    build = scope.areas["tools/build"]
    assert [f.path for f in build] == ["tools/build/compile_map.py", "tools/build/extra.py"]
    assert (build[0].insertions, build[0].deletions) == (2, 1)
    assert (build[1].insertions, build[1].deletions) == (1, 0)
    assert [f.path for f in scope.areas["docs"]] == ["docs/STATUS.md"]


def test_area_override_last_match_wins(repo):
    specs = review.parse_area_specs(["first:tools/**", "build:tools/build/**"])
    scope = review.build_scope(repo=repo, base="main", branch="feat", area_specs=specs)
    assert "build" in scope.areas and "first" not in scope.areas
    assert [f.path for f in scope.areas["build"]] == ["tools/build/compile_map.py", "tools/build/extra.py"]
    assert "docs" in scope.areas  # unmatched files keep the default area


def test_already_integrated_partition(repo):
    scope = review.build_scope(repo=repo, base="main", branch="feat")
    assert scope.already_integrated == ["shared.txt"]
    reviewed = [f.path for files in scope.areas.values() for f in files]
    assert "shared.txt" not in reviewed
    assert scope.changed == 5  # 4 reviewed + 1 integrated


def test_deleted_file_is_visible_with_zero_stats(repo):
    scope = review.build_scope(repo=repo, base="main", branch="feat")
    deleted = {f.path: f for f in scope.areas["root"]}["dell.txt"]
    assert (deleted.insertions, deleted.deletions) == (0, 0)


def test_missing_ref_raises(repo):
    with pytest.raises(review.ReviewError, match="unknown ref"):
        review.build_scope(repo=repo, base="nope", branch="feat")
    with pytest.raises(review.ReviewError, match="unknown ref"):
        review.build_scope(repo=repo, base="main", branch="nope")


def test_non_repo_path_raises(tmp_path):
    with pytest.raises(review.ReviewError, match="not a git repository"):
        review.build_scope(repo=tmp_path, base="main", branch="feat")


def test_parse_area_specs_rejects_malformed():
    with pytest.raises(review.ReviewError):
        review.parse_area_specs(["nocolon"])
    with pytest.raises(review.ReviewError):
        review.parse_area_specs(["name:"])
    assert review.parse_area_specs(["build:tools/build/*"]) == [("build", ("tools/build/*",))]


def test_auto_area_two_components():
    assert review.auto_area("tools/build/x.py") == "tools/build"
    assert review.auto_area("docs/STATUS.md") == "docs"
    assert review.auto_area("README.md") == "root"


def test_cli_wires_review_subcommand_json(repo, capsys):
    rc = cli.main(["--json", "review", "feat", "--repo", str(repo), "--base", "main"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "s2docs/v1"
    assert payload["branch"] == "feat" and payload["base"] == "main"
    assert payload["merge_base"]
    assert payload["already_integrated"] == ["shared.txt"]
    assert {a["name"] for a in payload["areas"]} == {"tools/build", "docs", "root"}
    build = next(a for a in payload["areas"] if a["name"] == "tools/build")
    assert {"path", "insertions", "deletions"} <= set(build["files"][0])


def test_cli_json_flag_after_subcommand(repo, capsys):
    rc = cli.main(["review", "feat", "--repo", str(repo), "--base", "main", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["branch"] == "feat"


def test_cli_markdown_output(repo, capsys):
    rc = cli.main(["review", "feat", "--repo", str(repo), "--base", "main"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# Review feat vs main" in out
    assert "5 changed file(s), 1 already-integrated excluded" in out
    assert "## tools/build (2 files)" in out
    assert "- tools/build/compile_map.py (+2/-1)" in out


def test_cli_missing_ref_exits_error(repo, capsys):
    rc = cli.main(["review", "feat", "--repo", str(repo), "--base", "nope"])
    assert rc == 1
    assert "unknown ref" in capsys.readouterr().err
