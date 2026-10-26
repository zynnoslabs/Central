import time
import requests as http
from flask import Blueprint, render_template, request, jsonify, current_app, abort
from flask_login import login_required
from flask_wtf.csrf import validate_csrf
from wtforms import ValidationError

from app.models import db, Instalacion, EventoSalud, Cliente

# ### CAMBIO AQUÍ ### — importamos funciones de alerta para notificar caídas/recuperaciones
from app.avisos.routes import enviar_alerta_caida, enviar_alerta_recuperacion

tablero_bp = Blueprint("tablero", __name__)

TIMEOUT_SALUD = 10  # segundos máximo por chequeo
HISTORIAL_TARJETA = 10  # últimos eventos que se muestran en cada card


# ─── Tablero principal ────────────────────────────────────────────────────────

@tablero_bp.route("/tablero")
@login_required
def inicio():
    instalaciones = (
        Instalacion.query
        .join(Cliente)
        .filter(Instalacion.activa == True)
        .order_by(Cliente.nombre, Instalacion.nombre)
        .all()
    )

    # Calcular semáforo por instalación
    resumen = []
    for inst in instalaciones:
        ultimo = inst.ultimo_estado()
        ### CAMBIO AQUÍ ### — cargamos historial reciente para mini-barra visual
        historial = (
            EventoSalud.query
            .filter_by(instalacion_id=inst.id)
            .order_by(EventoSalud.chequeado_en.desc())
            .limit(HISTORIAL_TARJETA)
            .all()
        )
        resumen.append({
            "inst": inst,
            "ultimo": ultimo,
            "semaforo": _semaforo(ultimo),
            "historial": list(reversed(historial)),  # cronológico: más viejo → más nuevo
        })

    # Ordenar: primero los que tienen error, después sin datos, después OK
    orden = {"error": 0, "sin_datos": 1, "ok": 2}
    resumen.sort(key=lambda r: orden.get(r["semaforo"], 1))

    total = len(resumen)
    ok_count = sum(1 for r in resumen if r["semaforo"] == "ok")
    error_count = sum(1 for r in resumen if r["semaforo"] == "error")

    return render_template(
        "tablero/inicio.html",
        resumen=resumen,
        total=total,
        ok_count=ok_count,
        error_count=error_count,
    )


# ─── Re-chequeo manual de una instalación ─────────────────────────────────────

### CAMBIO AQUÍ ### — endpoint para disparar un chequeo desde el panel, sin esperar cron
@tablero_bp.route("/rechequear/<int:inst_id>", methods=["POST"])
@login_required
def rechequear(inst_id):
    """
    Dispara un chequeo de salud inmediato sobre una instalación específica.
    Protegido con CSRF (token en header X-CSRFToken o campo _csrf_token).
    """
    # Validar CSRF manualmente (ya que es un POST con fetch/JS)
    token = (
        request.headers.get("X-CSRFToken")
        or request.form.get("_csrf_token")
        or ""
    )
    try:
        validate_csrf(token)
    except ValidationError:
        abort(400, "Token CSRF inválido.")

    inst = db.session.get(Instalacion, inst_id)
    if not inst or not inst.activa:
        abort(404)

    estado_previo = inst.ultimo_estado()
    evento = _chequear_instalacion(inst)

    # Alertas si cambia el estado
    if estado_previo is not None:
        if not evento.ok and estado_previo.ok:
            enviar_alerta_caida(inst, evento)
        elif evento.ok and not estado_previo.ok:
            enviar_alerta_recuperacion(inst)
    elif not evento.ok:
        enviar_alerta_caida(inst, evento)

    return jsonify({
        "ok": evento.ok,
        "tiempo_ms": evento.tiempo_ms,
        "codigo_http": evento.codigo_http,
        "detalle": evento.detalle,
        "chequeado_en": evento.chequeado_en.strftime("%d/%m/%Y %H:%M:%S"),
    })


# ─── Endpoint cron: chequear todas las instalaciones ──────────────────────────

@tablero_bp.route("/cron/chequear-salud")
def chequear_salud_cron():
    """
    Llamado por cron-job.org cada 5 minutos.
    Requiere token en header X-Cron-Token o parámetro ?token=
    """
    token_esperado = current_app.config.get("CRON_TOKEN", "")
    token_recibido = request.headers.get("X-Cron-Token") or request.args.get("token", "")

    if not token_esperado or token_recibido != token_esperado:
        return jsonify({"error": "No autorizado"}), 401

    instalaciones = Instalacion.query.filter_by(activa=True).all()
    resultados = []

    for inst in instalaciones:
        # ### CAMBIO AQUÍ ### — obtenemos estado previo para detectar cambios
        estado_previo = inst.ultimo_estado()
        evento = _chequear_instalacion(inst)

        # Enviar email solo cuando hay un cambio de estado (no en cada chequeo)
        if estado_previo is not None:
            if not evento.ok and estado_previo.ok:
                # Acaba de caerse
                enviar_alerta_caida(inst, evento)
            elif evento.ok and not estado_previo.ok:
                # Acaba de recuperarse
                enviar_alerta_recuperacion(inst)
        elif not evento.ok:
            # Primera medición y ya está caído
            enviar_alerta_caida(inst, evento)

        resultados.append({
            "instalacion": inst.nombre,
            "ok": evento.ok,
            "tiempo_ms": evento.tiempo_ms,
            "codigo_http": evento.codigo_http,
        })

    return jsonify({"chequeadas": len(resultados), "resultados": resultados})


# ─── Endpoint de salud de este propio panel ───────────────────────────────────

@tablero_bp.route("/salud")
def salud_panel():
    """Endpoint de health-check para el propio panel (Render lo usa)."""
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({"ok": True, "db": "ok"})
    except Exception as e:
        return jsonify({"ok": False, "db": str(e)}), 500


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _chequear_instalacion(inst: Instalacion) -> EventoSalud:
    """Hace una petición HTTP al endpoint /salud de la instalación y guarda el resultado."""
    ok = False
    tiempo_ms = None
    codigo_http = None
    detalle = None

    try:
        inicio = time.time()
        resp = http.get(inst.url_salud, timeout=TIMEOUT_SALUD)
        tiempo_ms = int((time.time() - inicio) * 1000)
        codigo_http = resp.status_code
        ok = resp.status_code == 200
        if not ok:
            detalle = f"HTTP {resp.status_code}"
    except http.exceptions.Timeout:
        detalle = f"Timeout ({TIMEOUT_SALUD}s)"
    except http.exceptions.ConnectionError as e:
        detalle = f"Error de conexión: {str(e)[:120]}"
    except Exception as e:
        detalle = f"Error inesperado: {str(e)[:120]}"

    evento = EventoSalud(
        instalacion_id=inst.id,
        ok=ok,
        tiempo_ms=tiempo_ms,
        codigo_http=codigo_http,
        detalle=detalle,
    )
    db.session.add(evento)
    db.session.commit()
    return evento


def _semaforo(ultimo: EventoSalud | None) -> str:
    """Devuelve 'ok', 'error' o 'sin_datos' según el último evento."""
    if ultimo is None:
        return "sin_datos"
    return "ok" if ultimo.ok else "error"
