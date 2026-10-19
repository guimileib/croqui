import requests

from .repositories import LeadRepository


class LeadService:
    """Lead business rules."""

    def __init__(self, repo: LeadRepository) -> None:
        self.repo = repo

    def list_leads(self):
        return self.repo.list_all()

    def place(self, user_id: int):
        lead = self.repo.add(user_id, "new")
        requests.post("https://hooks.example.com/leads", json={"id": lead.id})
        return lead

    def cancel(self, lead_id: int) -> None:
        self.repo.remove(lead_id)
