"""Gerente Financeiro Vorcaro. Só roda para os JIDs de GRUPOS_DESPESAS."""

import logging
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from database import engine
from models import Expense, InboundReceipt
from services.evolution import send_text
from services.gemini_client import extract_expense
from services.media import InboundContent, find_media, load_media
from services.parser import IncomingMessage
from services.taxonomia import (
    bank_from_text,
    canonical_center,
    canonical_document,
    canonical_principal,
    canonical_subcategory,
    fold,
    hint_from_text,
    principal_of,
)

logger = logging.getLogger("vorcaro.financeiro")

_TZ = ZoneInfo("America/Sao_Paulo")
_QUERY_WORDS = ("quanto", "resumo", "relatorio", "gastamos", "gastos", "despesas", "extrato", "total")
_FAILURE_REPLY = "Não consegui ler este lançamento. Envie o comprovante de novo ou o valor e a descrição."
_DOWNLOAD_REPLY = "Não consegui baixar o arquivo. Ative o base64 no webhook da Evolution ou envie o comprovante outra vez."
_UNSUPPORTED_REPLY = "Neste grupo eu leio texto, PDF, imagem e áudio de despesa."


def now_local() -> datetime:
    return datetime.now(_TZ)


def is_financial_query(text: str) -> bool:
    folded = fold(text)
    return any(re.search(rf"\b{re.escape(word)}\b", folded) for word in _QUERY_WORDS)


def format_brl(amount: float) -> str:
    formatted = f"{amount:,.2f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def format_registered_expense(expense: Expense) -> str:
    if expense.status_confirmacao == "PENDENTE_INFO":
        return "\n".join(
            (
                "⚠️ *DESPESA REGISTRADA COM DÚVIDA*",
                "━━━━━━━━━━━━━━━━━━",
                f"💰 *Valor:* R$ {format_brl(expense.valor)}",
                f"👤 *Favorecido:* {expense.favorecido or 'Não identificado'}",
                f"🏦 *Origem:* {expense.origem_recurso or 'Não identificada'}",
                f"❓ *Dúvida:* {expense.duvida_pendente or 'Faltam dados para classificar.'}",
                "_(Responda a esta mensagem dizendo se é Gabriel, Gabriela, Mirella, Moradia, Flats, Consórcio ou Postos)_",
            )
        )
    return "\n".join(
        (
            "🧾 *DESPESA REGISTRADA*",
            "━━━━━━━━━━━━━━━━━━",
            f"💰 *Valor:* R$ {format_brl(expense.valor)}",
            f"📅 *Data/Hora:* {_format_sp(expense.data_hora)}",
            f"🏷️ *Categoria:* {expense.categoria_principal} » {expense.subcategoria}",
            f"🏦 *Origem do Recurso:* {expense.origem_recurso or 'Não identificada'}",
            f"🏢 *Centro de Custo / Unidade:* {expense.centro_custo_ou_unidade or 'Não especificado'}",
            f"👤 *Favorecido:* {expense.favorecido or 'Não identificado'}",
            f"📄 *Documento:* {expense.tipo_documento}",
            "━━━━━━━━━━━━━━━━━━",
            "✅ _Lançamento conferido e salvo na base de dados._",
        )
    )


async def process_expense_message(incoming: IncomingMessage) -> None:
    try:
        if not _claim(incoming):
            logger.info("Mensagem financeira já processada: %s", incoming.external_id)
            return
        await _handle(incoming)
    except Exception as e:
        logger.exception(f"ERRO CRÍTICO NO PROCESSAMENTO DA DESPESA: {e}")
        _release(incoming.external_id)
        try:
            await send_text(incoming.remote_jid, _FAILURE_REPLY)
        except Exception as send_error:
            logger.exception(f"ERRO CRÍTICO NO PROCESSAMENTO DA DESPESA: {send_error}")


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

    if media is None and _is_clarification(text):
        resolved = _resolve_pending(incoming.remote_jid, text)
        if resolved is not None:
            await send_text(incoming.remote_jid, format_registered_expense(resolved))
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
        "Despesa %s registrada: %.2f %s / %s (%s)",
        expense.id,
        expense.valor,
        expense.categoria_principal,
        expense.subcategoria,
        expense.status_confirmacao,
    )
    await send_text(incoming.remote_jid, reply)


