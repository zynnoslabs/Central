"""Tests del módulo de cobros (Etapa 2)."""
import pytest
from datetime import date
from decimal import Decimal

from app import create_app
from app.models import db, Cliente, PlanCliente, Pago, Factura


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    """App con base de datos en memoria para tests."""
    class TestConfig:
        TESTING = True
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        WTF_CSRF_ENABLED = False
        SECRET_KEY = "test-secret-key"
        ADMIN_EMAIL = "admin@test.com"
        ADMIN_PASSWORD = "Admin1234!"
        MAIL_SUPPRESS_SEND = True
        SERVER_NAME = "localhost"

    application = create_app(TestConfig)
    with application.app_context():
        db.create_all()
        yield application


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


@pytest.fixture(scope="module")
def autenticado(client):
    """Cliente HTTP ya logueado."""
    client.post("/login", data={"email": "admin@test.com", "password": "Admin1234!"})
    return client


@pytest.fixture()
def cliente_de_prueba(app):
    """Crea un cliente de prueba y lo elimina después del test."""
    with app.app_context():
        c = Cliente(nombre="Cliente Test Cobros", email="cobros@test.com", estado="activo")
        db.session.add(c)
        db.session.commit()
        yield c
        db.session.delete(c)
        db.session.commit()


# ─── Tests de modelos ─────────────────────────────────────────────────────────

class TestPlanCliente:
    def test_crear_plan(self, app, cliente_de_prueba):
        with app.app_context():
            # Re-fetch para evitar DetachedInstanceError
            c = db.session.get(Cliente, cliente_de_prueba.id)
            plan = PlanCliente(
                cliente_id=c.id,
                descripcion="Plan Base",
                monto_ars=Decimal("15000.00"),
                dia_vencimiento=10,
                vigente_desde=date(2024, 1, 1),
            )
            db.session.add(plan)
            db.session.commit()

            guardado = db.session.get(PlanCliente, plan.id)
            assert guardado.monto_ars == Decimal("15000.00")
            assert guardado.activo is True
            assert guardado.dia_vencimiento == 10

            # Limpiar
            db.session.delete(guardado)
            db.session.commit()

    def test_repr(self, app):
        with app.app_context():
            p = PlanCliente(descripcion="Plan Test", monto_ars=Decimal("5000"))
            assert "Plan Test" in repr(p)


class TestPago:
    def test_crear_pago(self, app, cliente_de_prueba):
        with app.app_context():
            c = db.session.get(Cliente, cliente_de_prueba.id)
            pago = Pago(
                cliente_id=c.id,
                anio=2024,
                mes=6,
                monto_esperado=Decimal("15000.00"),
                estado="pendiente",
            )
            db.session.add(pago)
            db.session.commit()

            guardado = db.session.get(Pago, pago.id)
            assert guardado.estado == "pendiente"
            assert guardado.anio == 2024
            assert guardado.mes == 6

            db.session.delete(guardado)
            db.session.commit()

    def test_repr(self, app):
        with app.app_context():
            p = Pago(cliente_id=1, anio=2024, mes=3, monto_esperado=Decimal("1000"), estado="pagado")
            assert "2024/3" in repr(p)


class TestFactura:
    def test_crear_factura(self, app, cliente_de_prueba):
        with app.app_context():
            c = db.session.get(Cliente, cliente_de_prueba.id)
            f = Factura(
                cliente_id=c.id,
                numero="0001-00000001",
                fecha=date(2024, 6, 15),
                monto_ars=Decimal("15000.00"),
                concepto="Mensualidad junio 2024",
            )
            db.session.add(f)
            db.session.commit()

            guardada = db.session.get(Factura, f.id)
            assert guardada.numero == "0001-00000001"
            assert guardada.monto_ars == Decimal("15000.00")

            db.session.delete(guardada)
            db.session.commit()


# ─── Tests de rutas ───────────────────────────────────────────────────────────

class TestRutasCobros:
    def test_inicio_requiere_login(self, client):
        resp = client.get("/cobros/")
        assert resp.status_code in (302, 401)

    def test_inicio_autenticado(self, autenticado):
        resp = autenticado.get("/cobros/")
        assert resp.status_code == 200
        assert b"Cobros" in resp.data

    def test_lista_pagos(self, autenticado):
        resp = autenticado.get("/cobros/pagos")
        assert resp.status_code == 200

    def test_lista_facturas(self, autenticado):
        resp = autenticado.get("/cobros/facturas")
        assert resp.status_code == 200

    def test_monotributo(self, autenticado):
        resp = autenticado.get("/cobros/monotributo")
        assert resp.status_code == 200
        assert b"Monotributo" in resp.data

    def test_nuevo_pago_get(self, autenticado, app, cliente_de_prueba):
        with app.app_context():
            cid = cliente_de_prueba.id
        resp = autenticado.get(f"/cobros/clientes/{cid}/pago/nuevo")
        assert resp.status_code == 200

    def test_nuevo_plan_get(self, autenticado, app, cliente_de_prueba):
        with app.app_context():
            cid = cliente_de_prueba.id
        resp = autenticado.get(f"/cobros/clientes/{cid}/plan/nuevo")
        assert resp.status_code == 200

    def test_detalle_cliente(self, autenticado, app, cliente_de_prueba):
        with app.app_context():
            cid = cliente_de_prueba.id
        resp = autenticado.get(f"/cobros/clientes/{cid}")
        assert resp.status_code == 200

    def test_nueva_factura_get(self, autenticado):
        resp = autenticado.get("/cobros/facturas/nueva")
        assert resp.status_code == 200

    def test_nueva_factura_post(self, autenticado, app, cliente_de_prueba):
        with app.app_context():
            cid = cliente_de_prueba.id
        resp = autenticado.post("/cobros/facturas/nueva", data={
            "cliente_id": cid,
            "numero": "0001-00000099",
            "fecha": "2024-06-15",
            "monto_ars": "15000.00",
            "concepto": "Test factura",
        }, follow_redirects=True)
        assert resp.status_code == 200
        # Limpiamos la factura creada
        with app.app_context():
            f = Factura.query.filter_by(numero="0001-00000099").first()
            if f:
                db.session.delete(f)
                db.session.commit()

    def test_factura_cliente_inexistente_404(self, autenticado):
        resp = autenticado.get("/cobros/clientes/99999")
        assert resp.status_code == 404
