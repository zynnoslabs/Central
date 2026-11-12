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
import re
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
from app.models import db, ChatConversacion, ChatMensaje, ChatHint, ChatLead, ChatKB

# ── CORS para la API pública del widget ──────────────────────────────────────  ### CAMBIO AQUÍ ###
_CORS_ORIGINS_EXACT = {
    "https://zynnos.com",
    "https://www.zynnos.com",
    "https://zynnos-site.vercel.app",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
}

def _cors_allowed(origin: str) -> bool:
    """### CAMBIO AQUÍ ### — acepta dominios exactos + cualquier preview de Vercel."""
    if origin in _CORS_ORIGINS_EXACT:
        return True
    # Vercel genera URLs de preview únicas por deploy, ej: zynnos-site-abc123-org.vercel.app
    if origin.startswith("https://") and origin.endswith(".vercel.app"):
        return True
    return False

@chat_bp.after_request
def _add_cors(response):
    origin = request.headers.get("Origin", "")
    if _cors_allowed(origin):
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
    if _cors_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "3600"
    return resp

# ### CAMBIO AQUÍ ### Endpoint liviano para despertar Render sin crear conversación
@chat_bp.route("/api/ping", methods=["GET", "POST"])
def ping():
    """Ping liviano para warm-up del servidor (no crea conversaciones)."""
    origin = request.headers.get("Origin", "")
    resp = make_response(jsonify({"ok": True, "pong": True}), 200)
    if _cors_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
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

# ── KB — base de conocimiento editable ───────────────────────────────────────

