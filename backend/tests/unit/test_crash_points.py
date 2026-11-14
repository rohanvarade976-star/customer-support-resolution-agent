"""Crash points do nothing unless a test arms one, and can never be armed in production."""

from collections.abc import Iterator

import pytest

from support_agent.core import crash_points
from support_agent.core.config import get_settings
from support_agent.core.crash_points import CrashPoint, SimulatedCrash


@pytest.fixture(autouse=True)
def _disarmed() -> Iterator[None]:
    crash_points.disarm()
    yield
    crash_points.disarm()


def test_an_unarmed_point_does_nothing() -> None:
    for point in CrashPoint:
        crash_points.reached(point)  # no exception


def test_an_armed_point_crashes_once_and_only_there() -> None:
    crash_points.arm(CrashPoint.AFTER_PROVIDER_CALL)
    crash_points.reached(CrashPoint.AFTER_RESERVE)  # a different point: nothing
    with pytest.raises(SimulatedCrash) as crash:
        crash_points.reached(CrashPoint.AFTER_PROVIDER_CALL)
    assert crash.value.point is CrashPoint.AFTER_PROVIDER_CALL
    crash_points.reached(CrashPoint.AFTER_PROVIDER_CALL)  # used up: the "process" restarted


def test_a_simulated_crash_is_not_an_ordinary_exception() -> None:
    # `except Exception` (the worker's retry path) must not catch it, as with a dying process.
    assert not issubclass(SimulatedCrash, Exception)


def test_arming_is_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    production = get_settings().model_copy(update={"app_env": "production"})
    monkeypatch.setattr(crash_points, "get_settings", lambda: production)
    with pytest.raises(RuntimeError):
        crash_points.arm(CrashPoint.AFTER_RESERVE)
    crash_points.reached(CrashPoint.AFTER_RESERVE)  # still nothing armed
