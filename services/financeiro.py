"""Gerente Financeiro Vorcaro. Só roda para os JIDs de GRUPOS_DESPESAS."""

import logging
import re
import unicodedata
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from database import engine
from models import (
    CORPORATE_CATEGORIES,
    CORPORATE_DOCUMENT_TYPES,
    CORPORATE_UNITS,
    CorporateExpense,
    InboundReceipt,
)
from services.evolution import send_text
from services.gemini_client import GeminiError, extract_expense
from services.media import InboundContent, find_media, load_media
from services.parser import IncomingMessage

logger = logging.getLogger("vorcaro.financeiro")

_TZ = ZoneInfo("America/Sao_Paulo")
_QUERY_WORDS = ("quanto", "resumo", "relatorio", "gastamos", "gastos", "despesas", "extrato")
_FAILURE_REPLY = "Não consegui ler este lançamento. Envie o comprovante de novo ou o valor e a descrição."
_DOWNLOAD_REPLY = "Não consegui baixar o arquivo. Ative o base64 no webhook da Evolution ou envie o comprovante outra vez."
_UNSUPPORTED_REPLY = "Neste grupo eu leio texto, PDF, imagem e áudio de despesa."


def now_local() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def is_financial_query(text: str) -> bool:
    folded = _fold(text)
    return any(re.search(rf"\b{re.escape(word)}\b", folded) for word in _QUERY_WORDS)


def format_brl(amount: float) -> str:
    formatted = f"{amount:,.2f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def format_registered_expense(expense: CorporateExpense) -> str:
    return "\n".join(
        (
            "🧾 *DESPESA REGISTRADA*",
            "━━━━━━━━━━━━━━━━━━",
            f"💰 *Valor:* R$ {format_brl(expense.amount)}",
            f"📅 *Data/Hora:* {expense.occurred_at.strftime('%d/%m/%Y %H:%M')}",
            f"🏷️ *Categoria:* {expense.category}",
            f"👤 *Favorecido / Fornecedor:* {expense.supplier}",
            f"🏢 *Unidade Identificada:* {expense.unit}",
            f"📄 *Tipo de Documento:* {expense.document_type}",
            "━━━━━━━━━━━━━━━━━━",
            "✅ _Lançamento conferido com sucesso._",
        )
    )


async def process_expense_message(incoming: IncomingMessage) -> None:
    if not _claim(incoming):
        logger.info("Mensagem financeira já processada: %s", incoming.external_id)
        return
    try:
        await _handle(incoming)
    except Exception:
        logger.exception("Falha no Gerente Financeiro para %s", incoming.remote_jid)
        _release(incoming.external_id)
        await send_text(incoming.remote_jid, _FAILURE_REPLY)


async def _handle(incoming: IncomingMessage) -> None:
    node = incoming.raw or {}
    media = find_media(node)
    text = incoming.text.strip()

    if media is None and not text:
        return

    if media is None and is_financial_query(text):
        reply = _summary_reply(incoming.remote_jid, text, now_local())
        await send_text(incoming.remote_jid, reply)
        return

    if media is None:
        content = InboundContent(kind="text", text=text)
    else:
        loaded = await load_media(node, media)
        if loaded is None:
            _release(incoming.external_id)
            await send_text(incoming.remote_jid, _DOWNLOAD_REPLY)
            return
        caption = "\n".join(part for part in (text, loaded.text) if part).strip()
        content = InboundContent(
            kind=loaded.kind,
            text=caption,
            data=loaded.data,
            mime_type=loaded.mime_type,
            filename=loaded.filename,
        )

    if content.kind == "text" and not content.text:
        await send_text(incoming.remote_jid, _UNSUPPORTED_REPLY)
        return

    reference = now_local()
    extracted = await extract_expense(content, reference=reference)
    if extracted.get("error"):
        logger.info("Gemini não identificou despesa: %s", extracted.get("error"))
        _release(incoming.external_id)
        await send_text(incoming.remote_jid, _FAILURE_REPLY)
        return

    expense = _build_expense(extracted, incoming, content, reference)
    if expense is None:
        _release(incoming.external_id)
        await send_text(incoming.remote_jid, _FAILURE_REPLY)
        return

    with Session(engine) as session:
        session.add(expense)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.info("Despesa corporativa duplicada: %s", incoming.external_id)
            return
        session.refresh(expense)
        reply = format_registered_expense(expense)

    logger.info(
        "Despesa corporativa %s registrada: %.2f %s %s",
        expense.id,
        expense.amount,
        expense.category,
        expense.unit,
    )
    await send_text(incoming.remote_jid, reply)


