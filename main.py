import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from database import engine, init_db
from models import CONFIRMATION_BODY, CONFIRMATION_TEMPLATE_KEY, MessageTemplate, Transaction
from services.evolution import send_text
from services.parser import IncomingMessage, extract_messages, parse_expense

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vorcaro")

UNPARSED_REPLY = "Não consegui ler o valor. Exemplo: Almoço 85"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Vorcaro Bot", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/webhook/evolution")
async def evolution_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "json inválido"}, status_code=400)

    if not isinstance(payload, dict):
        return {"ok": True, "ignored": True}

    results = []
    for incoming in extract_messages(payload):
        if incoming.from_me or _is_broadcast(incoming.remote_jid):
            continue
        results.append(await _register(incoming))

    if not results:
        return {"ok": True, "ignored": True}
    return {"ok": True, "results": results}


async def _register(incoming: IncomingMessage) -> dict:
    parsed = parse_expense(incoming.text)
    if parsed is None:
        delivered = await send_text(incoming.remote_jid, UNPARSED_REPLY)
        return {"parsed": False, "delivered": delivered}

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

    delivered = await send_text(incoming.remote_jid, reply)
    logger.info(
        "Despesa %s registrada: %.2f %s %s",
        transaction_id,
        parsed.amount,
        parsed.status,
        parsed.category,
    )
    return {
        "parsed": True,
        "transaction_id": transaction_id,
        "delivered": delivered,
    }


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
