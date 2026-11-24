from sqlalchemy import Column, MetaData, Table, Text

SCHEMA = "core"

md = MetaData(schema=SCHEMA)

tb_agent = Table(
    "tb_agent",
    md,
    Column("agent_id", Text, primary_key=True),
    Column("status", Text, nullable=False),
)

tb_event = Table("tb_event", md, Column("agent_id", Text), Column("kind", Text))
