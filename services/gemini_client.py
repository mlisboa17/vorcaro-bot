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
from services.taxonomia import taxonomy_prompt

logger = logging.getLogger("vorcaro.gemini")

SYSTEM_PROMPT = f"""Você é o Gerente Financeiro Vorcaro.
Extraia uma despesa de texto, PDF, imagem ou áudio.
Responda somente com um objeto JSON, sem markdown:
{{
  "valor": 0.0,
  "data_hora": "YYYY-MM-DDTHH:MM:SS-03:00",
  "categoria_principal": "categoria exata da taxonomia",
  "subcategoria": "subcategoria exata da taxonomia",
  "origem_recurso": "banco, cartão, dinheiro ou caixa, ou null",
  "centro_custo_ou_unidade": "Gabriel, Posto VIP, Posto Real, Casa Caiada, Restaurante, Conveniência, Flat, Pessoal ou null",
  "favorecido": "nome ou razão social, ou null",
  "tipo_documento": "PIX, Boleto, TED, Cartão, Cupom ou Dinheiro",
  "status_confirmacao": "CONFIRMADO ou PENDENTE_INFO",
  "duvida_pendente": "pergunta curta ou null",
  "transcricao": "texto lido ou fala transcrita"
}}
{taxonomy_prompt()}
- valor é o número em reais, com ponto decimal.
- data_hora é o momento identificado, no fuso America/Sao_Paulo. Sem data, use a data de referência.
- Copie categoria_principal e subcategoria exatamente como estão na taxonomia.
- origem_recurso é obrigatória quando o comprovante ou a fala mostrar a conta debitada.
- Em áudio, transcreva e interprete a despesa.
- Sem despesa identificável, responda {{"error": "sem_despesa"}}.
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
