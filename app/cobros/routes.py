"""Módulo de cobros: planes, pagos mensuales, facturas y métricas de monotributo."""
import csv
import io
import json
from datetime import date, datetime, timezone
from calendar import monthrange
from decimal import Decimal

from flask import (
    Blueprint, render_template, redirect, url_for,
    flash, request, current_app, abort, jsonify, Response,
)
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, DecimalField, IntegerField, DateField, SelectField, TextAreaField
from wtforms.validators import DataRequired, NumberRange, Optional, Length

from app.models import db, Cliente, PlanCliente, Pago, Factura, Auditoria

cobros_bp = Blueprint("cobros", __name__)


# ─── Formularios ──────────────────────────────────────────────────────────────

class PlanForm(FlaskForm):
    descripcion = StringField("Descripción", validators=[DataRequired(), Length(max=255)])
    monto_ars = DecimalField("Monto mensual (ARS)", validators=[DataRequired(), NumberRange(min=0)])
    dia_vencimiento = IntegerField("Día de vencimiento", validators=[DataRequired(), NumberRange(min=1, max=28)], default=10)
    vigente_desde = DateField("Vigente desde", validators=[DataRequired()])
    vigente_hasta = DateField("Vigente hasta (dejar vacío si es actual)", validators=[Optional()])


class PagoForm(FlaskForm):
    anio = IntegerField("Año", validators=[DataRequired(), NumberRange(min=2024, max=2099)])
    mes = SelectField("Mes", coerce=int, choices=[
        (1,"Enero"),(2,"Febrero"),(3,"Marzo"),(4,"Abril"),
        (5,"Mayo"),(6,"Junio"),(7,"Julio"),(8,"Agosto"),
        (9,"Septiembre"),(10,"Octubre"),(11,"Noviembre"),(12,"Diciembre")
    ])
    monto_esperado = DecimalField("Monto esperado (ARS)", validators=[DataRequired(), NumberRange(min=0)])
    monto_recibido = DecimalField("Monto recibido (ARS)", validators=[Optional(), NumberRange(min=0)])
    estado = SelectField("Estado", choices=[
        ("pendiente","Pendiente"),("pagado","Pagado"),
        ("atrasado","Atrasado"),("perdonado","Perdonado")
    ])
    fecha_pago = DateField("Fecha de pago", validators=[Optional()])
    notas = TextAreaField("Notas", validators=[Optional(), Length(max=500)])


class FacturaForm(FlaskForm):
    cliente_id = SelectField("Cliente", coerce=int, validators=[DataRequired()])
    numero = StringField("Número (ej: 0001-00000042)", validators=[DataRequired(), Length(max=20)])
    fecha = DateField("Fecha", validators=[DataRequired()])
    monto_ars = DecimalField("Monto (ARS)", validators=[DataRequired(), NumberRange(min=0)])
    concepto = StringField("Concepto", validators=[Optional(), Length(max=500)])
    notas = TextAreaField("Notas", validators=[Optional(), Length(max=1000)])


# ─── Panel general de cobros ──────────────────────────────────────────────────

@cobros_bp.route("/")
@login_required
def inicio():
    """Vista principal: estado de pagos del mes actual y métricas de monotributo."""
    hoy = date.today()
    anio, mes = hoy.year, hoy.month

    # Pagos del mes actual
    pagos_mes = (
        Pago.query
        .filter_by(anio=anio, mes=mes)
        .join(Cliente)
        .order_by(Cliente.nombre)
        .all()
    )

    # Clientes activos sin pago registrado este mes
    clientes_activos = Cliente.query.filter_by(estado="activo").all()
    ids_con_pago = {p.cliente_id for p in pagos_mes}
    sin_pago = [c for c in clientes_activos if c.id not in ids_con_pago]

    # Métricas del mes
    total_esperado = sum(p.monto_esperado for p in pagos_mes)
    total_cobrado = sum(p.monto_recibido or 0 for p in pagos_mes if p.estado == "pagado")
    pendientes = [p for p in pagos_mes if p.estado in ("pendiente", "atrasado")]

    # Control de monotributo: facturado en los últimos 12 meses
    facturado_12m = _facturado_ultimos_12_meses()

    return render_template(
        "cobros/inicio.html",
        pagos_mes=pagos_mes,
        sin_pago=sin_pago,
        total_esperado=total_esperado,
        total_cobrado=total_cobrado,
        pendientes=pendientes,
        facturado_12m=facturado_12m,
        anio=anio,
        mes=mes,
        nombre_mes=_nombre_mes(mes),
    )


