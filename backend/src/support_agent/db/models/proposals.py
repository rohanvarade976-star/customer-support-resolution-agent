"""Action proposals made by agents, and the human approvals bound to them."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from support_agent.db.base import Base
from support_agent.db.models._columns import CreatedAt, UpdatedAt, UUIDPrimaryKey, str_enum
from support_agent.domain.enums import ActionType, ApprovalDecision, ProposalStatus

SHA256_HEX = "'^[0-9a-f]{64}$'"


class ActionProposal(UUIDPrimaryKey, CreatedAt, UpdatedAt, Base):
    """An immutable proposal: payload and hash are written once, only status changes."""

    __tablename__ = "action_proposals"
    __table_args__ = (
        CheckConstraint(f"payload_hash ~ {SHA256_HEX}", name="payload_hash_sha256"),
        # Target for the approvals foreign key below.
        UniqueConstraint("id", "payload_hash"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    action_type: Mapped[ActionType] = mapped_column(str_enum(ActionType, "action_type"))
    payload: Mapped[dict[str, Any]]
    payload_hash: Mapped[str] = mapped_column(String(64))  # SHA-256 of the canonical payload
    status: Mapped[ProposalStatus] = mapped_column(
        str_enum(ProposalStatus, "status"),
        default=ProposalStatus.PENDING_APPROVAL,
        server_default=ProposalStatus.PENDING_APPROVAL.value,
    )


class Approval(UUIDPrimaryKey, CreatedAt, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        # The approval must carry the exact hash of its proposal. An approval for
        # different contents (another amount or order) cannot even be stored.
        ForeignKeyConstraint(
            ["proposal_id", "payload_hash"],
            ["action_proposals.id", "action_proposals.payload_hash"],
        ),
        CheckConstraint(f"payload_hash ~ {SHA256_HEX}", name="payload_hash_sha256"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        # A human decision must record who decided and when.
        CheckConstraint(
            "decision NOT IN ('approved', 'rejected')"
            " OR (reviewer_id IS NOT NULL AND decided_at IS NOT NULL)",
            name="decision_has_reviewer",
        ),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(index=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    reviewer_id: Mapped[str | None] = mapped_column(String(255))  # reviewer's JWT `sub`
    decision: Mapped[ApprovalDecision] = mapped_column(
        str_enum(ApprovalDecision, "decision"),
        default=ApprovalDecision.PENDING,
        server_default=ApprovalDecision.PENDING.value,
    )
    expires_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
