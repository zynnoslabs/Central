import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Seguridad
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "")  ### CAMBIO AQUÍ ###
    WTF_CSRF_ENABLED = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 3600 * 8  # 8 horas

    # Base de datos
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "")  ### CAMBIO AQUÍ ###
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    # Mail
    MAIL_SERVER = "smtp.gmail.com"
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_USERNAME")

    # Cron
    CRON_TOKEN = os.environ.get("CRON_TOKEN", "")
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

    # Admin inicial
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

    ### CAMBIO AQUÍ ### — Chat widget / La Central
    GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
    CALLMEBOT_KEY  = os.environ.get("CALLMEBOT_KEY", "")
    OPERATOR_PHONE = os.environ.get("OPERATOR_PHONE", "5492392536325")
    CENTRAL_URL    = os.environ.get("CENTRAL_URL", "http://localhost:5000")

    # Intentos fallidos antes de bloqueo
    MAX_LOGIN_ATTEMPTS = 5
    LOCKOUT_MINUTES = 15


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SECRET_KEY = "clave-de-prueba"
    ADMIN_EMAIL = "admin@test.com"
    ADMIN_PASSWORD = "Test1234Seguro!!"
    CRON_TOKEN = "token-de-prueba"
    MAIL_SUPPRESS_SEND = True
