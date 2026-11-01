import atexit
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

_DB_DIR = tempfile.TemporaryDirectory()
_DB_FILE = Path(_DB_DIR.name) / "vorcaro.db"
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_FILE.resolve().as_posix()
os.environ["EVOLUTION_API_URL"] = ""
os.environ["EVOLUTION_API_KEY"] = ""
os.environ["EVOLUTION_INSTANCE"] = ""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine, init_db
from main import UNPARSED_REPLY, app
from models import CONFIRMATION_BODY, Transaction

atexit.register(engine.dispose)


def _payload(text: str, message_id: str = "MSG1", from_me: bool = False, extended: bool = False):
    message = (
        {"extendedTextMessage": {"text": text}}
        if extended
        else {"conversation": text}
    )
    return {
        "event": "messages.upsert",
        "instance": "vorcaro",
        "data": {
            "key": {
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": from_me,
                "id": message_id,
            },
            "message": message,
        },
    }


class WebhookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with Session(engine) as session:
            for row in session.exec(select(Transaction)).all():
                session.delete(row)
            session.commit()

    def test_health(self):
        with TestClient(app) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("main.send_text", new_callable=AsyncMock)
    def test_registra_e_confirma(self, send_text: AsyncMock):
        send_text.return_value = True
        with TestClient(app) as client:
            response = client.post("/webhook/evolution", json=_payload("Pago Morevie 1140"))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["results"][0]["parsed"])
        self.assertTrue(body["results"][0]["delivered"])

        with Session(engine) as session:
            saved = session.exec(select(Transaction)).one()
        self.assertEqual(saved.amount, 1140)
        self.assertEqual(saved.description, "Morevie")
        self.assertEqual(saved.status, "PAID")
        self.assertEqual(saved.category, "Outros")
        self.assertEqual(saved.source, "whatsapp")
        self.assertEqual(saved.raw_text, "Pago Morevie 1140")
        self.assertEqual(saved.external_id, "MSG1")
        self.assertEqual(saved.due_date, datetime.now(ZoneInfo("America/Sao_Paulo")).date())

        send_text.assert_awaited_once_with(
            "5511999999999@s.whatsapp.net",
            CONFIRMATION_BODY.format(amount=1140, description="Morevie", status="PAID"),
        )

    @patch("main.send_text", new_callable=AsyncMock)
    def test_extended_text_e_duplicata(self, send_text: AsyncMock):
        send_text.return_value = True
        with TestClient(app) as client:
            first = client.post(
                "/webhook/evolution",
                json=_payload("Posto 250", message_id="DUP", extended=True),
            )
            second = client.post(
                "/webhook/evolution",
                json=_payload("Posto 250", message_id="DUP", extended=True),
            )

        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.json()["results"][0]["duplicate"])
        with Session(engine) as session:
            rows = session.exec(select(Transaction)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(send_text.await_count, 1)

    @patch("main.send_text", new_callable=AsyncMock)
    def test_ignora_mensagem_propria(self, send_text: AsyncMock):
        with TestClient(app) as client:
            response = client.post(
                "/webhook/evolution",
                json=_payload("Almoço 85", from_me=True),
            )
        self.assertEqual(response.json(), {"ok": True, "ignored": True})
        send_text.assert_not_awaited()
        with Session(engine) as session:
            self.assertEqual(session.exec(select(Transaction)).all(), [])

    @patch("main.send_text", new_callable=AsyncMock)
    def test_texto_sem_valor_nao_grava(self, send_text: AsyncMock):
        send_text.return_value = True
        with TestClient(app) as client:
            response = client.post("/webhook/evolution", json=_payload("oi"))
        self.assertFalse(response.json()["results"][0]["parsed"])
        send_text.assert_awaited_once_with("5511999999999@s.whatsapp.net", UNPARSED_REPLY)
        with Session(engine) as session:
            self.assertEqual(session.exec(select(Transaction)).all(), [])

    def test_json_invalido(self):
        with TestClient(app) as client:
            response = client.post(
                "/webhook/evolution",
                content=b"nao-json",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
