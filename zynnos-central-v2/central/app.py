"""
zynnos-central — Backend principal
Flask + Flask-SocketIO + SQLite
"""

import os
import json
import sqlite3
import hashlib
import secrets
import urllib.parse
import urllib.request
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
import groq

# ── Config ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

socketio = SocketIO(app,
    cors_allowed_origins=[
        'https://zynnos-site.vercel.app',
        'https://www.zynnos.com',
        'https://zynnos.com',
        os.environ.get('CENTRAL_URL', 'http://localhost:5000'),
    ],
    async_mode='gevent'
)

GROQ_API_KEY   = os.environ.get('GROQ_API_KEY', '')
CALLMEBOT_KEY  = os.environ.get('CALLMEBOT_KEY', '')   # API key de CallMeBot
OPERATOR_PHONE = os.environ.get('OPERATOR_PHONE', '5492392536325')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'zynnos2024')
DB_PATH        = os.environ.get('DB_PATH', 'central.db')

groq_client = groq.Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ── Base de datos ───────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id          TEXT PRIMARY KEY,
            started_at  TEXT NOT NULL,
            status      TEXT DEFAULT 'active',
            rubro       TEXT,
            nombre      TEXT,
            telefono    TEXT,
            email       TEXT,
            source_url  TEXT,
            budget_est  TEXT,
            profile     TEXT,
            notes       TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            sent_at         TEXT NOT NULL,
            hidden          INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS operator_hints (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            hint            TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            applied         INTEGER DEFAULT 0
        );
        """)

init_db()

# ── Helpers ─────────────────────────────────────────────────────────────────
def now_iso():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

def send_whatsapp(text):
    """Envía mensaje por CallMeBot a WhatsApp del operador."""
    if not CALLMEBOT_KEY:
        return
    try:
        encoded = urllib.parse.quote(text)
        url = (f"https://api.callmebot.com/whatsapp.php"
               f"?phone={OPERATOR_PHONE}&text={encoded}&apikey={CALLMEBOT_KEY}")
        with urllib.request.urlopen(url, timeout=5) as r:
            pass
    except Exception as e:
        print(f"[WhatsApp] Error: {e}")

def analyze_conversation(conv_id):
    """Groq analiza la conversación y devuelve perfil + presupuesto estimado."""
    if not groq_client:
        return None
    with get_db() as db:
        msgs = db.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND hidden=0 ORDER BY id",
            (conv_id,)
        ).fetchall()
        conv = db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()

    if len(msgs) < 2:
        return None

    history = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs)

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
Tiempo de entrega: 1-4 semanas según el proyecto."""

    try:
        resp = groq_client.chat.completions.create(
            model='llama3-8b-8192',
            messages=[
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': f"Conversación:\n{history}"}
            ],
            temperature=0.3,
            max_tokens=400,
            response_format={'type': 'json_object'}
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[Groq analyze] Error: {e}")
        return None

def get_pending_hints(conv_id):
    """Obtiene instrucciones del operador no aplicadas aún."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, hint FROM operator_hints WHERE conversation_id=? AND applied=0 ORDER BY id",
            (conv_id,)
        ).fetchall()
    return rows

def mark_hints_applied(hint_ids):
    if not hint_ids:
        return
    with get_db() as db:
        db.executemany("UPDATE operator_hints SET applied=1 WHERE id=?", [(i,) for i in hint_ids])

# ── Auth ────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('operator'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        pwd = request.form.get('password', '')
        if pwd == ADMIN_PASSWORD:
            session['operator'] = True
            return redirect(url_for('central'))
        error = 'Contraseña incorrecta'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Panel Central ────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def central():
    with get_db() as db:
        convs = db.execute(
            "SELECT * FROM conversations ORDER BY started_at DESC LIMIT 50"
        ).fetchall()
    return render_template('central.html', conversations=convs)

@app.route('/conversation/<conv_id>')
@login_required
def conversation_detail(conv_id):
    with get_db() as db:
        conv = db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        msgs = db.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id",
            (conv_id,)
        ).fetchall()
        hints = db.execute(
            "SELECT * FROM operator_hints WHERE conversation_id=? ORDER BY id DESC",
            (conv_id,)
        ).fetchall()
    if not conv:
        return "Conversación no encontrada", 404
    return render_template('conversation.html', conv=conv, messages=msgs, hints=hints)

# ── API para el chat widget ──────────────────────────────────────────────────
@app.route('/api/chat/start', methods=['POST'])
def chat_start():
    """Inicia una nueva conversación."""
    data = request.json or {}
    conv_id = secrets.token_urlsafe(12)
    source  = data.get('source_url', '')

    with get_db() as db:
        db.execute(
            "INSERT INTO conversations (id, started_at, source_url) VALUES (?,?,?)",
            (conv_id, now_iso(), source)
        )

    # Notificar al operador por WhatsApp
    msg = f"🔔 *Nuevo chat Zynnos*\nDesde: {source or 'sitio web'}\nID: {conv_id}"
    send_whatsapp(msg)

    # Emitir evento al panel central
    socketio.emit('new_conversation', {'id': conv_id, 'started_at': now_iso(), 'source': source}, room='operators')

    return jsonify({'ok': True, 'conv_id': conv_id})

@app.route('/api/chat/message', methods=['POST'])
def chat_message():
    """Recibe mensaje del cliente, responde con IA."""
    data    = request.json or {}
    conv_id = data.get('conv_id')
    content = (data.get('content') or '').strip()[:1000]

    if not conv_id or not content:
        return jsonify({'ok': False, 'error': 'Faltan datos'}), 400

    # Verificar que la conversación existe
    with get_db() as db:
        conv = db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        return jsonify({'ok': False, 'error': 'Conversación no encontrada'}), 404

    # Guardar mensaje del cliente
    with get_db() as db:
        db.execute(
            "INSERT INTO messages (conversation_id, role, content, sent_at) VALUES (?,?,?,?)",
            (conv_id, 'user', content, now_iso())
        )

    # Emitir al panel central en tiempo real
    socketio.emit('new_message', {
        'conv_id': conv_id,
        'role': 'user',
        'content': content,
        'sent_at': now_iso()
    }, room='operators')

    # Obtener historial completo
    with get_db() as db:
        history = db.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND hidden=0 ORDER BY id",
            (conv_id,)
        ).fetchall()

    # Obtener instrucciones del operador pendientes
    pending_hints = get_pending_hints(conv_id)
    hint_text = ""
    if pending_hints:
        hint_text = "\n\nINSTRUCCIONES ESPECIALES DEL EQUIPO (incorporalas naturalmente, el cliente NO debe saber):\n"
        hint_text += "\n".join(f"- {h['hint']}" for h in pending_hints)

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

    messages_for_groq = [{'role': 'system', 'content': system_prompt}]
    for m in history:
        messages_for_groq.append({'role': m['role'], 'content': m['content']})

    # Llamar a Groq
    ai_response = "Contame más sobre tu negocio, ¿a qué te dedicás?"
    if groq_client:
        try:
            resp = groq_client.chat.completions.create(
                model='llama3-8b-8192',
                messages=messages_for_groq,
                temperature=0.7,
                max_tokens=200
            )
            ai_response = resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"[Groq chat] Error: {e}")

    # Marcar hints como aplicados
    if pending_hints:
        mark_hints_applied([h['id'] for h in pending_hints])

    # Guardar respuesta de la IA
    with get_db() as db:
        db.execute(
            "INSERT INTO messages (conversation_id, role, content, sent_at) VALUES (?,?,?,?)",
            (conv_id, 'assistant', ai_response, now_iso())
        )

    # Emitir respuesta al panel central
    socketio.emit('new_message', {
        'conv_id': conv_id,
        'role': 'assistant',
        'content': ai_response,
        'sent_at': now_iso()
    }, room='operators')

    # Analizar cada 3 mensajes del cliente
    with get_db() as db:
        user_msg_count = db.execute(
            "SELECT COUNT(*) as c FROM messages WHERE conversation_id=? AND role='user'",
            (conv_id,)
        ).fetchone()['c']

    if user_msg_count % 3 == 0:
        analysis = analyze_conversation(conv_id)
        if analysis:
            with get_db() as db:
                db.execute(
                    "UPDATE conversations SET budget_est=?, profile=? WHERE id=?",
                    (json.dumps(analysis), analysis.get('perfil', ''), conv_id)
                )
            socketio.emit('analysis_update', {
                'conv_id': conv_id,
                'analysis': analysis
            }, room='operators')

    return jsonify({'ok': True, 'response': ai_response})

@app.route('/api/chat/update', methods=['POST'])
def chat_update():
    """Actualiza datos del cliente (nombre, teléfono, email, rubro)."""
    data    = request.json or {}
    conv_id = data.get('conv_id')
    if not conv_id:
        return jsonify({'ok': False}), 400

    fields = {}
    for f in ['nombre', 'telefono', 'email', 'rubro']:
        if data.get(f):
            fields[f] = data[f]

    if fields:
        set_clause = ', '.join(f"{k}=?" for k in fields)
        values     = list(fields.values()) + [conv_id]
        with get_db() as db:
            db.execute(f"UPDATE conversations SET {set_clause} WHERE id=?", values)

        socketio.emit('conversation_updated', {
            'conv_id': conv_id,
            **fields
        }, room='operators')

        # Notificar WhatsApp si llega teléfono
        if 'telefono' in fields or 'nombre' in fields:
            nombre   = fields.get('nombre', 'Cliente')
            telefono = fields.get('telefono', '')
            send_whatsapp(f"📋 *{nombre}* dejó su contacto\n📱 {telefono}\nID: {conv_id}")

    return jsonify({'ok': True})

# ── API para el operador (panel central) ────────────────────────────────────
@app.route('/api/operator/hint', methods=['POST'])
@login_required
def operator_hint():
    """Operador envía instrucción para una conversación activa."""
    data    = request.json or {}
    conv_id = data.get('conv_id')
    hint    = (data.get('hint') or '').strip()[:500]

    if not conv_id or not hint:
        return jsonify({'ok': False}), 400

    with get_db() as db:
        db.execute(
            "INSERT INTO operator_hints (conversation_id, hint, created_at) VALUES (?,?,?)",
            (conv_id, hint, now_iso())
        )

    socketio.emit('hint_added', {'conv_id': conv_id, 'hint': hint}, room='operators')
    return jsonify({'ok': True})

@app.route('/api/operator/close', methods=['POST'])
@login_required
def operator_close():
    """Cierra una conversación."""
    conv_id = (request.json or {}).get('conv_id')
    if not conv_id:
        return jsonify({'ok': False}), 400
    with get_db() as db:
        db.execute("UPDATE conversations SET status='closed' WHERE id=?", (conv_id,))
    socketio.emit('conversation_closed', {'conv_id': conv_id}, room='operators')
    return jsonify({'ok': True})

# ── WebSocket ────────────────────────────────────────────────────────────────
@socketio.on('join_operators')
def on_join_operators(data):
    """Panel central se une al room de operadores."""
    join_room('operators')
    emit('joined', {'room': 'operators'})

@socketio.on('join_conversation')
def on_join_conversation(data):
    """Panel central se une al room de una conversación específica."""
    conv_id = data.get('conv_id')
    if conv_id:
        join_room(f'conv_{conv_id}')

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
