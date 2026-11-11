"""
chat/routes.py — API del widget de chat + análisis inteligente + WebSocket para La Central

Por cada mensaje del visitante se disparan en paralelo (threads):
  1. Respuesta conversacional de Groq (visible al visitante)
  2. Análisis estilométrico del texto acumulado
  3. Búsqueda web de nombre/negocio/rubro detectados
  4. Plan de propuesta incremental (se actualiza cada 2 mensajes)

Todo llega al panel Central via SocketIO en tiempo real.
"""

import os
import json
import secrets
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import request, jsonify, render_template, current_app, make_response
from flask_login import login_required
from flask_socketio import emit, join_room

from app.chat import chat_bp
from app.models import db, ChatConversacion, ChatMensaje, ChatHint, ChatLead

# ── CORS para la API pública del widget ──────────────────────────────────────  ### CAMBIO AQUÍ ###
_CORS_ORIGINS = {
    "https://zynnos.com",
    "https://www.zynnos.com",
    "https://zynnos-site.vercel.app",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
}

@chat_bp.after_request
def _add_cors(response):
    origin = request.headers.get("Origin", "")
    if origin in _CORS_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Vary"] = "Origin"
    return response

@chat_bp.route("/api/<path:_>", methods=["OPTIONS"])
def _cors_preflight(_):
    """Responde preflight OPTIONS para cualquier ruta /chat/api/*."""
    origin = request.headers.get("Origin", "")
    resp = make_response("", 204)
    if origin in _CORS_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "3600"
    return resp

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


# ── Búsqueda web DDG ─────────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 3) -> list[dict]:
    """
    Búsqueda web liviana usando DuckDuckGo Instant Answer API.
    Retorna lista de {title, url, snippet}.
    """
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Zynnos-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())

        results = []
        # AbstractText (Wikipedia-style)
        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading", query),
                "url": data.get("AbstractURL", ""),
                "snippet": data["AbstractText"][:300]
            })
        # RelatedTopics
        for t in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(t, dict) and t.get("Text") and t.get("FirstURL"):
                results.append({
                    "title": t.get("Text", "")[:80],
                    "url": t["FirstURL"],
                    "snippet": t["Text"][:200]
                })
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        current_app.logger.warning(f"[WebSearch] {e}")
        return []


# ── Análisis paralelo ─────────────────────────────────────────────────────────