_KB_DEFAULTS = [
    {"clave": "empresa",       "titulo": "Quiénes somos",        "orden": 1, "contenido":
        "Zynnos Labs es una agencia argentina de automatización con IA para pymes. "
        "Ayudamos a negocios locales a ahorrar tiempo y vender más usando tecnología simple y práctica."},
    {"clave": "servicios",     "titulo": "Servicios",             "orden": 2, "contenido":
        "1. Chatbot IA para atención al cliente (WhatsApp o web) — responde 24/7, califica leads\n"
        "2. Automatización de WhatsApp — seguimientos, recordatorios, confirmaciones automáticas\n"
        "3. Sistema de cobros y recordatorios — avisa a clientes con cuotas vencidas\n"
        "4. CRM simple — organiza contactos, seguimiento de ventas\n"
        "5. Automatización de redes sociales — programación y respuestas automáticas"},
    {"clave": "casos_exito",   "titulo": "Casos de éxito",        "orden": 3, "contenido":
        "- Veterinaria en Palermo: automatizamos los turnos por WhatsApp, bajaron un 80% los llamados al mostrador\n"
        "- Estudio contable en Rosario: sistema de cobros automático, recuperaron $400.000 en cuotas atrasadas en 2 meses\n"
        "- Gimnasio en CABA: chatbot que responde consultas y agenda turnos, el dueño dejó de responder WhatsApp de noche"},
    {"clave": "buen_cliente",  "titulo": "Perfil de buen cliente", "orden": 4, "contenido":
        "- Pyme de 1 a 20 personas\n"
        "- Tiene al menos un proceso repetitivo que hace a mano (responder mensajes, cobrar, agendar)\n"
        "- Usa WhatsApp para comunicarse con clientes\n"
        "- El dueño toma las decisiones directamente\n"
        "- Tiene presupuesto de $80.000 a $600.000 pesos"},
    {"clave": "objeciones",    "titulo": "Cómo manejar objeciones", "orden": 5, "contenido":
        "- 'Es caro': 'Entiendo. ¿Cuánto te cuesta por mes el tiempo que dedicás a hacer eso a mano?'\n"
        "- 'No sé si lo necesito': 'Hagamos el diagnóstico gratuito y vemos juntos si tiene sentido'\n"
        "- 'Ya lo intenté con otro': 'Qué pasó? Muchos vienen después de malas experiencias, nosotros trabajamos diferente'\n"
        "- 'No tengo tiempo para implementarlo': 'Nosotros nos encargamos de todo, vos no tenés que hacer nada técnico'"},
    {"clave": "objetivo_chat", "titulo": "Objetivo del chat",      "orden": 6, "contenido":
        "Conseguir que el visitante deje sus datos de contacto (teléfono o email) para coordinar un diagnóstico gratuito. "
        "El diagnóstico es sin costo y sin compromiso, podemos coordinar por WhatsApp o como le quede mejor al cliente. "
        "No cerrar precios por chat — siempre derivar al diagnóstico."},
    {"clave": "tono",          "titulo": "Tono y estilo",          "orden": 7, "contenido":
        "Amigable, directo, sin tecnicismos. Español rioplatense informal. "
        "El asesor se llama Matías. Nunca sonar a robot ni a telemarketing. "
        "Máximo 3 oraciones por mensaje. Una sola pregunta a la vez."},
    {"clave": "precios", "titulo": "Estructura de precios", "orden": 8, "contenido":
        "PRECIOS EN USD — referencia interna para orientar al operador. NUNCA dar precios exactos al cliente por chat.\n\n"
        "=== PRODUCTOS INDIVIDUALES ===\n"
        "- Sitio web completo (tienda, restaurante, barbería, etc.) con panel admin + legales: $500 USD · mant. $50/mes\n"
        "- Sistema de gestión a medida (CRM, reservas, stock, facturación, ARCA): $800–1.200 USD · mant. $80/mes\n"
        "- Agente IA de ventas (chat con embudo, extracción de leads, alertas): $600–900 USD · mant. $70/mes\n"
        "- Asistente financiero (WhatsApp/Telegram, gastos por foto, reportes, PDFs): $400–600 USD · mant. $40/mes\n"
        "- Gestión estratégica Airbnb (precios dinámicos, análisis competencia, aprendizaje): $700–1.000 USD · mant. $80/mes\n"
        "- Trading automatizado con IA (análisis redes + exchanges, operaciones automáticas): $1.500–2.500 USD · mant. $150/mes\n"
        "- App móvil Android (chat, emergencias, mapas, calendario, herramientas): $1.200–2.000 USD · mant. $100/mes\n"
        "- Monitoreo y seguridad en tiempo real (ubicación, vuelos, sismos, cámaras): $800–1.500 USD · mant. $100/mes\n\n"
        "=== COMBOS FIJOS ===\n"
        "- Negocio Digital (Sitio web + Agente IA de ventas): $950 USD · mant. $100/mes\n"
        "- Negocio Completo (Sitio + Agente IA + Sistema de gestión): $1.700 USD · mant. $170/mes\n"
        "- Combo Finanzas (Asistente financiero + Sistema de gestión): $1.200 USD · mant. $110/mes\n"
        "- Airbnb Pro (Gestión Airbnb + Agente IA para huéspedes): $1.200 USD · mant. $130/mes\n\n"
        "=== LÓGICA PARA COMBOS PERSONALIZADOS ===\n"
        "Si el cliente necesita una combinación no listada: sumar los precios individuales (tomar el valor medio)\n"
        "y aplicar 15–20% de descuento. La mensualidad siempre se suma completa sin descuento.\n"
        "Ejemplo: Sitio ($500) + Finanzas ($500) + Airbnb ($850) = $1.850 → combo $1.480 USD · mant. $170/mes\n\n"
        "=== FORMA DE COBRO ===\n"
        "- 50% antes de empezar (reserva el proyecto)\n"
        "- 50% al entregar el sistema funcionando\n"
        "- Mensualidad arranca el mes siguiente a la entrega\n"
        "- Moneda: USD de referencia, cobro en ARS al tipo de cambio del día\n\n"
        "=== USO EN EL CHAT ===\n"
        "Esta información es SOLO para orientar al operador en el resumen del lead y para que la IA\n"
        "entienda el rango de inversión. NUNCA citar precios exactos al cliente. Si pregunta el precio,\n"
        "responder: 'Depende de lo que necesitás exactamente — en el diagnóstico armamos la propuesta a medida.'\n"
        "Si insiste, se puede decir que los proyectos arrancan desde los $400 USD sin dar más detalle."},
    # ### CAMBIO AQUÍ ### — Nuevas secciones KB
    {"clave": "infraestructura", "titulo": "Infraestructura y tecnología", "orden": 9, "contenido":
        "STACK TÉCNICO DE ZYNNOS:\n"
        "- Backend: Python + Flask (APIs REST, lógica de negocio, integraciones)\n"
        "- Base de datos: PostgreSQL (Neon) para producción, SQLite para desarrollo\n"
        "- Frontend: HTML + CSS + JS + HTMX (sin frameworks pesados, carga rápida)\n"
        "- IA: Anthropic Claude (API oficial) y OpenAI GPT según el caso\n"
        "- Deploy: Render.com (plan gratuito o Starter $7/mes)\n"
        "- Repositorios: GitHub privado por cliente\n"
        "- WhatsApp: API oficial de Meta (no Twilio ni terceros)\n"
        "- Emails: SMTP vía Gmail (zynnos.labs@gmail.com) o SendGrid\n"
        "- Monitoreo: Sentry para errores en producción\n\n"
        "MODELO DE INFRAESTRUCTURA POR CLIENTE:\n"
        "- Un repositorio GitHub privado por cliente\n"
        "- Un servicio Render por cliente (en cuenta Zynnos)\n"
        "- Un proyecto Neon PostgreSQL por cliente\n"
        "- El cliente no necesita cuentas propias ni conocimiento técnico\n"
        "- Zynnos mantiene el control técnico completo\n\n"
        "POR QUÉ ESTA ARQUITECTURA:\n"
        "- Flask es liviano, fácil de mantener y suficiente para el 95% de los casos\n"
        "- Render ofrece deploy automático desde GitHub, SSL gratis y escalado simple\n"
        "- Neon es PostgreSQL serverless con tier gratuito generoso\n"
        "- HTMX evita la complejidad de React/Vue para interfaces de gestión interna\n"
        "- Todo el stack puede ser mantenido por una sola persona sin DevOps especializado"},
    {"clave": "no_hacemos", "titulo": "Lo que Zynnos NO hace", "orden": 10, "contenido":
        "SERVICIOS QUE ZYNNOS NO OFRECE:\n\n"
        "1. DISEÑO GRÁFICO INDEPENDIENTE\n"
        "   - No hacemos logos, branding, flyers ni piezas gráficas sueltas sin desarrollo asociado\n"
        "   - El diseño que incluimos es siempre parte de un sistema web o app\n\n"
        "2. DESARROLLO MOBILE NATIVO\n"
        "   - No desarrollamos apps para App Store ni Google Play en este momento\n"
        "   - Lo que ofrecemos son web apps (funcionan desde el navegador del celular)\n\n"
        "3. SISTEMAS ERP O CONTABLES COMPLEJOS\n"
        "   - No reemplazamos sistemas contables, facturación AFIP integrada o ERPs empresariales\n"
        "   - Para facturación electrónica: derivamos a soluciones específicas (Colppy, Xubio, etc.)\n\n"
        "4. SOPORTE 24/7 O SLA GARANTIZADO\n"
        "   - No ofrecemos soporte de guardia ni tiempos de respuesta contractuales menores a 24hs\n"
        "   - El mantenimiento incluye corrección de bugs y actualizaciones menores, no soporte operativo\n\n"
        "5. PROYECTOS SIN PRESUPUESTO MÍNIMO\n"
        "   - No tomamos proyectos por menos de $150 USD\n"
        "   - No hacemos 'pequeños cambios' o 'arreglos rápidos' sin evaluar el alcance\n\n"
        "6. TRABAJO PARA COMPETENCIA DIRECTA\n"
        "   - No tomamos dos clientes del mismo rubro y zona geográfica simultáneamente\n\n"
        "7. PROYECTOS SIN CLARIDAD DE REQUERIMIENTOS\n"
        "   - No empezamos a desarrollar sin una propuesta formal aprobada\n"
        "   - 'Algo parecido a X pero diferente' no es un requerimiento válido para cotizar\n\n"
        "CÓMO MANEJAR ESTO EN EL CHAT:\n"
        "Si el cliente pide algo de esta lista, responder: "
        "'Eso no es algo que hagamos directamente, pero en el diagnóstico podemos ver "
        "si hay una solución dentro de lo que sí hacemos o te derivamos a quien corresponde.'"},
    {"clave": "limites_legales", "titulo": "Límites legales — OBLIGATORIO respetar", "orden": 11, "contenido":
        "REGLAS QUE NUNCA SE PUEDEN VIOLAR EN EL CHAT:\n"
        "1. PRECIOS: Si el cliente pregunta precio POR PRIMERA VEZ, responder: 'El precio depende de cada proyecto, "
        "lo definimos juntos en el diagnóstico gratuito.' "
        "Si el cliente INSISTE en precio (segunda vez o más), dá un rango orientativo CLARO: "
        "'Un proyecto como el tuyo generalmente arranca en el orden de los $400–$900 USD según complejidad.' "
        "Nunca repitas la misma respuesta evasiva dos veces seguidas ante la misma pregunta.\n"
        "2. NUNCA prometer plazos de entrega concretos (ni '2 semanas', ni 'un mes', ni ningún número). "
        "Si preguntan cuánto tarda: 'Los tiempos dependen del alcance, lo definimos en la propuesta.'\n"
        "3. NUNCA comprometer funcionalidades específicas ('eso lo podemos hacer', 'ese sistema incluye X'). "
        "Usar siempre: 'Eso es algo que evaluamos en detalle en el diagnóstico.'\n"
        "4. NUNCA garantizar resultados ('vas a vender más', 'vas a ahorrar X horas'). "
        "Hablar siempre de posibilidades y casos anteriores, no de garantías.\n"
        "5. NUNCA decir que algo 'está incluido' o que algo 'no tiene costo adicional'.\n"
        "6. TODO lo que se acuerde, cotice o especifique vale SOLO cuando está en una propuesta formal firmada. "
        "El chat es informativo, no contractual. Si el cliente insiste en cerrar algo por chat, "
        "decirle: 'Con gusto avanzamos, pero los detalles finales siempre los cerramos por propuesta escrita.'\n"
        "IMPORTANTE PARA EL RESUMEN AL OPERADOR: cuando generes el resumen del cliente, "
        "recordar al operador humano que NINGUNA promesa, precio o plazo mencionado en este chat "
        "tiene validez contractual. Todo debe confirmarse en la propuesta formal."},
]


