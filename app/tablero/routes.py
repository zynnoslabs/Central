import json as _json
import os
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

# ─── Contexto Zynnos para Groq ────────────────────────────────────────────────
_ZYNNOS_CONTEXT = """
Sos el asistente estratégico interno de Pedro Santos, fundador de Zynnos Labs (Buenos Aires, Argentina).
Pedro tiene 23 años, es desarrollador independiente y lleva su agencia de automatización con IA.

CATÁLOGO DE SERVICIOS DE ZYNNOS:
1. Chatbot WhatsApp con IA — responde consultas, califica leads, opera 24/7
2. Agente de ventas IA — sigue prospectos, recuerda, persuade con contexto
3. Automatización de seguimiento — secuencias post-contacto sin intervención humana
4. Integración de sistemas — conecta CRM, facturación, e-commerce, reservas
5. Gestión de agenda con IA — toma turnos, confirma, cancela, reagenda
6. Reportes automáticos — dashboards y resúmenes enviados por WhatsApp o email

PRECIOS TÍPICOS (ARS):
- Chatbot básico: desde $80.000/mes
- Agente ventas: desde $150.000/mes
- Automatización: desde $60.000/mes proyecto
- Integración: desde $120.000 por proyecto
- Agenda IA: desde $70.000/mes
- Reportes: desde $40.000/mes

PERFIL DE CLIENTES IDEALES:
- Negocios con +20 consultas/día que no pueden responder todos
- Inmobiliarias, clínicas, talleres, tiendas online, distribuidores, restaurantes con delivery
- Dueños que trabajan solos o con 1-2 empleados, sin tiempo para seguimiento

CÓMO TRABAJA PEDRO:
- Arranca con diagnóstico gratuito, propuesta en 48hs, implementación en 1-2 semanas
- Contacto inicial por WhatsApp, luego videollamada de 20 min
- Garantía: si en 30 días no hay mejora visible, devuelve el primer mes
- Cobro mensual en ARS, transferencia bancaria

TU ROL: Cuando un lead llega desde la web con sus datos, analizás su situación y le decís a Pedro:
1. Qué tan calificado está el lead (frío / tibio / caliente)
2. Cuál servicio encaja mejor con su problema
3. Qué decirle en el primer mensaje de WhatsApp (tono informal, directo, argentino)
4. Próximo paso concreto para Pedro (no genérico)

Respondé siempre en español argentino, directo, sin paja. Formato: párrafos cortos.
Máximo 220 palabras.
"""


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

    clientes_activos    = Cliente.query.filter_by(estado="activo").count()
    clientes_en_prueba  = Cliente.query.filter_by(estado="en_prueba").count()
    clientes_suspendidos = Cliente.query.filter_by(estado="suspendido").count()
    clientes_total      = Cliente.query.count()

    cobros_pendientes = Pago.query.filter(
        Pago.estado.in_(["pendiente", "atrasado"]),
        Pago.anio == hoy.year,
        Pago.mes == hoy.month,
    ).count()

    cobros_atrasados = Pago.query.filter_by(estado="atrasado").count()

    pagos_mes = Pago.query.filter(
        Pago.estado == "pagado",
        Pago.anio == hoy.year,
        Pago.mes == hoy.month,
    ).all()
    monto_cobrado_mes = sum(float(p.monto_recibido or p.monto_esperado) for p in pagos_mes)

    pagos_esperados = Pago.query.filter(
        Pago.anio == hoy.year,
        Pago.mes == hoy.month,
    ).all()
    monto_esperado_mes = sum(float(p.monto_esperado) for p in pagos_esperados)

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
                    instalaciones_lentas.append({"inst": r["inst"], "avg_ms": avg_ms})

    uptime_promedio = round(sum(uptimes) / len(uptimes), 1) if uptimes else None

    avisos_pendientes = Aviso.query.filter_by(estado="pendiente").count()
    avisos_alta = Aviso.query.filter_by(estado="pendiente", prioridad="alta").count()

    # ── Leads web ─────────────────────────────────────────────────────────────
    leads_web = (
        Cliente.query
        .filter_by(estado="en_prueba")
        .order_by(Cliente.creado_en.desc())
        .limit(20)
        .all()
    )

    leads_parsed = []
    for lead in leads_web:
        info = {"rubro": "", "problema": "", "solucion": ""}
        if lead.notas:
            for linea in lead.notas.splitlines():
                if linea.startswith("Rubro:"):
                    info["rubro"] = linea.replace("Rubro:", "").strip()
                elif linea.startswith("Problema:"):
                    info["problema"] = linea.replace("Problema:", "").strip()
                elif linea.startswith("Solución recomendada:"):
                    info["solucion"] = linea.replace("Solución recomendada:", "").strip()
        leads_parsed.append({"cliente": lead, "info": info})

    return render_template(
        "tablero/inicio.html",
        resumen=resumen,
        total=total,
        ok_count=ok_count,
        error_count=error_count,
        clientes_activos=clientes_activos,
        clientes_en_prueba=clientes_en_prueba,
        clientes_suspendidos=clientes_suspendidos,
        clientes_total=clientes_total,
        cobros_pendientes=cobros_pendientes,
        cobros_atrasados=cobros_atrasados,
        monto_cobrado_mes=monto_cobrado_mes,
        monto_esperado_mes=monto_esperado_mes,
        uptime_promedio=uptime_promedio,
        instalaciones_lentas=instalaciones_lentas,
        avisos_pendientes=avisos_pendientes,
        avisos_alta=avisos_alta,
        leads_parsed=leads_parsed,
        hoy=hoy,
    )


