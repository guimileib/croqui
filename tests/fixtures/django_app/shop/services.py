from .models import Lead, LeadLine


def place_lead(customer_id: int, lines: list[dict]) -> Lead:
    lead = Lead.objects.create(customer_id=customer_id)
    LeadLine.objects.bulk_create([LeadLine(lead=lead, **line) for line in lines])
    return lead


class RefundService:
    """Marks a lead as refunded."""

    def refund(self, lead_id: int) -> Lead:
        lead = Lead.objects.get(pk=lead_id)
        lead.status = "refunded"
        lead.save()
        return lead
