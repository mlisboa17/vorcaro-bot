import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from database import DATABASE_URL, engine, ensure_data_dir, init_db
from models import CONFIRMATION_BODY, CONFIRMATION_TEMPLATE_KEY, MessageTemplate, Transaction
from services.evolution import send_text
from services.financeiro import process_expense_message
from services.parser import IncomingMessage, extract_messages, parse_expense
from services.settings import is_expense_group

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vorcaro")

UNPARSED_REPLY = "Não consegui ler o valor. Exemplo: Almoço 85"
HEALTH_BODY = {"status": "online", "system": "Vorcaro Finance Bot"}


def _bind() -> tuple[str, str]:
    host = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = os.getenv("PORT", "80").strip() or "80"
    return host, port


@asynccontextmanager
async def lifespan(_: FastAPI):
    host, port = _bind()
    logger.info("Iniciando Vorcaro Finance Bot em %s:%s", host, port)
    ensure_data_dir()
    logger.info("Diretório de dados pronto para %s", DATABASE_URL)
    init_db()
    logger.info("Vorcaro Finance Bot no ar em http://%s:%s", host, port)
    yield
    logger.info("Vorcaro Finance Bot encerrado")


app = FastAPI(title="Vorcaro Bot", version="1.0.0", lifespan=lifespan)


@app.get("/health")
@app.get("/")
def health():
    return HEALTH_BODY


@app.post("/webhook/evolution")
async def evolution_webhook(request: Request, background: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        logger.warning("Webhook Evolution recebeu corpo que não é JSON")
        return JSONResponse({"ok": True, "ignored": True}, status_code=200)

    if not isinstance(payload, dict) or not _is_message_upsert(payload):
        return {"ok": True, "ignored": True}

    incoming_messages = [
        incoming
        for incoming in extract_messages(payload)
        if not incoming.from_me and not _is_broadcast(incoming.remote_jid)
    ]
    if not incoming_messages:
        return {"ok": True, "ignored": True}

    expense_messages = [incoming for incoming in incoming_messages if is_expense_group(incoming.remote_jid)]
    # Fora de GRUPOS_DESPESAS o fluxo anterior segue intacto (texto / auditoria).
    other_messages = [
        incoming
        for incoming in incoming_messages
        if not is_expense_group(incoming.remote_jid) and incoming.text.strip()
    ]

    for incoming in expense_messages:
        background.add_task(process_expense_message, incoming)

    results = []
    try:
        for incoming in other_messages:
            results.append(_register(incoming, background))
    except Exception:
        logger.exception("Falha ao processar webhook da Evolution")
        if expense_messages:
            return {"status": "processing"}
        return {"ok": True}

    if expense_messages:
        return {"status": "processing"}
    if not results:
        return {"ok": True, "ignored": True}
    return {"ok": True, "results": results}


def _is_message_upsert(payload: dict) -> bool:
    event = payload.get("event")
    if not isinstance(event, str) or not event.strip():
        return True
    normalized = event.strip().replace(".", "_").replace("-", "_").lower()
    return normalized in {"messages_upsert", "message_upsert"}


def _register(incoming: IncomingMessage, background: BackgroundTasks) -> dict:
    parsed = parse_expense(incoming.text)
    if parsed is None:
        background.add_task(send_text, incoming.remote_jid, UNPARSED_REPLY)
        return {"parsed": False}

    with Session(engine) as session:
        if incoming.external_id:
            existing = session.exec(
                select(Transaction).where(Transaction.external_id == incoming.external_id)
            ).first()
            if existing is not None:
                return {"parsed": True, "duplicate": True, "transaction_id": existing.id}

        transaction = Transaction(
            description=parsed.description,
            amount=parsed.amount,
            due_date=parsed.due_date,
            category=parsed.category,
            status=parsed.status,
            source="whatsapp",
            raw_text=incoming.text,
            external_id=incoming.external_id,
        )
        session.add(transaction)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.info("Mensagem duplicada ignorada: %s", incoming.external_id)
            return {"parsed": True, "duplicate": True}
        session.refresh(transaction)
        transaction_id = transaction.id
        reply = _confirmation(session, parsed.amount, parsed.description, parsed.status)

    background.add_task(send_text, incoming.remote_jid, reply)
    logger.info(
        "Despesa %s registrada: %.2f %s %s",
        transaction_id,
        parsed.amount,
        parsed.status,
        parsed.category,
    )
    return {"parsed": True, "transaction_id": transaction_id}


def _confirmation(session: Session, amount: float, description: str, status: str) -> str:
    template = session.exec(
        select(MessageTemplate).where(MessageTemplate.key == CONFIRMATION_TEMPLATE_KEY)
    ).first()
    body = template.body if template is not None and template.body else CONFIRMATION_BODY
    safe_description = description.replace("{", "(").replace("}", ")")
    try:
        return body.format(amount=amount, description=safe_description, status=status)
    except (KeyError, IndexError, ValueError):
        logger.exception("Template de confirmação inválido; usando o texto padrão")
        return CONFIRMATION_BODY.format(
            amount=amount, description=safe_description, status=status
        )


def _is_broadcast(remote_jid: str) -> bool:
    return remote_jid.lower().endswith("@broadcast")