def _run_analysis(app, conv_id: str, history_text: str, user_msg: str, msg_count: int):
    """
    Se ejecuta en un thread separado. Hace:
      1. Extrae entidades (nombre, negocio, rubro) del historial
      2. Busca en web nombre + negocio/rubro
      3. Análisis estilométrico
      4. Plan de propuesta (cada 2 mensajes)
    Emite todo por SocketIO al panel.
    """
    with app.app_context():
        groq = get_groq()
        if not groq:
            return

        from app import socketio

        # ── Paso 1: extraer entidades ────────────────────────────────────────
        try:
            extraccion = groq.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": (
                        "Extraé del texto las entidades mencionadas. "
                        "Respondé SOLO JSON: "
                        '{"nombre_persona": "...", "nombre_negocio": "...", "rubro": "...", "ciudad": "..."} '
                        "Si no se menciona algo, usá null."
                    )},
                    {"role": "user", "content": history_text}
                ],
                temperature=0.1,
                max_tokens=150,
                response_format={"type": "json_object"}
            )
            entidades = json.loads(extraccion.choices[0].message.content)
        except Exception as e:
            current_app.logger.warning(f"[Análisis entidades] {e}")
            entidades = {}

        # ── Paso 2: búsqueda web ─────────────────────────────────────────────
        busquedas = {}
        queries = []
        if entidades.get("nombre_persona") and entidades.get("nombre_negocio"):
            queries.append((
                "persona",
                f'{entidades["nombre_persona"]} {entidades.get("ciudad","Argentina")}'
            ))
        if entidades.get("nombre_negocio"):
            queries.append((
                "negocio",
                f'{entidades["nombre_negocio"]} {entidades.get("ciudad","Argentina")}'
            ))
        if entidades.get("rubro"):
            queries.append((
                "rubro",
                f'automatización {entidades["rubro"]} Argentina tendencias 2024'
            ))

        for key, q in queries:
            resultados = web_search(q, max_results=3)
            if resultados:
                busquedas[key] = {"query": q, "resultados": resultados}

        # Emitir búsqueda apenas termina
        if busquedas:
            socketio.emit("web_research", {
                "conv_id": conv_id,
                "entidades": entidades,
                "busquedas": busquedas
            }, room="operators")

        # ── Paso 3: estilometría ─────────────────────────────────────────────
        try:
            estilo_resp = groq.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": (
                        "Analizá el estilo de escritura del CLIENTE (mensajes USER) del chat. "
                        "Respondé SOLO JSON con este formato exacto:\n"
                        "{\n"
                        '  "nivel_educativo": "alto|medio|bajo",\n'
                        '  "perfil": "dueño_pyme|empleado|emprendedor|profesional|otro",\n'
                        '  "tono": "formal|informal|técnico|emotivo",\n'
                        '  "urgencia_percibida": "alta|media|baja",\n'
                        '  "capacidad_decision": "alta|media|baja",\n'
                        '  "resumen_estilo": "1 oración sobre cómo se comunica"\n'
                        "}"
                    )},
                    {"role": "user", "content": f"Historial del chat:\n{history_text}"}
                ],
                temperature=0.2,
                max_tokens=200,
                response_format={"type": "json_object"}
            )
            estilo = json.loads(estilo_resp.choices[0].message.content)
        except Exception as e:
            current_app.logger.warning(f"[Estilometría] {e}")
            estilo = {}

        if estilo:
            socketio.emit("stilometry_update", {
                "conv_id": conv_id,
                "estilo": estilo
            }, room="operators")

        # ── Paso 4: plan de propuesta (cada 2 mensajes) ──────────────────────
        if msg_count % 2 == 0:
            contexto_web = ""
            if busquedas.get("negocio"):
                snippets = " | ".join(r["snippet"] for r in busquedas["negocio"]["resultados"][:2])
                contexto_web = f"\nInfo web del negocio: {snippets[:400]}"

            try:
                plan_resp = groq.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[
                        {"role": "system", "content": (
                            "Sos el estratega de ventas de Zynnos Labs, agencia de automatización IA para pymes argentinas.\n"
                            "Basándote en la conversación y la info web disponible, armá un plan de propuesta.\n"
                            "Respondé SOLO JSON:\n"
                            "{\n"
                            '  "perfil_cliente": "descripción breve en 1 oración",\n'
                            '  "problema_detectado": "problema principal identificado",\n'
                            '  "solucion_recomendada": "nombre de la solución Zynnos más adecuada",\n'
                            '  "argumentos_venta": ["arg1", "arg2", "arg3"],\n'
                            '  "precio_sugerido_min": 150000,\n'
                            '  "precio_sugerido_max": 350000,\n'
                            '  "tiempo_entrega": "2-3 semanas",\n'
                            '  "probabilidad_cierre": 65,\n'
                            '  "proximo_paso": "qué preguntarle o decirle ahora",\n'
                            '  "senales_compra": ["señal1"],\n'
                            '  "objeciones_posibles": ["objeción1"]\n'
                            "}\n"
                            "Precios en pesos argentinos. Zynnos cobra $80.000–$600.000 según complejidad."
                            + contexto_web
                        )},
                        {"role": "user", "content": f"Conversación:\n{history_text}"}
                    ],
                    temperature=0.3,
                    max_tokens=500,
                    response_format={"type": "json_object"}
                )
                plan = json.loads(plan_resp.choices[0].message.content)

                # Guardar plan en la conversación
                conv = ChatConversacion.query.get(conv_id)
                if conv:
                    conv.budget_est = json.dumps(plan)
                    conv.profile = plan.get("perfil_cliente", "")
                    db.session.commit()

                socketio.emit("proposal_update", {
                    "conv_id": conv_id,
                    "plan": plan
                }, room="operators")

            except Exception as e:
                current_app.logger.warning(f"[Plan propuesta] {e}")


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
    conv  = ChatConversacion.query.get_or_404(conv_id)
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

    send_whatsapp(f"🔔 *Nuevo chat Zynnos*\nDesde: {source or 'sitio web'}\nID: {conv_id}")

    from app import socketio
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

    # Obtener historial completo
    history = ChatMensaje.query.filter_by(conversation_id=conv_id, hidden=False).order_by(ChatMensaje.id).all()
    history_text = "\n".join(f"{m.role.upper()}: {m.content}" for m in history)
    user_count = sum(1 for m in history if m.role == "user")

    # ── Análisis paralelo (no bloquea la respuesta al visitante) ──────────── ### CAMBIO AQUÍ ###
    app = current_app._get_current_object()
    t = threading.Thread(
        target=_run_analysis,
        args=(app, conv_id, history_text, content, user_count),
        daemon=True
    )
    t.start()

    # ── Hints del operador pendientes ────────────────────────────────────────
    pending = ChatHint.query.filter_by(conversation_id=conv_id, applied=False).order_by(ChatHint.id).all()
    hint_text = ""
    if pending:
        hint_text = "\n\nINSTRUCCIONES ESPECIALES DEL EQUIPO (incorporalas naturalmente):\n"
        hint_text += "\n".join(f"- {h.hint}" for h in pending)

    system_prompt = (
        "Sos el asesor de Zynnos Labs, agencia argentina de automatización con IA para negocios locales.\n"
        "Tu objetivo: entender qué necesita el negocio del cliente y guiarlo a agendar una consulta.\n\n"
        "Reglas:\n"
        "- Hacé UNA sola pregunta a la vez\n"
        "- Sé amigable, natural, como un asesor humano\n"
        "- Recopilá: nombre, tipo de negocio, principal problema, si ya usa herramientas digitales\n"
        "- Cuando tengas suficiente info, ofrecé un diagnóstico gratuito y pedí el teléfono/email\n"
        "- Nunca menciones precios hasta que pregunten\n"
        "- Hablá en español rioplatense informal (vos, tenés, etc.)\n"
        "- Respuestas cortas: máximo 3 oraciones"
        + hint_text
    )

    # ### CAMBIO AQUÍ ### — solo los últimos 10 mensajes para evitar tokens excesivos
    history_trimmed = history[-10:] if len(history) > 10 else history
    messages_for_groq = [{"role": "system", "content": system_prompt}]
    for m in history_trimmed:
        messages_for_groq.append({"role": m.role, "content": m.content})

    ai_response = None
    groq = get_groq()
    print(f"[Groq] cliente={'OK' if groq else 'NONE'} mensajes={len(messages_for_groq)}", flush=True)
    if not groq:
        current_app.logger.error("[Groq] GROQ_API_KEY no configurada en Render")
        print("[Groq] ERROR: GROQ_API_KEY no configurada", flush=True)
    else:
        for modelo in [   ### CAMBIO AQUÍ ### lista amplia de modelos Groq activos
            "gemma2-9b-it",
            "llama-3.3-70b-versatile",
            "llama-3.1-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
        ]:
            try:
                resp = groq.chat.completions.create(
                    model=modelo,
                    messages=messages_for_groq,
                    temperature=0.7,
                    max_tokens=200
                )
                ai_response = resp.choices[0].message.content.strip()
                print(f"[Groq] OK con {modelo}: {ai_response[:80]}", flush=True)
                break
            except Exception as e:
                print(f"[Groq] FALLO {modelo}: {type(e).__name__}: {e}", flush=True)
                current_app.logger.error(f"[Groq {modelo}] {type(e).__name__}: {e}")

    if not ai_response:
        ai_response = "Gracias por la info. ¿Querés que te prepare un diagnóstico gratuito de lo que podríamos automatizar en tu negocio?"

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


