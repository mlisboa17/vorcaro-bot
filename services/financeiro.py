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
from services.parser import IncomingMessage, quoted_reference
from services.taxonomia import (
    bank_from_text,
    canonical_center,
    canonical_document,
    canonical_principal,
    canonical_subcategory,
    fold,
    hint_from_text,
    is_closed_pair,
    principal_of,
)

logger = logging.getLogger("vorcaro.financeiro")

_TZ = ZoneInfo("America/Sao_Paulo")
_QUERY_WORDS = ("quanto", "resumo", "relatorio", "gastamos", "gastos", "despesas", "extrato", "total")
_ORIGIN_DOUBT = "Informe a conta de origem (Bradesco, Itaú, Cartão, etc.)"
_CATEGORY_DOUBT = "Especifique a categoria do gasto"
_PENDING_NOTE = "Nota: Existem {count} lançamentos pendentes de confirmação neste período que não foram somados."
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
        lancamento = f"#{expense.id}" if expense.id else "novo"
        return "\n".join(
            (
                "⚠️ *DESPESA REGISTRADA COM DÚVIDA*",
                "━━━━━━━━━━━━━━━━━━",
                f"🔖 *Lançamento:* {lancamento}",
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

    stanza_id, quoted_text = quoted_reference(node)
    if media is None and (stanza_id or quoted_text or _is_clarification(text)):
        resolved = _resolve_pending(
            incoming.remote_jid,
            text,
            stanza_id=stanza_id,
            quoted_text=quoted_text,
        )
        if resolved is not None:
            await _deliver(incoming.remote_jid, format_registered_expense(resolved), resolved)
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
        session.expunge(expense)
        reply = format_registered_expense(expense)
        expense_id = expense.id
        status = expense.status_confirmacao

    logger.info(
        "Despesa %s registrada: %.2f %s / %s (%s)",
        expense_id,
        expense.valor,
        expense.categoria_principal,
        expense.subcategoria,
        status,
    )
    await _deliver(incoming.remote_jid, reply, expense)


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
    model_pending = extracted.get("status_confirmacao") == "PENDENTE_INFO"
    category_reliable = is_closed_pair(principal, subcategory) and not (model_pending and hinted_principal is None)
    if not category_reliable:
        principal = principal or "Despesas Pessoais & Outros"
        subcategory = subcategory or "Outros"
    favorecido = _favorecido(extracted, context, hinted_sub)
    doubts = _pending_reasons(
        category_reliable=category_reliable,
        origem=origem,
        model_doubt=_clean(extracted.get("duvida_pendente")) if model_pending else None,
    )
    return Expense(
        valor=amount,
        data_hora=_as_utc(_occurred_at(extracted.get("data_hora") or extracted.get("occurred_at"), reference)),
        categoria_principal=principal,
        subcategoria=subcategory,
        origem_recurso=origem,
        centro_custo_ou_unidade=centro,
        favorecido=favorecido,
        tipo_documento=canonical_document(extracted.get("tipo_documento") or extracted.get("document_type"), document_default),
        status_confirmacao="PENDENTE_INFO" if doubts else "CONFIRMADO",
        duvida_pendente=" ".join(doubts)[:500] if doubts else None,
        raw_text_ou_transcricao=(context or None),
        group_jid=incoming.remote_jid,
        external_id=incoming.external_id,
    )


def _summary_reply(group_jid: str, text: str, moment: datetime) -> str:
    start, end, label, principal, subcategory = _query_plan(text, moment)
    statement = select(Expense).where(Expense.group_jid == group_jid)
    if start is not None and end is not None:
        statement = statement.where(Expense.data_hora.between(start, end))
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

    confirmed = [row for row in rows if row.status_confirmacao != "PENDENTE_INFO"]
    pending_count = len(rows) - len(confirmed)
    total = sum(row.valor for row in confirmed)
    lines = [
        "📊 *CONSULTA DE DESPESAS*",
        "━━━━━━━━━━━━━━━━━━",
        f"📅 *Período:* {label}",
        f"🏷️ *Filtro:* {filtro}",
        f"💰 *Total:* R$ {format_brl(total)}",
        f"🧾 *Lançamentos:* {len(confirmed)}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for row in confirmed[:8]:
        lines.append(
            f"• {_format_sp(row.data_hora)} — {row.subcategoria} — "
            f"R$ {format_brl(row.valor)} — {row.origem_recurso or 'origem não informada'}"
        )
    if len(confirmed) > 8:
        lines.append(f"• … e mais {len(confirmed) - 8} lançamentos")
    if pending_count:
        lines.append(_PENDING_NOTE.format(count=pending_count))
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


def _resolve_pending(
    group_jid: str,
    text: str,
    *,
    stanza_id: str | None = None,
    quoted_text: str | None = None,
) -> Expense | None:
    with Session(engine) as session:
        pending = _select_pending(session, group_jid, stanza_id, quoted_text)
        if pending is None:
            return None
        _apply_clarification(pending, text)
        doubts = _pending_reasons(
            category_reliable=is_closed_pair(pending.categoria_principal, pending.subcategoria),
            origem=pending.origem_recurso,
            model_doubt=None,
        )
        if doubts:
            pending.status_confirmacao = "PENDENTE_INFO"
            pending.duvida_pendente = " ".join(doubts)[:500]
        else:
            pending.status_confirmacao = "CONFIRMADO"
            pending.duvida_pendente = None
        session.add(pending)
        session.commit()
        session.refresh(pending)
        session.expunge(pending)
        return pending


def _select_pending(
    session: Session,
    group_jid: str,
    stanza_id: str | None,
    quoted_text: str | None,
) -> Expense | None:
    """Reply cita o alerta. Sem reply, vale a pendência mais recente do grupo."""
    quoted = bool(stanza_id or _expense_id_from_alert(quoted_text) is not None)
    if stanza_id:
        matched = session.exec(
            select(Expense)
            .where(Expense.group_jid == group_jid)
            .where(Expense.alert_message_id == stanza_id)
            .where(Expense.status_confirmacao == "PENDENTE_INFO")
        ).first()
        if matched is not None:
            return matched
    cited = _expense_id_from_alert(quoted_text)
    if cited is not None:
        row = session.get(Expense, cited)
        if row is not None and row.group_jid == group_jid and row.status_confirmacao == "PENDENTE_INFO":
            return row
        return None
    if quoted:
        return None
    return session.exec(
        select(Expense)
        .where(Expense.group_jid == group_jid)
        .where(Expense.status_confirmacao == "PENDENTE_INFO")
        .order_by(Expense.data_hora.desc(), Expense.id.desc())
    ).first()


def _apply_clarification(pending: Expense, text: str) -> None:
    reply = _clarification_target(text)
    original = _clarification_target(pending.raw_text_ou_transcricao or "")
    principal, subcategory, center, bank = reply or (None, None, None, None)
    if principal is None and not is_closed_pair(pending.categoria_principal, pending.subcategoria):
        principal, subcategory, center, bank = _fill_gaps(
            (principal, subcategory, center, bank),
            original,
        )
    elif bank is None and original is not None:
        bank = original[3]
    if principal:
        pending.categoria_principal = principal
        if subcategory and principal_of(subcategory) == principal:
            pending.subcategoria = subcategory
    if center:
        pending.centro_custo_ou_unidade = center
    if bank:
        pending.origem_recurso = bank
    _align_favorecido(pending, text)
    combined = "\n".join(part for part in (pending.raw_text_ou_transcricao, text) if part)
    pending.raw_text_ou_transcricao = combined[:2000] or None


def _fill_gaps(
    current: tuple[str | None, str | None, str | None, str | None],
    fallback: tuple[str | None, str | None, str | None, str | None] | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    if fallback is None:
        return current
    filled = tuple(left or right for left, right in zip(current, fallback, strict=True))
    return filled[0], filled[1], filled[2], filled[3]


def _pending_reasons(*, category_reliable: bool, origem: str | None, model_doubt: str | None) -> list[str]:
    doubts: list[str] = []
    if not category_reliable:
        doubts.append(_CATEGORY_DOUBT)
    if not origem:
        doubts.append(_ORIGIN_DOUBT)
    if model_doubt and model_doubt not in doubts:
        doubts.append(model_doubt)
    return doubts


def _favorecido(extracted: dict, context: str, hinted_sub: str | None) -> str:
    if hinted_sub and hinted_sub.startswith("Gabriela"):
        return "Gabriela"
    if hinted_sub and hinted_sub.startswith("Mirella"):
        return "Mirella"
    cleaned = _clean(extracted.get("favorecido") or extracted.get("supplier"))
    if cleaned and fold(cleaned) == "gabriel" and re.search(r"\bgabriela\b", fold(context)):
        return "Gabriela"
    return cleaned or "Não identificado"


def _align_favorecido(pending: Expense, text: str) -> None:
    folded = fold(text)
    if re.search(r"\bgabriela\b", folded):
        pending.favorecido = "Gabriela"
    elif re.search(r"\bmirella\b", folded):
        pending.favorecido = "Mirella"


def _expense_id_from_alert(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"lan[cç]amento:\*?\s*#(\d+)", text, re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1))


async def _deliver(remote_jid: str, text: str, expense: Expense | None = None) -> None:
    outcome = await send_text(remote_jid, text)
    message_id = getattr(outcome, "message_id", None)
    if (
        expense is not None
        and expense.id
        and expense.status_confirmacao == "PENDENTE_INFO"
        and isinstance(message_id, str)
        and message_id
    ):
        _remember_alert(expense.id, message_id)


def _remember_alert(expense_id: int, message_id: str) -> None:
    with Session(engine) as session:
        row = session.get(Expense, expense_id)
        if row is None:
            return
        row.alert_message_id = message_id
        session.add(row)
        session.commit()


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
