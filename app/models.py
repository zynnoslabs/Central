from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
import bcrypt

db = SQLAlchemy()


def ahora():
    return datetime.now(timezone.utc)


# ─── USUARIO DEL PANEL ────────────────────────────────────────────────────────

class Usuario(UserMixin, db.Model):
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    es_admin = db.Column(db.Boolean, default=True)
    intentos_fallidos = db.Column(db.Integer, default=0)
    bloqueado_hasta = db.Column(db.DateTime(timezone=True), nullable=True)
    creado_en = db.Column(db.DateTime(timezone=True), default=ahora)
    ultimo_acceso = db.Column(db.DateTime(timezone=True), nullable=True)
    alertas_email = db.Column(db.Boolean, default=True, nullable=False, server_default="true")  ### CAMBIO AQUÍ ###

    def set_password(self, password: str):
        self.password_hash = bcrypt.hashpw(
            password.encode(), bcrypt.gensalt()
        ).decode()

    def check_password(self, password: str) -> bool:
        return bcrypt.checkpw(
            password.encode(), self.password_hash.encode()
        )

    def esta_bloqueado(self) -> bool:
        if self.bloqueado_hasta is None:
            return False
        return ahora() < self.bloqueado_hasta


# ─── CLIENTES ─────────────────────────────────────────────────────────────────

class Cliente(db.Model):
    __tablename__ = "clientes"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    telefono = db.Column(db.String(50), nullable=True)
    estado = db.Column(
        db.String(20),
        nullable=False,
        default="activo"
        # valores posibles: activo, en_prueba, suspendido, baja
    )
    notas = db.Column(db.Text, nullable=True)
    creado_en = db.Column(db.DateTime(timezone=True), default=ahora)

    instalaciones = db.relationship(
        "Instalacion", back_populates="cliente", cascade="all, delete-orphan"
    )
    auditoria = db.relationship("Auditoria", back_populates="cliente")
    ### CAMBIO AQUÍ ### — relaciones nuevas de cobros
    planes = db.relationship("PlanCliente", back_populates="cliente", cascade="all, delete-orphan")
    pagos = db.relationship("Pago", back_populates="cliente", cascade="all, delete-orphan")
    facturas = db.relationship("Factura", back_populates="cliente", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Cliente {self.nombre}>"


# ─── PRODUCTOS ────────────────────────────────────────────────────────────────

class Producto(db.Model):
    __tablename__ = "productos"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)       # ej: PalermitanHost
    descripcion = db.Column(db.Text, nullable=True)
    repo_github = db.Column(db.String(255), nullable=True)   # ej: zynnoslabs/palermitanhost
    activo = db.Column(db.Boolean, default=True)
    creado_en = db.Column(db.DateTime(timezone=True), default=ahora)

    instalaciones = db.relationship("Instalacion", back_populates="producto")

    def __repr__(self):
        return f"<Producto {self.nombre}>"


# ─── INSTALACIONES ────────────────────────────────────────────────────────────

class Instalacion(db.Model):
    __tablename__ = "instalaciones"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey("productos.id"), nullable=False)

    # Identificación
    nombre = db.Column(db.String(255), nullable=False)       # ej: PalermitanHost - Adriano
    url_sistema = db.Column(db.String(500), nullable=False)  # URL pública en Render
    url_salud = db.Column(db.String(500), nullable=False)    # ej: https://xxx.onrender.com/salud

    # Servicios externos (opcionales por ahora)
    render_service_id = db.Column(db.String(100), nullable=True)
    neon_project_id = db.Column(db.String(100), nullable=True)
    sentry_project = db.Column(db.String(100), nullable=True)

    ### CAMBIO AQUÍ ### — token único para autenticar eventos del agente de actividad
    agente_token = db.Column(db.String(64), nullable=True)

    # Estado
    activa = db.Column(db.Boolean, default=True)
    creado_en = db.Column(db.DateTime(timezone=True), default=ahora)

    ### CAMBIO AQUÍ ### — costo mensual de esta instalación (Render, etc.) en USD
    costo_usd = db.Column(db.Numeric(10, 2), nullable=True, default=0)

    cliente = db.relationship("Cliente", back_populates="instalaciones")
    producto = db.relationship("Producto", back_populates="instalaciones")
    eventos_salud = db.relationship(
        "EventoSalud", back_populates="instalacion", cascade="all, delete-orphan"
    )
    ### CAMBIO AQUÍ ### — eventos de actividad del agente
    eventos_actividad = db.relationship(
        "EventoActividad", back_populates="instalacion", cascade="all, delete-orphan"
    )

    def ultimo_estado(self):
        """Devuelve el EventoSalud más reciente."""
        return (
            EventoSalud.query
            .filter_by(instalacion_id=self.id)
            .order_by(EventoSalud.chequeado_en.desc())
            .first()
        )

    def __repr__(self):
        return f"<Instalacion {self.nombre}>"


