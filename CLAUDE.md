# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# First-time setup (clones Tools + Data-DE into ./data/, copies config samples,
# generates JWT_SECRET). Edit config/secrets.env + config/users.yaml after.
./scripts/setup.sh

# Run locally without Docker (requires Python 3.11+; aeneas in requirements.txt
# needs libespeak-dev + setuptools<72 — see Dockerfile if install fails)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=data/OpenParliamentTV-Tools .venv/bin/uvicorn src.main:app --reload

# Run in Docker (dev: HTTP on :8000; the local docker-compose.override.yml
# bind-mounts src/ for --reload and mounts Data-DE from outside ./data/)
docker compose up -d
docker compose --profile production up -d   # + nginx TLS on :443
docker compose --profile ner up -d          # + entity-fishing on :8090

# Tests
.venv/bin/pytest -q
.venv/bin/pytest tests/test_workflow_runner.py::test_runner_streams_logs_and_updates_progress -q

# Ops
./scripts/backup.sh                         # tars config/ (sans secrets) + status/
./scripts/compat_check.sh <session-id>      # verifies manager == legacy ./optv output
                                            # requires BASE_URL + COMPAT_COOKIE env
```

Health check: `curl http://localhost:8000/health`. Live job log: `/jobs/<id>` (WebSocket at `/ws/logs/<id>`).

## Architecture

### Manager / Tools split

This repo is a **FastAPI conductor** for the separate **OpenParliamentTV-Tools** repo. The Tools repo is cloned into `data/OpenParliamentTV-Tools/` and used by the manager in two ways:

- **Workflow execution** runs as a **subprocess**: each job spawns `python -m optv.parliaments.<id>.workflow ...` with `PYTHONPATH=parliament.tools_dir`. Before each job, the runner does `git pull` on `tools_dir` and `data_dir`, so Tools changes pushed to `main` propagate to the next job automatically — same semantics as the legacy `optv pull && optv update` cron, no Conductor restart needed. Stdout is read line-by-line, streamed to `LogStreamer`, and pattern-matched for stage/session progress (see [progress.py](src/workflow/progress.py)).
- **In-process imports** are used only for `optv.parliaments.<id>.common` (session counting in `_estimate_total_sessions`, UI session-status reads in [session_content.py](src/services/session_content.py) and [status_tracker.py](src/services/status_tracker.py)) and for `optv.parliaments.load_manifest` (per-parliament metadata at startup). `common.py` is stable; if it changes, run `docker compose restart app` to pick it up. Workflow code (`workflow.py`, scrapers, parsers, mergers, `optv.shared.*`) does NOT need a restart — fresh subprocess per job.

Per-parliament metadata that's a code capability (name, language, periods, supported stages, official entity-dump URL, retry tuning) lives in **`optv/parliaments/<id>/manifest.yaml`** in the Tools repo and is loaded via `optv.parliaments.load_manifest()`. Conductor's `config/parliaments.yaml` holds deployment-only fields (`tools_dir`, `data_dir`, `git_remote`, `stages`) and may override any manifest field. Adding a new parliament: ship `workflow.py` + `common.py` + `manifest.yaml` in Tools, add a 2-line block to `parliaments.yaml`, run `setup.sh`, restart Conductor.

The legacy `optv` bash script at the repo root is the pre-manager path and is intentionally unchanged; `scripts/compat_check.sh` diffs the two to prove parity. The legacy cron and Conductor coexist in different directories with separate Tools+Data checkouts and separate SSH/git identities — see "Git identity + SSH" below.

### Job lifecycle

Jobs flow through a **file-backed queue** (not a database) in `status/jobs/`:
- `queue.json` (FIFO), `current.json` (single in-progress job), `history/<id>.json` (one per terminal state).
- All writes guarded by `fcntl.flock` on `.lock` so the FastAPI workers, scheduler, and external tooling can read/write safely. See [JobManager](src/services/job_manager.py).
- Exactly one [Worker](src/services/worker.py) task runs inside the FastAPI event loop and drains the queue sequentially (1s poll). There is no multi-worker concurrency — running two jobs at once is not supported.
- Cancellation is cooperative: queued jobs are removed immediately; running jobs get `status=cancelling` and the runner checks between stages via `_is_cancelled`.

### Stages

UI stages: `download`, `parse`, `merge`, `nel`, `align`, `ner`, `publish`. Only the first six map to subprocess `workflow.py` CLI flags (`_PIPELINE_STAGES` + `_build_argv` in [runner.py](src/workflow/runner.py)). `publish` is handled separately — the runner shells out to `git add/commit/push` inside `parliament.data_dir`, using `GIT_USER_NAME` / `GIT_USER_EMAIL` from `secrets.env` threaded via `git -c user.name=... -c user.email=...` per command (no global git config mutation). `git commit` returning 1 (nothing to commit) is tolerated; any other non-zero exit aborts.

### Git identity + SSH

