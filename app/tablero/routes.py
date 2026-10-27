import secrets
import time
import requests as http
from flask import Blueprint, render_template, request, jsonify, current_app, abort, flash, redirect, url_for
from flask_login import login_required
from flask_wtf import FlaskForm
from flask_wtf.csrf import validate_csrf
from wtforms import StringField, BooleanField, DecimalField
from wtforms.validators import DataRequired, Length, Optional, URL
from wtforms import ValidationError
from datetime import date
from decimal import Decimal

from app.models import db, Instalacion, EventoSalud, Cliente, Pago, Aviso, Factura

from app.avisos.routes import enviar_alerta_caida, enviar_alerta_recuperacion

tablero_bp = Blueprint("tablero", __name__)

TIMEOUT_SALUD = 10
HISTORIAL_TARJETA = 10
HISTORIAL_DETALLE = 200


# ─── Formulario de edición de instalación ─────────────────────────────────────

class InstalacionForm(FlaskForm):
    nombre          = StringField("Nombre", validators=[DataRequired(), Length(max=255)])
    url_sistema     = StringField("URL sistema", validators=[DataRequired(), URL(), Length(max=500)])
    url_salud       = StringField("URL salud", validators=[DataRequired(), URL(), Length(max=500)])
    render_service_id = StringField("Render Service ID", validators=[Optional(), Length(max=100)])
    neon_project_id = StringField("Neon Project ID", validators=[Optional(), Length(max=100)])
    sentry_project  = StringField("Sentry Project", validators=[Optional(), Length(max=100)])
    costo_usd       = DecimalField("Costo mensual (USD)", validators=[Optional()], places=2, default=0)
    activa          = BooleanField("Activa")


# ─── Tablero principal ────────────────────────────────────────────────────────

@tablero_bp.route("/tablero")
@login_required
def inicio():
    hoy = date.today()

    # ── Instalaciones activas y semáforo ──────────────────────────────────────
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

    # ── Métricas de clientes ──────────────────────────────────────────────────
    clientes_activos    = Cliente.query.filter_by(estado="activo").count()
    clientes_en_prueba  = Cliente.query.filter_by(estado="en_prueba").count()
    clientes_suspendidos = Cliente.query.filter_by(estado="suspendido").count()
    clientes_total      = Cliente.query.count()

    # ── Métricas de cobros (mes actual) ───────────────────────────────────────
    cobros_pendientes = Pago.query.filter(
        Pago.estado.in_(["pendiente", "atrasado"]),
        Pago.anio == hoy.year,
        Pago.mes == hoy.month,
    ).count()

    cobros_atrasados = Pago.query.filter_by(estado="atrasado").count()

    # Monto cobrado este mes (pagos con estado=pagado del mes actual)
    pagos_mes = Pago.query.filter(
        Pago.estado == "pagado",
        Pago.anio == hoy.year,
        Pago.mes == hoy.month,
    ).all()
    monto_cobrado_mes = sum(float(p.monto_recibido or p.monto_esperado) for p in pagos_mes)

    # Monto esperado este mes (todos los pagos del mes)
    pagos_esperados = Pago.query.filter(
        Pago.anio == hoy.year,
        Pago.mes == hoy.month,
    ).all()
    monto_esperado_mes = sum(float(p.monto_esperado) for p in pagos_esperados)

    # ── Métricas de performance (últimos 20 eventos por instalación) ──────────
    ### CAMBIO AQUÍ ### — uptime promedio y alertas de lentitud
    uptimes = []
    instalaciones_lentas = []
    for r in resumen:
        eventos_recientes = (
            EventoSalud.query
            .filter_by(instalacion_id=r["inst"].id)
            .order_by(EventoSalud.chequeado_en.desc())
            .limit(20)
            .all()
        )
        if eventos_recientes:
            ok_n = sum(1 for e in eventos_recientes if e.ok)
            uptime = round(ok_n / len(eventos_recientes) * 100, 1)
            uptimes.append(uptime)

            tiempos_ok = [e.tiempo_ms for e in eventos_recientes if e.ok and e.tiempo_ms]
            if tiempos_ok:
                avg_ms = sum(tiempos_ok) // len(tiempos_ok)
                if avg_ms > 1000:
                    instalaciones_lentas.append({
                        "inst": r["inst"],
                        "avg_ms": avg_ms,
                    })

    uptime_promedio = round(sum(uptimes) / len(uptimes), 1) if uptimes else None

    # ── Avisos ────────────────────────────────────────────────────────────────
    avisos_pendientes = Aviso.query.filter_by(estado="pendiente").count()
    avisos_alta = Aviso.query.filter_by(estado="pendiente", prioridad="alta").count()

    return render_template(
        "tablero/inicio.html",
        resumen=resumen,
        total=total,
        ok_count=ok_count,
        error_count=error_count,
        # clientes
        clientes_activos=clientes_activos,
        clientes_en_prueba=clientes_en_prueba,
        clientes_suspendidos=clientes_suspendidos,
        clientes_total=clientes_total,
        # cobros
        cobros_pendientes=cobros_pendientes,
        cobros_atrasados=cobros_atrasados,
        monto_cobrado_mes=monto_cobrado_mes,
        monto_esperado_mes=monto_esperado_mes,
        # performance
        uptime_promedio=uptime_promedio,
        instalaciones_lentas=instalaciones_lentas,
        # avisos
        avisos_pendientes=avisos_pendientes,
        avisos_alta=avisos_alta,
        # fecha
        hoy=hoy,
    )


