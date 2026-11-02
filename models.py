from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

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
    created_at: datetime = Field(default_factory=utcnow)


CORPORATE_CATEGORIES = (
    "Combustível",
    "Manutenção",
    "Pessoal",
    "Suprimentos",
    "Tributos",
    "Outros",
)
CORPORATE_UNITS = (
    "Posto VIP",
    "Real",
    "Casa Caiada",
    "Matriz",
    "Não especificado",
)
CORPORATE_DOCUMENT_TYPES = (
    "PIX",
    "Boleto",
    "Recibo Fiscal",
    "Áudio",
    "Nota",
)


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
    created_at: datetime = Field(default_factory=utcnow)


class InboundReceipt(SQLModel, table=True):
    """Evita que um reenvio do webhook lance a mesma despesa duas vezes."""

    __tablename__ = "inbound_receipts"

    id: Optional[int] = Field(default=None, primary_key=True)
    external_id: str = Field(unique=True, index=True)
    group_jid: str
    created_at: datetime = Field(default_factory=utcnow)


class MessageTemplate(SQLModel, table=True):
    __tablename__ = "message_templates"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    body: str
    created_at: datetime = Field(default_factory=utcnow)
