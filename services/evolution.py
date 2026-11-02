import logging
from urllib.parse import quote

import httpx

from services.settings import evolution_config

logger = logging.getLogger("vorcaro.evolution")


def destination_for(remote_jid: str) -> str:
    jid = remote_jid.strip()
    lowered = jid.lower()
    if lowered.endswith("@g.us") or lowered.endswith("@lid"):
        return jid
    local = jid.split("@", 1)[0].split(":", 1)[0]
    return "".join(char for char in local if char.isdigit())


async def send_text(remote_jid: str, text: str, *, delay: int = 1000) -> bool:
    """Envia texto pela Evolution API v2. Falhas ficam no log e não sobem ao webhook."""
    config = evolution_config()
    number = destination_for(remote_jid)
    logger.info(f"Tentando enviar resposta para {remote_jid} via instância {config.instance}...")
    if not config.ready or not number:
        logger.warning("Evolution API incompleta; mensagem não enviada.")
        return False

    url = f"{config.base_url}/message/sendText/{quote(config.instance, safe='')}"
    payload = {"number": number, "text": text, "delay": delay}
    headers = {"apikey": config.api_key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError:
        logger.exception("Falha de rede ao enviar mensagem pela Evolution API para %s", number)
        return False

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
        return False
    return True