# ─── Pagos ────────────────────────────────────────────────────────────────────

@cobros_bp.route("/pagos")
@login_required
def lista_pagos():
    """Lista de pagos, filtrable por año/mes y estado."""  ### CAMBIO AQUÍ ###
    hoy = date.today()
    anio   = request.args.get("anio",   hoy.year,  type=int)
    mes    = request.args.get("mes",    hoy.month, type=int)
    estado = request.args.get("estado", "todos")

    q = Pago.query.filter_by(anio=anio, mes=mes).join(Cliente)
    if estado and estado != "todos":
        q = q.filter(Pago.estado == estado)

    pagos = q.order_by(Cliente.nombre).all()

    # totales del período sin filtro de estado (para mostrar en métricas)
    todos_pagos = Pago.query.filter_by(anio=anio, mes=mes).all()
    total_esperado = sum(p.monto_esperado for p in todos_pagos)
    total_cobrado  = sum(p.monto_recibido or 0 for p in todos_pagos if p.estado == "pagado")

    return render_template(
        "cobros/pagos.html",
        pagos=pagos,
        anio=anio,
        mes=mes,
        estado_filtro=estado,
        nombre_mes=_nombre_mes(mes),
        meses=_lista_meses(),
        total_esperado=total_esperado,  ### CAMBIO AQUÍ ###
        total_cobrado=total_cobrado,    ### CAMBIO AQUÍ ###
    )


@cobros_bp.route("/clientes/<int:cliente_id>/pago/nuevo", methods=["GET", "POST"])
@login_required
def nuevo_pago(cliente_id):
    cliente = db.get_or_404(Cliente, cliente_id)
    hoy = date.today()

    # Pre-rellenar con el plan activo del cliente
    plan_activo = _plan_activo(cliente_id)
    monto_sugerido = plan_activo.monto_ars if plan_activo else Decimal("0")

    form = PagoForm(
        anio=hoy.year,
        mes=hoy.month,
        monto_esperado=monto_sugerido,
    )

    if form.validate_on_submit():
        # Evitar duplicado
        existente = Pago.query.filter_by(
            cliente_id=cliente_id,
            anio=form.anio.data,
            mes=form.mes.data
        ).first()
        if existente:
            flash(f"Ya existe un pago registrado para {_nombre_mes(form.mes.data)} {form.anio.data}.", "warning")
            return redirect(url_for("cobros.nuevo_pago", cliente_id=cliente_id))

        pago = Pago(
            cliente_id=cliente_id,
            plan_id=plan_activo.id if plan_activo else None,
            anio=form.anio.data,
            mes=form.mes.data,
            monto_esperado=form.monto_esperado.data,
            monto_recibido=form.monto_recibido.data,
            estado=form.estado.data,
            fecha_pago=form.fecha_pago.data,
            notas=form.notas.data,
        )
        db.session.add(pago)
        _auditar("cobros.pago_nuevo", cliente_id, {"anio": form.anio.data, "mes": form.mes.data})
        db.session.commit()
        flash("Pago registrado.", "success")
        return redirect(url_for("cobros.detalle_cliente", cliente_id=cliente_id))

    return render_template("cobros/form_pago.html", form=form, cliente=cliente, plan_activo=plan_activo)


