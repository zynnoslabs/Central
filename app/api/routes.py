"""
API externa de Zynnos — endpoints para leads desde la web pública.
Autenticación: header X-Zynnos-Token debe coincidir con LEADS_API_TOKEN en env.
"""
import os
import json
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from app.models import db, Cliente, Auditoria

api_bp = Blueprint("api", __name__)

# ─── Token de autenticación simple ───────────────────────────────────────────

def _verificar_token():
    token_esperado = os.environ.get("LEADS_API_TOKEN", "")
    token_recibido = request.headers.get("X-Zynnos-Token", "")
    if not token_esperado or token_recibido != token_esperado:
        return False
    return True


# ─── POST /api/leads/nuevo ────────────────────────────────────────────────────

@api_bp.route("/leads/nuevo", methods=["POST"])
def nuevo_lead():
    """
    Recibe un lead desde el formulario de la web de Zynnos.
    Body JSON:
      nombre       str   requerido
      email        str   requerido
      telefono     str   opcional
      rubro        str   opcional
      problema     str   descripción libre del problema
      solucion     str   solución recomendada por Groq
      diagnostico  str   texto completo del diagnóstico
    """
    if not _verificar_token():
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "Body JSON requerido"}), 400

    nombre = (data.get("nombre") or "").strip()
    email = (data.get("email") or "").strip().lower()
    telefono = (data.get("telefono") or "").strip() or None
    rubro = (data.get("rubro") or "").strip()
    problema = (data.get("problema") or "").strip()
    solucion = (data.get("solucion") or "").strip()
    diagnostico = (data.get("diagnostico") or "").strip()

    if not nombre or not email:
        return jsonify({"ok": False, "error": "nombre y email son requeridos"}), 422

    # Armar notas internas con todo el contexto
    ahora = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    notas = f"[Lead web — {ahora}]\n"
    if rubro:
        notas += f"Rubro: {rubro}\n"
    if problema:
        notas += f"Problema: {problema}\n"
    if solucion:
        notas += f"Solución recomendada: {solucion}\n"
    if diagnostico:
        notas += f"\nDiagnóstico:\n{diagnostico}"

    # Crear cliente con estado "en_prueba"
    cliente = Cliente(
        nombre=nombre,
        email=email,
        telefono=telefono,
        estado="en_prueba",
        notas=notas.strip(),
    )
    db.session.add(cliente)
    db.session.flush()

    # Registrar en auditoría (sin usuario — es acción externa)
    log = Auditoria(
        usuario_id=None,
        cliente_id=cliente.id,
        accion="lead.web",
        detalle=json.dumps({
            "rubro": rubro,
            "solucion": solucion,
        }, ensure_ascii=False),
        ip=request.remote_addr,
    )
    db.session.add(log)
    db.session.commit()

    return jsonify({
        "ok": True,
        "cliente_id": cliente.id,
        "mensaje": f"Lead «{nombre}» registrado correctamente.",
    }), 201