# ─── EVENTOS DE SALUD (chequeos de /salud) ────────────────────────────────────

class EventoSalud(db.Model):
    __tablename__ = "eventos_salud"

    id = db.Column(db.Integer, primary_key=True)
    instalacion_id = db.Column(db.Integer, db.ForeignKey("instalaciones.id"), nullable=False)
    ok = db.Column(db.Boolean, nullable=False)               # True = respondió bien
    tiempo_ms = db.Column(db.Integer, nullable=True)         # tiempo de respuesta en ms
    codigo_http = db.Column(db.Integer, nullable=True)       # 200, 500, timeout, etc.
    detalle = db.Column(db.String(500), nullable=True)       # mensaje de error si falló
    chequeado_en = db.Column(db.DateTime(timezone=True), default=ahora)

    instalacion = db.relationship("Instalacion", back_populates="eventos_salud")


# ─── EVENTOS DE ACTIVIDAD (del agente en cada instalación) ────────────────────

### CAMBIO AQUÍ ### — tabla nueva para recibir actividad anónima desde los sistemas
class EventoActividad(db.Model):
    """
    Registra acciones técnicas y de uso que el agente de cada instalación reporta.
    NUNCA contiene datos personales de terceros (sin nombres, emails, teléfonos).
    Solo eventos técnicos: tipo de acción, sección usada, conteo, estado.
    """
    __tablename__ = "eventos_actividad"

    id = db.Column(db.Integer, primary_key=True)
    instalacion_id = db.Column(db.Integer, db.ForeignKey("instalaciones.id"), nullable=False)

    # Tipo de evento (ej: "pagina_vista", "accion", "error_ui", "ia_usada", "login")
    tipo = db.Column(db.String(50), nullable=False)

    # Subtipo o contexto (ej: "/dashboard", "exportar_informe", "GPT-completions")
    subtipo = db.Column(db.String(100), nullable=True)

    # Metadatos técnicos opcionales — JSON sin datos personales
    # Ej: {"tiempo_ms": 320, "resultado": "ok", "tokens_usados": 150}
    detalle = db.Column(db.Text, nullable=True)

    recibido_en = db.Column(db.DateTime(timezone=True), default=ahora)

    instalacion = db.relationship("Instalacion", back_populates="eventos_actividad")

    def __repr__(self):
        return f"<EventoActividad {self.tipo} inst={self.instalacion_id}>"


# ─── AUDITORÍA ────────────────────────────────────────────────────────────────

class Auditoria(db.Model):
    __tablename__ = "auditoria"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"), nullable=True)
    accion = db.Column(db.String(100), nullable=False)       # ej: "cliente.crear"
    detalle = db.Column(db.Text, nullable=True)              # JSON con los datos relevantes
    ip = db.Column(db.String(45), nullable=True)
    registrado_en = db.Column(db.DateTime(timezone=True), default=ahora)

    usuario = db.relationship("Usuario")
    cliente = db.relationship("Cliente", back_populates="auditoria")


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO DE COBROS — ETAPA 2
# ═══════════════════════════════════════════════════════════════════════════════

### CAMBIO AQUÍ ### — Plan de mensualidad de un cliente

