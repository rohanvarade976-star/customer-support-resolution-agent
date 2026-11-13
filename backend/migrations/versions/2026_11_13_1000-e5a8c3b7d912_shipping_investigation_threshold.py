"""shipping policy investigation threshold

Revision ID: e5a8c3b7d912
Revises: 9d2c4e6f8a1b
Create Date: 2026-11-13 10:00:00.000000

- policy_documents gains investigation_threshold_days (from the shipping_delays front
  matter): a shipment more than this many days past its expected delivery date is passed
  to a specialist. Nullable, like refund_window_days: only that one policy sets it.
  Existing rows stay NULL until the policies are ingested again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5a8c3b7d912"
down_revision: str | Sequence[str] | None = "9d2c4e6f8a1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "policy_documents",
        sa.Column("investigation_threshold_days", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_policy_documents_investigation_threshold_days_positive"),
        "policy_documents",
        "investigation_threshold_days IS NULL OR investigation_threshold_days > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_policy_documents_investigation_threshold_days_positive"),
        "policy_documents",
        type_="check",
    )
    op.drop_column("policy_documents", "investigation_threshold_days")
