from alembic import context

from app.db.models import SCHEMA

context.configure(version_table_schema=SCHEMA)
