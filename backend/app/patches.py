"""Agno compatibility patches.

These monkey-patches work around two issues in Agno (as of v2.6.22).
Each is guarded so it self-reports when Agno ships a native fix,
making upgrades safe and the patches easy to retire.

Import this module once at startup (main.py) before building the team.
"""

import functools
import warnings

from agno.db.sqlite import SqliteDb
from agno.scheduler.manager import ScheduleManager

# ── Patch 1: ScheduleManager.__deepcopy__ ─────────────────────────────
#
# Agno deepcopies tool state (including SchedulerTools -> ScheduleManager)
# during team runs. ScheduleManager holds a SqliteDb whose SQLAlchemy engine
# is unpicklable. The manager is a shared identity handle, so returning self
# on deepcopy is semantically correct.
#
# Agno already defines __deepcopy__ on Model, Gemini, LiteLLM, AzureOpenAI,
# and PgVector — but not yet on ScheduleManager. Guard so we notice when they do.

if "__deepcopy__" not in ScheduleManager.__dict__:
    ScheduleManager.__deepcopy__ = lambda self, memo: self
else:
    warnings.warn(
        "ScheduleManager now defines __deepcopy__ natively — "
        "remove the patch in app/patches.py",
        DeprecationWarning,
        stacklevel=2,
    )


# ── Patch 2: claim_due_schedule lock grace ────────────────────────────
#
# Agno's claim_due_schedule() treats a lock as stale after lock_grace_seconds
# (default 300s) and re-claims + re-fires the schedule. Long-running schedules
# can cross the 5-min threshold and double-fire. Bump the default to 900s.
#
# We wrap the original method (with functools.wraps to preserve introspection)
# and only inject the new default — callers can still pass an explicit value.

DEFAULT_LOCK_GRACE_SECONDS = 900
_original_claim_due_schedule = SqliteDb.claim_due_schedule


@functools.wraps(_original_claim_due_schedule)
def _claim_due_schedule(self, worker_id, *args, lock_grace_seconds=None, **kwargs):
    if lock_grace_seconds is None:
        lock_grace_seconds = DEFAULT_LOCK_GRACE_SECONDS
    return _original_claim_due_schedule(
        self, worker_id, *args, lock_grace_seconds=lock_grace_seconds, **kwargs
    )


SqliteDb.claim_due_schedule = _claim_due_schedule
