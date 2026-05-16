"""Regression tests for the `optv pull` force-sync + single-instance guard.

The cron fires every ~10 min, but pipeline jobs run much longer, so runs
overlap. And the legacy `optv pull` curl-wrote `metadata/entities.json` into
the Data-DE working tree, which could wedge a later `git pull`. The fix:

  * `flock` makes `optv` single-instance — overlapping cron runs exit cleanly,
    so git operations never race a running pipeline.
  * `optv pull` force-syncs each repo to origin (`git fetch && git reset
    --hard @{u}`) instead of a plain `git pull` — recovering from any dirty
    or diverged state. Safe because the flock guarantees no job is running.
"""
import subprocess
from pathlib import Path

OPTV = Path(__file__).resolve().parent.parent / "optv"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def test_optv_is_single_instance_and_force_syncs():
    """`optv` must be flock-guarded and force-sync to origin in `pull`."""
    script = OPTV.read_text()
    assert "flock" in script, \
        "optv must be flock-guarded against overlapping cron runs"
    assert "git reset --hard @{u}" in script, \
        "optv pull must force-sync to origin, not a plain `git pull`"


def test_force_sync_recovers_dirty_and_diverged_repo(tmp_path):
    """`git fetch && git reset --hard @{u}` recovers from BOTH a dirty working
    tree and a stranded local commit — the states a plain `git pull` wedges on."""
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
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "branch", f"--set-upstream-to=origin/{branch}", branch)

    # origin advances (a second clone pushes a newer entities.json)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "metadata" / "entities.json").write_text('{"v": 2}\n')
    _git(other, "commit", "-aqm", "advance entities")
    _git(other, "push", "-q")

    # the Pi's repo is BOTH dirty (curl) AND diverged (stranded unpushed commit)
    entities.write_text('{"v": 99}\n')
    (repo / "stranded.txt").write_text("unpushed pipeline output\n")
    _git(repo, "add", "stranded.txt")
    _git(repo, "commit", "-q", "-m", "stranded local commit")

    # a plain pull cannot fast-forward this — it would abort
    blocked = subprocess.run(["git", "pull", "origin", branch], cwd=str(repo),
                             capture_output=True, text=True)
    assert blocked.returncode != 0, "expected the diverged/dirty repo to block a plain pull"

    # the fix: fetch + reset --hard @{u} force-syncs cleanly to origin
    _git(repo, "fetch", "origin")
    _git(repo, "reset", "--hard", "@{u}")
    assert entities.read_text() == '{"v": 2}\n'      # origin's version restored
    assert not (repo / "stranded.txt").exists()      # stranded commit discarded
