import csv
import io
import json
from datetime import datetime, timezone
from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, Response
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional

from app.models import db, Cliente, Producto, Instalacion, Auditoria, Pago  ### CAMBIO AQUÍ ###

clientes_bp = Blueprint("clientes", __name__)


# ─── Formularios ──────────────────────────────────────────────────────────────

class ClienteForm(FlaskForm):
    nombre = StringField("Nombre / Empresa", validators=[DataRequired(), Length(max=255)])
    email = StringField("Email de contacto", validators=[DataRequired(), Email(), Length(max=255)])
    telefono = StringField("Teléfono", validators=[Optional(), Length(max=50)])
    estado = SelectField("Estado", choices=[
        ("activo", "Activo"),
        ("en_prueba", "En prueba"),
        ("suspendido", "Suspendido"),
        ("baja", "Baja"),
    ])
    notas = TextAreaField("Notas internas", validators=[Optional(), Length(max=2000)])


class InstalacionForm(FlaskForm):
    nombre = StringField("Nombre de instalación", validators=[DataRequired(), Length(max=255)])
    producto_id = SelectField("Producto", coerce=int, validators=[DataRequired()])
    url_sistema = StringField("URL del sistema", validators=[DataRequired(), Length(max=500)])
    url_salud = StringField("URL endpoint /salud", validators=[DataRequired(), Length(max=500)])
    render_service_id = StringField("Render Service ID", validators=[Optional(), Length(max=100)])
    neon_project_id = StringField("Neon Project ID", validators=[Optional(), Length(max=100)])
    sentry_project = StringField("Sentry Project Slug", validators=[Optional(), Length(max=100)])
    agente_token = StringField("Token del agente (Zynnos)", validators=[Optional(), Length(max=64)])


# ─── Clientes ─────────────────────────────────────────────────────────────────

@clientes_bp.route("/")
@login_required
def lista():
    clientes = Cliente.query.order_by(Cliente.creado_en.desc()).all()
    ### CAMBIO AQUÍ ### — calcular días sin contacto desde último pago
    hoy = datetime.now(timezone.utc)
    clientes_data = []
    for c in clientes:
        ultimo_pago = (
            Pago.query
            .filter_by(cliente_id=c.id)
            .filter(Pago.fecha_pago.isnot(None))
            .order_by(Pago.fecha_pago.desc())
            .first()
        )
        if ultimo_pago and ultimo_pago.fecha_pago:
            # fecha_pago es db.Date — convertir a datetime para restar
            fp = datetime(
                ultimo_pago.fecha_pago.year,
                ultimo_pago.fecha_pago.month,
                ultimo_pago.fecha_pago.day,
                tzinfo=timezone.utc,
            )
            dias = (hoy - fp).days
        else:
            creado = c.creado_en
            if creado.tzinfo is None:
                creado = creado.replace(tzinfo=timezone.utc)
            dias = (hoy - creado).days
        clientes_data.append({"cliente": c, "dias_sin_contacto": dias})
    return render_template("clientes/lista.html", clientes=clientes, clientes_data=clientes_data)


