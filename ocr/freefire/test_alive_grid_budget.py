"""Every alive-grid row's team name must eventually get read.

Run it with no arguments:

    python ocr\\freefire\\test_alive_grid_budget.py

Pass a directory to point it at a different copy of the engine, which is
how it was checked against the version from before the fix:

    python ocr\\freefire\\test_alive_grid_budget.py C:\\some\\older\\copy

WHAT THIS GUARDS

classify_alive_grid_crops reads twelve elim numbers and twelve team names
against one 140ms budget, and reads all twelve numbers before the first
name. Two ways that starved the names completely -- not slowly, but so
that no name on any row was ever read, while every elim stayed current:

  * The budget running out inside the elim pass. No rotation of the ROWS
    can fix that, because what is starving the names is the order of the
    two PASSES. (The `11` case below.)
  * The rotation cursor advancing by the COUNT of deferred reads. With
    twelve rows, a poll that reads every elim and no name defers exactly
    twelve -- one whole turn of the cursor, landing it back where it
    started, so the same rows are read and deferred forever. A fixed
    point that looks like slow progress. (The `12` case below.)

Both showed up on the dashboard as rows stuck on "waiting its turn to be
read" indefinitely.

TWO THINGS ARE DELIBERATE

The clock is FAKE and advances a fixed step per OCR call, so "how many
reads fit in the budget" is exact rather than whatever the machine
happened to manage. The fixed point only appears when the number of
completed reads is an exact multiple of the row count, which a wall-clock
test hits only by luck -- an earlier version of this test passed against
the broken engine for precisely that reason.

Every crop is unique random noise, so the engine's OCR memo never hits. A
memo hit is free, and free reads are the case where nothing is starved
and there is no bug to see. The busy table -- a firefight, every crop
changing -- is when the budget actually bites.

The set-up checks itself each poll, because an arrangement that quietly
fails to create the condition would otherwise "pass" against a broken
engine, which is the one outcome a test like this must never produce.
"""
import os
import sys

import numpy as np

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1
                else os.path.dirname(os.path.abspath(__file__)))
import freefire_engine as E

ROWS, PLAYERS = 12, 4
ATTEMPTS = ROWS * 2          # one elim + one name per row


class FakeTime:
    """Stands in for the `time` module inside the engine."""

    def __init__(self, real):
        self._real = real
        self.now = 1000.0

    def perf_counter(self):
        return self.now

    def __getattr__(self, name):        # everything else stays real
        return getattr(self._real, name)


def run(reads_that_fit, polls=60):
    """Sizes the per-read cost so exactly `reads_that_fit` of the 24
    attempted calls complete within FREEFIRE_OCR_BUDGET_MS.

    A read proceeds while elapsed <= deadline, so with k reads done the
    (k+1)th still starts when k*cost <= budget: the count that completes
    is floor(budget/cost) + 1. Putting the ratio half-way between two
    integers lands that count exactly on `reads_that_fit`.
    """
    import time as real_time
    clock = FakeTime(real_time)
    budget = E.FREEFIRE_OCR_BUDGET_MS / 1000.0
    cost = budget / (reads_that_fit - 0.5)

    E.time = clock
    E._ocr_rotation = 0
    E._OCR_MEMO.clear()
    rng = np.random.default_rng(7)

    completed = []
    overrun = set()
    seen = set()                      # id() of each crop actually read

    def stub(img, *a, **k):
        def read():
            clock.now += cost
            completed.append(1)
            return "X"
        value = E._ocr_memoised("k", [img], read)
        if value is not E._OCR_SKIPPED:
            seen.add(id(img))
        return value

    E.ocr_small_number = stub
    E.ocr_team_name = stub

    team_rows = set()
    for poll in range(polls):
        crops = [([rng.integers(0, 255, (27, 9, 3), dtype=np.uint8)
                   for _ in range(PLAYERS)],
                  rng.integers(0, 255, (41, 52, 3), dtype=np.uint8),
                  rng.integers(0, 255, (29, 93, 3), dtype=np.uint8))
                 for _ in range(ROWS)]
        del completed[:]
        seen.clear()
        E.classify_alive_grid_crops(crops)

        # The budget must buy what it was sized for, and the engine may
        # lend exactly ONE read on top to keep the names from starving.
        # More than one means that loan is open-ended, which is the thing
        # the budget exists to prevent -- so this is a real assertion,
        # not only a set-up check.
        over = len(completed) - reads_that_fit
        if over < 0 or over > 1:
            raise AssertionError(
                "poll %d: budget sized for %d reads, %d completed (%+d)"
                % (poll, reads_that_fit, len(completed), over))
        overrun.add(over)

        for row in range(ROWS):
            if id(crops[row][2]) in seen:
                team_rows.add(row)
    return sorted(team_rows), max(overrun)


def main():
    print("engine under test:", E.__file__)
    failures = 0
    # 12 is the cursor's fixed point: every elim read, no name, and twelve
    # deferrals is one whole turn of a twelve-row cursor. 11 starves the
    # names without involving the cursor at all. 24 is the control -- room
    # for everything, which must keep working either way.
    for fits in (12, 11, 24):
        rows, over = run(fits)
        missing = [r for r in range(ROWS) if r not in rows]
        failures += bool(missing)
        print("  [%s] %2d/%d reads fit -> %2d/%d team rows ever read, "
              "missing %s  [overrun %+d read]"
              % ("PASS" if not missing else "FAIL", fits, ATTEMPTS,
                 len(rows), ROWS, missing, over))
    print("RESULT:", "all rows reached" if not failures
          else "%d starved case(s)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
