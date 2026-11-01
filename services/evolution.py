import logging
import os
from urllib.parse import quote

import httpx

logger = logging.getLogger("vorcaro.evolution")


def destination_for(remote_jid: str) -> str:
    jid = remote_jid.strip()
    lowered = jid.lower()
    if lowered.endswith("@g.us") or lowered.endswith("@lid"):
        return jid
    local = jid.split("@", 1)[0].split(":", 1)[0]
    return "".join(char for char in local if char.isdigit())


async def send_text(remote_jid: str, text: str) -> bool:
    """Envia uma resposta curta pela Evolution API v2. Não propaga falha de rede."""
    base = os.getenv("EVOLUTION_API_URL", "").strip().rstrip("/")
    api_key = os.getenv("EVOLUTION_API_KEY", "").strip()
    instance = os.getenv("EVOLUTION_INSTANCE", "").strip()
    number = destination_for(remote_jid)

    if not base or not api_key or not instance or not number:
        logger.warning("Evolution API incompleta; confirmação não enviada.")
        return False

    url = f"{base}/message/sendText/{quote(instance, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                url,
                json={"number": number, "text": text},
                headers={"apikey": api_key},
            )
            response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Falha ao enviar mensagem pela Evolution API")
        return False
    return True
