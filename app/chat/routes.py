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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detectar_etapa_venta(history: list, conv) -> dict:
    """
    ### CAMBIO AQUÍ ###
    Detecta en qué etapa del proceso de venta estamos y devuelve
    la instrucción específica para esa etapa.
    """
    user_msgs = [m.content.lower() for m in history if m.role == "user"]
    user_count = len(user_msgs)
    combined = " ".join(user_msgs)

    # Señales de cierre inminente
    señales_cierre = ["llamame", "llamá", "quiero hablar", "podemos hablar", "cuánto sale",
                      "cuanto sale", "precio", "cuánto cuesta", "cuanto cuesta",
                      "me interesa", "quiero empezar", "cómo arrancamos", "como arrancamos"]
    # Señales de dolor/problema
    señales_dolor = ["no puedo", "no tengo tiempo", "me cuesta", "problema", "difícil",
                     "pierdo", "no llego", "tarde", "manual", "todo a mano", "mucho trabajo"]

    tiene_nombre = bool(conv.nombre)
    tiene_negocio = bool(conv.rubro)
    tiene_contacto = bool(conv.telefono or conv.email)
    expreso_dolor = any(s in combined for s in señales_dolor)
    quiere_cerrar = any(s in combined for s in señales_cierre)

    if quiere_cerrar or tiene_contacto:
        return {
            "nombre": "ETAPA 5 — CIERRE",
            "instruccion": (
                "El cliente mostró interés o quiere hablar. "
                "Pedí su teléfono o email para coordinar el diagnóstico gratuito. "
                "Si ya lo tenés, confirmá que lo van a contactar pronto. "
                "Si pide llamada, usá [SOLICITUD_LLAMADA] al final de tu respuesta."
            )
        }
    elif expreso_dolor and tiene_negocio:
        return {
            "nombre": "ETAPA 3-4 — CONSECUENCIA Y SOLUCIÓN",
            "instruccion": (
                "El cliente ya describió su problema. Ayudalo a ver el costo real: "
                "preguntá cuánto tiempo le lleva por semana o cuánto le afecta. "
                "Luego, en el próximo mensaje, presentá la solución de Zynnos de forma concreta."
            )
        }
    elif tiene_nombre and tiene_negocio:
        return {
            "nombre": "ETAPA 2 — DOLOR",
            "instruccion": (
                "Ya sabés quién es y qué negocio tiene. "
                "Ahora preguntá cuál es el mayor problema o la tarea que más tiempo les consume. "
                "Sé empático, no interrogues."
            )
        }
    elif tiene_nombre or user_count >= 2:
        return {
            "nombre": "ETAPA 1-2 — RAPPORT/NEGOCIO",
            "instruccion": (
                "Conseguiste el nombre o hay interacción inicial. "
                "Preguntá sobre el tipo de negocio de forma casual si no lo sabés. "
                "Mostrá curiosidad genuina, no es un formulario."
            )
        }
    else:
        return {
            "nombre": "ETAPA 1 — PRIMER CONTACTO",
            "instruccion": (
                "Primer mensaje del visitante. Saludá calurosamente, presentate como Matías de Zynnos, "
                "y preguntá el nombre de forma natural. Nada de ventas todavía."
            )
        }