@chat_bp.route("/leads")
@login_required
def leads():
    """Vista de leads capturados en modo fallback."""
    all_leads = ChatLead.query.order_by(ChatLead.creado_en.desc()).all()
    return render_template("chat/leads.html", leads=all_leads)


@chat_bp.route("/api/lead/contactado", methods=["POST"])
@login_required
def api_lead_contactado():
    """Marca un lead como contactado."""
    lead_id = (request.json or {}).get("id")
    lead = ChatLead.query.get(lead_id)
    if not lead:
        return jsonify({"ok": False}), 404
    lead.contactado = not lead.contactado  # toggle
    db.session.commit()
    return jsonify({"ok": True, "contactado": lead.contactado})


@chat_bp.route("/api/diagnostico")
def api_diagnostico():
    """Endpoint temporal para verificar estado de Groq."""  ### CAMBIO AQUÍ ###
    key = os.environ.get("GROQ_API_KEY", "")
    result = {
        "key_presente": bool(key),
        "key_prefix": key[:8] + "..." if len(key) > 8 else "(vacia)",
    }
    try:
        import groq as groq_lib
        result["groq_importado"] = True
        if key:
            client = groq_lib.Groq(api_key=key)
            modelos = ["gemma2-9b-it","llama-3.3-70b-versatile","llama-3.1-70b-versatile","llama-3.1-8b-instant","mixtral-8x7b-32768"]
            result["modelos_probados"] = {}
            for m in modelos:
                try:
                    r = client.chat.completions.create(model=m, messages=[{"role":"user","content":"di OK"}], max_tokens=5)
                    result["modelos_probados"][m] = "OK: " + r.choices[0].message.content
                    if not result.get("test_ok"):
                        result["test_ok"] = True
                        result["modelo_activo"] = m
                except Exception as e:
                    result["modelos_probados"][m] = f"FALLO: {type(e).__name__}"
        else:
            result["test_ok"] = False
            result["test_error"] = "key vacía"
    except Exception as e:
        result["test_ok"] = False
        result["test_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return jsonify(result)


@chat_bp.route("/api/lead", methods=["POST"])       # ### CAMBIO AQUÍ ###
def api_lead():
    """Guarda un lead capturado en modo fallback del widget."""
    data     = request.get_json(silent=True) or {}
    nombre   = (data.get("nombre") or "").strip()
    whatsapp = (data.get("whatsapp") or "").strip()
    pagina   = (data.get("pagina") or "")[:500]

    if not nombre or not whatsapp:
        return jsonify({"ok": False, "error": "nombre y whatsapp requeridos"}), 400

    lead = ChatLead(nombre=nombre, whatsapp=whatsapp, pagina=pagina)
    db.session.add(lead)
    db.session.commit()

    # Notificar a los operadores en tiempo real
    try:
        from app import socketio
        socketio.emit("new_lead", {
            "id": lead.id,
            "nombre": nombre,
            "whatsapp": whatsapp,
            "creado_en": lead.creado_en.isoformat()
        }, room="operators")
    except Exception:
        pass

    return jsonify({"ok": True})
