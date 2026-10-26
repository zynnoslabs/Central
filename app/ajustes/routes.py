import json
import re

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField
from wtforms.validators import DataRequired, Length, EqualTo, ValidationError

from app.models import db, Usuario, Auditoria

ajustes_bp = Blueprint("ajustes", __name__)  ### CAMBIO AQUÍ ###


# ─── Validador de email ────────────────────────────────────────────────────────

def _validar_email(form, field):
    patron = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'
    if not re.match(patron, field.data or ""):
        raise ValidationError("Ingresá un email válido.")


# ─── Formularios ──────────────────────────────────────────────────────────────

class CambiarEmailForm(FlaskForm):
    email_nuevo = StringField("Nuevo email", validators=[DataRequired(), _validar_email])
    password_actual = PasswordField("Contraseña actual (para confirmar)", validators=[DataRequired()])


class CambiarPasswordForm(FlaskForm):
    password_actual = PasswordField("Contraseña actual", validators=[DataRequired()])
    password_nuevo = PasswordField("Nueva contraseña", validators=[DataRequired(), Length(min=8)])
    password_confirmar = PasswordField(
        "Confirmar nueva contraseña",
        validators=[DataRequired(), EqualTo("password_nuevo", message="Las contraseñas no coinciden.")]
    )


class AlertasForm(FlaskForm):
    alertas_email = BooleanField("Recibir alertas de caída/recuperación por email")


# ─── Panel de ajustes ─────────────────────────────────────────────────────────

@ajustes_bp.route("/")
@login_required
def inicio():  ### CAMBIO AQUÍ ###
    form_email = CambiarEmailForm()
    form_password = CambiarPasswordForm()
    form_alertas = AlertasForm(obj=current_user)
    return render_template(
        "ajustes/inicio.html",
        form_email=form_email,
        form_password=form_password,
        form_alertas=form_alertas,
    )


# ─── Cambiar email ────────────────────────────────────────────────────────────

@ajustes_bp.route("/email", methods=["POST"])
@login_required
def cambiar_email():  ### CAMBIO AQUÍ ###
    form = CambiarEmailForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for e in errors:
                flash(e, "danger")
        return redirect(url_for("ajustes.inicio"))

    if not current_user.check_password(form.password_actual.data):
        flash("Contraseña incorrecta.", "danger")
        return redirect(url_for("ajustes.inicio"))

    nuevo = form.email_nuevo.data.strip().lower()
    if nuevo == current_user.email:
        flash("El nuevo email es igual al actual.", "warning")
        return redirect(url_for("ajustes.inicio"))

    # Verificar que no esté en uso
    if Usuario.query.filter_by(email=nuevo).first():
        flash("Ese email ya está en uso.", "danger")
        return redirect(url_for("ajustes.inicio"))

    email_anterior = current_user.email
    current_user.email = nuevo
    db.session.commit()
    _auditar("ajustes.cambiar_email", {"anterior": email_anterior, "nuevo": nuevo})
    flash("Email actualizado correctamente.", "success")
    return redirect(url_for("ajustes.inicio"))


# ─── Cambiar contraseña ───────────────────────────────────────────────────────

@ajustes_bp.route("/password", methods=["POST"])
@login_required
def cambiar_password():  ### CAMBIO AQUÍ ###
    form = CambiarPasswordForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for e in errors:
                flash(e, "danger")
        return redirect(url_for("ajustes.inicio"))

    if not current_user.check_password(form.password_actual.data):
        flash("Contraseña actual incorrecta.", "danger")
        return redirect(url_for("ajustes.inicio"))

    current_user.set_password(form.password_nuevo.data)
    db.session.commit()
    _auditar("ajustes.cambiar_password", {})
    flash("Contraseña actualizada correctamente.", "success")
    return redirect(url_for("ajustes.inicio"))


# ─── Configuración de alertas ─────────────────────────────────────────────────

@ajustes_bp.route("/alertas", methods=["POST"])
@login_required
def guardar_alertas():  ### CAMBIO AQUÍ ###
    form = AlertasForm()
    if not form.validate_on_submit():
        flash("Error al guardar ajustes.", "danger")
        return redirect(url_for("ajustes.inicio"))

    current_user.alertas_email = form.alertas_email.data
    db.session.commit()
    _auditar("ajustes.alertas", {"alertas_email": form.alertas_email.data})
    flash("Preferencias de alertas guardadas.", "success")
    return redirect(url_for("ajustes.inicio"))


# ─── Helper ────────────────────────────────────────────────────────────────────

def _auditar(accion, detalle_dict):
    log = Auditoria(
        usuario_id=current_user.id,
        accion=accion,
        detalle=json.dumps(detalle_dict, ensure_ascii=False),
        ip=request.remote_addr,
    )
    db.session.add(log)
    db.session.commit()
