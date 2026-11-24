from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import tb_agent, tb_event


class AgentsRepository:
    def __init__(self, db) -> None:
        self._db = db

    def get(self, agent_id):
        return self._db.execute(select(tb_agent).where(tb_agent.c.agent_id == agent_id)).first()

    def register(self, agent_id):
        self._db.execute(pg_insert(tb_agent).values(agent_id=agent_id, status="active"))

    def deactivate(self, agent_id):
        self._db.execute(update(tb_agent).where(tb_agent.c.agent_id == agent_id).values(status="off"))


class EventsRepository:
    def __init__(self, db) -> None:
        self._db = db

    def kinds(self):
        return self._db.execute(select(tb_event.c.kind)).all()
