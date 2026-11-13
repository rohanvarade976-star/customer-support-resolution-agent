"""status_to_adopt: reconciliation only records news, and only moves a refund forward."""

import pytest

from support_agent.domain.enums import RefundOperationStatus as S
from support_agent.domain.refund_outcomes import status_to_adopt


@pytest.mark.parametrize(
    ("current", "reported", "expected"),
    [
        # Found after an unknown outcome or a crash: adopt what the provider says.
        (S.PENDING_RECONCILIATION, S.SUBMITTED, S.SUBMITTED),
        (S.PENDING_RECONCILIATION, S.SUCCEEDED, S.SUCCEEDED),
        (S.RESERVED, S.FAILED, S.FAILED),
        # The provider's lag moves forward.
        (S.SUBMITTED, S.PROCESSING, S.PROCESSING),
        (S.PROCESSING, S.SUCCEEDED, S.SUCCEEDED),
        # Nothing new.
        (S.SUBMITTED, S.SUBMITTED, None),
        (S.PENDING_RECONCILIATION, None, None),
        # An unrecognised provider word maps to pending_reconciliation: not an answer.
        (S.SUBMITTED, S.PENDING_RECONCILIATION, None),
        # Never backwards, and never away from a final status.
        (S.PROCESSING, S.SUBMITTED, None),
        (S.SUCCEEDED, S.FAILED, None),
        (S.FAILED, S.SUCCEEDED, None),
    ],
)
def test_status_to_adopt(current: S, reported: S | None, expected: S | None) -> None:
    assert status_to_adopt(current, reported) == expected
