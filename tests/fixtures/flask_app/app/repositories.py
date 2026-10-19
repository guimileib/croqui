from .extensions import db
from .models import Lead


class LeadRepository:
    """Persistence for leads."""

    def list_all(self):
        return Lead.query.order_by(Lead.id).all()

    def get(self, lead_id: int):
        return Lead.query.get(lead_id)

    def add(self, user_id: int, status: str):
        lead = Lead(user_id=user_id, status=status)
        db.session.add(lead)
        db.session.commit()
        return lead

    def remove(self, lead_id: int) -> None:
        Lead.query.filter_by(id=lead_id).delete()
        db.session.commit()
