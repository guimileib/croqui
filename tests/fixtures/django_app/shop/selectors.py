from .models import Order


def recent_orders(limit: int = 20):
    return Order.objects.filter(status="new").order_by("-created_at")[:limit]
