from flask import Blueprint

ajustes_bp = Blueprint("ajustes", __name__)

from app.ajustes import routes  # noqa: F401, E402
