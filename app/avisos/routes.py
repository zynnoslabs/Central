### CAMBIO AQUÍ ### — routes.py completo reemplaza el anterior
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from flask_mail import Message
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField
from wtforms.validators import DataRequired, Length, Optional

from app import mail
from app.models import db, Aviso, Cliente, EventoSalud, Instalacion, ahora

avisos_bp = Blueprint("avisos", __name__)


# ─── Formulario ───────────────────────────────────────────────────────────────

class AvisoForm(FlaskForm):
    titulo = StringField("Título", validators=[DataRequired(), Length(max=255)])
    cuerpo = TextAreaField("Descripción", validators=[Optional(), Length(max=2000)])
    prioridad = SelectField("Prioridad", choices=[
        ("alta", "🔴 Alta"),
        ("media", "🟡 Media"),
        ("baja", "🟢 Baja"),
    ], default="media")
    cliente_id = SelectField("Cliente (opcional)", coerce=int, validators=[Optional()])


# ─── Lista de avisos ──────────────────────────────────────────────────────────

@avisos_bp.route("/")
@login_required
def lista():
    filtro = request.args.get("estado", "pendiente")
    if filtro == "todos":
        avisos = Aviso.query.order_by(
            Aviso.estado.asc(),
            Aviso.prioridad.asc(),
            Aviso.creado_en.desc()
        ).all()
    else:
        avisos = Aviso.query.filter_by(estado=filtro).order_by(
            Aviso.prioridad.asc(),
            Aviso.creado_en.desc()
        ).all()
    pendientes = Aviso.query.filter_by(estado="pendiente").count()
    return render_template("avisos/lista.html", avisos=avisos, filtro=filtro, pendientes=pendientes)


# ─── Nuevo aviso ──────────────────────────────────────────────────────────────

@avisos_bp.route("/nuevo", methods=["GET", "POST"])
@login_required
def nuevo():
    form = AvisoForm()
    clientes = Cliente.query.order_by(Cliente.nombre).all()
    form.cliente_id.choices = [(0, "— Sin cliente —")] + [(c.id, c.nombre) for c in clientes]
    if form.validate_on_submit():
        aviso = Aviso(
            titulo=form.titulo.data.strip(),
            cuerpo=form.cuerpo.data.strip() if form.cuerpo.data else None,
            prioridad=form.prioridad.data,
            estado="pendiente",
            cliente_id=form.cliente_id.data if form.cliente_id.data != 0 else None,
        )
        db.session.add(aviso)
        db.session.commit()
        flash(f"Aviso «{aviso.titulo}» creado.", "success")
        return redirect(url_for("avisos.lista"))
    return render_template("avisos/form.html", form=form, titulo="Nuevo aviso")


# ─── Editar aviso ─────────────────────────────────────────────────────────────

@avisos_bp.route("/<int:aviso_id>/editar", methods=["GET", "POST"])
@login_required
def editar(aviso_id):
    aviso = Aviso.query.get_or_404(aviso_id)
    form = AvisoForm(obj=aviso)
    clientes = Cliente.query.order_by(Cliente.nombre).all()
    form.cliente_id.choices = [(0, "— Sin cliente —")] + [(c.id, c.nombre) for c in clientes]
    if request.method == "GET":
        form.cliente_id.data = aviso.cliente_id or 0
    if form.validate_on_submit():
        aviso.titulo = form.titulo.data.strip()
        aviso.cuerpo = form.cuerpo.data.strip() if form.cuerpo.data else None
        aviso.prioridad = form.prioridad.data
        aviso.cliente_id = form.cliente_id.data if form.cliente_id.data != 0 else None
        db.session.commit()
        flash("Aviso actualizado.", "success")
        return redirect(url_for("avisos.lista"))
    return render_template("avisos/form.html", form=form, titulo="Editar aviso", aviso=aviso)


# ─── Resolver aviso ───────────────────────────────────────────────────────────

@avisos_bp.route("/<int:aviso_id>/resolver", methods=["POST"])
@login_required
def resolver(aviso_id):
    aviso = Aviso.query.get_or_404(aviso_id)
    aviso.estado = "resuelto"
    aviso.resuelto_en = ahora()
    db.session.commit()
    flash(f"Aviso «{aviso.titulo}» marcado como resuelto.", "success")
    return redirect(request.referrer or url_for("avisos.lista"))


# ─── Reabrir aviso ────────────────────────────────────────────────────────────

@avisos_bp.route("/<int:aviso_id>/reabrir", methods=["POST"])
@login_required
def reabrir(aviso_id):
    aviso = Aviso.query.get_or_404(aviso_id)
    aviso.estado = "pendiente"
    aviso.resuelto_en = None
    db.session.commit()
    flash(f"Aviso «{aviso.titulo}» reabierto.", "info")
    return redirect(request.referrer or url_for("avisos.lista"))


# ─── Eliminar aviso ───────────────────────────────────────────────────────────

@avisos_bp.route("/<int:aviso_id>/eliminar", methods=["POST"])
@login_required
def eliminar(aviso_id):
    aviso = Aviso.query.get_or_404(aviso_id)
    titulo = aviso.titulo
    db.session.delete(aviso)
    db.session.commit()
    flash(f"Aviso «{titulo}» eliminado.", "info")
    return redirect(url_for("avisos.lista"))


# ─── Alertas de salud (funciones de email, se mantienen) ─────────────────────

def enviar_alerta_caida(instalacion: Instalacion, evento: EventoSalud):
    from flask import current_app
    admin_email = current_app.config.get("ADMIN_EMAIL")
    if not admin_email:
        return
    asunto = f"⚠️ CAÍDA: {instalacion.nombre}"
    cuerpo = f"""
Zynnos Central detectó un problema en una instalación.

📦 Sistema:   {instalacion.nombre}
👤 Cliente:   {instalacion.cliente.nombre}
🔗 URL:       {instalacion.url_sistema}
❌ Error:     {evento.detalle or 'Sin detalle'}
🕐 Horario:   {evento.chequeado_en.strftime('%d/%m/%Y %H:%M:%S')} UTC

Revisá el tablero: {current_app.config.get('BASE_URL', '')}/tablero
"""
    try:
        msg = Message(subject=asunto, recipients=[admin_email], body=cuerpo)
        mail.send(msg)
    except Exception as e:
        current_app.logger.error(f"Error enviando alerta email: {e}")


def enviar_alerta_recuperacion(instalacion: Instalacion):
    from flask import current_app
    admin_email = current_app.config.get("ADMIN_EMAIL")
    if not admin_email:
        return
    asunto = f"✅ RECUPERADO: {instalacion.nombre}"
    cuerpo = f"""
¡Buenas noticias! El sistema volvió a funcionar correctamente.

📦 Sistema:   {instalacion.nombre}
👤 Cliente:   {instalacion.cliente.nombre}
🔗 URL:       {instalacion.url_sistema}

Revisá el tablero: {current_app.config.get('BASE_URL', '')}/tablero
"""
    try:
        msg = Message(subject=asunto, recipients=[admin_email], body=cuerpo)
        mail.send(msg)
    except Exception as e:
        current_app.logger.error(f"Error enviando alerta recuperación: {e}")
