"""
zynnos_agente.py — Agente liviano de Zynnos para reportar actividad al panel central.

INSTALACIÓN EN CUALQUIER SISTEMA ZYNNOS:
  1. Copiar este archivo a la raíz del proyecto (junto a app.py o wsgi.py).
  2. Agregar al .env:
       ZYNNOS_PANEL_URL=https://central-gh9z.onrender.com
       ZYNNOS_AGENTE_TOKEN=<token de la instalación en el panel>
  3. Llamar a `registrar(tipo, subtipo, detalle)` en los puntos del sistema que
     quieras monitorear.

GARANTÍAS:
  - Si el panel no responde, el sistema del cliente NO se traba ni lanza excepciones.
  - Nunca envíes nombres, emails, teléfonos ni montos de terceros en `detalle`.
  - Solo datos técnicos y de uso anonimizados (tiempos, secciones, conteos, resultados).

USO BÁSICO:
  from zynnos_agente import registrar

  # En una vista de Flask:
  registrar("pagina_vista", "/dashboard", {"tiempo_ms": 340})
  registrar("accion", "exportar_informe", {"resultado": "ok"})
  registrar("ia_usada", "asistente", {"tokens": 210})
  registrar("error_ui", "/calendar", {"codigo": 500})
  registrar("login", None, {"resultado": "ok"})

MODO BATCH (para enviar varios eventos de una sola petición HTTP):
  from zynnos_agente import Agente
  with Agente.batch() as b:
      b.agregar("pagina_vista", "/calendario")
      b.agregar("pagina_vista", "/precios")
  # Al salir del `with`, se envían todos juntos.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("zynnos_agente")

# ─── Configuración desde variables de entorno ─────────────────────────────────

_PANEL_URL = None
_AGENTE_TOKEN = None


def _config():
    global _PANEL_URL, _AGENTE_TOKEN
    if _PANEL_URL is None:
        _PANEL_URL = os.environ.get("ZYNNOS_PANEL_URL", "").rstrip("/")
    if _AGENTE_TOKEN is None:
        _AGENTE_TOKEN = os.environ.get("ZYNNOS_AGENTE_TOKEN", "")
    return _PANEL_URL, _AGENTE_TOKEN


# ─── Firma HMAC ───────────────────────────────────────────────────────────────

def _firmar(eventos: list, token: str) -> str:
    payload_str = json.dumps(eventos, separators=(",", ":"), sort_keys=True)
    return hmac.new(
        token.encode(),
        payload_str.encode(),
        hashlib.sha256,
    ).hexdigest()


# ─── Envío de eventos ─────────────────────────────────────────────────────────

def _enviar(eventos: list) -> bool:
    """
    Envía la lista de eventos al panel. Devuelve True si tuvo éxito.
    No lanza excepciones en ningún caso.
    """
    panel_url, token = _config()

    if not panel_url or not token:
        logger.debug("zynnos_agente: ZYNNOS_PANEL_URL o ZYNNOS_AGENTE_TOKEN no configurados.")
        return False

    if not eventos:
        return True

    try:
        import requests as _req  # noqa: PLC0415

        firma = _firmar(eventos, token)
        body = {"firma": firma, "eventos": eventos}
        url = f"{panel_url}/actividad/ingresar/{token}"

        resp = _req.post(url, json=body, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return True
            else:
                logger.debug(f"zynnos_agente: panel rechazó eventos: {data}")
        else:
            logger.debug(f"zynnos_agente: respuesta inesperada {resp.status_code}")
    except Exception as exc:
        # Nunca dejar que un fallo del panel trabe el sistema cliente
        logger.debug(f"zynnos_agente: error al enviar (ignorado): {exc}")

    return False


def _enviar_async(eventos: list):
    """Envía en un hilo de fondo para no bloquear la request del usuario."""
    t = threading.Thread(target=_enviar, args=(eventos,), daemon=True)
    t.start()


# ─── API pública ──────────────────────────────────────────────────────────────

def registrar(
    tipo: str,
    subtipo: Optional[str] = None,
    detalle: Optional[dict] = None,
    *,
    asincrono: bool = True,
):
    """
    Registra un evento de actividad y lo envía al panel.

    Args:
        tipo:      Categoría del evento. Ej: "pagina_vista", "accion", "ia_usada",
                   "error_ui", "login". Máx 50 caracteres.
        subtipo:   Contexto específico. Ej: "/dashboard", "exportar_pdf".
                   Sin datos personales. Máx 100 caracteres.
        detalle:   Dict con metadatos técnicos opcionales.
                   NUNCA incluir nombres, emails, teléfonos, montos de clientes.
                   Ej: {"tiempo_ms": 310, "resultado": "ok", "tokens": 150}
        asincrono: Si True (default), el envío no bloquea la request del usuario.
    """
    evento = {"tipo": str(tipo)[:50]}
    if subtipo:
        evento["subtipo"] = str(subtipo)[:100]
    if detalle and isinstance(detalle, dict):
        evento["detalle"] = detalle

    if asincrono:
        _enviar_async([evento])
    else:
        _enviar([evento])


# ─── Modo batch ───────────────────────────────────────────────────────────────

@dataclass
class _BatchContext:
    _eventos: list = field(default_factory=list)

    def agregar(
        self,
        tipo: str,
        subtipo: Optional[str] = None,
        detalle: Optional[dict] = None,
    ):
        """Agrega un evento al batch sin enviarlo todavía."""
        ev = {"tipo": str(tipo)[:50]}
        if subtipo:
            ev["subtipo"] = str(subtipo)[:100]
        if detalle and isinstance(detalle, dict):
            ev["detalle"] = detalle
        self._eventos.append(ev)


class Agente:
    @staticmethod
    @contextmanager
    def batch(asincrono: bool = True):
        """
        Context manager para enviar varios eventos en una sola petición HTTP.

        Ejemplo:
            with Agente.batch() as b:
                b.agregar("pagina_vista", "/calendario")
                b.agregar("pagina_vista", "/precios")
        """
        ctx = _BatchContext()
        try:
            yield ctx
        finally:
            if ctx._eventos:
                if asincrono:
                    _enviar_async(ctx._eventos)
                else:
                    _enviar(ctx._eventos)