@cobros_bp.route("/pagos/<int:pago_id>/editar", methods=["GET", "POST"])
@login_required
def editar_pago(pago_id):
    pago = db.get_or_404(Pago, pago_id)
    form = PagoForm(obj=pago)

    if form.validate_on_submit():
        pago.monto_esperado = form.monto_esperado.data
        pago.monto_recibido = form.monto_recibido.data
        pago.estado = form.estado.data
        pago.fecha_pago = form.fecha_pago.data
        pago.notas = form.notas.data
        _auditar("cobros.pago_editar", pago.cliente_id, {"pago_id": pago_id, "estado": form.estado.data})
        db.session.commit()
        flash("Pago actualizado.", "success")
        return redirect(url_for("cobros.detalle_cliente", cliente_id=pago.cliente_id))

    return render_template("cobros/form_pago.html", form=form, cliente=pago.cliente, editar=True)


### CAMBIO AQUÍ ### — ruta eliminar pago
@cobros_bp.route("/pagos/<int:pago_id>/eliminar", methods=["POST"])
@login_required
def eliminar_pago(pago_id):
    pago = db.get_or_404(Pago, pago_id)
    cliente_id = pago.cliente_id
    resumen = f"{_nombre_mes(pago.mes)} {pago.anio}"
    _auditar("cobros.pago_eliminar", cliente_id, {"pago_id": pago_id, "periodo": resumen})
    db.session.delete(pago)
    db.session.commit()
    flash(f"Pago de {resumen} eliminado.", "info")
    return redirect(url_for("cobros.detalle_cliente", cliente_id=cliente_id))


@cobros_bp.route("/pagos/<int:pago_id>/marcar-pagado", methods=["POST"])
@login_required
def marcar_pagado(pago_id):
    """Acción rápida: marcar un pago como pagado con la fecha de hoy."""
    pago = db.get_or_404(Pago, pago_id)
    pago.estado = "pagado"
    pago.fecha_pago = date.today()
    if not pago.monto_recibido:
        pago.monto_recibido = pago.monto_esperado
    _auditar("cobros.pago_marcar_pagado", pago.cliente_id, {"pago_id": pago_id})
    db.session.commit()
    flash("Pago marcado como pagado.", "success")
    next_url = request.referrer or url_for("cobros.inicio")
    return redirect(next_url)


### CAMBIO AQUÍ ### — Acción masiva: marcar múltiples pagos como pagados
@cobros_bp.route("/pagos/bulk-accion", methods=["POST"])
@login_required
def bulk_accion():
    """Marcar múltiples pagos como pagados (acción masiva desde checkbox)."""
    ids_raw = request.form.getlist("pago_ids")
    accion  = request.form.get("accion", "marcar_pagado")
    anio    = request.form.get("anio", date.today().year, type=int)
    mes     = request.form.get("mes",  date.today().month, type=int)

    if not ids_raw:
        flash("Seleccioná al menos un pago.", "warning")
        return redirect(url_for("cobros.lista_pagos", anio=anio, mes=mes))

    try:
        ids = [int(i) for i in ids_raw]
    except ValueError:
        abort(400)

    pagos = Pago.query.filter(Pago.id.in_(ids)).all()
    hoy = date.today()
    count = 0
    for pago in pagos:
        if accion == "marcar_pagado" and pago.estado != "pagado":
            pago.estado = "pagado"
            pago.fecha_pago = hoy
            if not pago.monto_recibido:
                pago.monto_recibido = pago.monto_esperado
            _auditar("cobros.bulk_pagado", pago.cliente_id, {"pago_id": pago.id})
            count += 1
        elif accion == "marcar_atrasado" and pago.estado != "pagado":
            pago.estado = "atrasado"
            _auditar("cobros.bulk_atrasado", pago.cliente_id, {"pago_id": pago.id})
            count += 1

    db.session.commit()
    flash(f"{count} pago{'s' if count != 1 else ''} actualizado{'s' if count != 1 else ''}.", "success")
    return redirect(url_for("cobros.lista_pagos", anio=anio, mes=mes))


# ─── Planes ───────────────────────────────────────────────────────────────────

