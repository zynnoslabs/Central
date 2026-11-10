from flask import Blueprint

cobros_bp = Blueprint("cobros", __name__)

from app.cobros import routes  # noqa: F401, E402
