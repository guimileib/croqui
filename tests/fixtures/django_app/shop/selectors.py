from .models import Lead


def recent_leads(limit: int = 20):
    return Lead.objects.filter(status="new").order_by("-created_at")[:limit]
