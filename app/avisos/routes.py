from flask import Blueprint, render_template, jsonify, request, current_app
from flask_login import login_required
from flask_mail import Message

from app import mail
from app.models import db, EventoSalud, Instalacion, Cliente

avisos_bp = Blueprint("avisos", __name__)


# ─── Historial de eventos de salud ────────────────────────────────────────────

@avisos_bp.route("/")
@login_required
def historial():
    eventos = (
        EventoSalud.query
        .join(Instalacion)
        .join(Cliente)
        .order_by(EventoSalud.chequeado_en.desc())
        .limit(200)
        .all()
    )
    return render_template("avisos/historial.html", eventos=eventos)


# ─── Envío de alerta por email ────────────────────────────────────────────────

def enviar_alerta_caida(instalacion: Instalacion, evento: EventoSalud):
    """
    Envía un email de alerta cuando una instalación cae.
    Se llama desde tablero/routes.py después de detectar un error.
    """
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
        msg = Message(
            subject=asunto,
            recipients=[admin_email],
            body=cuerpo,
        )
        mail.send(msg)
    except Exception as e:
        current_app.logger.error(f"Error enviando alerta email: {e}")


def enviar_alerta_recuperacion(instalacion: Instalacion):
    """Email de recuperación cuando un sistema vuelve a funcionar."""
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
        msg = Message(
            subject=asunto,
            recipients=[admin_email],
            body=cuerpo,
        )
        mail.send(msg)
    except Exception as e:
        current_app.logger.error(f"Error enviando alerta recuperación: {e}")