def _build_expense(
    extracted: dict,
    incoming: IncomingMessage,
    content: InboundContent,
    reference: datetime,
) -> Expense | None:
    amount = _amount(extracted.get("valor", extracted.get("amount")))
    if amount is None:
        logger.error("Gemini não devolveu valor utilizável: %s", extracted.get("valor"))
        return None

    context = "\n".join(part for part in (content.text, str(extracted.get("transcricao") or "")) if part)
    hinted_principal, hinted_sub, hinted_center = hint_from_text(context)
    principal = hinted_principal or canonical_principal(extracted.get("categoria_principal") or extracted.get("category"))
    subcategory = hinted_sub or canonical_subcategory(
        extracted.get("subcategoria") or extracted.get("category"),
        principal,
    )
    if subcategory and principal is None:
        principal = principal_of(subcategory)
    if principal and subcategory and principal_of(subcategory) != principal:
        subcategory = canonical_subcategory(subcategory, principal)

    origem = _clean(extracted.get("origem_recurso")) or bank_from_text(context)
    centro = hinted_center or canonical_center(extracted.get("centro_custo_ou_unidade") or extracted.get("unit"))
    if centro is None and principal == "Filho (Gabriel)":
        centro = "Gabriel"
    if centro is None and subcategory and subcategory.startswith("Flats"):
        centro = "Flat"

    document_default = "PIX" if content.kind in {"text", "audio"} else "Cupom"
    doubts: list[str] = []
    model_doubt = _clean(extracted.get("duvida_pendente"))
    if extracted.get("status_confirmacao") == "PENDENTE_INFO" and model_doubt:
        doubts.append(model_doubt)
    if principal is None or subcategory is None:
        doubts.append(
            "Não deu para classificar. É Gabriel, Gabriela, Mirella, Moradia, Flats, Consórcio ou Postos?"
        )
        principal = principal or "Despesas Pessoais & Outros"
        subcategory = subcategory or "Outros"
    if not origem:
        doubts.append("De qual conta, banco ou caixa saiu este valor?")

    unique_doubts = list(dict.fromkeys(doubts))
    pending = bool(unique_doubts)
    return Expense(
        valor=amount,
        data_hora=_as_utc(_occurred_at(extracted.get("data_hora") or extracted.get("occurred_at"), reference)),
        categoria_principal=principal,
        subcategoria=subcategory,
        origem_recurso=origem,
        centro_custo_ou_unidade=centro,
        favorecido=_clean(extracted.get("favorecido") or extracted.get("supplier")) or "Não identificado",
        tipo_documento=canonical_document(extracted.get("tipo_documento") or extracted.get("document_type"), document_default),
        status_confirmacao="PENDENTE_INFO" if pending else "CONFIRMADO",
        duvida_pendente=" ".join(unique_doubts)[:500] if pending else None,
        raw_text_ou_transcricao=(context or None),
        group_jid=incoming.remote_jid,
        external_id=incoming.external_id,
    )


