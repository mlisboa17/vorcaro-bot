import os
import unittest
from unittest.mock import patch

from services.evolution import destination_for, send_text


class _Response:
    def raise_for_status(self):
        return None


class _Client:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, json, headers):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _Response()


class EvolutionTests(unittest.IsolatedAsyncioTestCase):
    def test_destino_de_jid(self):
        self.assertEqual(destination_for("5511999999999@s.whatsapp.net"), "5511999999999")
        self.assertEqual(
            destination_for("1203630@g.us"),
            "1203630@g.us",
        )

    async def test_send_text_usa_endpoint_v2(self):
        client = _Client()
        with patch.dict(
            os.environ,
            {
                "EVOLUTION_API_URL": "http://evolution.local:8080/",
                "EVOLUTION_API_KEY": "secret",
                "EVOLUTION_INSTANCE": "vorcaro",
            },
        ):
            with patch("services.evolution.httpx.AsyncClient", return_value=client):
                delivered = await send_text("5511999999999@s.whatsapp.net", "ok")

        self.assertTrue(delivered)
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["url"], "http://evolution.local:8080/message/sendText/vorcaro")
        self.assertEqual(call["json"], {"number": "5511999999999", "text": "ok"})
        self.assertEqual(call["headers"], {"apikey": "secret"})

    async def test_sem_configuracao_nao_dispara(self):
        with patch.dict(
            os.environ,
            {"EVOLUTION_API_URL": "", "EVOLUTION_API_KEY": "", "EVOLUTION_INSTANCE": ""},
        ):
            delivered = await send_text("5511999999999@s.whatsapp.net", "ok")
        self.assertFalse(delivered)


if __name__ == "__main__":
    unittest.main()
