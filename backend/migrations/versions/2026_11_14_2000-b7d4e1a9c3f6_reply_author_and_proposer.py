"""ticket reply author and the reviewer who proposed a refund

Revision ID: b7d4e1a9c3f6
Revises: e2b6c9d4a1f3
Create Date: 2026-11-14 20:00:00

Two small columns (ADR-022):

- tickets.reply_author: who wrote the ticket's verified reply, "assistant" (the workflow's
  fixed or validated message) or "specialist" (a reviewer's reply to an escalated ticket).
  Existing replies were all written by runs, so they become "assistant".
- action_proposals.proposed_by: the staff user who created a manual refund proposal; NULL
  when the workflow proposed it. Separation of duties: that person may not decide it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d4e1a9c3f6"
down_revision: str | Sequence[str] | None = "e2b6c9d4a1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column(
            "reply_author",
            sa.Enum(
                "assistant",
                "specialist",
                name="reply_author",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="assistant",
            nullable=False,
        ),
    )
    op.add_column("action_proposals", sa.Column("proposed_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_action_proposals_proposed_by_staff_users"),
        "action_proposals",
        "staff_users",
        ["proposed_by"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_action_proposals_proposed_by_staff_users"), "action_proposals", type_="foreignkey"
    )
    op.drop_column("action_proposals", "proposed_by")
    op.drop_constraint(op.f("ck_tickets_reply_author"), "tickets", type_="check")
    op.drop_column("tickets", "reply_author")