### CAMBIO AQUÍ ### — exportar clientes a CSV
@clientes_bp.route("/exportar.csv")
@login_required
def exportar_csv():
    clientes = Cliente.query.order_by(Cliente.nombre).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Nombre", "Email", "Teléfono", "Estado", "Instalaciones activas", "Alta"])
    for c in clientes:
        inst_activas = sum(1 for i in c.instalaciones if i.activa)
        writer.writerow([
            c.nombre,
            c.email,
            c.telefono or "",
            c.estado,
            inst_activas,
            c.creado_en.strftime("%d/%m/%Y"),
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=clientes.csv"}
    )


@clientes_bp.route("/nuevo", methods=["GET", "POST"])
@login_required
def nuevo():
    form = ClienteForm()
    if form.validate_on_submit():
        cliente = Cliente(
            nombre=form.nombre.data.strip(),
            email=form.email.data.strip().lower(),
            telefono=form.telefono.data.strip() if form.telefono.data else None,
            estado=form.estado.data,
            notas=form.notas.data,
        )
        db.session.add(cliente)
        db.session.flush()  # para obtener el ID antes de commit
        _log(current_user.id, cliente.id, "cliente.crear", {"nombre": cliente.nombre})
        db.session.commit()
        flash(f"Cliente «{cliente.nombre}» creado.", "success")
        return redirect(url_for("clientes.detalle", cliente_id=cliente.id))
    return render_template("clientes/form.html", form=form, titulo="Nuevo cliente")


@clientes_bp.route("/<int:cliente_id>")
@login_required
def detalle(cliente_id):
    from app.models import Aviso  ### CAMBIO AQUÍ ### — avisos del cliente en ficha
    from datetime import date
    cliente = Cliente.query.get_or_404(cliente_id)
    avisos_cliente = (
        Aviso.query
        .filter_by(cliente_id=cliente_id)
        .order_by(Aviso.estado.asc(), Aviso.creado_en.desc())
        .all()
    )
    hoy = date.today()
    return render_template("clientes/detalle.html", cliente=cliente,
                           avisos_cliente=avisos_cliente, hoy=hoy)


@clientes_bp.route("/<int:cliente_id>/editar", methods=["GET", "POST"])
@login_required
def editar(cliente_id):
    cliente = Cliente.query.get_or_404(cliente_id)
    form = ClienteForm(obj=cliente)
    if form.validate_on_submit():
        form.populate_obj(cliente)
        cliente.email = cliente.email.strip().lower()
        _log(current_user.id, cliente.id, "cliente.editar", {"nombre": cliente.nombre})
        db.session.commit()
        flash("Cliente actualizado.", "success")
        return redirect(url_for("clientes.detalle", cliente_id=cliente.id))
    return render_template("clientes/form.html", form=form, titulo="Editar cliente", cliente=cliente)


### CAMBIO AQUÍ ### — ruta eliminar cliente
@clientes_bp.route("/<int:cliente_id>/eliminar", methods=["POST"])
@login_required
def eliminar(cliente_id):
    cliente = Cliente.query.get_or_404(cliente_id)
    nombre = cliente.nombre
    # El cascade en el modelo borra instalaciones, pagos, facturas, planes y auditoría asociados
    db.session.delete(cliente)
    db.session.commit()
    flash(f"Cliente «{nombre}» eliminado.", "info")
    return redirect(url_for("clientes.lista"))


# ─── Instalaciones ─────────────────────────────────────────────────────────────

@clientes_bp.route("/<int:cliente_id>/instalaciones/nueva", methods=["GET", "POST"])
@login_required
def nueva_instalacion(cliente_id):
    cliente = Cliente.query.get_or_404(cliente_id)
    form = InstalacionForm()
    form.producto_id.choices = [
        (p.id, p.nombre) for p in Producto.query.filter_by(activo=True).all()
    ]
    if form.validate_on_submit():
        inst = Instalacion(
            cliente_id=cliente.id,
            producto_id=form.producto_id.data,
            nombre=form.nombre.data.strip(),
            url_sistema=form.url_sistema.data.strip().rstrip("/"),
            url_salud=form.url_salud.data.strip().rstrip("/"),
            render_service_id=form.render_service_id.data or None,
            neon_project_id=form.neon_project_id.data or None,
            sentry_project=form.sentry_project.data or None,
            agente_token=form.agente_token.data.strip() or None,
        )
        db.session.add(inst)
        db.session.flush()
        _log(current_user.id, cliente.id, "instalacion.crear", {"nombre": inst.nombre, "url": inst.url_sistema})
        db.session.commit()
        flash(f"Instalación «{inst.nombre}» agregada.", "success")
        return redirect(url_for("clientes.detalle", cliente_id=cliente.id))
    return render_template(
        "clientes/instalacion_form.html",
        form=form, cliente=cliente, titulo="Nueva instalación"
    )


@clientes_bp.route("/instalaciones/<int:inst_id>/toggle")
@login_required
def toggle_instalacion(inst_id):
    inst = Instalacion.query.get_or_404(inst_id)
    inst.activa = not inst.activa
    accion = "instalacion.activar" if inst.activa else "instalacion.desactivar"
    _log(current_user.id, inst.cliente_id, accion, {"nombre": inst.nombre})
    db.session.commit()
    estado = "activada" if inst.activa else "desactivada"
    flash(f"Instalación {estado}.", "info")
    return redirect(url_for("clientes.detalle", cliente_id=inst.cliente_id))


### CAMBIO AQUÍ ### — ruta eliminar instalación
@clientes_bp.route("/instalaciones/<int:inst_id>/eliminar", methods=["POST"])
@login_required
def eliminar_instalacion(inst_id):
    inst = Instalacion.query.get_or_404(inst_id)
    cliente_id = inst.cliente_id
    nombre = inst.nombre
    db.session.delete(inst)
    db.session.commit()
    flash(f"Instalación «{nombre}» eliminada.", "info")
    return redirect(url_for("clientes.detalle", cliente_id=cliente_id))


# ─── Notas rápidas ────────────────────────────────────────────────────────────

@clientes_bp.route("/<int:cliente_id>/notas", methods=["POST"])
@login_required
def guardar_notas(cliente_id):
    cliente = Cliente.query.get_or_404(cliente_id)
    texto = request.form.get("notas", "").strip()
    cliente.notas = texto or None
    _log(current_user.id, cliente.id, "cliente.notas", {"chars": len(texto)})
    db.session.commit()
    flash("Notas guardadas.", "success")
    return redirect(url_for("clientes.detalle", cliente_id=cliente.id))


# ─── Helper ────────────────────────────────────────────────────────────────────

def _log(usuario_id, cliente_id, accion, detalle_dict):
    log = Auditoria(
        usuario_id=usuario_id,
        cliente_id=cliente_id,
        accion=accion,
        detalle=json.dumps(detalle_dict, ensure_ascii=False),
        ip=request.remote_addr,
    )
    db.session.add(log)
