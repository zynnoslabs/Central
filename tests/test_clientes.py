"""Pruebas del módulo de clientes."""
import pytest
from app.models import Cliente


def test_lista_clientes_requiere_login(client):
    """La lista de clientes redirige al login si no hay sesión."""
    resp = client.get("/clientes/", follow_redirects=False)
    assert resp.status_code == 302


def test_crear_cliente(cliente_logueado, app):
    """Se puede crear un cliente correctamente."""
    from tests.conftest import _get_csrf
    csrf = _get_csrf(cliente_logueado)
    resp = cliente_logueado.post("/clientes/nuevo", data={
        "nombre": "Adriano Pérez",
        "email": "adriano@test.com",
        "telefono": "1112345678",
        "estado": "activo",
        "notas": "",
        "csrf_token": csrf,
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        c = Cliente.query.filter_by(email="adriano@test.com").first()
        assert c is not None
        assert c.nombre == "Adriano Pérez"


def test_detalle_cliente(cliente_logueado, app):
    """Se puede ver el detalle de un cliente existente."""
    with app.app_context():
        c = Cliente.query.filter_by(email="adriano@test.com").first()
        if c:
            resp = cliente_logueado.get(f"/clientes/{c.id}")
            assert resp.status_code == 200
            assert "Adriano Pérez".encode() in resp.data


def test_cliente_no_existente_da_404(cliente_logueado):
    """Un ID de cliente inexistente devuelve 404."""
    resp = cliente_logueado.get("/clientes/99999")
    assert resp.status_code == 404
