"""Harden users table: FK approved_by, locked_until, updated_at; drop unused approval_token.

Revision ID: a8f3c1d2e4b5
Revises: i1a2b3c4d5e8
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8f3c1d2e4b5"
down_revision: Union[str, Sequence[str], None] = "i1a2b3c4d5e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop unused approval_token if present
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("users")}
    uniques = inspector.get_unique_constraints("users")

    if "approval_token" in columns:
        for uq in uniques:
            if uq.get("column_names") == ["approval_token"] or "approval_token" in (
                uq.get("column_names") or []
            ):
                op.drop_constraint(uq["name"], "users", type_="unique")
        op.drop_column("users", "approval_token")

    if "locked_until" not in columns:
        op.add_column("users", sa.Column("locked_until", sa.DateTime(), nullable=True))

    if "updated_at" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=True,
            ),
        )

    # Add FK for approved_by (SET NULL on delete)
    fks = inspector.get_foreign_keys("users")
    has_approved_by_fk = any(
        fk.get("constrained_columns") == ["approved_by"] for fk in fks
    )
    if not has_approved_by_fk and "approved_by" in columns:
        # Clear orphaned approved_by values before adding FK
        op.execute(
            sa.text(
                "UPDATE users SET approved_by = NULL "
                "WHERE approved_by IS NOT NULL AND approved_by NOT IN (SELECT id FROM users)"
            )
        )
        op.create_foreign_key(
            "fk_users_approved_by",
            "users",
            "users",
            ["approved_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("users")}
    fks = inspector.get_foreign_keys("users")

    for fk in fks:
        if fk.get("constrained_columns") == ["approved_by"]:
            op.drop_constraint(fk["name"], "users", type_="foreignkey")

    if "updated_at" in columns:
        op.drop_column("users", "updated_at")
    if "locked_until" in columns:
        op.drop_column("users", "locked_until")

    if "approval_token" not in columns:
        op.add_column("users", sa.Column("approval_token", sa.String(), nullable=True))
        op.create_unique_constraint("users_approval_token_key", "users", ["approval_token"])
