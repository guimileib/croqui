from .extensions import db
from .models import Order


class OrderRepository:
    """Persistence for orders."""

    def list_all(self):
        return Order.query.order_by(Order.id).all()

    def get(self, order_id: int):
        return Order.query.get(order_id)

    def add(self, user_id: int, status: str):
        order = Order(user_id=user_id, status=status)
        db.session.add(order)
        db.session.commit()
        return order

    def remove(self, order_id: int) -> None:
        Order.query.filter_by(id=order_id).delete()
        db.session.commit()
