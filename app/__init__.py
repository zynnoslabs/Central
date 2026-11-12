import json
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_mail import Mail
from flask_socketio import SocketIO

from app.config import Config
from app.models import db, Usuario

# ─── Extensiones globales ─────────────────────────────────────────────────────
login_manager = LoginManager()
csrf = CSRFProtect()
mail = Mail()
socketio = SocketIO()  ### CAMBIO AQUÍ ###


def create_app(config_class=Config):
    """App factory — crea y configura la aplicación Flask."""
    app = Flask(__name__, template_folder="templates")
    app.config.from_object(config_class)

    # ─── Inicializar extensiones ───────────────────────────────────────────────
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    mail.init_app(app)

    ### CAMBIO AQUÍ ### — SocketIO para el chat widget
    socketio.init_app(app,
        cors_allowed_origins="*",   # ### CAMBIO AQUÍ ### acepta cualquier origen (vercel previews, dominio propio, etc.)
        async_mode="gevent"
    )

    # ─── Configurar Flask-Login ────────────────────────────────────────────────
    login_manager.login_view = "auth.login"           # redirige a /login si no autenticado
    login_manager.login_message = "Iniciá sesión para continuar."
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(Usuario, int(user_id))

    # ─── Registrar blueprints ──────────────────────────────────────────────────
    from app.auth.routes import auth_bp
    from app.clientes.routes import clientes_bp
    from app.tablero.routes import tablero_bp
    from app.avisos.routes import avisos_bp
    from app.cobros.routes import cobros_bp
    from app.actividad.routes import actividad_bp
    from app.ajustes.routes import ajustes_bp  ### CAMBIO AQUÍ ###
    from app.api.routes import api_bp  ### CAMBIO AQUÍ ###
    from app.chat import chat_bp  ### CAMBIO AQUÍ ###

    app.register_blueprint(auth_bp)
    app.register_blueprint(clientes_bp, url_prefix="/clientes")
    app.register_blueprint(tablero_bp, url_prefix="/tablero")
    app.register_blueprint(avisos_bp, url_prefix="/avisos")
    app.register_blueprint(cobros_bp, url_prefix="/cobros")
    app.register_blueprint(actividad_bp, url_prefix="/actividad")
    app.register_blueprint(ajustes_bp, url_prefix="/ajustes")  ### CAMBIO AQUÍ ###
    app.register_blueprint(api_bp, url_prefix="/api")  ### CAMBIO AQUÍ ###
    app.register_blueprint(chat_bp, url_prefix="/chat")  ### CAMBIO AQUÍ ###

    # ─── Eximir /actividad/ingresar/* de CSRF (llega del agente externo) ──────
    csrf.exempt(actividad_bp)
    csrf.exempt(api_bp)  ### CAMBIO AQUÍ ###
    csrf.exempt(chat_bp)  ### CAMBIO AQUÍ ###

    # ─── Crear tablas y usuario admin inicial ──────────────────────────────────
    with app.app_context():
        db.create_all()
        _migrar_columnas(app)  ### CAMBIO AQUÍ ### — migraciones manuales para PostgreSQL
        _crear_admin_si_no_existe(app)

    # ─── Filtro Jinja para parsear JSON en templates ──────────────────────────
    ### CAMBIO AQUÍ ###
    import json as _json
    app.jinja_env.filters["from_json"] = _json.loads

    # ─── Context processor: datos globales para todos los templates ─────────────
    from flask_login import current_user
    from app.models import Aviso

    @app.context_processor
    def inject_globals():
        """Inyecta variables disponibles en todos los templates."""
        avisos_nav = 0
        if current_user.is_authenticated:
            try:
                avisos_nav = Aviso.query.filter_by(estado="pendiente").count()
            except Exception:
                pass
        return {"avisos_nav": avisos_nav}

    # ─── WebSocket handlers para el chat ──────────────────────────────────────
    ### CAMBIO AQUÍ ###
    @socketio.on("join_operators")
    def on_join_operators(data):
        from flask_socketio import join_room, emit
        join_room("operators")
        emit("joined", {"room": "operators"})

    @socketio.on("join_conversation")
    def on_join_conversation(data):
        from flask_socketio import join_room
        conv_id = data.get("conv_id")
        if conv_id:
            join_room(f"conv_{conv_id}")

    return app


def _crear_admin_si_no_existe(app):
    """Crea el usuario admin en el primer arranque si no existe ninguno."""
    admin_email = app.config.get("ADMIN_EMAIL")
    admin_password = app.config.get("ADMIN_PASSWORD")

    if not admin_email or not admin_password:
        return  # No hay credenciales en env, saltear

    existe = Usuario.query.filter_by(email=admin_email).first()
    if existe:
        return  # Ya existe, no tocar

    admin = Usuario(email=admin_email, es_admin=True)
    admin.set_password(admin_password)
    db.session.add(admin)
    db.session.commit()

    # Registrar en auditoría
    from app.models import Auditoria
    log = Auditoria(
        usuario_id=admin.id,
        accion="sistema.admin_inicial",
        detalle=json.dumps({"email": admin_email}),
    )
    db.session.add(log)
    db.session.commit()


# ── Migraciones manuales para PostgreSQL (ADD COLUMN idempotente) ────────────
### CAMBIO AQUÍ ###
def _migrar_columnas(app):
    """Agrega columnas nuevas que db.create_all() no añade a tablas existentes."""
    try:
        engine = db.engine
        with engine.connect() as conn:
            # Detectar si es PostgreSQL o SQLite
            dialect = engine.dialect.name

            migraciones = [
                # (tabla, columna, definición SQL)
                ("avisos", "fecha_vencimiento", "DATE"),
            ]

            for tabla, columna, tipo in migraciones:
                if dialect == "postgresql":
                    resultado = conn.execute(db.text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ), {"t": tabla, "c": columna})
                    existe = resultado.fetchone()
                else:
                    # SQLite: PRAGMA table_info
                    resultado = conn.execute(db.text(f"PRAGMA table_info({tabla})"))
                    cols = [row[1] for row in resultado.fetchall()]
                    existe = columna in cols

                if not existe:
                    conn.execute(db.text(
                        f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}"
                    ))
                    conn.commit()
                    app.logger.info(f"Migración: {tabla}.{columna} ({tipo}) agregada.")
    except Exception as e:
        app.logger.warning(f"_migrar_columnas error (no crítico): {e}")


# ── Compatibilidad con gunicorn app:app ──────────────────────────────────────
### CAMBIO AQUÍ ### — permite arrancar con "gunicorn app:app" O "gunicorn run:app"
app = create_app()
