"""Works out a refund's current status from how long ago it was created.

Pure function, no I/O: the status is never stored, it is recomputed on every read.
This is why the mock needs no background tasks to move refunds along.
"""

from datetime import datetime, timedelta

from mock_providers.payments.schemas import FinalOutcome, ProviderRefundStatus


def current_status(
    *,
    created_at: datetime,
    final_outcome: FinalOutcome,
    now: datetime,
    submitted_seconds: float,
    processing_seconds: float,
) -> ProviderRefundStatus:
    elapsed = now - created_at
    if elapsed < timedelta(seconds=submitted_seconds):
        return ProviderRefundStatus.SUBMITTED
    if elapsed < timedelta(seconds=submitted_seconds + processing_seconds):
        return ProviderRefundStatus.PROCESSING
    return ProviderRefundStatus(final_outcome.value)