class PlanCliente(db.Model):
    """Registra el plan vigente de un cliente: producto, monto en ARS y período."""
    __tablename__ = "planes_clientes"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"), nullable=False)

    # Descripción del plan
    descripcion = db.Column(db.String(255), nullable=False)  # ej: "PalermitanHost Plan Base"
    monto_ars = db.Column(db.Numeric(12, 2), nullable=False) # monto mensual en pesos
    dia_vencimiento = db.Column(db.Integer, default=10)      # día del mes que vence el pago

    # Vigencia
    vigente_desde = db.Column(db.Date, nullable=False)
    vigente_hasta = db.Column(db.Date, nullable=True)        # None = activo actualmente
    activo = db.Column(db.Boolean, default=True)

    creado_en = db.Column(db.DateTime(timezone=True), default=ahora)

    cliente = db.relationship("Cliente", back_populates="planes")
    pagos = db.relationship("Pago", back_populates="plan")

    def __repr__(self):
        return f"<PlanCliente {self.descripcion} ${self.monto_ars}>"


### CAMBIO AQUÍ ### — Registro de un pago mensual

class Pago(db.Model):
    """Un pago mensual de un cliente. Un registro por mes por cliente."""
    __tablename__ = "pagos"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("planes_clientes.id"), nullable=True)

    # Período que cubre este pago
    anio = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)              # 1–12

    # Montos
    monto_esperado = db.Column(db.Numeric(12, 2), nullable=False)
    monto_recibido = db.Column(db.Numeric(12, 2), nullable=True)

    # Estado: pendiente / pagado / atrasado / perdonado
    estado = db.Column(db.String(20), nullable=False, default="pendiente")

    fecha_pago = db.Column(db.Date, nullable=True)           # cuándo pagó realmente
    notas = db.Column(db.Text, nullable=True)

    creado_en = db.Column(db.DateTime(timezone=True), default=ahora)
    actualizado_en = db.Column(db.DateTime(timezone=True), default=ahora, onupdate=ahora)

    cliente = db.relationship("Cliente", back_populates="pagos")
    plan = db.relationship("PlanCliente", back_populates="pagos")

    __table_args__ = (
        db.UniqueConstraint("cliente_id", "anio", "mes", name="uq_pago_cliente_mes"),
    )

    def __repr__(self):
        return f"<Pago {self.cliente_id} {self.anio}/{self.mes} {self.estado}>"


### CAMBIO AQUÍ ### — Factura emitida (registrada a mano, no generada)

class Factura(db.Model):
    """Factura C emitida en ARCA. Solo se registra, no se genera aquí."""
    __tablename__ = "facturas"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"), nullable=False)

    numero = db.Column(db.String(20), nullable=False)        # ej: "0001-00000042"
    fecha = db.Column(db.Date, nullable=False)
    monto_ars = db.Column(db.Numeric(12, 2), nullable=False)
    concepto = db.Column(db.String(500), nullable=True)      # descripción libre
    notas = db.Column(db.Text, nullable=True)

    creado_en = db.Column(db.DateTime(timezone=True), default=ahora)

    cliente = db.relationship("Cliente", back_populates="facturas")

    def __repr__(self):
        return f"<Factura {self.numero} ${self.monto_ars}>"


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO DE AVISOS — ETAPA 5
# ═══════════════════════════════════════════════════════════════════════════════

### CAMBIO AQUÍ ### — Aviso interno con seguimiento
class Aviso(db.Model):
    """
    Aviso interno del panel: tarea, recordatorio o alerta con prioridad y estado.
    Puede estar vinculado opcionalmente a un cliente.
    """
    __tablename__ = "avisos"

    id = db.Column(db.Integer, primary_key=True)

    titulo = db.Column(db.String(255), nullable=False)
    cuerpo = db.Column(db.Text, nullable=True)

    # alta / media / baja
    prioridad = db.Column(db.String(10), nullable=False, default="media")

    # pendiente / resuelto
    estado = db.Column(db.String(15), nullable=False, default="pendiente")

    # Vínculo opcional a un cliente
    cliente_id = db.Column(db.Integer, db.ForeignKey("clientes.id"), nullable=True)

    fecha_vencimiento = db.Column(db.Date, nullable=True)  ### CAMBIO AQUÍ ### — vencimiento opcional

    creado_en = db.Column(db.DateTime(timezone=True), default=ahora)
    resuelto_en = db.Column(db.DateTime(timezone=True), nullable=True)

    cliente = db.relationship("Cliente", backref=db.backref("avisos", lazy="dynamic"))

    def __repr__(self):
        return f"<Aviso {self.id} [{self.prioridad}] {self.titulo[:40]}>"


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO DE CHAT WIDGET — integración con el sitio
# ═══════════════════════════════════════════════════════════════════════════════

