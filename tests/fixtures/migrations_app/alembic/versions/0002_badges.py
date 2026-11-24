"""badges, linked after the fact"""

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table("badges", sa.Column("id", sa.Integer(), primary_key=True))
    op.add_column("badges", sa.Column("holder", sa.Integer()))
    op.create_foreign_key("fk_badges_holder", "badges", "members", ["holder"], ["id"])