The container mounts a dedicated SSH keystore at `/root/.ssh` from `${SSH_KEY_DIR:-./config/ssh}`. `setup.sh` generates `config/ssh/id_ed25519` (no passphrase, since the container can't enter one) and pins `github.com` in `known_hosts`. The public key must be added as a **deploy key with write access** on each Data repo — `setup.sh` prints the URLs. One key authorizes pull+push for every Data repo it's installed on; no per-parliament key juggling.

The legacy `optv` cron uses the Pi user's `~/.ssh` and `git config --global` — completely separate from the Conductor container's `config/ssh/` and `secrets.env`. They never interfere.

### Progress + log streaming

Workflow subprocess stdout is read line-by-line and pushed to `LogStreamer`. Each line is regex-matched against `_STAGE_PATTERNS` and `_SESSION_PATTERNS` in [progress.py](src/workflow/progress.py) to advance `Job.stage` and bump `Job.sessions_completed`/`Job.progress`. **If Tools changes the matching log strings (e.g. `"Publishing 21045"`, `"Time-aligning 21045"`), progress bars break silently — update the patterns in `progress.py` to match.** Tools' `workflow.py` calls `logging.basicConfig(format='%(asctime)s %(levelname)-8s %(name)s %(message)s')`, so the patterns operate on formatted lines (with timestamp prefixes) — they use `pat.search()` for substring matching, which works either way.

The `JobLogHandler` class in `progress.py` is a leftover from the in-process era; it's not currently wired by the runner (in-process Tools loggers no longer exist for workflow code), but kept around in case `_estimate_total_sessions` or other in-process callers want it.

### Services wiring

Services are **not FastAPI-DI-constructed**. [src/main.py](src/main.py) builds them once in the `lifespan` context manager and stashes them on a module-level [Registry](src/services/registry.py) singleton. FastAPI routes read them via `Depends(get_job_manager)` etc. The config (`get_config`) is a separate `@lru_cache` singleton loaded from `config/*.yaml` + `config/secrets.env`.

### Config hot-reload

`config/schedules.yaml` is watched by `watchfiles` in [SchedulerService._watch_config](src/services/scheduler.py); edits reload schedules within seconds without restarting. `parliaments.yaml`, `users.yaml`, `notifications.yaml`, and `secrets.env` are read once at startup — restart the container to pick up changes.

### Auth + RBAC

GitHub OAuth → JWT in an `httpOnly` cookie (`optv_token`). Roles: `viewer` < `editor` < `admin`; every `/api/*` handler gates with `Depends(require_role(...))`. Unauthenticated page routes redirect to `/login` (not 401). `config/users.yaml` is the allow-list — users not in it get 403 at callback. WebSocket handshakes reuse the cookie via `_authorize()` in [websocket.py](src/api/websocket.py).

**Auth master switch.** `AUTH_ENABLED` in `secrets.env` (default `true`) turns the whole thing off in one place: when `false`, login is bypassed and every request resolves to the synthetic `ANONYMOUS_ADMIN` (defined in [config.py](src/config.py)) — `username=local-admin`, `role=admin`. The four resolvers short-circuit on this flag: `current_user` ([dependencies.py](src/auth/dependencies.py)), `_user_from_cookie` ([pages.py](src/web/pages.py)), WS `_authorize`, and `/auth/login` (redirects to `/`). `validate_startup()` then stops requiring `GITHUB_CLIENT_ID/SECRET`, `JWT_SECRET`, and a non-empty `users.yaml`, and logs a loud warning. The nav user widget and the login button are hidden via the `auth_enabled` template var (threaded through `_nav_ctx`). No safety guard — disabling it anywhere (incl. the production profile) gives every visitor admin; that's the operator's call. Read once at startup, so restart to flip it. `setup.sh` prompts "Enable GitHub authentication?" (default yes) and skips the OAuth/JWT/users.yaml steps when declined.

### Templating

Jinja2 templates under `src/templates/` use HTMX + Alpine + Tailwind (CDN). Page routes in [web/pages.py](src/web/pages.py) return full HTML for navigations and fragments for HTMX swaps; they check auth and redirect, unlike the `/api/*` JSON routes which return 401/403.

## Conventions

- **Do not** add DB migrations or ORM code — the job store is deliberately files + `flock`.
- **Do not** modify the legacy `optv` bash script; it is the compat baseline.
- **Per-parliament metadata goes in the Tools manifest** (`optv/parliaments/<id>/manifest.yaml`), not in Conductor config — see "Manager / Tools split" above. The principle: facts that make Tools usable standalone (name, language, supported periods/stages, official entity-dump URL) live in Tools; deployment-only fields (paths, git remotes, stage selection) live in Conductor's `parliaments.yaml`. Conductor can override any manifest field per-parliament if a deployment needs to deviate.
- `current_period` is **computed** as `max(parliament.periods)` — don't add it as a config field. The accessor `parliament.current_period` is preserved for all 17 call sites.
- When editing anything that touches the Tools subprocess invocation, keep CLI args in sync: `_build_argv` in [runner.py](src/workflow/runner.py) writes them; the Tools `workflow.py` argparse reads them.
- Tests inject fake `optv.parliaments.<id>.common` into `sys.modules` for in-process `_estimate_total_sessions` calls, and write a real on-disk `workflow.py` for the subprocess to import (see [test_workflow_runner.py](tests/test_workflow_runner.py)).
- `config/secrets.env`, `config/*.yaml` (non-sample), `config/ssh/`, `status/`, and `data/` are gitignored — never commit them.
