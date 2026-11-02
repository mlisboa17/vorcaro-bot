from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel

CATEGORIES = (
    "Imobiliário",
    "Consórcios",
    "Família",
    "Fixos",
    "Cartão",
    "Outros",
)

CONFIRMATION_TEMPLATE_KEY = "expense_confirmed"
CONFIRMATION_BODY = (
    "✅ Vorcaro: R$ {amount:.2f} registrado em '{description}' ({status})."
)


class Transaction(SQLModel, table=True):
    __tablename__ = "transactions"

    id: Optional[int] = Field(default=None, primary_key=True)
    description: str
    amount: float
    due_date: date
    category: str
    status: str = Field(default="PENDING")
    source: str = Field(default="whatsapp")
    raw_text: Optional[str] = None
    external_id: Optional[str] = Field(default=None, unique=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Expense(SQLModel, table=True):
    """Plano de contas do Gerente Financeiro. Separado dos lançamentos por texto."""

    __tablename__ = "expenses"

    id: Optional[int] = Field(default=None, primary_key=True)
    valor: float
    data_hora: datetime = Field(index=True)
    categoria_principal: str = Field(index=True)
    subcategoria: str = Field(index=True)
    origem_recurso: Optional[str] = None
    centro_custo_ou_unidade: Optional[str] = None
    favorecido: Optional[str] = None
    tipo_documento: str
    status_confirmacao: str = Field(default="CONFIRMADO")
    duvida_pendente: Optional[str] = Field(default=None)
    raw_text_ou_transcricao: Optional[str] = Field(default=None)
    group_jid: str = Field(index=True)
    external_id: Optional[str] = Field(default=None, unique=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CorporateExpense(SQLModel, table=True):
    """Lançamento do Gerente Financeiro. Separado dos lançamentos por texto."""

    __tablename__ = "corporate_expenses"

    id: Optional[int] = Field(default=None, primary_key=True)
    amount: float
    occurred_at: datetime = Field(index=True)
    category: str
    supplier: str
    unit: str
    document_type: str
    description: str = ""
    raw_text: Optional[str] = None
    group_jid: str = Field(index=True)
    external_id: Optional[str] = Field(default=None, unique=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InboundReceipt(SQLModel, table=True):
    """Evita que um reenvio do webhook lance a mesma despesa duas vezes."""

    __tablename__ = "inbound_receipts"

    id: Optional[int] = Field(default=None, primary_key=True)
    external_id: str = Field(unique=True, index=True)
    group_jid: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MessageTemplate(SQLModel, table=True):
    __tablename__ = "message_templates"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    body: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