# ─── Detalle de instalación ───────────────────────────────────────────────────

@tablero_bp.route("/instalacion/<int:inst_id>")
@login_required
def detalle_instalacion(inst_id):
    inst = db.session.get(Instalacion, inst_id)
    if not inst:
        abort(404)

    ultimo = inst.ultimo_estado()
    eventos = (
        EventoSalud.query
        .filter_by(instalacion_id=inst.id)
        .order_by(EventoSalud.chequeado_en.desc())
        .limit(HISTORIAL_DETALLE)
        .all()
    )

    total_eventos = len(eventos)
    ok_eventos = sum(1 for e in eventos if e.ok)
    uptime_pct = round((ok_eventos / total_eventos * 100), 1) if total_eventos else 0

    return render_template(
        "tablero/detalle.html",
        inst=inst,
        ultimo=ultimo,
        semaforo=_semaforo(ultimo),
        eventos=eventos,
        total_eventos=total_eventos,
        uptime_pct=uptime_pct,
    )


# ─── Editar instalación ───────────────────────────────────────────────────────

@tablero_bp.route("/instalacion/<int:inst_id>/editar", methods=["GET", "POST"])
@login_required
def editar_instalacion(inst_id):
    inst = db.session.get(Instalacion, inst_id)
    if not inst:
        abort(404)

    form = InstalacionForm(obj=inst)

    if form.validate_on_submit():
        inst.nombre           = form.nombre.data
        inst.url_sistema      = form.url_sistema.data
        inst.url_salud        = form.url_salud.data
        inst.render_service_id = form.render_service_id.data or None
        inst.neon_project_id  = form.neon_project_id.data or None
        inst.sentry_project   = form.sentry_project.data or None
        inst.costo_usd        = form.costo_usd.data or 0
        inst.activa           = form.activa.data
        db.session.commit()
        flash("Instalación actualizada.", "success")
        return redirect(url_for("tablero.detalle_instalacion", inst_id=inst.id))

    return render_template("tablero/editar_instalacion.html", inst=inst, form=form)


# ─── Regenerar token del agente ───────────────────────────────────────────────

@tablero_bp.route("/instalacion/<int:inst_id>/regenerar-token", methods=["POST"])
@login_required
def regenerar_token(inst_id):
    inst = db.session.get(Instalacion, inst_id)
    if not inst:
        abort(404)
    inst.agente_token = secrets.token_hex(32)
    db.session.commit()
    flash("Token regenerado correctamente.", "success")
    return redirect(url_for("tablero.detalle_instalacion", inst_id=inst.id))


# ─── Re-chequeo manual de una instalación ─────────────────────────────────────

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


# ─── Endpoint cron: chequear todas las instalaciones ──────────────────────────

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


# ─── Endpoint de salud de este propio panel ───────────────────────────────────

@tablero_bp.route("/salud")
def salud_panel():
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({"ok": True, "db": "ok"})
    except Exception as e:
        return jsonify({"ok": False, "db": str(e)}), 500


# ─── Helpers ──────────────────────────────────────────────────────────────────

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