@cobros_bp.route("/clientes/<int:cliente_id>/plan/nuevo", methods=["GET", "POST"])
@login_required
def nuevo_plan(cliente_id):
    cliente = db.get_or_404(Cliente, cliente_id)
    form = PlanForm(vigente_desde=date.today())

    if form.validate_on_submit():
        # Desactivar plan anterior si existe
        plan_prev = _plan_activo(cliente_id)
        if plan_prev:
            plan_prev.activo = False
            plan_prev.vigente_hasta = form.vigente_desde.data

        plan = PlanCliente(
            cliente_id=cliente_id,
            descripcion=form.descripcion.data,
            monto_ars=form.monto_ars.data,
            dia_vencimiento=form.dia_vencimiento.data,
            vigente_desde=form.vigente_desde.data,
            vigente_hasta=form.vigente_hasta.data,
        )
        db.session.add(plan)
        _auditar("cobros.plan_nuevo", cliente_id, {"monto": str(form.monto_ars.data)})
        db.session.commit()
        flash("Plan guardado.", "success")
        return redirect(url_for("cobros.detalle_cliente", cliente_id=cliente_id))

    return render_template("cobros/form_plan.html", form=form, cliente=cliente)


# ─── Facturas ─────────────────────────────────────────────────────────────────

@cobros_bp.route("/facturas")
@login_required
def lista_facturas():
    facturas = (
        Factura.query
        .join(Cliente)
        .order_by(Factura.fecha.desc())
        .all()
    )
    return render_template("cobros/facturas.html", facturas=facturas)


@cobros_bp.route("/facturas/nueva", methods=["GET", "POST"])
@login_required
def nueva_factura():
    form = FacturaForm()
    form.cliente_id.choices = [
        (c.id, c.nombre)
        for c in Cliente.query.order_by(Cliente.nombre).all()
    ]

    if form.validate_on_submit():
        factura = Factura(
            cliente_id=form.cliente_id.data,
            numero=form.numero.data.strip(),
            fecha=form.fecha.data,
            monto_ars=form.monto_ars.data,
            concepto=form.concepto.data,
            notas=form.notas.data,
        )
        db.session.add(factura)
        _auditar("cobros.factura_nueva", form.cliente_id.data, {
            "numero": form.numero.data,
            "monto": str(form.monto_ars.data)
        })
        db.session.commit()
        flash("Factura registrada.", "success")
        return redirect(url_for("cobros.lista_facturas"))

    return render_template("cobros/form_factura.html", form=form)


@cobros_bp.route("/facturas/<int:factura_id>/eliminar", methods=["POST"])
@login_required
def eliminar_factura(factura_id):
    factura = db.get_or_404(Factura, factura_id)
    _auditar("cobros.factura_eliminar", factura.cliente_id, {"numero": factura.numero})
    db.session.delete(factura)
    db.session.commit()
    flash("Factura eliminada.", "info")
    return redirect(url_for("cobros.lista_facturas"))


# ─── Detalle de cobros por cliente ───────────────────────────────────────────

@cobros_bp.route("/clientes/<int:cliente_id>")
@login_required
def detalle_cliente(cliente_id):
    cliente = db.get_or_404(Cliente, cliente_id)
    plan_activo = _plan_activo(cliente_id)
    pagos = (
        Pago.query
        .filter_by(cliente_id=cliente_id)
        .order_by(Pago.anio.desc(), Pago.mes.desc())
        .all()
    )
    facturas = (
        Factura.query
        .filter_by(cliente_id=cliente_id)
        .order_by(Factura.fecha.desc())
        .all()
    )
    total_facturado = sum(f.monto_ars for f in facturas)
    total_cobrado = sum(p.monto_recibido or 0 for p in pagos if p.estado == "pagado")

    return render_template(
        "cobros/detalle_cliente.html",
        cliente=cliente,
        plan_activo=plan_activo,
        pagos=pagos,
        facturas=facturas,
        total_facturado=total_facturado,
        total_cobrado=total_cobrado,
    )


# ─── Métricas de monotributo ──────────────────────────────────────────────────

