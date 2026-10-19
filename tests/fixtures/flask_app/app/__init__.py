from flask import Blueprint, Flask

from .blueprints.leads import bp as leads_bp
from .blueprints.reports import bp as reports_bp
from .blueprints.users import bp as users_bp
from .extensions import db

api_bp = Blueprint("api", __name__, url_prefix="/api")
api_bp.register_blueprint(users_bp)


def create_app() -> Flask:
    app = Flask(__name__)
    db.init_app(app)
    app.register_blueprint(leads_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(reports_bp, url_prefix="/reports")

    @app.get("/health")
    def health():
        return {"ok": True}

    return app
