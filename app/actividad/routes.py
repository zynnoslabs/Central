import hashlib
import hmac
import json
from datetime import datetime, timezone, timedelta

from flask import Blueprint, render_template, request, jsonify, abort, current_app
from flask_login import login_required
from sqlalchemy import func

from app.models import db, EventoActividad, Instalacion, Cliente

actividad_bp = Blueprint("actividad", __name__)

# Máximo de eventos que acepta el panel por request del agente
MAX_EVENTOS_BATCH = 50

# Cuántos eventos mostrar en el feed general
FEED_LIMITE = 200

# Cuántos eventos mostrar en la línea de tiempo por cliente
TIMELINE_LIMITE = 100


# ─── Recepción de eventos desde el agente ─────────────────────────────────────

@actividad_bp.route("/ingresar/<agente_token>", methods=["POST"])
def ingresar(agente_token):
    """
    Recibe eventos de actividad desde el agente instalado en cada sistema.

    El agente envía un JSON firmado con HMAC-SHA256 usando el agente_token
    de la instalación como clave secreta.

    Body esperado:
    {
        "firma": "<hmac-sha256-hex>",
        "eventos": [
            {
                "tipo": "pagina_vista",
                "subtipo": "/dashboard",
                "detalle": {"tiempo_ms": 210}   ← opcional, solo datos técnicos
            },
            ...
        ]
    }

    Privacidad: el agente NUNCA envía nombres, emails, teléfonos ni montos de
    terceros. Solo eventos técnicos y de uso anonimizados.
    """
    # 1. Buscar la instalación por token
    inst = Instalacion.query.filter_by(agente_token=agente_token, activa=True).first()
    if not inst:
        # Responder 200 igual para no bloquear el sistema cliente en caso de error
        return jsonify({"ok": False, "error": "token_invalido"}), 200

    # 2. Leer y validar el body
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "json_invalido"}), 200

    firma_recibida = body.get("firma", "")
    eventos_raw = body.get("eventos", [])

    if not isinstance(eventos_raw, list):
        return jsonify({"ok": False, "error": "formato_invalido"}), 200

    # 3. Verificar firma HMAC-SHA256
    # La firma cubre el JSON de la lista de eventos (serializado de forma estable)
    payload_str = json.dumps(eventos_raw, separators=(",", ":"), sort_keys=True)
    firma_esperada = hmac.new(
        agente_token.encode(),
        payload_str.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(firma_recibida, firma_esperada):
        current_app.logger.warning(
            f"Firma inválida para instalación {inst.id} ({inst.nombre})"
        )
        return jsonify({"ok": False, "error": "firma_invalida"}), 200

    # 4. Guardar eventos (máximo MAX_EVENTOS_BATCH por request)
    guardados = 0
    for ev in eventos_raw[:MAX_EVENTOS_BATCH]:
        tipo = str(ev.get("tipo", ""))[:50]
        subtipo = str(ev.get("subtipo", "") or "")[:100]
        detalle_raw = ev.get("detalle")

        if not tipo:
            continue

        # Serializar detalle como JSON si es dict, truncar si es string
        if isinstance(detalle_raw, dict):
            detalle_str = json.dumps(detalle_raw, ensure_ascii=False)[:500]
        elif detalle_raw:
            detalle_str = str(detalle_raw)[:500]
        else:
            detalle_str = None

        evento = EventoActividad(
            instalacion_id=inst.id,
            tipo=tipo,
            subtipo=subtipo or None,
            detalle=detalle_str,
        )
        db.session.add(evento)
        guardados += 1

    db.session.commit()
    return jsonify({"ok": True, "guardados": guardados}), 200


# ─── Feed general de actividad ────────────────────────────────────────────────

@actividad_bp.route("/")
@login_required
def feed():
    """Feed general: últimos N eventos de todas las instalaciones."""
    eventos = (
        EventoActividad.query
        .join(Instalacion)
        .join(Cliente)
        .order_by(EventoActividad.recibido_en.desc())
        .limit(FEED_LIMITE)
        .all()
    )

    # Resumen: conteo por tipo en las últimas 24h
    hace_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    resumen_tipos = (
        db.session.query(EventoActividad.tipo, func.count(EventoActividad.id))
        .filter(EventoActividad.recibido_en >= hace_24h)
        .group_by(EventoActividad.tipo)
        .order_by(func.count(EventoActividad.id).desc())
        .all()
    )

    return render_template(
        "actividad/feed.html",
        eventos=eventos,
        resumen_tipos=resumen_tipos,
        total_24h=sum(c for _, c in resumen_tipos),
    )


# ─── Línea de tiempo por cliente ──────────────────────────────────────────────

@actividad_bp.route("/cliente/<int:cliente_id>")
@login_required
def timeline_cliente(cliente_id):
    """Línea de tiempo de actividad de un cliente específico."""
    cliente = db.session.get(Cliente, cliente_id)
    if not cliente:
        abort(404)

    # IDs de todas las instalaciones del cliente
    inst_ids = [i.id for i in cliente.instalaciones]

    eventos = (
        EventoActividad.query
        .filter(EventoActividad.instalacion_id.in_(inst_ids))
        .order_by(EventoActividad.recibido_en.desc())
        .limit(TIMELINE_LIMITE)
        .all()
    ) if inst_ids else []

    # Conteo por instalación
    conteo_inst = {}
    if inst_ids:
        hace_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        rows = (
            db.session.query(
                EventoActividad.instalacion_id,
                func.count(EventoActividad.id)
            )
            .filter(
                EventoActividad.instalacion_id.in_(inst_ids),
                EventoActividad.recibido_en >= hace_24h,
            )
            .group_by(EventoActividad.instalacion_id)
            .all()
        )
        conteo_inst = {inst_id: cnt for inst_id, cnt in rows}

    return render_template(
        "actividad/timeline_cliente.html",
        cliente=cliente,
        eventos=eventos,
        conteo_inst=conteo_inst,
    )
