import json
from datetime import datetime, timezone, timedelta

from flask import (
    Blueprint, render_template, redirect, url_for,
    flash, request, current_app
)
from flask_login import login_user, logout_user, login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField
import re
from wtforms.validators import DataRequired, Length, ValidationError

from app.models import db, Usuario, Auditoria

auth_bp = Blueprint("auth", __name__)


# ─── Validador de email sin dependencia externa ────────────────────────────────

def _validar_email(form, field):  ### CAMBIO AQUÍ ###
    patron = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'
    if not re.match(patron, field.data or ""):
        raise ValidationError("Ingresá un email válido.")


# ─── Formulario de login ───────────────────────────────────────────────────────

class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), _validar_email])
    password = PasswordField("Contraseña", validators=[DataRequired(), Length(min=8)])
    recordarme = BooleanField("Recordarme 8 horas")


# ─── Rutas ─────────────────────────────────────────────────────────────────────

@auth_bp.route("/", methods=["GET"])
@auth_bp.route("/login", methods=["GET"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("tablero.inicio"))
    form = LoginForm()
    return render_template("login.html", form=form)


@auth_bp.route("/login", methods=["POST"])
def login_post():
    form = LoginForm()
    if not form.validate_on_submit():
        return render_template("login.html", form=form), 400

    email = form.email.data.strip().lower()
    password = form.password.data
    ip = request.remote_addr

    usuario = Usuario.query.filter_by(email=email).first()

    # ── Usuario no existe ──────────────────────────────────────────────────────
    if not usuario:
        _log_auditoria(None, None, "auth.login_fallido", {"email": email, "razon": "no_existe"}, ip)
        flash("Email o contraseña incorrectos.", "danger")
        return render_template("login.html", form=form), 401

    # ── Cuenta bloqueada ───────────────────────────────────────────────────────
    if usuario.esta_bloqueado():
        minutos = current_app.config["LOCKOUT_MINUTES"]
        flash(f"Cuenta bloqueada por {minutos} minutos por demasiados intentos fallidos.", "danger")
        return render_template("login.html", form=form), 403

    # ── Contraseña incorrecta ──────────────────────────────────────────────────
    if not usuario.check_password(password):
        usuario.intentos_fallidos += 1
        max_intentos = current_app.config["MAX_LOGIN_ATTEMPTS"]

        if usuario.intentos_fallidos >= max_intentos:
            minutos = current_app.config["LOCKOUT_MINUTES"]
            usuario.bloqueado_hasta = datetime.now(timezone.utc) + timedelta(minutes=minutos)
            _log_auditoria(usuario.id, None, "auth.cuenta_bloqueada", {"intentos": usuario.intentos_fallidos}, ip)
            flash(f"Demasiados intentos. Cuenta bloqueada por {minutos} minutos.", "danger")
        else:
            restantes = max_intentos - usuario.intentos_fallidos
            flash(f"Contraseña incorrecta. Intentos restantes: {restantes}.", "danger")

        db.session.commit()
        return render_template("login.html", form=form), 401

    # ── Login exitoso ──────────────────────────────────────────────────────────
    usuario.intentos_fallidos = 0
    usuario.bloqueado_hasta = None
    usuario.ultimo_acceso = datetime.now(timezone.utc)
    db.session.commit()

    login_user(usuario, remember=form.recordarme.data)
    _log_auditoria(usuario.id, None, "auth.login_ok", {"ip": ip}, ip)

    next_page = request.args.get("next")
    if next_page and next_page.startswith("/"):  # evitar redirección abierta
        return redirect(next_page)
    return redirect(url_for("tablero.inicio"))


@auth_bp.route("/logout")
@login_required
def logout():
    _log_auditoria(current_user.id, None, "auth.logout", {}, request.remote_addr)
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("auth.login"))


# ─── Helper ────────────────────────────────────────────────────────────────────

def _log_auditoria(usuario_id, cliente_id, accion, detalle_dict, ip):
    log = Auditoria(
        usuario_id=usuario_id,
        cliente_id=cliente_id,
        accion=accion,
        detalle=json.dumps(detalle_dict, ensure_ascii=False),
        ip=ip,
    )
    db.session.add(log)
    db.session.commit()
