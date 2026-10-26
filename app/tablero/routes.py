import time
import requests as http
from flask import Blueprint, render_template, request, jsonify, current_app, abort
from flask_login import login_required
from flask_wtf.csrf import validate_csrf
from wtforms import ValidationError

from app.models import db, Instalacion, EventoSalud, Cliente, Pago, Aviso  ### CAMBIO AQUÍ ###

from app.avisos.routes import enviar_alerta_caida, enviar_alerta_recuperacion

tablero_bp = Blueprint("tablero", __name__)

TIMEOUT_SALUD = 10
HISTORIAL_TARJETA = 10


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

    resumen = []
    for inst in instalaciones:
        ultimo = inst.ultimo_estado()
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
            "historial": list(reversed(historial)),
        })

    orden = {"error": 0, "sin_datos": 1, "ok": 2}
    resumen.sort(key=lambda r: orden.get(r["semaforo"], 1))

    total = len(resumen)
    ok_count = sum(1 for r in resumen if r["semaforo"] == "ok")
    error_count = sum(1 for r in resumen if r["semaforo"] == "error")

    ### CAMBIO AQUÍ ### — métricas adicionales para tiles del tablero
    from datetime import date
    hoy = date.today()

    clientes_activos = Cliente.query.filter_by(estado="activo").count()

    cobros_pendientes = Pago.query.filter(
        Pago.estado.in_(["pendiente", "atrasado"]),
        Pago.anio == hoy.year,
        Pago.mes == hoy.month,
    ).count()

    avisos_pendientes = Aviso.query.filter_by(estado="pendiente").count()

    return render_template(
        "tablero/inicio.html",
        resumen=resumen,
        total=total,
        ok_count=ok_count,
        error_count=error_count,
        clientes_activos=clientes_activos,
        cobros_pendientes=cobros_pendientes,
        avisos_pendientes=avisos_pendientes,
    )


@tablero_bp.route("/rechequear/<int:inst_id>", methods=["POST"])
@login_required
def rechequear(inst_id):
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


@tablero_bp.route("/cron/chequear-salud")
def chequear_salud_cron():
    token_esperado = current_app.config.get("CRON_TOKEN", "")
    token_recibido = request.headers.get("X-Cron-Token") or request.args.get("token", "")

    if not token_esperado or token_recibido != token_esperado:
        return jsonify({"error": "No autorizado"}), 401

    instalaciones = Instalacion.query.filter_by(activa=True).all()
    resultados = []

    for inst in instalaciones:
        estado_previo = inst.ultimo_estado()
        evento = _chequear_instalacion(inst)

        if estado_previo is not None:
            if not evento.ok and estado_previo.ok:
                enviar_alerta_caida(inst, evento)
            elif evento.ok and not estado_previo.ok:
                enviar_alerta_recuperacion(inst)
        elif not evento.ok:
            enviar_alerta_caida(inst, evento)

        resultados.append({
            "instalacion": inst.nombre,
            "ok": evento.ok,
            "tiempo_ms": evento.tiempo_ms,
            "codigo_http": evento.codigo_http,
        })

    return jsonify({"chequeadas": len(resultados), "resultados": resultados})


@tablero_bp.route("/salud")
def salud_panel():
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({"ok": True, "db": "ok"})
    except Exception as e:
        return jsonify({"ok": False, "db": str(e)}), 500


def _chequear_instalacion(inst: Instalacion) -> EventoSalud:
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
    if ultimo is None:
        return "sin_datos"
    return "ok" if ultimo.ok else "error"
