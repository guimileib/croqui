from flask import Blueprint, jsonify, request
from flask.views import MethodView

from ..extensions import db
from ..models import User

bp = Blueprint("users", __name__, url_prefix="/users")


class UserCollection(MethodView):
    def get(self):
        return jsonify(User.query.all())

    def post(self):
        user = User(email=request.json["email"])
        db.session.add(user)
        db.session.commit()
        return jsonify(user.id), 201


class UserAPI(MethodView):
    def get(self, user_id):
        return jsonify(User.query.get_or_404(user_id))

    def delete(self, user_id):
        user = User.query.get_or_404(user_id)
        db.session.delete(user)
        db.session.commit()
        return "", 204


bp.add_url_rule("/", view_func=UserCollection.as_view("users"))
bp.add_url_rule("/<int:user_id>", view_func=UserAPI.as_view("user"))
