import requests

from .repositories import OrderRepository


class OrderService:
    """Order business rules."""

    def __init__(self, repo: OrderRepository) -> None:
        self.repo = repo

    def list_orders(self):
        return self.repo.list_all()

    def place(self, user_id: int):
        order = self.repo.add(user_id, "new")
        requests.post("https://hooks.example.com/orders", json={"id": order.id})
        return order

    def cancel(self, order_id: int) -> None:
        self.repo.remove(order_id)