def _summary_reply(group_jid: str, text: str, moment: datetime) -> str:
    start, end, label, principal, subcategory = _query_plan(text, moment)
    statement = select(Expense).where(Expense.group_jid == group_jid)
    if start is not None and end is not None:
        statement = statement.where(Expense.data_hora >= start).where(Expense.data_hora <= end)
    if principal is not None:
        statement = statement.where(Expense.categoria_principal == principal)
    if subcategory is not None:
        statement = statement.where(Expense.subcategoria == subcategory)
    with Session(engine) as session:
        rows = list(session.exec(statement.order_by(Expense.data_hora.desc())).all())
    filtro = " » ".join(part for part in (principal, subcategory) if part) or "todas"
    if not rows:
        return (
            "📊 *CONSULTA DE DESPESAS*\n━━━━━━━━━━━━━━━━━━\n"
            f"Nenhuma despesa em {label} para {filtro}."
        )

    total = sum(row.valor for row in rows)
    lines = [
        "📊 *CONSULTA DE DESPESAS*",
        "━━━━━━━━━━━━━━━━━━",
        f"📅 *Período:* {label}",
        f"🏷️ *Filtro:* {filtro}",
        f"💰 *Total:* R$ {format_brl(total)}",
        f"🧾 *Lançamentos:* {len(rows)}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for row in rows[:8]:
        lines.append(
            f"• {_format_sp(row.data_hora)} — {row.subcategoria} — "
            f"R$ {format_brl(row.valor)} — {row.origem_recurso or 'origem não informada'}"
        )
    if len(rows) > 8:
        lines.append(f"• … e mais {len(rows) - 8} lançamentos")
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _query_plan(
    text: str, moment: datetime
) -> tuple[datetime | None, datetime | None, str, str | None, str | None]:
    folded = fold(text)
    principal, subcategory, _center = hint_from_text(text)
    if principal is None and "moradia" in folded:
        principal = "Moradia & Imóveis Pessoais"
    if "posto vip" in folded:
        principal = "Operação Empresas (Grupo Lisboa)"
    has_period = any(word in folded for word in ("hoje", "semana", "mes"))
    if not has_period and (principal is not None or "total" in folded):
        return None, None, "todo o período", principal, subcategory
    start, end, label = _query_window(text, moment)
    return start, end, label, principal, subcategory


def _query_window(text: str, moment: datetime) -> tuple[datetime, datetime, str]:
    folded = fold(text)
    local = _as_local(moment)
    end = _as_utc(_at(local.date(), time.max))
    if "hoje" in folded:
        return _as_utc(_at(local.date(), time.min)), end, local.strftime("%d/%m/%Y")
    if "semana" in folded:
        return _as_utc(_at(local.date() - timedelta(days=6), time.min)), end, "últimos 7 dias"
    start = _as_utc(_at(local.date().replace(day=1), time.min))
    return start, end, local.strftime("%m/%Y")


def _is_clarification(text: str) -> bool:
    folded = fold(text)
    if is_financial_query(text) or _amount(text) is not None:
        return False
    if re.search(r"\b(paguei|pago|pagou|comprei|transferi|pix|boleto)\b", folded):
        return False
    return _clarification_target(text) is not None


def _clarification_target(text: str) -> tuple[str | None, str | None, str | None, str | None] | None:
    folded = fold(text)
    bank = bank_from_text(text)
    principal, subcategory, center = hint_from_text(text)
    if "moradia" in folded:
        principal = "Moradia & Imóveis Pessoais"
    if "posto vip" in folded:
        principal, center = "Operação Empresas (Grupo Lisboa)", "Posto VIP"
    elif "posto real" in folded:
        principal, center = "Operação Empresas (Grupo Lisboa)", "Posto Real"
    elif "casa caiada" in folded:
        principal, center = "Operação Empresas (Grupo Lisboa)", "Casa Caiada"
    elif re.search(r"\bpostos?\b", folded):
        principal = "Operação Empresas (Grupo Lisboa)"
    if principal is None and bank is None:
        return None
    return principal, subcategory, center, bank


def _resolve_pending(group_jid: str, text: str) -> Expense | None:
    target = _clarification_target(text)
    if target is None:
        return None
    principal, subcategory, center, bank = target
    with Session(engine) as session:
        pending = session.exec(
            select(Expense)
            .where(Expense.group_jid == group_jid)
            .where(Expense.status_confirmacao == "PENDENTE_INFO")
            .order_by(Expense.created_at.desc())
        ).first()
        if pending is None:
            return None
        if principal:
            pending.categoria_principal = principal
            if subcategory and principal_of(subcategory) == principal:
                pending.subcategoria = subcategory
        if center:
            pending.centro_custo_ou_unidade = center
        if bank:
            pending.origem_recurso = bank
        still_missing = []
        if not pending.origem_recurso:
            still_missing.append("De qual conta, banco ou caixa saiu este valor?")
        if principal_of(pending.subcategoria) != pending.categoria_principal:
            still_missing.append("Falta a subcategoria dessa classificação.")
        if still_missing:
            pending.status_confirmacao = "PENDENTE_INFO"
            pending.duvida_pendente = " ".join(still_missing)
        else:
            pending.status_confirmacao = "CONFIRMADO"
            pending.duvida_pendente = None
        session.add(pending)
        session.commit()
        session.refresh(pending)
        return pending


def _at(day, clock: time) -> datetime:
    return datetime.combine(day, clock).replace(tzinfo=_TZ)


def _as_local(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_TZ)
    return moment.astimezone(_TZ)


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
        return _as_local(reference)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return _as_local(reference)
    return _as_local(parsed)


def _as_utc(moment: datetime) -> datetime:
    return _as_local(moment).astimezone(timezone.utc)


def _format_sp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_TZ).strftime("%d/%m/%Y %H:%M")


def _clean(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or value.strip().lower() == "null":
        return None
    return value.strip()[:180]
