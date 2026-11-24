from sqlalchemy import text

from app.db.models import SCHEMA


class SchemaRepository:
    def __init__(self, db) -> None:
        self._db = db

    def revision(self):
        return self._db.execute(text(f"SELECT version_num FROM {SCHEMA}.alembic_version")).scalar()


class StatusRepository:
    def __init__(self, db) -> None:
        self._db = db

    def status(self, agent_id):
        query = f"SELECT status FROM {SCHEMA}.tb_agent WHERE agent_id = :a"
        return self._db.execute(text(query), {"a": agent_id}).scalar()
