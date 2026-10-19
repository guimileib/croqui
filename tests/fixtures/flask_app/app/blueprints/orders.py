from flask import Blueprint, jsonify, request

from ..models import OrderItem
from ..repositories import OrderRepository
from ..services import OrderService

bp = Blueprint("orders", __name__, url_prefix="/orders")
order_service = OrderService(OrderRepository())


@bp.route("/")
def list_orders():
    """List every order."""
    return jsonify(order_service.list_orders())


@bp.post("/")
def create_order():
    """Place a new order for the caller."""
    return jsonify(order_service.place(request.json["user_id"]))


@bp.route("/<int:order_id>", methods=["DELETE"])
def cancel_order(order_id):
    order_service.cancel(order_id)
    return "", 204


@bp.get("/<int:order_id>/items")
def order_items(order_id):
    return jsonify(OrderItem.query.filter_by(order_id=order_id).all())
