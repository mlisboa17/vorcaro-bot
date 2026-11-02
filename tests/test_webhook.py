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
os.environ["EVOLUTION_INSTANCE_NAME"] = ""
os.environ["GRUPOS_DESPESAS"] = ""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine, init_db
from main import HEALTH_BODY, UNPARSED_REPLY, app
from models import CONFIRMATION_BODY, CorporateExpense, InboundReceipt, Transaction
from services.financeiro import format_registered_expense, is_financial_query, process_expense_message
from services.parser import IncomingMessage

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
        _clear_tables()

    def test_health(self):
        with TestClient(app) as client:
            health = client.get("/health")
            root = client.get("/")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(root.status_code, 200)
        self.assertEqual(health.json(), HEALTH_BODY)
        self.assertEqual(root.json(), HEALTH_BODY)

    @patch("main.send_text", new_callable=AsyncMock)
    def test_registra_e_confirma(self, send_text: AsyncMock):
        send_text.return_value = True
        with TestClient(app) as client:
            response = client.post("/webhook/evolution", json=_payload("Pago Morevie 1140"))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["results"][0]["parsed"])

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
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "ignored": True})

    def test_evento_fora_de_mensagem_e_ignorado(self):
        payload = _payload("Almoço 85", message_id="CONN1")
        payload["event"] = "connection.update"
        with TestClient(app) as client:
            response = client.post("/webhook/evolution", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "ignored": True})
        with Session(engine) as session:
            self.assertEqual(session.exec(select(Transaction)).all(), [])

    @patch("main.process_expense_message", new_callable=AsyncMock)
    def test_grupo_de_despesas_responde_processing(self, process: AsyncMock):
        payload = _payload("quanto gastamos hoje?", message_id="GRP1")
        payload["event"] = "MESSAGES_UPSERT"
        payload["data"]["key"]["remoteJid"] = "120363411915877448@g.us"
        with patch.dict(os.environ, {"GRUPOS_DESPESAS": "120363411915877448@g.us, 120363000000000001@g.us"}):
            with TestClient(app) as client:
                response = client.post("/webhook/evolution", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "processing"})
        process.assert_awaited()
        with Session(engine) as session:
            self.assertEqual(session.exec(select(Transaction)).all(), [])

    @patch("main.process_expense_message", new_callable=AsyncMock)
    def test_from_me_no_grupo_de_despesas_e_ignorado(self, process: AsyncMock):
        payload = _payload("Almoço 85", message_id="ME1", from_me=True)
        payload["data"]["key"]["remoteJid"] = "120363411915877448@g.us"
        with patch.dict(os.environ, {"GRUPOS_DESPESAS": "120363411915877448@g.us"}):
            with TestClient(app) as client:
                response = client.post("/webhook/evolution", json=payload)
        self.assertEqual(response.json(), {"ok": True, "ignored": True})
        process.assert_not_awaited()

    @patch("main.send_text", new_callable=AsyncMock)
    def test_outro_grupo_permanece_no_fluxo_anterior(self, send_text: AsyncMock):
        send_text.return_value = True
        payload = _payload("Almoço 85", message_id="LOJA1")
        payload["data"]["key"]["remoteJid"] = "120363000000000009@g.us"
        with patch.dict(os.environ, {"GRUPOS_DESPESAS": "120363411915877448@g.us"}):
            with TestClient(app) as client:
                response = client.post("/webhook/evolution", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.json())
        with Session(engine) as session:
            saved = session.exec(select(Transaction)).one()
            corporate = session.exec(select(CorporateExpense)).all()
        self.assertEqual(saved.description, "Almoço")
        self.assertEqual(corporate, [])


def _clear_tables() -> None:
    with Session(engine) as session:
        for model in (Transaction, CorporateExpense, InboundReceipt):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.commit()


class CorporateFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        _clear_tables()

    def test_reconhecimento_de_consulta(self):
        self.assertTrue(is_financial_query("quanto gastamos hoje?"))
        self.assertTrue(is_financial_query("resumo de despesas"))
        self.assertFalse(is_financial_query("Paguei 120 reais de frete da distribuidora hoje"))

    def test_mensagem_registrada_no_formato_do_gerente(self):
        expense = CorporateExpense(
            amount=1140.5,
            occurred_at=datetime(2026, 11, 2, 9, 15),
            category="Suprimentos",
            supplier="Distribuidora Norte",
            unit="Posto VIP",
            document_type="Nota",
            group_jid="120363411915877448@g.us",
        )
        text = format_registered_expense(expense)
        self.assertEqual(
            text,
            "\n".join(
                (
                    "🧾 *DESPESA REGISTRADA*",
                    "━━━━━━━━━━━━━━━━━━",
                    "💰 *Valor:* R$ 1.140,50",
                    "📅 *Data/Hora:* 02/11/2026 09:15",
                    "🏷️ *Categoria:* Suprimentos",
                    "👤 *Favorecido / Fornecedor:* Distribuidora Norte",
                    "🏢 *Unidade Identificada:* Posto VIP",
                    "📄 *Tipo de Documento:* Nota",
                    "━━━━━━━━━━━━━━━━━━",
                    "✅ _Lançamento conferido com sucesso._",
                )
            ),
        )

    async def test_interpretacao_grava_despesa_corporativa(self):
        incoming = IncomingMessage(
            text="Paguei 120 reais de frete da distribuidora hoje",
            remote_jid="120363411915877448@g.us",
            external_id="AUD1",
            from_me=False,
            raw={"key": {"id": "AUD1"}, "message": {"conversation": "Paguei 120 reais de frete"}},
        )
        with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
            extract_expense.return_value = {
                "amount": 120,
                "occurred_at": "2026-11-02T09:15:00",
                "category": "Suprimentos",
                "supplier": "Distribuidora Norte",
                "unit": "Posto VIP",
                "document_type": "Áudio",
                "description": "Frete",
            }
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(incoming)
                await process_expense_message(incoming)

        self.assertEqual(send.await_count, 1)
        body = send.await_args.args[1]
        self.assertIn("R$ 120,00", body)
        self.assertIn("Áudio", body)
        with Session(engine) as session:
            row = session.exec(select(CorporateExpense)).one()
        self.assertEqual(row.amount, 120)
        self.assertEqual(row.supplier, "Distribuidora Norte")
        self.assertEqual(row.group_jid, incoming.remote_jid)

    async def test_consulta_usa_o_banco_e_nao_o_gemini(self):
        moment = datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
        with Session(engine) as session:
            session.add(
                CorporateExpense(
                    amount=50,
                    occurred_at=moment,
                    category="Combustível",
                    supplier="Posto Shell",
                    unit="Real",
                    document_type="PIX",
                    description="diesel",
                    group_jid="120363411915877448@g.us",
                    external_id="SEED1",
                )
            )
            session.commit()
        incoming = IncomingMessage(
            text="quanto gastamos hoje?",
            remote_jid="120363411915877448@g.us",
            external_id="Q1",
            from_me=False,
            raw={"message": {"conversation": "quanto gastamos hoje?"}},
        )
        with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(incoming)
        extract_expense.assert_not_awaited()
        body = send.await_args.args[1]
        self.assertIn("RESUMO DE DESPESAS", body)
        self.assertIn("50,00", body)
        self.assertIn("Combustível", body)


if __name__ == "__main__":
    unittest.main()
