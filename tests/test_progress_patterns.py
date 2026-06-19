"""Pins the cross-repo log-string contract with OpenParliamentTV-Tools.

Tools' work-aware stage runners emit a descriptive header (matched here to
advance Job.stage) only when a stage has work, and a no-op line otherwise. This
test fails loudly if a future Tools reword makes a work header stop advancing
the stage, or — just as bad — makes a no-op line falsely advance it.
"""

from __future__ import annotations

import re

from src.workflow.progress import _STAGE_PATTERNS

# Logging prefix Tools prepends (logging.basicConfig format). The runner matches
# via pat.search() on the raw line, so the prefix must not matter — assert both.
_PREFIX = "2026-06-19 12:15:24 INFO     optv.shared.workflow "


def _stage_for(line: str) -> str | None:
    for pat, stage in _STAGE_PATTERNS:
        if pat.search(line):
            return stage
    return None


# The work-case headers Tools emits when a stage actually processes sessions
# (note the trailing "(N session(s))" count the refactor added — appended, so
# the substrings still match).
WORK_HEADERS = {
    "Downloading media and proceeding data for period 21": "download",
    "Merging data from /d/media and /d/proceedings into /d/merged (3 session(s))": "merge",
    "Linking entities with wikidata IDs (3 session(s))": "nel",
    "Updating time-alignment for merged files (3 session(s))": "align",
    "Updating NER for published sessions (3 session(s))": "ner",
}

# The honest no-op lines — must match NO stage pattern, so Job.stage does not
# falsely advance on a run that processed nothing.
NOOP_LINES = [
    "Merge: all in-scope sessions up to date, nothing to merge",
    "NEL link: all sessions current, nothing to link",
    "NEL link: no entities.json available, nothing to link",
    "Time-alignment: all sessions current, nothing to align",
    "NER: all sessions current, nothing to extract",
    "NEL entity dump unchanged (2720 entities)",
    "No entity-dump platform configured for DE — using committed entities.json",
]


def test_work_headers_advance_the_expected_stage():
    for line, stage in WORK_HEADERS.items():
        assert _stage_for(line) == stage, f"{line!r} should map to {stage!r}"
        assert _stage_for(_PREFIX + line) == stage, f"prefixed {line!r} should map to {stage!r}"


def test_noop_lines_never_advance_a_stage():
    for line in NOOP_LINES:
        assert _stage_for(line) is None, f"{line!r} must not match any stage pattern"
        assert _stage_for(_PREFIX + line) is None, f"prefixed {line!r} must not match"
