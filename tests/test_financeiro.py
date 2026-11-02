import base64
import os
import unittest
from unittest.mock import patch

from services.gemini_client import parse_model_json
from services.media import decode_base64, find_media, normalize_mime
from services.settings import is_expense_group


class MediaTests(unittest.TestCase):
    def test_pdf_por_mimetype_e_por_nome(self):
        encoded = base64.b64encode(b"%PDF-1.4 " + b"A" * 40).decode()
        by_mime = find_media(
            {
                "message": {
                    "documentMessage": {"mimetype": "application/pdf", "fileName": "boleto.pdf"},
                    "base64": encoded,
                }
            }
        )
        by_name = find_media(
            {
                "message": {
                    "documentMessage": {
                        "mimetype": "application/octet-stream",
                        "fileName": "comprovante.PDF",
                    }
                }
            }
        )
        self.assertIsNotNone(by_mime)
        self.assertEqual(by_mime.kind, "pdf")
        self.assertEqual(by_mime.mime_type, "application/pdf")
        self.assertTrue(decode_base64(by_mime.inline_base64).startswith(b"%PDF"))
        self.assertEqual(by_name.kind, "pdf")
        self.assertIsNone(
            find_media({"message": {"documentMessage": {"mimetype": "application/msword", "fileName": "a.docx"}}})
        )

    def test_imagem_e_audio(self):
        image = find_media(
            {"message": {"imageMessage": {"mimetype": "image/png", "caption": "PIX mercado"}}}
        )
        audio = find_media(
            {
                "message": {
                    "audioMessage": {"mimetype": "audio/ogg; codecs=opus", "ptt": True},
                    "base64": base64.b64encode(b"OggSaudio-bytes!!").decode(),
                }
            }
        )
        wrapped = find_media(
            {
                "message": {
                    "documentWithCaptionMessage": {
                        "message": {
                            "documentMessage": {"mimetype": "application/pdf", "fileName": "nota.pdf"}
                        }
                    }
                }
            }
        )
        self.assertEqual(image.kind, "image")
        self.assertEqual(image.mime_type, "image/png")
        self.assertEqual(image.caption, "PIX mercado")
        self.assertEqual(audio.kind, "audio")
        self.assertEqual(normalize_mime(audio.mime_type, "audio"), "audio/ogg")
        self.assertEqual(wrapped.kind, "pdf")

    def test_base64_com_prefixo_data_url(self):
        raw = base64.b64encode(b"imagem-jpeg-valida").decode()
        decoded = decode_base64(f"data:image/jpeg;base64,{raw}")
        self.assertEqual(decoded, b"imagem-jpeg-valida")


class GeminiJsonTests(unittest.TestCase):
    def test_json_com_cerca_markdown(self):
        payload = parse_model_json(
            """```json
            {"amount": 120.5, "category": "Suprimentos"}
            ```"""
        )
        self.assertEqual(payload["amount"], 120.5)
        self.assertEqual(payload["category"], "Suprimentos")


class GroupRoutingTests(unittest.TestCase):
    def test_lista_de_jids(self):
        with patch.dict(
            os.environ,
            {"GRUPOS_DESPESAS": "120363411915877448@g.us, 120363000000000001@g.us"},
        ):
            self.assertTrue(is_expense_group("120363411915877448@g.us"))
            self.assertTrue(is_expense_group("120363000000000001@g.us"))
            self.assertFalse(is_expense_group("120363999999999999@g.us"))
            self.assertFalse(is_expense_group("5511999999999@s.whatsapp.net"))


if __name__ == "__main__":
    unittest.main()
