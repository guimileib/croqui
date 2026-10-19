from flask import Blueprint, jsonify, request

from ..models import LeadItem
from ..repositories import LeadRepository
from ..services import LeadService

bp = Blueprint("leads", __name__, url_prefix="/leads")
lead_service = LeadService(LeadRepository())


@bp.route("/")
def list_leads():
    """List every lead."""
    return jsonify(lead_service.list_leads())


@bp.post("/")
def create_lead():
    """Place a new lead for the caller."""
    return jsonify(lead_service.place(request.json["user_id"]))


@bp.route("/<int:lead_id>", methods=["DELETE"])
def cancel_lead(lead_id):
    lead_service.cancel(lead_id)
    return "", 204


@bp.get("/<int:lead_id>/items")
def lead_items(lead_id):
    return jsonify(LeadItem.query.filter_by(lead_id=lead_id).all())