def _get_kb() -> dict:
    """
    ### CAMBIO AQUÍ ###
    Carga la KB desde la BD. Si una clave no existe, la crea con el valor default.
    Retorna dict {clave: contenido}.
    """
    existentes = {r.clave: r for r in ChatKB.query.all()}
    result = {}
    for d in _KB_DEFAULTS:
        if d["clave"] not in existentes:
            # sembrar el default
            row = ChatKB(clave=d["clave"], titulo=d["titulo"],
                         orden=d["orden"], contenido=d["contenido"])
            db.session.add(row)
            result[d["clave"]] = d["contenido"]
        else:
            result[d["clave"]] = existentes[d["clave"]].contenido
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return result


def _kb_to_system_context(kb: dict) -> str:
    """Convierte la KB en texto para el system prompt."""
    return (
        f"=== QUIÉNES SOMOS ===\n{kb.get('empresa','')}\n\n"
        f"=== SERVICIOS QUE OFRECEMOS ===\n{kb.get('servicios','')}\n\n"
        f"=== CASOS DE ÉXITO REALES ===\n{kb.get('casos_exito','')}\n\n"
        f"=== PERFIL DE BUEN CLIENTE ===\n{kb.get('buen_cliente','')}\n\n"
        f"=== CÓMO MANEJAR OBJECIONES ===\n{kb.get('objeciones','')}\n\n"
        f"=== OBJETIVO DEL CHAT ===\n{kb.get('objetivo_chat','')}\n\n"
        f"=== TONO Y ESTILO ===\n{kb.get('tono','')}\n\n"
        f"=== ESTRUCTURA DE PRECIOS (uso interno — no citar al cliente) ===\n{kb.get('precios','')}\n\n"
        f"=== ⚠️ LÍMITES LEGALES — CUMPLIMIENTO OBLIGATORIO ===\n{kb.get('limites_legales','')}"
    )


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
    señales_cierre = ["llamame", "llamá", "quiero hablar", "podemos hablar",
                      "quiero empezar", "cómo arrancamos", "como arrancamos",
                      "manden info", "mandame info", "dejame el contacto", "quiero que me contacten"]
    # Señales de dolor/problema — solo palabras que CLARAMENTE indican un problema real
    # ### CAMBIO AQUÍ ### — quitadas palabras genéricas que disparaban Etapa 2 en el primer mensaje
    señales_dolor = ["no puedo", "no tengo tiempo", "me cuesta", "problema", "difícil",
                     "pierdo tiempo", "no llego", "todo a mano", "mucho trabajo",
                     "cobros atrasados", "cobrar cuotas", "turno por whatsapp",
                     "respondo mensajes", "agenda manual", "se me acumula", "se acumula",
                     "no doy abasto", "no llego a", "me lleva horas"]

    tiene_nombre = bool(conv.nombre)
    tiene_negocio = bool(conv.rubro)
    # ### CAMBIO AQUÍ ### — también detectar teléfono en el último mensaje del historial
    ultimo_user_msg = user_msgs[-1] if user_msgs else ""
    telefono_en_msg = bool(re.search(r'\b\d[\d\s\-]{6,}\d\b', ultimo_user_msg))
    tiene_contacto = bool(conv.telefono or conv.email or telefono_en_msg)
    expreso_dolor = any(s in combined for s in señales_dolor)
    quiere_cerrar = any(s in combined for s in señales_cierre)

    # ### CAMBIO AQUÍ ### — detectar si el cliente preguntó precio 2+ veces (insistencia)
    palabras_precio = ["precio", "cuánto cuesta", "cuanto cuesta", "cuánto sale", "cuanto sale",
                       "cuánto cobran", "cuanto cobran", "cuánto es", "cuanto es",
                       "planes", "tarifas", "valores", "presupuesto"]
    veces_pregunto_precio = sum(
        1 for m in user_msgs
        if any(p in m for p in palabras_precio)
    )

    if veces_pregunto_precio >= 2:
        return {
            "nombre": "ETAPA 5 — PRECIO INSISTENTE",
            "instruccion": (
                "El cliente ya preguntó el precio más de una vez. "
                "Dá un rango orientativo CLARO y sin rodeos: "
                "'Un sistema como el que describís generalmente arranca en el orden de los $400–$900 USD, "
                "dependiendo de los módulos y complejidad. Después del diagnóstico gratuito armamos la propuesta exacta.' "
                "NO volvás a decir solo 'depende del diagnóstico' sin dar el rango — eso ya lo dijiste y el cliente quiere números. "
                "Después del rango, podés invitar al diagnóstico para afinar el número, pero el rango es obligatorio."
            )
        }

    # ### CAMBIO AQUÍ ### — Etapa 5 solo si YA hubo conversación real (mínimo 3 mensajes del cliente)
    # o si ya tiene contacto guardado. Evita saltar al cierre en el primer mensaje.
    cierre_prematuro = quiere_cerrar and user_count < 3
    if (quiere_cerrar and not cierre_prematuro) or tiene_contacto:
        return {
            "nombre": "ETAPA 5 — CIERRE",
            "instruccion": (
                "El cliente mostró interés real tras una conversación. "
                "Pedí su número de teléfono o email para coordinar el diagnóstico gratuito. "
                "Cuando el cliente ya dio su número o email: agradecele, hacé un resumen breve de lo que pidió o comentó, y usá [SOLICITUD_CONTACTO] al final. "
                "NO digas que lo van a contactar pronto ni pongas tiempos — solo agradecé y resumí."
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

def _verificar_objetivo(app, conv_id: str, history_text: str, msg_count: int):
    """
    ### CAMBIO AQUÍ ###
    Corre cada 3 mensajes del cliente. Evalúa si la IA está cumpliendo el objetivo
    de venta o se desvió. Emite 'coach_alert' al panel si detecta un problema.
    """
    with app.app_context():
        groq = get_groq()
        if not groq:
            return
        from app import socketio

        # Cargar objetivo desde KB
        kb_objetivo = ""
        try:
            kb_obj = ChatKB.query.filter_by(clave="objetivo_chat").first()
            if kb_obj:
                kb_objetivo = kb_obj.contenido
        except Exception:
            pass

        try:
            resp = groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": (
                        "Sos el supervisor de ventas de Zynnos Labs. Analizás conversaciones del chatbot.\n"
                        "Objetivo del chat: " + (kb_objetivo or "Que el visitante deje su contacto para coordinar el diagnóstico gratuito.") + "\n\n"
                        "Evaluá la conversación y respondé SOLO JSON:\n"
                        "{\n"
                        '  "en_camino": true/false,\n'
                        '  "etapa_actual": "1-5 o descripción breve",\n'
                        '  "problema_detectado": "null o descripción del desvío",\n'
                        '  "accion_sugerida": "qué debería hacer la IA ahora para retomar el camino",\n'
                        '  "nivel_alerta": "ok|advertencia|critico"\n'
                        "}\n"
                        "nivel_alerta:\n"
                        "- ok: la IA va por buen camino\n"
                        "- advertencia: se está tardando demasiado en avanzar de etapa\n"
                        "- critico: la IA se desvió del objetivo, está dando info innecesaria, o el cliente se está yendo"
                    )},
                    {"role": "user", "content": f"Conversación (mensaje {msg_count}):\n{history_text}"}
                ],
                temperature=0.1,
                max_tokens=250,
                response_format={"type": "json_object"}
            )
            evaluacion = json.loads(resp.choices[0].message.content)

            # Solo emitir si hay algo relevante para el operador
            if evaluacion.get("nivel_alerta") in ("advertencia", "critico"):
                socketio.emit("coach_alert", {
                    "conv_id": conv_id,
                    "evaluacion": evaluacion,
                    "msg_count": msg_count
                }, room="operators")

        except Exception as e:
            current_app.logger.warning(f"[Verificador] {e}")


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
    # Usar socketio.start_background_task en vez de threading.Thread
    # porque con async_mode="gevent" los threads normales no pueden emitir por websocket
    from app import socketio as _sio
    app = current_app._get_current_object()
    _sio.start_background_task(_run_analysis, app, conv_id, history_text, content, user_count)

    # ── Verificador de objetivo (cada 3 mensajes del cliente) ──────────────
    if user_count % 3 == 0 and user_count > 0:
        _sio.start_background_task(_verificar_objetivo, app, conv_id, history_text, user_count)

    # ── Hints del operador pendientes ────────────────────────────────────────
    pending = ChatHint.query.filter_by(conversation_id=conv_id, applied=False).order_by(ChatHint.id).all()
    hint_text = ""
    if pending:
        hint_text = "\n\nINSTRUCCIONES ESPECIALES DEL EQUIPO (incorporalas naturalmente, son prioritarias):\n"
        hint_text += "\n".join(f"- {h.hint}" for h in pending)

    # ### CAMBIO AQUÍ ### — cargar KB + datos del visitante + etapa de venta
    kb = _get_kb()
    kb_context = _kb_to_system_context(kb)
    datos_conocidos = _resumir_datos_conocidos(conv, history_text)
    etapa_venta = _detectar_etapa_venta(history, conv)

    system_prompt = (
        "Sos Matías, asesor de ventas de Zynnos Labs.\n"
        "Hablás como una persona real: amigable, directo, sin exagerar. Nunca sonás a robot ni a telemarketing.\n\n"

        "BASE DE CONOCIMIENTO DE ZYNNOS:\n"
        + kb_context + "\n\n"

        "DATOS YA CONOCIDOS DEL VISITANTE:\n"
        + datos_conocidos + "\n\n"

        "ETAPA ACTUAL: " + etapa_venta["nombre"] + "\n"
        "Instrucción para esta etapa: " + etapa_venta["instruccion"] + "\n\n"

        "FLUJO DE VENTA (seguí el orden, no saltés etapas):\n"
        "ETAPA 1 — RAPPORT: Saludá, preguntá el nombre y el negocio. Nada de ventas todavía.\n"
        "ETAPA 2 — DOLOR: Preguntá qué tarea les consume más tiempo. Cuando lo describa, reflejalo con empatía.\n"
        "ETAPA 3 — CONSECUENCIA: Ayudalo a ver el costo real. '¿Y eso cuánto tiempo te lleva por semana?'\n"
        "ETAPA 4 — SOLUCIÓN: Presentá la solución concreta de Zynnos para ese problema. Ofrecé el diagnóstico.\n"
        "ETAPA 5 — CIERRE: Pedí número o email. Cuando lo dé, agradecele y hacé un resumen de lo que pidió. Usá [SOLICITUD_CONTACTO]. No digas que lo contactarán pronto ni pongas tiempos.\n\n"

        "LO QUE ZYNNOS NO HACE (CRÍTICO — nunca prometás esto):\n"
        "- NO desarrollamos apps nativas para App Store ni Play Store\n"
        "- NO hacemos logos, diseño gráfico ni branding suelto\n"
        "- NO reemplazamos sistemas contables ni ERPs\n"
        "- NO ofrecemos soporte 24/7 ni SLA garantizado\n"
        "- Si el cliente pide algo de esta lista, decí brevemente que no lo hacemos directamente "
        "y reencauzá hacia lo que SÍ podemos hacer (web app, automatización, chatbot, etc.).\n\n"

        "REGLAS ESTRICTAS:\n"
        "- UNA SOLA PREGUNTA por mensaje. Si querés preguntar dos cosas, elegí la más importante.\n"
        "- Máximo 2-3 oraciones en total. Corto y directo.\n"
        "- Si ya sabés un dato, no lo volvás a preguntar. JAMÁS repetir una pregunta que el cliente ya respondió.\n"
        "- Si el cliente ya respondió qué problema tiene, NO volvás a preguntar qué problema tiene. Avanzá a la siguiente etapa.\n"
        "- Nunca des precios hasta que pregunten\n"
        "- Español rioplatense informal, nada de mayúsculas ni exclamaciones exageradas\n"
        "- Si el cliente dice 'llamame' o quiere hablar, activá etapa 5 directo\n"
        "- NUNCA hagas más de una pregunta en el mismo mensaje, aunque sean distintos temas\n"
        "- Si el cliente ya mencionó su problema o necesidad, tu próximo mensaje debe avanzar (empatizar o presentar solución), NO volver a preguntar lo mismo\n"
        "- Si rechazás algo (pedido ilegal, fuera de scope), siempre reencauzá: decí qué SÍ podemos hacer en 1 oración\n"
        "- CRÍTICO: NO pidas número de teléfono, WhatsApp ni email hasta llegar a ETAPA 5. Seguí el flujo en orden.\n"
        "- CRÍTICO: NUNCA digas 'en instantes te contactamos' ni 'te vamos a llamar' ni similares. Solo cuando el cliente ya dio su contacto, agradecé y resumí lo que pidió."
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
                    max_tokens=500  # ### CAMBIO AQUÍ ### subido para evitar cortes
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
    solicito_llamada = bool(re.search(r'\[\s*SOLICITUD_CONTACTO\s*\]', ai_response))  # ### CAMBIO AQUÍ ### regex por si el AI agrega espacios
    ai_response_limpia = re.sub(r'\[\s*SOLICITUD_CONTACTO\s*\]', '', ai_response).strip()

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
            f"📬 *Solicitud de contacto*\n"
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


# ── Base de conocimiento — panel de edición ──────────────────────────────────

@chat_bp.route("/kb")
@login_required
def kb_panel():
    """Vista de edición de la base de conocimiento del agente."""
    with current_app.app_context():
        kb_rows = _get_kb()  # siembra defaults si no existen
    secciones = ChatKB.query.order_by(ChatKB.orden).all()
    return render_template("chat/kb.html", secciones=secciones)


@chat_bp.route("/api/kb/save", methods=["POST"])
@login_required
def api_kb_save():
    """Guarda una sección de la KB."""  ### CAMBIO AQUÍ ###
    data     = request.json or {}
    clave    = data.get("clave", "").strip()
    contenido = data.get("contenido", "").strip()

    if not clave:
        return jsonify({"ok": False, "error": "Falta clave"}), 400

    row = ChatKB.query.filter_by(clave=clave).first()
    if row:
        row.contenido = contenido
    else:
        # clave nueva (el operador puede agregar secciones custom)
        titulo = data.get("titulo", clave).strip()
        orden  = data.get("orden", 99)
        row = ChatKB(clave=clave, titulo=titulo, contenido=contenido, orden=orden)
        db.session.add(row)

    db.session.commit()
    return jsonify({"ok": True})
