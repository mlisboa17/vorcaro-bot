"""Extração financeira com Gemini Flash.

O modelo devolve JSON. O texto enviado ao WhatsApp é montado depois,
no formato fixo do Gerente Financeiro, para a consulta usar os mesmos campos.
"""

import asyncio
import json
import logging
import re
from datetime import datetime

from services.media import InboundContent
from services.settings import gemini_api_key, gemini_model

logger = logging.getLogger("vorcaro.gemini")

SYSTEM_PROMPT = """Você é o Gerente Financeiro Vorcaro.
Extraia uma despesa corporativa do texto, PDF, imagem ou áudio recebido.
Responda somente com um objeto JSON, sem markdown, nestas chaves:
{
  "amount": 0.0,
  "occurred_at": "YYYY-MM-DDTHH:MM:SS",
  "category": "Combustível | Manutenção | Pessoal | Suprimentos | Tributos | Outros",
  "supplier": "razão social ou nome, ou Não identificado",
  "unit": "Posto VIP | Real | Casa Caiada | Matriz | Não especificado",
  "document_type": "PIX | Boleto | Recibo Fiscal | Áudio | Nota",
  "description": "resumo curto"
}
Regras:
- amount é o valor em reais. Use ponto como separador decimal.
- occurred_at é a data e hora identificadas no documento ou na fala, no fuso America/Sao_Paulo.
- Se a data não aparecer, use a data de referência informada.
- category, unit e document_type precisam ser exatamente um dos valores listados.
- supplier é o favorecido ou fornecedor. Sem nome, use "Não identificado".
- Em áudio, transcreva a fala e interprete a despesa declarada.
- Se não houver despesa, responda {"error": "sem_despesa"}.
"""


class GeminiError(RuntimeError):
    pass


async def extract_expense(content: InboundContent, *, reference: datetime) -> dict:
    api_key = gemini_api_key()
    if not api_key:
        raise GeminiError("GEMINI_API_KEY ausente")

    model_name = gemini_model()
    prompt = _user_prompt(content, reference)

    def _generate() -> str:
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise GeminiError("Pacote google-generativeai não instalado") from exc

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name,
            system_instruction=SYSTEM_PROMPT,
        )
        parts: list[object] = [prompt]
        if content.data and content.mime_type:
            parts.append({"mime_type": content.mime_type, "data": content.data})
        logger.info(
            "Enviando %s ao %s (%s bytes)",
            content.kind,
            model_name,
            len(content.data or b""),
        )
        response = model.generate_content(
            parts,
            generation_config={"temperature": 0.1, "response_mime_type": "application/json"},
        )
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise GeminiError("Gemini não retornou texto")
        return text

    try:
        raw = await asyncio.to_thread(_generate)
    except GeminiError:
        raise
    except Exception as exc:
        logger.exception("Falha na chamada ao Gemini")
        raise GeminiError("Falha na chamada ao Gemini") from exc

    try:
        return parse_model_json(raw)
    except (ValueError, TypeError) as exc:
        logger.error("Resposta do Gemini não é JSON. Trecho: %s", raw[:1500])
        raise GeminiError("Resposta do Gemini não é JSON") from exc


def parse_model_json(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("JSON ausente")
    payload = json.loads(cleaned[start : end + 1])
    if isinstance(payload, list):
        payload = next((item for item in payload if isinstance(item, dict)), None)
    if not isinstance(payload, dict):
        raise ValueError("JSON sem objeto")
    return payload


def _user_prompt(content: InboundContent, reference: datetime) -> str:
    lines = [
        f"Data de referência (America/Sao_Paulo): {reference.strftime('%d/%m/%Y %H:%M')}.",
        f"Tipo de entrada: {content.kind}.",
    ]
    if content.filename:
        lines.append(f"Nome do ficheiro: {content.filename}")
    if content.text:
        lines.append(f"Texto ou legenda: {content.text}")
    if content.kind == "audio":
        lines.append("Transcreva o áudio e extraia a despesa declarada.")
    else:
        lines.append("Extraia a despesa do material.")
    return "\n".join(lines)
