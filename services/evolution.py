import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from services.settings import evolution_config

logger = logging.getLogger("vorcaro.evolution")


@dataclass(frozen=True)
class SendOutcome:
    ok: bool
    message_id: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def destination_for(remote_jid: str) -> str:
    jid = remote_jid.strip()
    lowered = jid.lower()
    if lowered.endswith("@g.us") or lowered.endswith("@lid"):
        return jid
    local = jid.split("@", 1)[0].split(":", 1)[0]
    return "".join(char for char in local if char.isdigit())


async def send_text(remote_jid: str, text: str, *, delay: int = 1000) -> SendOutcome:
    """Envia texto pela Evolution API v2. Falhas ficam no log e não sobem ao webhook."""
    config = evolution_config()
    number = destination_for(remote_jid)
    logger.info(f"Tentando enviar resposta para {remote_jid} via instância {config.instance}...")
    if not config.ready or not number:
        logger.warning("Evolution API incompleta; mensagem não enviada.")
        return SendOutcome(False)

    url = f"{config.base_url}/message/sendText/{quote(config.instance, safe='')}"
    payload = {"number": number, "text": text, "delay": delay}
    headers = {"apikey": config.api_key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError:
        logger.exception("Falha de rede ao enviar mensagem pela Evolution API para %s", number)
        return SendOutcome(False)

    body = response.text[:2000]
    logger.info(f"Evolution respondeu status={response.status_code} body={body}")
    if response.status_code not in {200, 201}:
        logger.error(
            "Evolution API recusou o envio (%s) para %s em %s. Corpo: %s",
            response.status_code,
            number,
            url,
            body,
        )
        return SendOutcome(False)
    return SendOutcome(True, _message_id(response.text))


def _message_id(body: str) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        return None
    candidates = [payload, payload.get("data"), payload.get("message")]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        key = candidate.get("key")
        if isinstance(key, dict) and isinstance(key.get("id"), str) and key["id"].strip():
            return key["id"].strip()
    return None
