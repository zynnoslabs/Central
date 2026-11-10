"""
chat/routes.py — API del widget de chat + WebSocket para La Central
"""

import os
import json
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import request, jsonify, render_template, current_app
from flask_login import login_required
from flask_socketio import emit, join_room

from app.chat import chat_bp
from app.models import db, ChatConversacion, ChatMensaje, ChatHint

# ── Groq client (lazy) ────────────────────────────────────────────────────────
_groq_client = None

def get_groq():
    global _groq_client
    if _groq_client is None:
        try:
            import groq as groq_lib
            key = os.environ.get("GROQ_API_KEY", "")
            if key:
                _groq_client = groq_lib.Groq(api_key=key)
        except ImportError:
            pass
    return _groq_client


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def send_whatsapp(text: str):
    """Envía mensaje por CallMeBot al operador."""
    key   = os.environ.get("CALLMEBOT_KEY", "")
    phone = os.environ.get("OPERATOR_PHONE", "5492392536325")
    if not key:
        return
    try:
        encoded = urllib.parse.quote(text)
        url = (f"https://api.callmebot.com/whatsapp.php"
               f"?phone={phone}&text={encoded}&apikey={key}")
        with urllib.request.urlopen(url, timeout=5):
            pass
    except Exception as e:
        current_app.logger.warning(f"[WhatsApp] {e}")


def analyze_conversation(conv_id: str):
    """Groq analiza el historial y devuelve perfil + presupuesto."""
    groq = get_groq()
    if not groq:
        return None

    msgs = ChatMensaje.query.filter_by(conversation_id=conv_id, hidden=False).order_by(ChatMensaje.id).all()
    if len(msgs) < 2:
        return None

    history = "\n".join(f"{m.role.upper()}: {m.content}" for m in msgs)

    system = """Sos el analista interno de Zynnos Labs. Analizás conversaciones de prospectos.
Devolvé SOLO un JSON con este formato exacto:
{
  "perfil": "descripción breve del cliente en 1 oración",
  "urgencia": "alta|media|baja",
  "capacidad_pago": "alta|media|baja",
  "intencion": "descripción de lo que quiere en 1 oración",
  "solucion_recomendada": "nombre de la solución Zynnos más adecuada",
  "precio_min": 150000,
  "precio_max": 300000,
  "tiempo_entrega": "X semanas",
  "probabilidad_cierre": 75,
  "notas_vendedor": "consejo específico para cerrar esta venta"
}
Los precios son en pesos argentinos. Zynnos cobra entre $80.000 y $600.000 según complejidad.
Tiempo de entrega: 1-4 semanas."""

    try:
        resp = groq.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Conversación:\n{history}"}
            ],
            temperature=0.3,
            max_tokens=400,
            response_format={"type": "json_object"}
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        current_app.logger.warning(f"[Groq analyze] {e}")
        return None


# ── Panel del operador ────────────────────────────────────────────────────────

@chat_bp.route("/")
@login_required
def panel():
    """Lista de conversaciones del chat widget."""
    convs = ChatConversacion.query.order_by(ChatConversacion.started_at.desc()).limit(50).all()
    return render_template("chat/panel.html", conversations=convs)


@chat_bp.route("/<conv_id>")
@login_required
def detalle(conv_id):
    conv = ChatConversacion.query.get_or_404(conv_id)
    msgs  = ChatMensaje.query.filter_by(conversation_id=conv_id).order_by(ChatMensaje.id).all()
    hints = ChatHint.query.filter_by(conversation_id=conv_id).order_by(ChatHint.id.desc()).all()
    return render_template("chat/detalle.html", conv=conv, messages=msgs, hints=hints)


# ── API pública (widget en el sitio) ─────────────────────────────────────────

@chat_bp.route("/api/start", methods=["POST"])
def api_start():
    data    = request.json or {}
    conv_id = secrets.token_urlsafe(12)
    source  = data.get("source_url", "")

    conv = ChatConversacion(id=conv_id, source_url=source)
    db.session.add(conv)
    db.session.commit()

    # Notificar operador
    send_whatsapp(f"🔔 *Nuevo chat Zynnos*\nDesde: {source or 'sitio web'}\nID: {conv_id}")

    # Emitir al panel en tiempo real
    from app import socketio  # import tardío para evitar circular
    socketio.emit("new_conversation", {
        "id": conv_id,
        "started_at": now_iso(),
        "source": source
    }, room="operators")

    return jsonify({"ok": True, "conv_id": conv_id})