def _build_expense(
    extracted: dict,
    incoming: IncomingMessage,
    content: InboundContent,
    reference: datetime,
) -> CorporateExpense | None:
    amount = _amount(extracted.get("amount"))
    if amount is None:
        logger.error("Gemini não devolveu valor utilizável: %s", extracted.get("amount"))
        return None
    document_default = {
        "audio": "Áudio",
        "pdf": "Nota",
        "image": "Recibo Fiscal",
        "text": "PIX",
    }.get(content.kind, "Nota")
    return CorporateExpense(
        amount=amount,
        occurred_at=_occurred_at(extracted.get("occurred_at"), reference),
        category=_choice(extracted.get("category"), CORPORATE_CATEGORIES, "Outros"),
        supplier=_supplier(extracted.get("supplier")),
        unit=_choice(extracted.get("unit"), CORPORATE_UNITS, "Não especificado"),
        document_type=_choice(extracted.get("document_type"), CORPORATE_DOCUMENT_TYPES, document_default),
        description=str(extracted.get("description") or content.text or "")[:300],
        raw_text=content.text or None,
        group_jid=incoming.remote_jid,
        external_id=incoming.external_id,
    )


def _summary_reply(group_jid: str, text: str, moment: datetime) -> str:
    start, end, label = _query_window(text, moment)
    with Session(engine) as session:
        rows = session.exec(
            select(CorporateExpense)
            .where(CorporateExpense.group_jid == group_jid)
            .where(CorporateExpense.occurred_at >= start)
            .where(CorporateExpense.occurred_at <= end)
        ).all()
    if not rows:
        return f"📊 *RESUMO DE DESPESAS*\n━━━━━━━━━━━━━━━━━━\nNenhuma despesa em {label}."

    total = sum(row.amount for row in rows)
    by_category: dict[str, float] = {}
    for row in rows:
        by_category[row.category] = by_category.get(row.category, 0.0) + row.amount
    lines = [
        "📊 *RESUMO DE DESPESAS*",
        "━━━━━━━━━━━━━━━━━━",
        f"📅 *Período:* {label}",
        f"💰 *Total:* R$ {format_brl(total)}",
        f"🧾 *Lançamentos:* {len(rows)}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for category, value in sorted(by_category.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"🏷️ {category}: R$ {format_brl(value)}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _query_window(text: str, moment: datetime) -> tuple[datetime, datetime, str]:
    folded = _fold(text)
    end = datetime.combine(moment.date(), time.max)
    if "hoje" in folded:
        start = datetime.combine(moment.date(), time.min)
        return start, end, moment.strftime("%d/%m/%Y")
    if "semana" in folded:
        start = datetime.combine(moment.date() - timedelta(days=6), time.min)
        return start, end, "últimos 7 dias"
    start = datetime.combine(moment.date().replace(day=1), time.min)
    return start, end, moment.strftime("%m/%Y")


def _claim(incoming: IncomingMessage) -> bool:
    if not incoming.external_id:
        return True
    with Session(engine) as session:
        session.add(InboundReceipt(external_id=incoming.external_id, group_jid=incoming.remote_jid))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return False
    return True


def _release(external_id: str | None) -> None:
    if not external_id:
        return
    with Session(engine) as session:
        row = session.exec(select(InboundReceipt).where(InboundReceipt.external_id == external_id)).first()
        if row is None:
            return
        session.delete(row)
        session.commit()


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    elif isinstance(value, str):
        token = value.strip().replace("R$", "").strip()
        if "," in token:
            token = token.replace(".", "").replace(",", ".")
        try:
            amount = float(token)
        except ValueError:
            return None
    else:
        return None
    if amount <= 0:
        return None
    return round(amount, 2)


def _occurred_at(value: object, reference: datetime) -> datetime:
    if not isinstance(value, str) or not value.strip():
        return reference
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return reference
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_TZ).replace(tzinfo=None)
    return parsed


def _supplier(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "Não identificado"
    return value.strip()[:180]


def _choice(value: object, options: tuple[str, ...], default: str) -> str:
    if not isinstance(value, str):
        return default
    folded = _fold(value)
    for option in options:
        if _fold(option) == folded:
            return option
    return default


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    without = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return without.lower()
