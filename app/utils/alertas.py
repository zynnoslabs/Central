"""
Utilidades de alertas automáticas para Zynnos Central.
Genera avisos internos cuando un cliente lleva demasiados días sin pagar.
"""
### CAMBIO AQUÍ ### — módulo nuevo de alertas automáticas
from datetime import date, datetime, timezone


# Días sin pago a partir de los cuales se genera alerta
UMBRAL_DIAS_ALERTA = 35


def generar_alertas_pago(umbral_dias: int = UMBRAL_DIAS_ALERTA) -> dict:
    """
    Recorre clientes activos y crea un Aviso por cada uno que lleve más de
    `umbral_dias` sin registrar un pago. No duplica: si ya existe un aviso
    pendiente con el mismo título para ese cliente, lo omite.

    Retorna {"creados": int, "omitidos": int, "clientes": [nombres]}.
    """
    from app.models import db, Cliente, Pago, Aviso

    hoy = date.today()
    creados = 0
    omitidos = 0
    nombres_alertados = []

    clientes_activos = Cliente.query.filter_by(estado="activo").all()

    for cliente in clientes_activos:
        # Último pago confirmado
        ultimo_pago = (
            Pago.query
            .filter_by(cliente_id=cliente.id)
            .filter(Pago.fecha_pago.isnot(None))
            .order_by(Pago.fecha_pago.desc())
            .first()
        )

        if ultimo_pago and ultimo_pago.fecha_pago:
            fp = ultimo_pago.fecha_pago
            if hasattr(fp, "year"):
                ref = date(fp.year, fp.month, fp.day)
            else:
                ref = fp
        else:
            # Sin pagos: usar fecha de alta del cliente
            creado = cliente.creado_en
            if isinstance(creado, datetime):
                ref = creado.date()
            else:
                ref = creado

        dias = (hoy - ref).days

        if dias < umbral_dias:
            continue  # está al día

        titulo = f"Sin pago: {cliente.nombre} ({dias}d)"

        # Buscar si ya existe aviso pendiente similar para este cliente
        ya_existe = (
            Aviso.query
            .filter_by(cliente_id=cliente.id, estado="pendiente")
            .filter(Aviso.titulo.like(f"Sin pago: {cliente.nombre}%"))
            .first()
        )

        if ya_existe:
            omitidos += 1
            continue

        prioridad = "alta" if dias >= 60 else "media"

        aviso = Aviso(
            titulo=titulo,
            cuerpo=(
                f"El cliente {cliente.nombre} lleva {dias} días sin registrar un pago.\n"
                f"Último pago: {ref.strftime('%d/%m/%Y')}."
            ),
            prioridad=prioridad,
            estado="pendiente",
            cliente_id=cliente.id,
        )
        db.session.add(aviso)
        creados += 1
        nombres_alertados.append(cliente.nombre)

    if creados:
        db.session.commit()

    return {"creados": creados, "omitidos": omitidos, "clientes": nombres_alertados}