@chat_bp.route("/api/message", methods=["POST"])
def api_message():
    data    = request.json or {}
    conv_id = data.get("conv_id")
    content = (data.get("content") or "").strip()[:1000]

    if not conv_id or not content:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400

    conv = ChatConversacion.query.get(conv_id)
    if not conv:
        return jsonify({"ok": False, "error": "Conversación no encontrada"}), 404

    # Guardar mensaje del cliente
    msg_user = ChatMensaje(conversation_id=conv_id, role="user", content=content)
    db.session.add(msg_user)
    db.session.commit()

    from app import socketio
    socketio.emit("new_message", {
        "conv_id": conv_id,
        "role": "user",
        "content": content,
        "sent_at": now_iso()
    }, room="operators")

    # Obtener historial
    history = ChatMensaje.query.filter_by(conversation_id=conv_id, hidden=False).order_by(ChatMensaje.id).all()

    # Hints del operador pendientes
    pending = ChatHint.query.filter_by(conversation_id=conv_id, applied=False).order_by(ChatHint.id).all()
    hint_text = ""
    if pending:
        hint_text = "\n\nINSTRUCCIONES ESPECIALES DEL EQUIPO (incorporalas naturalmente, el cliente NO debe saber):\n"
        hint_text += "\n".join(f"- {h.hint}" for h in pending)

    system_prompt = f"""Sos el asistente de ventas de Zynnos Labs, una agencia argentina de automatización con IA para negocios locales.
Tu objetivo: entender qué necesita el negocio del cliente y guiarlo hacia agendar una consulta con el equipo.

Reglas:
- Hacé UNA sola pregunta a la vez, nunca múltiples
- Sé amigable, natural, como un asesor humano experto
- Recopilá: nombre, tipo de negocio, principal problema operativo, si ya usó herramientas digitales
- Cuando tengas suficiente info, ofrecé un diagnóstico gratuito y pedí el teléfono/email
- Nunca menciones precios hasta que el cliente pregunte
- Hablá en español rioplatense informal (vos, tenés, etc.)
- Respuestas cortas: máximo 3 oraciones{hint_text}"""

    messages_for_groq = [{"role": "system", "content": system_prompt}]
    for m in history:
        messages_for_groq.append({"role": m.role, "content": m.content})

    ai_response = "Contame más sobre tu negocio, ¿a qué te dedicás?"
    groq = get_groq()
    if groq:
        try:
            resp = groq.chat.completions.create(
                model="llama3-8b-8192",
                messages=messages_for_groq,
                temperature=0.7,
                max_tokens=200
            )
            ai_response = resp.choices[0].message.content.strip()
        except Exception as e:
            current_app.logger.warning(f"[Groq chat] {e}")

    # Marcar hints como aplicados
    for h in pending:
        h.applied = True
    db.session.commit()

    # Guardar respuesta IA
    msg_ai = ChatMensaje(conversation_id=conv_id, role="assistant", content=ai_response)
    db.session.add(msg_ai)
    db.session.commit()

    socketio.emit("new_message", {
        "conv_id": conv_id,
        "role": "assistant",
        "content": ai_response,
        "sent_at": now_iso()
    }, room="operators")

    # Análisis cada 3 mensajes del cliente
    user_count = ChatMensaje.query.filter_by(conversation_id=conv_id, role="user").count()
    if user_count % 3 == 0:
        analysis = analyze_conversation(conv_id)
        if analysis:
            conv.budget_est = json.dumps(analysis)
            conv.profile    = analysis.get("perfil", "")
            db.session.commit()
            socketio.emit("analysis_update", {
                "conv_id": conv_id,
                "analysis": analysis
            }, room="operators")

    return jsonify({"ok": True, "response": ai_response})


@chat_bp.route("/api/update", methods=["POST"])
def api_update():
    data    = request.json or {}
    conv_id = data.get("conv_id")
    if not conv_id:
        return jsonify({"ok": False}), 400

    conv = ChatConversacion.query.get(conv_id)
    if not conv:
        return jsonify({"ok": False}), 404

    changed = False
    for field in ["nombre", "telefono", "email", "rubro"]:
        val = data.get(field)
        if val:
            setattr(conv, field, val)
            changed = True

    if changed:
        db.session.commit()
        from app import socketio
        socketio.emit("conversation_updated", {
            "conv_id": conv_id,
            **{f: getattr(conv, f) for f in ["nombre", "telefono", "email", "rubro"] if getattr(conv, f)}
        }, room="operators")

        if data.get("telefono") or data.get("nombre"):
            send_whatsapp(f"📋 *{conv.nombre or 'Cliente'}* dejó su contacto\n📱 {conv.telefono or ''}\nID: {conv_id}")

    return jsonify({"ok": True})


# ── API del operador (requiere login) ─────────────────────────────────────────

@chat_bp.route("/api/hint", methods=["POST"])
@login_required
def api_hint():
    data    = request.json or {}
    conv_id = data.get("conv_id")
    hint    = (data.get("hint") or "").strip()[:500]
    if not conv_id or not hint:
        return jsonify({"ok": False}), 400

    h = ChatHint(conversation_id=conv_id, hint=hint)
    db.session.add(h)
    db.session.commit()

    from app import socketio
    socketio.emit("hint_added", {"conv_id": conv_id, "hint": hint}, room="operators")
    return jsonify({"ok": True})


@chat_bp.route("/api/close", methods=["POST"])
@login_required
def api_close():
    conv_id = (request.json or {}).get("conv_id")
    if not conv_id:
        return jsonify({"ok": False}), 400
    conv = ChatConversacion.query.get(conv_id)
    if conv:
        conv.status = "closed"
        db.session.commit()
    from app import socketio
    socketio.emit("conversation_closed", {"conv_id": conv_id}, room="operators")
    return jsonify({"ok": True})
