from flask import Blueprint

chat_bp = Blueprint("chat", __name__)

from app.chat import routes  # noqa: F401, E402
