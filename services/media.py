"""Extrai PDF, imagem e áudio do payload da Evolution API v2."""

import base64
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from services.settings import evolution_config

logger = logging.getLogger("vorcaro.media")

_WRAPPERS = (
    "ephemeralMessage",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "documentWithCaptionMessage",
    "editedMessage",
)
_MEDIA_KEYS = ("documentMessage", "imageMessage", "audioMessage")


@dataclass(frozen=True)
class MediaRef:
    kind: str
    mime_type: str
    filename: str
    caption: str
    inline_base64: str | None
    url: str | None


@dataclass(frozen=True)
class InboundContent:
    kind: str
    text: str
    data: bytes | None = None
    mime_type: str | None = None
    filename: str | None = None


def message_has_media(node: dict) -> bool:
    return find_media(node) is not None


def find_media(node: dict) -> MediaRef | None:
    if not isinstance(node, dict):
        return None
    message = _unwrap_message(node.get("message") if isinstance(node.get("message"), dict) else node)
    for key in _MEDIA_KEYS:
        media = message.get(key)
        if not isinstance(media, dict):
            continue
        kind = _kind_of(key, media)
        if kind is None:
            continue
        filename = _filename(media)
        mime = normalize_mime(str(media.get("mimetype") or media.get("mimeType") or ""), kind)
        return MediaRef(
            kind=kind,
            mime_type=mime,
            filename=filename,
            caption=_caption(media),
            inline_base64=_first_base64(
                message.get("base64"),
                media.get("base64"),
                node.get("base64"),
            ),
            url=_http_url(media),
        )
    return None


def normalize_mime(mime: str, kind: str) -> str:
    base = mime.split(";", 1)[0].strip().lower()
    if kind == "pdf":
        return "application/pdf"
    if kind == "image":
        if base in {"image/jpeg", "image/png", "image/webp"}:
            return base
        return "image/jpeg"
    if base in {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/webm", "audio/aac"}:
        return base
    if "mp4" in mime.lower() or "m4a" in mime.lower():
        return "audio/mp4"
    return "audio/ogg"


def decode_base64(value: str) -> bytes | None:
    raw = value.strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    raw = re.sub(r"\s+", "", raw)
    if len(raw) < 16:
        return None
    try:
        decoded = base64.b64decode(raw, validate=False)
    except (ValueError, TypeError):
        return None
    return decoded or None


async def load_media(node: dict, media: MediaRef) -> InboundContent | None:
    """Usa o base64 do webhook, a URL direta ou o endpoint da Evolution."""
    inline = decode_base64(media.inline_base64) if media.inline_base64 else None
    if inline:
        logger.info("Mídia %s recebida em base64 (%s bytes)", media.kind, len(inline))
        return _content(media, inline)

    if media.url and not _is_whatsapp_cdn(media.url):
        downloaded = await _download_url(media.url)
        if downloaded:
            logger.info("Mídia %s baixada pela URL (%s bytes)", media.kind, len(downloaded))
            return _content(media, downloaded)

    fetched, fetched_mime = await _download_from_evolution(node)
    if fetched:
        mime = normalize_mime(fetched_mime or media.mime_type, media.kind)
        logger.info("Mídia %s obtida na Evolution API (%s bytes)", media.kind, len(fetched))
        return InboundContent(
            kind=media.kind,
            text=media.caption,
            data=fetched,
            mime_type=mime,
            filename=media.filename or None,
        )

    if media.url:
        downloaded = await _download_url(media.url)
        if downloaded:
            logger.info("Mídia %s baixada no fallback de URL (%s bytes)", media.kind, len(downloaded))
            return _content(media, downloaded)

    logger.error("Não foi possível obter a mídia %s (%s)", media.kind, media.filename or media.mime_type)
    return None


def _content(media: MediaRef, data: bytes) -> InboundContent:
    return InboundContent(
        kind=media.kind,
        text=media.caption,
        data=data,
        mime_type=media.mime_type,
        filename=media.filename or None,
    )


def _unwrap_message(message: dict) -> dict:
    for wrapper in _WRAPPERS:
        inner = message.get(wrapper)
        if isinstance(inner, dict) and isinstance(inner.get("message"), dict):
            return _unwrap_message(inner["message"])
    return message


def _kind_of(key: str, media: dict) -> str | None:
    mime = str(media.get("mimetype") or media.get("mimeType") or "").lower()
    filename = _filename(media).lower()
    if key == "documentMessage":
        if "pdf" in mime or filename.endswith(".pdf"):
            return "pdf"
        return None
    if key == "imageMessage":
        return "image"
    if key == "audioMessage":
        return "audio"
    return None


def _filename(media: dict) -> str:
    for key in ("fileName", "filename", "title"):
        value = media.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def _caption(media: dict) -> str:
    caption = media.get("caption")
    if isinstance(caption, str):
        return caption.strip()
    return ""


def _first_base64(*candidates: object) -> str | None:
    for candidate in candidates:
        if isinstance(candidate, str) and len(candidate.strip()) > 32:
            return candidate.strip()
    return None


def _http_url(media: dict) -> str | None:
    for key in ("url", "mediaUrl", "fileUrl"):
        value = media.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def _is_whatsapp_cdn(url: str) -> bool:
    lowered = url.lower()
    return "mmg.whatsapp.net" in lowered or "whatsapp.net" in lowered


async def _download_url(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        logger.exception("Falha ao baixar mídia pela URL")
        return None
    if response.status_code != 200 or not response.content:
        logger.error("Download da mídia retornou %s (%s bytes)", response.status_code, len(response.content))
        return None
    return response.content


async def _download_from_evolution(node: dict) -> tuple[bytes | None, str | None]:
    config = evolution_config()
    if not config.ready:
        logger.error("Evolution API incompleta; mídia não pode ser descarregada")
        return None, None

    message = {key: value for key, value in node.items() if key != "base64"}
    nested = message.get("message")
    if isinstance(nested, dict) and "base64" in nested:
        message["message"] = {key: value for key, value in nested.items() if key != "base64"}

    url = f"{config.base_url}/chat/getBase64FromMediaMessage/{quote(config.instance, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(
                url,
                json={"message": message, "convertToMp4": False},
                headers={"apikey": config.api_key, "Content-Type": "application/json"},
            )
    except httpx.HTTPError:
        logger.exception("Falha de rede ao pedir base64 para a Evolution API")
        return None, None

    if response.status_code not in {200, 201}:
        logger.error(
            "Evolution getBase64 respondeu %s. Corpo: %s",
            response.status_code,
            response.text[:2000],
        )
        return None, None

    try:
        body = response.json()
    except ValueError:
        logger.error("Evolution getBase64 não retornou JSON. Corpo: %s", response.text[:2000])
        return None, None

    encoded = _base64_from_body(body)
    if not encoded:
        logger.error("Evolution getBase64 sem campo base64. Corpo: %s", str(body)[:2000])
        return None, None
    decoded = decode_base64(encoded)
    mime = body.get("mimetype") or body.get("mimeType")
    if not isinstance(mime, str):
        media = body.get("media")
        if isinstance(media, dict) and isinstance(media.get("mimetype"), str):
            mime = media["mimetype"]
        else:
            mime = None
    return decoded, mime


def _base64_from_body(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    direct = _first_base64(body.get("base64"), body.get("data"))
    if direct:
        return direct
    media = body.get("media")
    if isinstance(media, dict):
        return _first_base64(media.get("base64"), media.get("data"))
    return None
