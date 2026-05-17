"""Regression tests for the `optv pull` git hygiene + single-instance guard.

  * `flock` makes `optv` single-instance — the cron fires every ~10 min but
    jobs run longer, so overlapping runs would race each other's git
    operations. A run that finds another in progress exits cleanly.
  * `optv pull` discards the curl-regenerated `metadata/entities.json` before
    `git pull`, so a generated file left in the working tree can't wedge the
    pull with "local changes would be overwritten by merge".
"""
import subprocess
from pathlib import Path

OPTV = Path(__file__).resolve().parent.parent / "optv"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def test_optv_is_single_instance_and_discards_generated_entities():
    """`optv` must be flock-guarded and discard entities.json before pulling."""
    script = OPTV.read_text()
    assert "flock" in script, \
        "optv must be flock-guarded against overlapping cron runs"
    assert "git checkout -- metadata/entities.json" in script, \
        "optv pull must discard the generated entities.json before `git pull`"
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

    # a second clone advances entities.json on origin
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
