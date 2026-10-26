"""Pruebas de login, logout y protección de rutas."""
import pytest
from app.config import TestConfig


def test_login_pagina_ok(client):
    """La página de login carga sin errores."""
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"Zynnos Central" in resp.data


def test_login_exitoso(client, app):
    """Login con credenciales correctas redirige al tablero."""
    from tests.conftest import _get_csrf
    csrf = _get_csrf(client)
    resp = client.post("/login", data={
        "email": TestConfig.ADMIN_EMAIL,
        "password": TestConfig.ADMIN_PASSWORD,
        "csrf_token": csrf,
    }, follow_redirects=True)
    assert resp.status_code == 200
    # Después del login debería ver el tablero
    assert b"Tablero" in resp.data or b"tablero" in resp.data.lower()


def test_login_password_incorrecta(client):
    """Login con contraseña incorrecta devuelve 401."""
    from tests.conftest import _get_csrf
    csrf = _get_csrf(client)
    resp = client.post("/login", data={
        "email": TestConfig.ADMIN_EMAIL,
        "password": "contraseña_incorrecta",
        "csrf_token": csrf,
    })
    assert resp.status_code == 401


def test_ruta_protegida_sin_login(client):
    """Acceder al tablero sin login redirige al login."""
    resp = client.get("/tablero", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.location


def test_salud_endpoint_publico(client):
    """El endpoint /salud es público y devuelve JSON con ok:true."""
    resp = client.get("/salud")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True


def test_cron_sin_token(client):
    """El endpoint de cron rechaza peticiones sin token."""
    resp = client.get("/cron/chequear-salud")
    assert resp.status_code == 401


def test_cron_token_incorrecto(client):
    """El endpoint de cron rechaza tokens incorrectos."""
    resp = client.get("/cron/chequear-salud?token=token-incorrecto")
    assert resp.status_code == 401


def test_cron_token_correcto(client):
    """El endpoint de cron acepta el token correcto."""
    resp = client.get(
        "/cron/chequear-salud",
        headers={"X-Cron-Token": TestConfig.CRON_TOKEN}
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "chequeadas" in data
