"""Named places in the refund path where a test can make the process "die" (ADR-015).

The refund executor calls `reached(point)` at three moments that matter for money:

    AFTER_RESERVE        the reserved row is committed; the provider has not been called
    AFTER_PROVIDER_CALL  the provider answered; nothing about its answer is recorded yet
    AFTER_RECORD         the outcome is written (not committed); run and ticket not updated

Nothing is armed unless a test arms a point, so in the running app `reached` is one
comparison and returns. Arming is refused when APP_ENV=production.

`SimulatedCrash` derives from BaseException on purpose: the worker's `except Exception`
must not catch it and turn it into a tidy retry. It escapes like a dying process would,
leaving the job "running" until its lease runs out.
"""

from enum import StrEnum

from support_agent.core.config import get_settings


class CrashPoint(StrEnum):
    AFTER_RESERVE = "after_reserve"
    AFTER_PROVIDER_CALL = "after_provider_call"
    AFTER_RECORD = "after_record"


class SimulatedCrash(BaseException):
    """The process "died" at `point`. Only ever raised in tests."""

    def __init__(self, point: CrashPoint) -> None:
        super().__init__(point.value)
        self.point = point


_armed: CrashPoint | None = None


def arm(point: CrashPoint) -> None:
    """Crash the next time `point` is reached (once). Refused in production."""
    global _armed
    if get_settings().app_env == "production":
        raise RuntimeError("crash points cannot be armed in production")
    _armed = point


def disarm() -> None:
    global _armed
    _armed = None


def reached(point: CrashPoint) -> None:
    """Called by the refund path. Raises SimulatedCrash only if a test armed this point."""
    global _armed
    if _armed is point:
        _armed = None  # one crash per arming, like a process that dies once
        raise SimulatedCrash(point)