# ─── Endpoint Groq: asesoría sobre un lead ────────────────────────────────────

@tablero_bp.route("/tablero/groq-lead/<int:cliente_id>", methods=["POST"])
@login_required
def groq_lead(cliente_id):
    token = (
        request.headers.get("X-CSRFToken")
        or request.form.get("_csrf_token")
        or ""
    )
    try:
        validate_csrf(token)
    except ValidationError:
        return jsonify({"ok": False, "error": "CSRF inválido"}), 400

    cliente = db.session.get(Cliente, cliente_id)
    if not cliente or cliente.estado != "en_prueba":
        return jsonify({"ok": False, "error": "Lead no encontrado"}), 404

    info = {"rubro": "No especificado", "problema": "No especificado",
            "solucion": "No especificado", "diagnostico": ""}
    if cliente.notas:
        for linea in cliente.notas.splitlines():
            if linea.startswith("Rubro:"):
                info["rubro"] = linea.replace("Rubro:", "").strip()
            elif linea.startswith("Problema:"):
                info["problema"] = linea.replace("Problema:", "").strip()
            elif linea.startswith("Solución recomendada:"):
                info["solucion"] = linea.replace("Solución recomendada:", "").strip()
            elif linea.startswith("Diagnóstico:"):
                info["diagnostico"] = linea.replace("Diagnóstico:", "").strip()

    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        return jsonify({"ok": False, "error": "GROQ_API_KEY no configurada en Render"}), 500

    user_msg = (
        f"Nombre: {cliente.nombre}\n"
        f"Email: {cliente.email}\n"
        f"Tel: {cliente.telefono or 'No dejó'}\n"
        f"Rubro: {info['rubro']}\n"
        f"Problema: {info['problema']}\n"
        f"Solución que Groq sugirió: {info['solucion']}\n"
        f"Diagnóstico previo: {info['diagnostico'][:300] if info['diagnostico'] else 'No disponible'}"
    )

    try:
        resp = http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={
                "model": "llama3-8b-8192",
                "messages": [
                    {"role": "system", "content": _ZYNNOS_CONTEXT},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.6,
                "max_tokens": 350,
            },
            timeout=15,
        )
        if not resp.ok:
            return jsonify({"ok": False, "error": f"Groq respondió {resp.status_code}"}), 502
        data = resp.json()
        texto = data["choices"][0]["message"]["content"].strip()
        return jsonify({"ok": True, "respuesta": texto})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─── Endpoint: activar lead ───────────────────────────────────────────────────

@tablero_bp.route("/tablero/activar-lead/<int:cliente_id>", methods=["POST"])
@login_required
def activar_lead(cliente_id):
    token = (
        request.headers.get("X-CSRFToken")
        or request.form.get("_csrf_token")
        or ""
    )
    try:
        validate_csrf(token)
    except ValidationError:
        return jsonify({"ok": False, "error": "CSRF inválido"}), 400

    cliente = db.session.get(Cliente, cliente_id)
    if not cliente:
        return jsonify({"ok": False, "error": "Cliente no encontrado"}), 404

    cliente.estado = "activo"
    db.session.commit()
    return jsonify({"ok": True, "nombre": cliente.nombre})


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


# ─── Re-chequeo manual ────────────────────────────────────────────────────────

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


# ─── Cron: chequear todas las instalaciones ───────────────────────────────────

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


# ─── Salud del panel ──────────────────────────────────────────────────────────

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