@cobros_bp.route("/monotributo")
@login_required
def monotributo():
    """Control del tope del monotributo: facturado en los últimos 12 meses."""
    facturado_12m = _facturado_ultimos_12_meses()

    # Desglose mes a mes (últimos 12)
    desglose = _desglose_mensual_12m()

    # Categorías de monotributo 2024 (actualizar según RG AFIP vigente)
    # Fuente: valores orientativos — actualizar con los oficiales de ARCA
    categorias = [
        {"letra": "A", "tope": 3_300_000},
        {"letra": "B", "tope": 4_950_000},
        {"letra": "C", "tope": 6_600_000},
        {"letra": "D", "tope": 9_900_000},
        {"letra": "E", "tope": 13_200_000},
        {"letra": "F", "tope": 16_500_000},
        {"letra": "G", "tope": 19_800_000},
        {"letra": "H", "tope": 30_000_000},
        {"letra": "I", "tope": 38_250_000},
        {"letra": "J", "tope": 47_250_000},
        {"letra": "K", "tope": 57_750_000},
    ]

    categoria_actual = None
    proxima_categoria = None
    for i, cat in enumerate(categorias):
        if float(facturado_12m) <= cat["tope"]:
            categoria_actual = cat
            if i + 1 < len(categorias):
                proxima_categoria = categorias[i + 1]
            break

    return render_template(
        "cobros/monotributo.html",
        facturado_12m=facturado_12m,
        desglose=desglose,
        categorias=categorias,
        categoria_actual=categoria_actual,
        proxima_categoria=proxima_categoria,
    )


# ─── Cron: generación automática de pagos mensuales ──────────────────────────

@cobros_bp.route("/cron/generar-pagos", methods=["POST"])
def cron_generar_pagos():
    """Crea pagos del mes para todos los clientes activos con plan activo.
    Llamar el día 1 de cada mes via cron externo (cron-job.org).
    Requiere header X-Cron-Token con el valor de la env var CRON_TOKEN.
    """
    token = request.headers.get("X-Cron-Token", "")
    if not token or token != current_app.config.get("CRON_TOKEN", ""):
        abort(403)

    hoy = date.today()
    anio, mes = hoy.year, hoy.month

    # Clientes activos con plan activo
    clientes = (
        Cliente.query
        .filter_by(estado="activo")
        .join(PlanCliente, (PlanCliente.cliente_id == Cliente.id) & (PlanCliente.activo == True))
        .all()
    )

    creados = 0
    omitidos = 0
    for cliente in clientes:
        plan = _plan_activo(cliente.id)
        if not plan:
            continue
        # No duplicar si ya existe pago para este mes
        existente = Pago.query.filter_by(
            cliente_id=cliente.id,
            anio=anio,
            mes=mes,
        ).first()
        if existente:
            omitidos += 1
            continue
        pago = Pago(
            cliente_id=cliente.id,
            plan_id=plan.id,
            anio=anio,
            mes=mes,
            monto_esperado=plan.monto_ars,
            estado="pendiente",
        )
        db.session.add(pago)
        creados += 1

    db.session.commit()
    return jsonify({"ok": True, "creados": creados, "omitidos": omitidos, "periodo": f"{anio}/{mes:02d}"})


# ─── Helpers privados ─────────────────────────────────────────────────────────

def _plan_activo(cliente_id):
    return PlanCliente.query.filter_by(cliente_id=cliente_id, activo=True).first()


def _facturado_ultimos_12_meses():
    """Suma de facturas emitidas en los últimos 12 meses corridos."""
    from sqlalchemy import func
    hoy = date.today()
    # primer día hace 12 meses
    if hoy.month == 12:
        desde = date(hoy.year - 1, 1, 1)
    else:
        desde = date(hoy.year - 1, hoy.month + 1, 1)
    result = db.session.query(
        func.coalesce(func.sum(Factura.monto_ars), 0)
    ).filter(Factura.fecha >= desde).scalar()
    return result or Decimal("0")


def _desglose_mensual_12m():
    """Lista de {anio, mes, nombre_mes, total} para los últimos 12 meses."""
    from sqlalchemy import func
    hoy = date.today()
    resultado = []
    for i in range(11, -1, -1):
        # retroceder i meses
        mes = hoy.month - i
        anio = hoy.year
        while mes <= 0:
            mes += 12
            anio -= 1
        total = db.session.query(
            func.coalesce(func.sum(Factura.monto_ars), 0)
        ).filter(
            db.extract('year', Factura.fecha) == anio,
            db.extract('month', Factura.fecha) == mes
        ).scalar() or Decimal("0")
        resultado.append({"anio": anio, "mes": mes, "nombre_mes": _nombre_mes(mes), "total": total})
    return resultado


