"""Regression test for the `optv pull` entities.json wedge.

The legacy `optv` script curl-overwrites `metadata/entities.json` into the
Data-DE working tree on every `pull`. Before the fix, a `git pull` that also
updated that file aborted with "local changes would be overwritten by merge",
silently stranding the Pi (which cannot be reconciled by hand). The fix
discards the generated file (`git checkout -- metadata/entities.json`) before
pulling, so the pull always fast-forwards.
"""
import subprocess
from pathlib import Path

OPTV = Path(__file__).resolve().parent.parent / "optv"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def test_optv_pull_discards_entities_before_pulling():
    """The `pull` command must discard entities.json before the curl/pull."""
    script = OPTV.read_text()
    assert "git checkout -- metadata/entities.json" in script, \
        "optv `pull` no longer discards the generated entities.json"
    # the discard must precede the curl that re-creates the file
    assert (script.index("git checkout -- metadata/entities.json")
            < script.index("entity-dump")), \
        "the entities.json discard must run before the curl re-fetch"


def test_dirty_entities_json_does_not_block_pull(tmp_path):
    """A locally-modified entities.json blocks a plain pull, but discarding it
    first lets the pull fast-forward — the reconciliation the fix relies on."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    meta = repo / "metadata"
    meta.mkdir()
    entities = meta / "entities.json"
    entities.write_text('{"v": 1}\n')
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))

    # a second clone advances entities.json on origin (a manual dev-machine push)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "metadata" / "entities.json").write_text('{"v": 2}\n')
    _git(other, "commit", "-aqm", "advance entities")
    _git(other, "push", "-q", "origin", f"HEAD:{branch}")

    # the curl leaves entities.json locally modified and uncommitted
    entities.write_text('{"v": 99, "curl": true}\n')

    # plain pull is blocked — the failure mode the fix addresses
    blocked = subprocess.run(["git", "pull", "origin", branch], cwd=str(repo),
                             capture_output=True, text=True)
    assert blocked.returncode != 0, "expected the dirty file to block the pull"

    # the fix: discard the generated file, then the pull fast-forwards cleanly
    _git(repo, "checkout", "--", "metadata/entities.json")
    ok = subprocess.run(["git", "pull", "origin", branch], cwd=str(repo),
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert entities.read_text() == '{"v": 2}\n'