def _resumir_datos_conocidos(conv, history_text: str) -> str:
    """
    ### CAMBIO AQUÍ ###
    Genera un resumen de lo que ya se sabe del visitante para inyectarlo
    en el system prompt del agente conversacional.
    """
    lineas = []

    # Datos guardados en la BD (ya confirmados)
    if conv.nombre:
        lineas.append(f"- Nombre: {conv.nombre}")
    if conv.rubro:
        lineas.append(f"- Rubro/negocio: {conv.rubro}")
    if conv.telefono:
        lineas.append(f"- Teléfono: {conv.telefono}")
    if conv.email:
        lineas.append(f"- Email: {conv.email}")

    # Extraer datos mencionados en la conversación aunque no estén en BD
    # Búsqueda simple por patrones en el historial del cliente
    user_lines = [l.replace("USER: ", "").strip()
                  for l in history_text.split("\n") if l.startswith("USER:")]
    combined = " ".join(user_lines).lower()

    if not conv.nombre and any(w in combined for w in ["me llamo", "soy ", "mi nombre"]):
        lineas.append("- Nombre: (mencionado en la conversación, extraer)")
    if not conv.rubro and any(w in combined for w in ["negocio", "empresa", "local", "tienda", "trabajo"]):
        lineas.append("- Negocio: (mencionado en la conversación, extraer)")

    # Info del budget_est si existe
    if conv.budget_est:
        try:
            plan = json.loads(conv.budget_est)
            if plan.get("problema_detectado") and not any("Problema" in l for l in lineas):
                lineas.append(f"- Problema detectado: {plan['problema_detectado']}")
            if plan.get("perfil_cliente") and not any("Perfil" in l for l in lineas):
                lineas.append(f"- Perfil: {plan['perfil_cliente']}")
        except Exception:
            pass

    if not lineas:
        return "- Ninguno todavía (primer contacto o conversación muy inicial)"

    return "\n".join(lineas)


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
        # ### CAMBIO AQUÍ ### — modelo más capaz + prompt más preciso + actualizar BD
        try:
            extraccion = groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": (
                        "Analizá esta conversación de ventas y extraé la información del CLIENTE (mensajes USER).\n"
                        "Respondé SOLO JSON con este formato exacto:\n"
                        "{\n"
                        '  "nombre_persona": "nombre y apellido si se mencionó, si no null",\n'
                        '  "nombre_negocio": "nombre del negocio si se mencionó, si no null",\n'
                        '  "rubro": "tipo de negocio en 2-3 palabras (ej: veterinaria, restaurante, estudio contable), si no null",\n'
                        '  "ciudad": "ciudad o provincia si se mencionó, si no null",\n'
                        '  "telefono": "número de teléfono con código de área si se mencionó, si no null",\n'
                        '  "email": "email si se mencionó, si no null",\n'
                        '  "problema_principal": "problema o necesidad principal en 1 oración, si no null",\n'
                        '  "herramientas_actuales": "qué herramientas/sistemas usan hoy, si no null",\n'
                        '  "empleados": "cantidad aproximada de empleados si se mencionó, si no null"\n'
                        "}\n"
                        "IMPORTANTE: extraé solo lo que el cliente dijo explícitamente. No inventes ni inferras."
                    )},
                    {"role": "user", "content": f"Conversación:\n{history_text}"}
                ],
                temperature=0.0,
                max_tokens=300,
                response_format={"type": "json_object"}
            )
            entidades = json.loads(extraccion.choices[0].message.content)

            # ### CAMBIO AQUÍ ### — actualizar BD con datos extraídos si no los teníamos
            conv_upd = ChatConversacion.query.get(conv_id)
            if conv_upd:
                changed = False
                if entidades.get("nombre_persona") and not conv_upd.nombre:
                    conv_upd.nombre = entidades["nombre_persona"]
                    changed = True
                if entidades.get("rubro") and not conv_upd.rubro:
                    conv_upd.rubro = entidades["rubro"]
                    changed = True
                if entidades.get("telefono") and not conv_upd.telefono:
                    conv_upd.telefono = entidades["telefono"]
                    changed = True
                if entidades.get("email") and not conv_upd.email:
                    conv_upd.email = entidades["email"]
                    changed = True
                if changed:
                    db.session.commit()
                    socketio.emit("conversation_updated", {
                        "conv_id": conv_id,
                        **{f: getattr(conv_upd, f) for f in ["nombre", "telefono", "email", "rubro"]
                           if getattr(conv_upd, f)}
                    }, room="operators")

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
        # ### CAMBIO AQUÍ ### — modelo 70b + más campos de contexto
        if msg_count % 2 == 0:
            contexto_web = ""
            if busquedas.get("negocio"):
                snippets = " | ".join(r["snippet"] for r in busquedas["negocio"]["resultados"][:2])
                contexto_web = f"\nInfo web del negocio: {snippets[:400]}"

            # Incluir datos estructurados extraídos en el contexto
            contexto_entidades = ""
            if entidades:
                campos = {
                    "nombre_persona": "Cliente",
                    "nombre_negocio": "Negocio",
                    "rubro": "Rubro",
                    "ciudad": "Ciudad",
                    "problema_principal": "Problema",
                    "herramientas_actuales": "Herramientas actuales",
                    "empleados": "Empleados"
                }
                items = [f"{label}: {entidades[k]}" for k, label in campos.items() if entidades.get(k)]
                if items:
                    contexto_entidades = "\nDatos del cliente:\n" + "\n".join(f"  - {i}" for i in items)

            try:
                plan_resp = groq.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": (
                            "Sos el estratega de ventas de Zynnos Labs, agencia de automatización IA para pymes argentinas.\n"
                            "Basándote en la conversación, los datos del cliente y la info web, armá un plan de propuesta.\n"
                            "Respondé SOLO JSON:\n"
                            "{\n"
                            '  "perfil_cliente": "descripción del perfil en 1 oración",\n'
                            '  "problema_detectado": "problema principal identificado con precisión",\n'
                            '  "solucion_recomendada": "solución Zynnos más adecuada (chatbot IA / automatización WhatsApp / sistema de cobros / etc.)",\n'
                            '  "argumentos_venta": ["argumento específico para este cliente", "arg2", "arg3"],\n'
                            '  "precio_sugerido_min": 150000,\n'
                            '  "precio_sugerido_max": 350000,\n'
                            '  "tiempo_entrega": "X semanas",\n'
                            '  "probabilidad_cierre": 65,\n'
                            '  "proximo_paso": "acción concreta a tomar ahora con este cliente",\n'
                            '  "senales_compra": ["señal específica observada en la conversación"],\n'
                            '  "objeciones_posibles": ["objeción probable basada en el perfil"],\n'
                            '  "info_faltante": ["dato que aún no tenemos y sería útil para la propuesta"]\n'
                            "}\n"
                            "Precios en pesos argentinos. Zynnos cobra $80.000–$600.000 según complejidad.\n"
                            "Soluciones disponibles: chatbot IA para atención, automatización de WhatsApp, "
                            "sistema de cobros/recordatorios, CRM simple, automatización de redes sociales."
                            + contexto_entidades
                            + contexto_web
                        )},
                        {"role": "user", "content": f"Conversación:\n{history_text}"}
                    ],
                    temperature=0.3,
                    max_tokens=600,
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

    # ### CAMBIO AQUÍ ### — inyectar datos ya recopilados + etapa de venta detectada
    datos_conocidos = _resumir_datos_conocidos(conv, history_text)
    etapa_venta = _detectar_etapa_venta(history, conv)

    system_prompt = (
        "Sos Matías, asesor de Zynnos Labs — agencia argentina que automatiza pymes con IA.\n"
        "Hablás como alguien real: amigable, directo, sin exagerar. Nunca sonás a robot ni a vendedor de telemarketing.\n\n"

        "DATOS YA CONOCIDOS DEL VISITANTE:\n"
        + datos_conocidos + "\n\n"

        "ETAPA ACTUAL DE LA CONVERSACIÓN: " + etapa_venta["nombre"] + "\n"
        "Instrucción para esta etapa: " + etapa_venta["instruccion"] + "\n\n"

        "FLUJO GENERAL (seguí el orden, no saltés etapas):\n"
        "ETAPA 1 — RAPPORT: Saludá, preguntá el nombre y el negocio de forma natural. No vendas nada todavía.\n"
        "ETAPA 2 — DOLOR: Preguntá qué tarea les consume más tiempo o qué problema se repite. "
        "Cuando el cliente lo describa, reflejalo con empatía ('eso suena agotador', 'eso les debe costar plata'). "
        "Hacé que sienta que entendés su problema.\n"
        "ETAPA 3 — CONSECUENCIA: Ayudalo a ver el costo real del problema. "
        "Preguntá algo como '¿Y eso cuánto tiempo te lleva por semana?' o '¿Cuántos clientes perdés por eso?'. "
        "El objetivo es que EL MISMO diga que necesita resolverlo.\n"
        "ETAPA 4 — SOLUCIÓN: Recién acá mencionás que Zynnos puede ayudarlos. "
        "Sé específico: 'Para un negocio como el tuyo, nosotros armamos un sistema que [X]'. "
        "No des precios, ofrecé el diagnóstico gratuito.\n"
        "ETAPA 5 — CIERRE: Pedí el teléfono o email para coordinar la llamada de diagnóstico. "
        "Si pide hablar por teléfono ahora, respondé: 'Perfecto, te llama nuestro equipo a la brevedad. "
        "¿Cuál es tu número?' y usá la etiqueta [SOLICITUD_LLAMADA] al final de tu respuesta.\n\n"

        "REGLAS IRROMPIBLES:\n"
        "- UNA sola pregunta por mensaje\n"
        "- Si ya sabés un dato, no lo volvás a preguntar\n"
        "- Nunca des precios hasta que pregunten; si preguntan, decí 'depende del proyecto, pero arrancamos desde $80.000'\n"
        "- Español rioplatense informal (vos, tenés, hacés)\n"
        "- Máximo 3 oraciones por respuesta\n"
        "- Si el cliente dice que quiere hablar, está apurado, o dice 'llamame', activá etapa 5 directo\n"
        "- Nunca uses signos de exclamación múltiples ni emojis de más de 1 por mensaje"
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
        for modelo in [   ### CAMBIO AQUÍ ### modelos activos Groq nov-2026
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
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

    # ### CAMBIO AQUÍ ### — detectar solicitud de llamada en la respuesta IA
    solicito_llamada = "[SOLICITUD_LLAMADA]" in ai_response
    ai_response_limpia = ai_response.replace("[SOLICITUD_LLAMADA]", "").strip()

    # Marcar hints como aplicados
    for h in pending:
        h.applied = True
    db.session.commit()

    # Guardar respuesta IA (sin la etiqueta interna)
    msg_ai = ChatMensaje(conversation_id=conv_id, role="assistant", content=ai_response_limpia)
    db.session.add(msg_ai)
    db.session.commit()

    socketio.emit("new_message", {
        "conv_id": conv_id,
        "role": "assistant",
        "content": ai_response_limpia,
        "sent_at": now_iso()
    }, room="operators")

    # ### CAMBIO AQUÍ ### — alerta de llamada al operador (SocketIO + WhatsApp)
    if solicito_llamada:
        nombre_cliente = conv.nombre or "el visitante"
        tel_cliente = conv.telefono or "(sin teléfono aún)"
        socketio.emit("call_requested", {
            "conv_id": conv_id,
            "nombre": nombre_cliente,
            "telefono": tel_cliente,
            "source": conv.source_url or "sitio web"
        }, room="operators")
        send_whatsapp(
            f"📞 *Solicitud de llamada*\n"
            f"Cliente: {nombre_cliente}\n"
            f"Teléfono: {tel_cliente}\n"
            f"Desde: {conv.source_url or 'sitio web'}\n"
            f"ID: {conv_id}"
        )

    return jsonify({"ok": True, "response": ai_response_limpia, "call_requested": solicito_llamada})


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
            modelos = ["llama-3.3-70b-versatile","llama-3.1-8b-instant","openai/gpt-oss-20b","openai/gpt-oss-120b"]
            result["modelos_probados"] = {}
            for m in modelos:
                try:
                    r = client.chat.completions.create(model=m, messages=[{"role":"user","content":"di OK"}], max_tokens=5)
                    result["modelos_probados"][m] = "OK: " + r.choices[0].message.content
                    if not result.get("test_ok"):
                        result["test_ok"] = True
                        result["modelo_activo"] = m
                except Exception as e:
                    result["modelos_probados"][m] = f"FALLO: {type(e).__name__}: {str(e)[:150]}"
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
