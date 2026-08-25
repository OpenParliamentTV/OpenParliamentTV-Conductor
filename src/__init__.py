"""OpenParliamentTV Conductor.

`__version__` is the deploy marker. The container has no git checkout inside it
— the Dockerfile copies `src/` only — so this string is the one thing that can
tell you which code an image is actually running. `/health` reports it, and
every job log opens with it.

Bump it whenever you push something you need to confirm has landed on a
deployment you can't otherwise inspect.
"""

__version__ = "2026-08-25-resource-guard"
