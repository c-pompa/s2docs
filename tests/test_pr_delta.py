"""scripts/ci/pr_delta.py echoes what the repository says, never a reconstruction of it.

An upstream PR comment pastes the delta this script prints (docs/FEEDBACK_CI.md). These tests build
a throwaway repository from a fixture pyproject and assert that every runtime range, the uvx ref,
the --exclude-newer instant, the plugin version and the commit list come out byte-for-byte.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "pr_delta.py"
FIXTURE = Path(__file__).parent / "fixtures" / "pr_delta" / "pyproject.fixture.toml"
PIN = "0123456789abcdef0123456789abcdef01234567"
INSTANT = "2026-10-10T00:00:00Z"
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           **{f"GIT_{r}_{k}": v for r in ("AUTHOR", "COMMITTER") for k, v in (("NAME", "t"), ("EMAIL", "t@x.invalid"))}}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout


def _mcp(pin: str = PIN, instant: str = INSTANT) -> dict:
    return {"mcpServers": {"s2docs": {"type": "stdio", "command": "uvx", "args": [
        "--exclude-newer", instant, "--from", f"git+https://example.invalid/s2docs.git@{pin}", "s2docs", "mcp"]}}}


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "plugin").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base: as reviewed")
    (repo / "pyproject.toml").write_bytes(FIXTURE.read_bytes())
    (repo / "plugin" / "mcp.json").write_text(json.dumps(_mcp(), indent=2), encoding="utf-8")
    (repo / "plugin" / "plugin.json").write_text(json.dumps({"name": "s2docs", "version": "0.1.2"}), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "deps: bound the runtime ranges")
    (repo / "README").write_text("fixed\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "plugin: re-pin")
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo), *args], capture_output=True, text=True)


def _fixture_ranges() -> list[bytes]:
    """The dependency strings as raw bytes of the fixture file, cut out of [project] by hand."""
    raw = FIXTURE.read_bytes()
    block = raw.split(b"dependencies = [", 1)[1].split(b"]\n", 1)[0]
    return re.findall(rb'^\s+"(.*)",$', block, flags=re.MULTILINE)


def test_every_runtime_range_is_echoed_byte_for_byte(repo):
    proc = _run(repo, "--from-sha", "HEAD~2", "--to-sha", "HEAD")
    assert proc.returncode == 0, proc.stderr
    ranges = _fixture_ranges()
    assert len(ranges) == 4
    listed = [line[3:-1].encode() for line in proc.stdout.splitlines() if line.startswith("- `") and
              not line.startswith(("- `--", "- pinned"))]
    assert listed == ranges, (listed, ranges)
    assert b"pytest" not in b"".join(listed), "a dev-group dependency is not a runtime range"


def test_the_launch_pin_version_and_commits_are_read_back(repo):
    proc = _run(repo, "--from-sha", "HEAD~2", "--to-sha", "HEAD")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert f"- `--from git+https://example.invalid/s2docs.git@{PIN}`" in out
    assert f"- pinned commit: `{PIN}`" in out
    assert f"- `--exclude-newer {INSTANT}`" in out
    assert "Plugin version (`plugin/plugin.json`): `0.1.2`" in out
    a, b = _git(repo, "rev-parse", "HEAD~2", "HEAD").split()
    assert f"Commits `{a}..{b}`" in out
    assert _git(repo, "log", "--oneline", f"{a}..{b}").rstrip("\n") in out
    assert "base: as reviewed" not in out, "the reviewed commit itself is not part of the delta"


@pytest.mark.parametrize(
    ("plant", "message"),
    [
        pytest.param(lambda r: (r / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8"),
                     "no readable [project].dependencies", id="no-dependencies"),
        pytest.param(lambda r: (r / "plugin" / "mcp.json").write_text(json.dumps(_mcp(pin="main")), encoding="utf-8"),
                     "not a full 40-hex commit sha", id="pin-is-a-branch"),
        pytest.param(lambda r: (r / "plugin" / "plugin.json").unlink(), "unreadable input: plugin/plugin.json",
                     id="no-plugin-json"),
    ],
)
def test_an_unreadable_value_prints_nothing_and_exits_2(repo, plant, message):
    plant(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "break an input")
    proc = _run(repo, "--from-sha", "HEAD~3", "--to-sha", "HEAD")
    assert proc.returncode == 2 and proc.stdout == "", proc.stdout
    assert message in proc.stderr, proc.stderr


def test_local_edits_against_the_described_commit_are_refused(repo):
    text = (repo / "pyproject.toml").read_text(encoding="utf-8").replace("<0.144", "<0.200")
    (repo / "pyproject.toml").write_text(text, encoding="utf-8")
    proc = _run(repo, "--from-sha", "HEAD~2", "--to-sha", "HEAD")
    assert proc.returncode == 2 and proc.stdout == ""
    assert "differ from HEAD" in proc.stderr


def test_an_unknown_commit_is_refused(repo):
    proc = _run(repo, "--from-sha", "f" * 40, "--to-sha", "HEAD")
    assert proc.returncode == 2 and f"{'f' * 40} is not a commit" in proc.stderr
