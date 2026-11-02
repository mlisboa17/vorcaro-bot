import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from models import CATEGORIES
from services.media import message_has_media

_TZ = ZoneInfo("America/Sao_Paulo")

_STATUS_RE = re.compile(r"\b(paguei|pagou|pago|paga)\b", re.IGNORECASE)
_NOISE_RE = re.compile(r"\b(reais|real)\b", re.IGNORECASE)
_ARTICLE_RE = re.compile(r"^(?:o|a|os|as|um|uma)\s+", re.IGNORECASE)
_AMOUNT_RE = re.compile(
    r"(?:r\$\s*)?(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)
_DATE_WORD_RE = re.compile(
    r"\b(?:vencimento|vence|venc)\b(?:\s+dia)?\s+"
    r"(?P<day>\d{1,2})(?:/(?P<month>\d{1,2})(?:/(?P<year>\d{2,4}))?)?",
    re.IGNORECASE,
)
_DATE_DAY_RE = re.compile(
    r"\bdia\s+(?P<day>\d{1,2})(?:/(?P<month>\d{1,2})(?:/(?P<year>\d{2,4}))?)?\b",
    re.IGNORECASE,
)
_DATE_NUMERIC_RE = re.compile(
    r"\b(?P<day>\d{1,2})/(?P<month>\d{1,2})(?:/(?P<year>\d{2,4}))?\b"
)

# Palavras já sem acento. A ordem segue a prioridade das categorias.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Imobiliário", ("aluguel", "imovel", "iptu", "condominio", "financiamento", "apartamento")),
    ("Consórcios", ("consorcio",)),
    ("Família", ("familia", "escola", "filho", "filha", "mesada", "pensao")),
    ("Fixos", ("luz", "agua", "internet", "telefone", "energia", "gas", "netflix", "spotify", "assinatura", "seguro")),
    ("Cartão", ("cartao", "fatura", "nubank", "credito")),
)


@dataclass(frozen=True)
class ParsedExpense:
    amount: float
    description: str
    due_date: date
    category: str
    status: str


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    remote_jid: str
    external_id: str | None
    from_me: bool
    raw: dict | None = None


def current_date() -> date:
    return datetime.now(_TZ).date()


def parse_expense(source: str | dict, *, today: date | None = None) -> ParsedExpense | None:
    """Extrai valor, descrição, vencimento, categoria e status de um texto ou payload."""
    if isinstance(source, dict):
        messages = [
            item
            for item in extract_messages(source)
            if not item.from_me and item.text.strip()
        ]
        if not messages:
            return None
        text = messages[0].text
    else:
        text = source
    return _parse_text(text, today=today or current_date())


def extract_messages(payload: dict) -> list[IncomingMessage]:
    if not isinstance(payload, dict):
        return []
    found: list[IncomingMessage] = []
    for node in _message_nodes(payload):
        key = node.get("key") if isinstance(node.get("key"), dict) else {}
        remote_jid = key.get("remoteJid") or key.get("remoteJidAlt")
        text = _message_text(node.get("message")) or ""
        if not isinstance(remote_jid, str) or not remote_jid.strip():
            continue
        if not text and not message_has_media(node):
            continue
        external_id = key.get("id")
        found.append(
            IncomingMessage(
                text=text,
                remote_jid=remote_jid.strip(),
                external_id=external_id if isinstance(external_id, str) and external_id else None,
                from_me=_as_bool(key.get("fromMe")),
                raw=node,
            )
        )
    return found


def _parse_text(text: str, *, today: date) -> ParsedExpense | None:
    raw = text.strip()
    if not raw:
        return None

    status = "PAID" if _STATUS_RE.search(raw) else "PENDING"
    without_date, due_date = _consume_date(raw, today)
    amount, without_amount = _consume_amount(without_date)
    if amount is None:
        return None

    description = _clean_description(without_amount)
    return ParsedExpense(
        amount=amount,
        description=description,
        due_date=due_date,
        category=_category_of(raw),
        status=status,
    )


def _message_nodes(payload: dict) -> list[dict]:
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    nested = data.get("messages")
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    if "key" in data or "message" in data:
        return [data]
    return []


def _message_text(message: object) -> str | None:
    if not isinstance(message, dict):
        return None
    conversation = message.get("conversation")
    if isinstance(conversation, str) and conversation.strip():
        return conversation.strip()
    extended = message.get("extendedTextMessage")
    if isinstance(extended, dict):
        text = extended.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _consume_date(text: str, today: date) -> tuple[str, date]:
    match = _DATE_WORD_RE.search(text) or _DATE_DAY_RE.search(text) or _DATE_NUMERIC_RE.search(text)
    if match is None:
        return text, today
    due = _date_from_match(match, today)
    blanked = f"{text[: match.start()]} {text[match.end() :]}"
    return blanked, due


def _date_from_match(match: re.Match[str], today: date) -> date:
    day = int(match.group("day"))
    month_raw = match.group("month")
    year_raw = match.group("year")
    month = int(month_raw) if month_raw else today.month
    if year_raw:
        year = int(year_raw)
        if year < 100:
            year += 2000
    else:
        year = today.year
    try:
        return date(year, month, day)
    except ValueError:
        return today


def _consume_amount(text: str) -> tuple[float | None, str]:
    matches = list(_AMOUNT_RE.finditer(text))
    if not matches:
        return None, text

    chosen = matches[-1]
    for candidate in reversed(matches):
        window = text[max(0, candidate.start() - 3) : candidate.start() + 2].lower()
        if "r$" in window:
            chosen = candidate
            break

    try:
        amount = round(_parse_amount_token(chosen.group(1)), 2)
    except ValueError:
        return None, text
    if amount <= 0:
        return None, text

    remaining = f"{text[: chosen.start()]} {text[chosen.end() :]}"
    return amount, remaining


def _parse_amount_token(token: str) -> float:
    """Converte número no formato brasileiro. 1.140,50 -> 1140.50; 1.140 -> 1140."""
    if "," in token:
        normalized = token.replace(".", "").replace(",", ".")
    elif token.count(".") > 1:
        normalized = token.replace(".", "")
    elif "." in token:
        fraction = token.split(".", 1)[1]
        normalized = token.replace(".", "") if len(fraction) == 3 else token
    else:
        normalized = token
    return float(normalized)


def _clean_description(text: str) -> str:
    cleaned = _STATUS_RE.sub(" ", text)
    cleaned = _NOISE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -.,;:")
    cleaned = _ARTICLE_RE.sub("", cleaned).strip()
    if not cleaned:
        return "Sem descrição"
    return cleaned[:300]


def _category_of(text: str) -> str:
    folded = _fold(text)
    for category, keywords in _CATEGORY_KEYWORDS:
        for keyword in keywords:
            if re.search(rf"\b{re.escape(keyword)}\b", folded):
                return category
    return "Outros"


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    without_accents = "".join(
        char for char in normalized if unicodedata.category(char) != "Mn"
    )
    return without_accents.lower()


def _assert_categories() -> None:
    names = {category for category, _ in _CATEGORY_KEYWORDS}
    names.add("Outros")
    if names != set(CATEGORIES):
        raise RuntimeError("Categorias do parser divergem do modelo.")


_assert_categories()
