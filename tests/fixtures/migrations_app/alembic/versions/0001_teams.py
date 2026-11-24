"""teams and members"""

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("owner", sa.Integer()),
        sa.ForeignKeyConstraint(["owner"], ["members.id"]),
    )


def downgrade() -> None:
    op.drop_table("members")
    op.drop_table("teams")
