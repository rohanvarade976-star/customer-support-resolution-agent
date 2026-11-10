"""Client for the payment provider's refund API.

Refund creation is NEVER retried automatically (docs/decisions.md, ADR-004):
- could not connect at all          -> ProviderUnavailableError (nothing was sent, no refund)
- timeout / dropped connection / 5xx -> OutcomeUnknownError (a refund may exist)
- 4xx                                -> ProviderRejectedError
An unknown outcome is resolved later with find_refund_by_key(idempotency_key).

Reads (get_refund, find_refund_by_key) are safe to repeat, so they are retried.
"""

from datetime import datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationError

from support_agent.core.config import Settings
from support_agent.domain.enums import RefundOperationStatus
from support_agent.integrations.errors import OutcomeUnknownError, ProviderUnavailableError
from support_agent.integrations.http import BaseProviderClient, ProviderHttp

PROVIDER = "payments"

# The provider's status words -> our RefundOperationStatus.
_STATUS_MAP = {
    "submitted": RefundOperationStatus.SUBMITTED,
    "processing": RefundOperationStatus.PROCESSING,
    "succeeded": RefundOperationStatus.SUCCEEDED,
    "failed": RefundOperationStatus.FAILED,
}


def map_refund_status(provider_status: str) -> RefundOperationStatus:
    """The one place provider refund statuses are translated into ours.

    A status we do not recognise maps to PENDING_RECONCILIATION: we cannot verify what
    it means, so we must not guess success or failure.
    """
    return _STATUS_MAP.get(provider_status, RefundOperationStatus.PENDING_RECONCILIATION)


class ProviderRefund(BaseModel):
    """A refund as the provider reports it."""

    provider_reference: str
    idempotency_key: str
    order_reference: str
    amount_minor: int  # paise
    currency: str
    reason: str
    provider_status: str = Field(validation_alias="status")  # the provider's own word
    created_at: datetime

    @property
    def status(self) -> RefundOperationStatus:
        return map_refund_status(self.provider_status)


class PaymentClient(BaseProviderClient):
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(ProviderHttp(PROVIDER, settings.payments_base_url, settings, transport))

    async def create_refund(
        self,
        *,
        idempotency_key: str,
        order_reference: str,
        amount_minor: int,
        currency: str,
        reason: str,
    ) -> ProviderRefund:
        """Ask the provider to refund money. Exactly one attempt, never retried here."""
        body = {
            "order_reference": order_reference,
            "amount_minor": amount_minor,
            "currency": currency,
            "reason": reason,
        }
        try:
            response = await self._http.send_once(
                "POST", "/refunds", json=body, headers={"Idempotency-Key": idempotency_key}
            )
        except ProviderUnavailableError as exc:
            if not exc.request_sent:
                raise  # we never reached the provider, so no refund can exist
            raise OutcomeUnknownError(
                PROVIDER,
                idempotency_key,
                f"Refund outcome unknown ({exc.error_code}); reconcile by idempotency key.",
            ) from exc

        try:
            return ProviderRefund.model_validate_json(response.content)
        except ValidationError as exc:
            # A 2xx means the refund was probably created, but we cannot read it to confirm.
            raise OutcomeUnknownError(
                PROVIDER, idempotency_key, "Refund response could not be read; reconcile by key."
            ) from exc

    async def get_refund(self, provider_reference: str) -> ProviderRefund:
        """Current state of a refund. Raises ResourceNotFoundError if it does not exist."""
        response = await self._http.request("GET", f"/refunds/{quote(provider_reference, safe='')}")
        return ProviderRefund.model_validate_json(response.content)

    async def find_refund_by_key(self, idempotency_key: str) -> ProviderRefund:
        """Reconciliation: did a request with this key create a refund?

        Raises ResourceNotFoundError if no refund was created with this key.
        """
        response = await self._http.request(
            "GET", "/refunds", params={"idempotency_key": idempotency_key}
        )
        return ProviderRefund.model_validate_json(response.content)
