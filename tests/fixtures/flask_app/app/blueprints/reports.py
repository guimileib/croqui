from flask import Blueprint, jsonify
from sqlalchemy import text

from ..extensions import db

bp = Blueprint("reports", __name__)

TOP_SKUS = "SELECT sku, SUM(quantity) FROM lead_note GROUP BY sku ORDER BY 2 DESC LIMIT 10"


def _revenue_rows():
    return db.session.execute(text("SELECT o.id FROM leads o JOIN user u ON u.id = o.user_id")).all()


@bp.route("/revenue")
def revenue():
    """Leads joined with their users, written as raw SQL."""
    return jsonify([tuple(r) for r in _revenue_rows()])


@bp.route("/top-skus")
def top_skus():
    return jsonify([tuple(r) for r in db.session.execute(text(TOP_SKUS))])


@bp.route("/adhoc/<name>")
def adhoc(name):
    sql = f"SELECT * FROM {name}"
    return jsonify(db.session.execute(text(sql)).all())
