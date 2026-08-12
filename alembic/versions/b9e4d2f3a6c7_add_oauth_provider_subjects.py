"""Add OAuth provider subject columns for secure account linking.

Revision ID: b9e4d2f3a6c7
Revises: a8f3c1d2e4b5
Create Date: 2026-08-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b9e4d2f3a6c7"
down_revision: Union[str, Sequence[str], None] = "a8f3c1d2e4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("users")}

    if "google_sub" not in columns:
        op.add_column("users", sa.Column("google_sub", sa.String(length=255), nullable=True))
        op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)
    if "facebook_id" not in columns:
        op.add_column("users", sa.Column("facebook_id", sa.String(length=255), nullable=True))
        op.create_index("ix_users_facebook_id", "users", ["facebook_id"], unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("users")}

    if "facebook_id" in columns:
        op.drop_index("ix_users_facebook_id", table_name="users")
        op.drop_column("users", "facebook_id")
    if "google_sub" in columns:
        op.drop_index("ix_users_google_sub", table_name="users")
        op.drop_column("users", "google_sub")
