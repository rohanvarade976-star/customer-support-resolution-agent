"""Saves each finished eval run straight away, so an interrupted eval resumes where it stopped.

The file is JSON Lines: one `CaseResult` per line, appended and flushed to disk as soon as a
run ends. A crash can only lose the run in progress, or leave a half-written last line,
which is ignored on the next load (that run simply happens again).

A key is (configuration, case id, repeat). If a key appears more than once (a run whose
models were unreachable, retried later), the last line wins.
"""

import os
from pathlib import Path

from pydantic import ValidationError

from evals.cases import CaseResult


class CorruptResultsError(RuntimeError):
    """A line other than the last cannot be read: the file was edited or damaged."""


class ResultStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[CaseResult]:
        """Every saved result, latest per key, in the order first saved."""
        if not self.path.exists():
            return []
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]
        latest: dict[tuple[str, str, int], CaseResult] = {}
        for number, line in enumerate(lines, start=1):
            try:
                result = CaseResult.model_validate_json(line)
            except ValidationError:
                if number == len(lines):
                    break  # interrupted mid-write: that run will happen again
                raise CorruptResultsError(f"{self.path}: line {number} is unreadable") from None
            latest[result.key] = result
        return list(latest.values())

    def finished_keys(self) -> set[tuple[str, str, int]]:
        """Keys that need no further run. Infrastructure failures are not finished."""
        return {r.key for r in self.load() if not r.infrastructure_failure}

    def append(self, result: CaseResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(result.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())  # on disk before the next case starts

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
