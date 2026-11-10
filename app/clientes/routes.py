import json
from flask import Blueprint, render_template, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional

from app.models import db, Cliente, Producto, Instalacion, Auditoria

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
    return render_template("clientes/lista.html", clientes=clientes)


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
    cliente = Cliente.query.get_or_404(cliente_id)
    return render_template("clientes/detalle.html", cliente=cliente)


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
