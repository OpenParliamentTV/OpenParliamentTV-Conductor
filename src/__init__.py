"""OpenParliamentTV Conductor.

Build identity, reported by `/health` and stamped on every job log's first line.
The image carries no git checkout — the Dockerfile copies `src/` only — so
neither of these can be derived at runtime; both have to be put there at build
time.

`__commit__` is authoritative when present: the Dockerfile takes a `GIT_SHA`
build arg that `scripts/self-update.sh` fills with the commit it just pulled.
It is None for images built any other way (a manual `docker compose up --build`,
or by a self-update.sh predating the stamping), which is why `__version__`
still exists — a hand-set string that always answers "did my push land?".

Bump `__version__` when pushing something whose arrival you need to confirm.
Once `__commit__` is reliably populated on a deployment, it supersedes it.
"""

import os

__version__ = "2026-08-26-commit-stamp"

# "unknown" is the Dockerfile's ARG default, i.e. nobody stamped this image.
# Normalized to None so /health shows a null rather than a plausible-looking
# value that means nothing.
__commit__ = os.environ.get("CONDUCTOR_COMMIT") or None
if __commit__ == "unknown":
    __commit__ = None
