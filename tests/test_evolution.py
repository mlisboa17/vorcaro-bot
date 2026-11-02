import os
import unittest
from unittest.mock import patch

from services.evolution import destination_for, send_text


class _Response:
    def __init__(self, status_code: int = 200, text: str = "ok"):
        self.status_code = status_code
        self.text = text


class _Client:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.response = kwargs.get("response") or _Response()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, json, headers):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response


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
        self.assertIsNone(delivered.message_id)
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["url"], "http://evolution.local:8080/message/sendText/vorcaro")
        self.assertEqual(
            call["json"],
            {"number": "5511999999999", "text": "ok", "delay": 1000},
        )
        self.assertEqual(call["headers"]["apikey"], "secret")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")

    async def test_send_text_devolve_id_da_mensagem(self):
        client = _Client()
        client.response = _Response(text='{"key": {"id": "ALERT9"}}')
        with patch.dict(
            os.environ,
            {
                "EVOLUTION_API_URL": "http://evolution.local:8080",
                "EVOLUTION_API_KEY": "secret",
                "EVOLUTION_INSTANCE": "vorcaro",
            },
        ):
            with patch("services.evolution.httpx.AsyncClient", return_value=client):
                delivered = await send_text("120363411915877448@g.us", "dúvida")
        self.assertTrue(delivered)
        self.assertEqual(delivered.message_id, "ALERT9")

    async def test_status_de_erro_registra_o_corpo(self):
        client = _Client()
        client.response = _Response(status_code=400, text='{"error":"number invalid"}')
        with patch.dict(
            os.environ,
            {
                "EVOLUTION_API_URL": "http://evolution.local:8080",
                "EVOLUTION_API_KEY": "secret",
                "EVOLUTION_INSTANCE_NAME": "vorcaro",
                "EVOLUTION_INSTANCE": "",
            },
        ):
            with patch("services.evolution.httpx.AsyncClient", return_value=client):
                with self.assertLogs("vorcaro.evolution", level="ERROR") as logs:
                    delivered = await send_text("120363411915877448@g.us", "resumo")

        self.assertFalse(delivered)
        self.assertIn("400", "\n".join(logs.output))
        self.assertIn("number invalid", "\n".join(logs.output))
        self.assertEqual(client.calls[0]["json"]["number"], "120363411915877448@g.us")

    async def test_sem_configuracao_nao_dispara(self):
        with patch.dict(
            os.environ,
            {"EVOLUTION_API_URL": "", "EVOLUTION_API_KEY": "", "EVOLUTION_INSTANCE": ""},
        ):
            delivered = await send_text("5511999999999@s.whatsapp.net", "ok")
        self.assertFalse(delivered)


if __name__ == "__main__":
    unittest.main()