### CAMBIO AQUÍ ### — Conversación iniciada desde el widget del sitio

class ChatConversacion(db.Model):
    __tablename__ = "chat_conversaciones"

    id         = db.Column(db.String(20), primary_key=True)
    started_at = db.Column(db.DateTime(timezone=True), default=ahora)
    status     = db.Column(db.String(20), default="active")

    # Datos del prospecto (se van completando durante el chat)
    nombre    = db.Column(db.String(255), nullable=True)
    telefono  = db.Column(db.String(50),  nullable=True)
    email     = db.Column(db.String(255), nullable=True)
    rubro     = db.Column(db.String(100), nullable=True)
    source_url = db.Column(db.String(500), nullable=True)

    # Análisis generado por Groq
    budget_est = db.Column(db.Text, nullable=True)   # JSON del análisis (propuesta)
    profile    = db.Column(db.Text, nullable=True)   # resumen en texto
    notes      = db.Column(db.Text, nullable=True)
    estilo_json = db.Column(db.Text, nullable=True)  ### CAMBIO AQUÍ ### — JSON estilometría
    web_json    = db.Column(db.Text, nullable=True)  ### CAMBIO AQUÍ ### — JSON búsqueda web

    mensajes = db.relationship("ChatMensaje", back_populates="conversacion", cascade="all, delete-orphan")
    hints    = db.relationship("ChatHint",    back_populates="conversacion", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ChatConversacion {self.id} {self.status}>"


class ChatMensaje(db.Model):
    __tablename__ = "chat_mensajes"

    id              = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.String(20), db.ForeignKey("chat_conversaciones.id"), nullable=False)
    role            = db.Column(db.String(20), nullable=False)   # user | assistant
    content         = db.Column(db.Text, nullable=False)
    sent_at         = db.Column(db.DateTime(timezone=True), default=ahora)
    hidden          = db.Column(db.Boolean, default=False)

    conversacion = db.relationship("ChatConversacion", back_populates="mensajes")

    def __repr__(self):
        return f"<ChatMensaje {self.role} conv={self.conversation_id}>"


class ChatHint(db.Model):
    """Instrucción secreta que el operador envía para guiar la IA."""
    __tablename__ = "chat_hints"

    id              = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.String(20), db.ForeignKey("chat_conversaciones.id"), nullable=False)
    hint            = db.Column(db.Text, nullable=False)
    created_at      = db.Column(db.DateTime(timezone=True), default=ahora)
    applied         = db.Column(db.Boolean, default=False)

    conversacion = db.relationship("ChatConversacion", back_populates="hints")

    def __repr__(self):
        return f"<ChatHint conv={self.conversation_id} applied={self.applied}>"


class ChatLead(db.Model):                          # ### CAMBIO AQUÍ ###
    """Lead capturado por el widget cuando el backend falla (modo fallback)."""
    __tablename__ = "chat_leads"

    id          = db.Column(db.Integer, primary_key=True)
    nombre      = db.Column(db.String(255), nullable=False)
    whatsapp    = db.Column(db.String(100), nullable=False)
    pagina      = db.Column(db.String(500), nullable=True)
    contactado  = db.Column(db.Boolean, default=False)
    creado_en   = db.Column(db.DateTime(timezone=True), default=ahora)

    def __repr__(self):
        return f"<ChatLead {self.nombre} {self.whatsapp}>"


class ChatKB(db.Model):                              # ### CAMBIO AQUÍ ###
    """
    Base de conocimiento editable para el agente de chat.
    Una sola fila por clave (key). El agente la lee en cada conversación.
    """
    __tablename__ = "chat_kb"

    id         = db.Column(db.Integer, primary_key=True)
    clave      = db.Column(db.String(64), unique=True, nullable=False)   # ej: "servicios"
    titulo     = db.Column(db.String(128), nullable=False)                # label en UI
    contenido  = db.Column(db.Text, nullable=False, default="")          # texto libre
    orden      = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.DateTime(timezone=True), default=ahora, onupdate=ahora)

    def __repr__(self):
        return f"<ChatKB {self.clave}>"
