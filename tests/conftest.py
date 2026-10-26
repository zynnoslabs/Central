import pytest
from app import create_app
from app.models import db as _db, Usuario, Producto
from app.config import TestConfig


@pytest.fixture(scope="session")
def app():
    """App configurada para tests con SQLite en memoria."""
    app = create_app(TestConfig)
    yield app


@pytest.fixture(scope="session")
def db(app):
    """Base de datos compartida para toda la sesión de tests."""
    with app.app_context():
        _db.create_all()
        # Crear producto de prueba
        prod = Producto(nombre="PalermitanHost", repo_github="zynnoslabs/palermitanhost")
        _db.session.add(prod)
        _db.session.commit()
        yield _db
        _db.drop_all()


@pytest.fixture
def client(app, db):
    """Cliente HTTP para pruebas de rutas."""
    with app.test_client() as c:
        yield c


@pytest.fixture
def admin_usuario(app, db):
    """Usuario admin ya creado por la app factory (via ADMIN_EMAIL/ADMIN_PASSWORD en TestConfig)."""
    with app.app_context():
        return Usuario.query.filter_by(email=TestConfig.ADMIN_EMAIL).first()


@pytest.fixture
def cliente_logueado(client, app):
    """Cliente HTTP con sesión de admin abierta."""
    with app.app_context():
        client.post("/login", data={
            "email": TestConfig.ADMIN_EMAIL,
            "password": TestConfig.ADMIN_PASSWORD,
            "csrf_token": _get_csrf(client),
        }, follow_redirects=True)
    return client


def _get_csrf(client):
    """Extrae el token CSRF de la página de login."""
    resp = client.get("/login")
    html = resp.data.decode()
    # Buscar el valor del campo hidden csrf_token
    import re
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1) if match else ""