def _nombre_mes(mes: int) -> str:
    nombres = ["","Enero","Febrero","Marzo","Abril","Mayo","Junio",
               "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
    return nombres[mes]


def _lista_meses():
    return [(i, _nombre_mes(i)) for i in range(1, 13)]


def _auditar(accion, cliente_id, detalle_dict):
    log = Auditoria(
        usuario_id=current_user.id,
        cliente_id=cliente_id,
        accion=accion,
        detalle=json.dumps(detalle_dict, ensure_ascii=False),
        ip=request.remote_addr,
    )
    db.session.add(log)


# ─── Exportación CSV ──────────────────────────────────────────────────────────

@cobros_bp.route("/exportar/pagos")
@login_required
def exportar_pagos():
    """Descarga CSV de todos los pagos filtrado por año (opcional)."""
    anio = request.args.get("anio", type=int)

    q = Pago.query.join(Cliente).order_by(Pago.anio.desc(), Pago.mes.desc(), Cliente.nombre)
    if anio:
        q = q.filter(Pago.anio == anio)

    pagos = q.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Cliente", "Año", "Mes", "Estado",
        "Monto esperado (ARS)", "Monto recibido (ARS)",
        "Fecha pago", "Notas",
    ])
    for p in pagos:
        writer.writerow([
            p.id,
            p.cliente.nombre,
            p.anio,
            p.mes,
            p.estado,
            p.monto_esperado,
            p.monto_recibido if p.monto_recibido is not None else "",
            p.fecha_pago.isoformat() if p.fecha_pago else "",
            p.notas or "",
        ])

    nombre = f"pagos_{anio or 'todos'}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={nombre}"},
    )


@cobros_bp.route("/exportar/facturas")
@login_required
def exportar_facturas():
    """Descarga CSV de todas las facturas filtrado por año (opcional)."""
    anio = request.args.get("anio", type=int)

    q = Factura.query.join(Cliente).order_by(Factura.fecha.desc())
    if anio:
        q = q.filter(db.extract("year", Factura.fecha) == anio)

    facturas = q.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Número", "Cliente", "Fecha",
        "Monto (ARS)", "Concepto", "Notas",
    ])
    for f in facturas:
        writer.writerow([
            f.id,
            f.numero,
            f.cliente.nombre,
            f.fecha.isoformat(),
            f.monto_ars,
            f.concepto or "",
            f.notas or "",
        ])

    nombre = f"facturas_{anio or 'todas'}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={nombre}"},
    )


@cobros_bp.route("/exportar/resumen-mensual")
@login_required
def exportar_resumen_mensual():
    """Descarga CSV con resumen de cobros por mes (todos los años)."""
    from sqlalchemy import func

    rows = (
        db.session.query(
            Pago.anio,
            Pago.mes,
            func.count(Pago.id).label("cantidad"),
            func.coalesce(func.sum(Pago.monto_esperado), 0).label("esperado"),
            func.coalesce(func.sum(
                db.case((Pago.estado == "pagado", Pago.monto_recibido), else_=0)
            ), 0).label("recibido"),
            func.sum(db.case((Pago.estado == "pendiente", 1), else_=0)).label("pendientes"),
            func.sum(db.case((Pago.estado == "atrasado", 1), else_=0)).label("atrasados"),
            func.sum(db.case((Pago.estado == "pagado", 1), else_=0)).label("pagados"),
        )
        .group_by(Pago.anio, Pago.mes)
        .order_by(Pago.anio.desc(), Pago.mes.desc())
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Año", "Mes", "Total cobros", "Esperado (ARS)",
        "Recibido (ARS)", "Pendientes", "Atrasados", "Pagados",
    ])
    for r in rows:
        writer.writerow([
            r.anio, r.mes, r.cantidad,
            r.esperado, r.recibido,
            r.pendientes, r.atrasados, r.pagados,
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=resumen_mensual.csv"},
    )
