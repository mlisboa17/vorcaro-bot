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


class MessageTemplate(SQLModel, table=True):
    __tablename__ = "message_templates"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    body: str
    created_at: datetime = Field(default_factory=utcnow)
