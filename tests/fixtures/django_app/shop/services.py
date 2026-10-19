from .models import Order, OrderLine


def place_order(customer_id: int, lines: list[dict]) -> Order:
    order = Order.objects.create(customer_id=customer_id)
    OrderLine.objects.bulk_create([OrderLine(order=order, **line) for line in lines])
    return order


class RefundService:
    """Marks an order as refunded."""

    def refund(self, order_id: int) -> Order:
        order = Order.objects.get(pk=order_id)
        order.status = "refunded"
        order.save()
        return order
