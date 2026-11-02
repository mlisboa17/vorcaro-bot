import atexit
import os
import tempfile
import unittest
from datetime import datetime, timezone
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
from models import CONFIRMATION_BODY, CorporateExpense, Expense, InboundReceipt, Transaction
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
            with self.assertLogs("vorcaro", level="INFO") as logs:
                with TestClient(app) as client:
                    response = client.post("/webhook/evolution", json=payload)
        logged = "\n".join(logs.output)
        self.assertIn("Webhook recebido de: 120363411915877448@g.us", logged)
        self.assertIn("GRUPOS_DESPESAS atual: 120363411915877448@g.us, 120363000000000001@g.us", logged)
        self.assertIn("conversation", logged)
        self.assertNotIn("não está em GRUPOS_DESPESAS", logged)
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
            with self.assertLogs("vorcaro", level="INFO") as logs:
                with TestClient(app) as client:
                    response = client.post("/webhook/evolution", json=payload)
        logged = "\n".join(logs.output)
        self.assertIn(
            "Mensagem ignorada: 120363000000000009@g.us não está em GRUPOS_DESPESAS",
            logged,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.json())
        with Session(engine) as session:
            saved = session.exec(select(Transaction)).one()
            corporate = session.exec(select(Expense)).all()
        self.assertEqual(saved.description, "Almoço")
        self.assertEqual(corporate, [])


def _clear_tables() -> None:
    with Session(engine) as session:
        for model in (Transaction, CorporateExpense, Expense, InboundReceipt):
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
        expense = Expense(
            valor=1140.5,
            data_hora=datetime(2026, 11, 2, 9, 15, tzinfo=ZoneInfo("America/Sao_Paulo")),
            categoria_principal="Operação Empresas (Grupo Lisboa)",
            subcategoria="Mercadoria Revenda (Loja / Food)",
            origem_recurso="Itaú",
            centro_custo_ou_unidade="Posto VIP",
            favorecido="Distribuidora Norte",
            tipo_documento="Cupom",
            status_confirmacao="CONFIRMADO",
            group_jid="120363411915877448@g.us",
        )
        text = format_registered_expense(expense)
        self.assertIn("🧾 *DESPESA REGISTRADA*", text)
        self.assertIn("R$ 1.140,50", text)
        self.assertIn("02/11/2026 09:15", text)
        self.assertIn("Operação Empresas (Grupo Lisboa) » Mercadoria Revenda (Loja / Food)", text)
        self.assertIn("Itaú", text)
        self.assertIn("Posto VIP", text)
        self.assertIn("Distribuidora Norte", text)
        self.assertIn("Cupom", text)
        self.assertIn("Lançamento conferido e salvo na base de dados.", text)

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
                "valor": 120,
                "data_hora": "2026-11-02T09:15:00-03:00",
                "categoria_principal": "Operação Empresas (Grupo Lisboa)",
                "subcategoria": "Mercadoria Revenda (Loja / Food)",
                "origem_recurso": "Bradesco",
                "centro_custo_ou_unidade": "Posto VIP",
                "favorecido": "Distribuidora Norte",
                "tipo_documento": "PIX",
                "status_confirmacao": "CONFIRMADO",
                "transcricao": "Paguei 120 reais de frete pelo Bradesco",
            }
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(incoming)
                await process_expense_message(incoming)

        self.assertEqual(send.await_count, 1)
        body = send.await_args.args[1]
        self.assertIn("R$ 120,00", body)
        self.assertIn("Bradesco", body)
        self.assertIn("Operação Empresas (Grupo Lisboa) » Mercadoria Revenda (Loja / Food)", body)
        with Session(engine) as session:
            row = session.exec(select(Expense)).one()
        self.assertEqual(row.valor, 120)
        self.assertEqual(row.favorecido, "Distribuidora Norte")
        self.assertEqual(row.origem_recurso, "Bradesco")
        self.assertEqual(row.status_confirmacao, "CONFIRMADO")
        self.assertEqual(row.group_jid, incoming.remote_jid)

    async def test_consulta_usa_o_banco_e_nao_o_gemini(self):
        moment = datetime.now(ZoneInfo("America/Sao_Paulo"))
        with Session(engine) as session:
            session.add(
                Expense(
                    valor=50,
                    data_hora=moment,
                    categoria_principal="Filho (Gabriel)",
                    subcategoria="Educação (Marista / Material / Cursos)",
                    origem_recurso="Itaú",
                    centro_custo_ou_unidade="Gabriel",
                    favorecido="Colégio Marista",
                    tipo_documento="PIX",
                    status_confirmacao="CONFIRMADO",
                    group_jid="120363411915877448@g.us",
                    external_id="SEED1",
                )
            )
            session.commit()
        incoming = IncomingMessage(
            text="gastos com gabriel este mês",
            remote_jid="120363411915877448@g.us",
            external_id="Q1",
            from_me=False,
            raw={"message": {"conversation": "gastos com gabriel este mês"}},
        )
        with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(incoming)
        extract_expense.assert_not_awaited()
        body = send.await_args.args[1]
        self.assertIn("CONSULTA DE DESPESAS", body)
        self.assertIn("50,00", body)
        self.assertIn("Filho (Gabriel)", body)
        self.assertIn("Educação (Marista / Material / Cursos)", body)

    async def test_sem_origem_grava_duvida_e_resposta_confirma(self):
        incoming = IncomingMessage(
            text="PIX de 80 reais para uma pessoa",
            remote_jid="120363411915877448@g.us",
            external_id="DUV1",
            from_me=False,
            raw={"key": {"id": "DUV1"}, "message": {"conversation": "PIX de 80 reais"}},
        )
        with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
            extract_expense.return_value = {
                "valor": 80,
                "categoria_principal": "Despesas Pessoais & Outros",
                "subcategoria": "Outros",
                "favorecido": "João",
                "tipo_documento": "PIX",
                "status_confirmacao": "PENDENTE_INFO",
                "duvida_pendente": "Transferência para pessoa física sem contexto.",
            }
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(incoming)
        body = send.await_args.args[1]
        self.assertIn("DESPESA REGISTRADA COM DÚVIDA", body)
        self.assertIn("R$ 80,00", body)
        self.assertIn("João", body)
        self.assertIn("Não identificada", body)
        with Session(engine) as session:
            row = session.exec(select(Expense)).one()
        self.assertEqual(row.status_confirmacao, "PENDENTE_INFO")
        self.assertIn("conta", row.duvida_pendente.lower())

        reply = IncomingMessage(
            text="escola do Gabriel pelo Bradesco",
            remote_jid="120363411915877448@g.us",
            external_id="DUV2",
            from_me=False,
            raw={"message": {"conversation": "escola do Gabriel pelo Bradesco"}},
        )
        with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(reply)
        extract_expense.assert_not_awaited()
        confirmed = send.await_args.args[1]
        self.assertIn("DESPESA REGISTRADA", confirmed)
        self.assertIn("Filho (Gabriel) » Educação (Marista / Material / Cursos)", confirmed)
        self.assertIn("Bradesco", confirmed)
        with Session(engine) as session:
            row = session.exec(select(Expense)).one()
        self.assertEqual(row.status_confirmacao, "CONFIRMADO")
        self.assertIsNone(row.duvida_pendente)
        self.assertEqual(row.origem_recurso, "Bradesco")
        self.assertEqual(row.centro_custo_ou_unidade, "Gabriel")

    async def test_consulta_flats_e_consorcios_no_periodo_todo(self):
        old = datetime(2024, 1, 15, 15, 0, tzinfo=timezone.utc)
        with Session(engine) as session:
            session.add(
                Expense(
                    valor=300,
                    data_hora=old,
                    categoria_principal="Investimentos & Patrimônio",
                    subcategoria="Flats (Condomínio / IPTU / Pool / Reforma)",
                    origem_recurso="Itaú",
                    centro_custo_ou_unidade="Flat",
                    favorecido="Condomínio",
                    tipo_documento="Boleto",
                    group_jid="120363411915877448@g.us",
                    external_id="FLAT1",
                )
            )
            session.add(
                Expense(
                    valor=450.5,
                    data_hora=old,
                    categoria_principal="Investimentos & Patrimônio",
                    subcategoria="Consórcios (Parcelas / Lances)",
                    origem_recurso="Santander",
                    favorecido="Consórcio",
                    tipo_documento="Boleto",
                    group_jid="120363411915877448@g.us",
                    external_id="CONS1",
                )
            )
            session.commit()

        async def ask(text: str) -> str:
            incoming = IncomingMessage(
                text=text,
                remote_jid="120363411915877448@g.us",
                external_id=text,
                from_me=False,
                raw={"message": {"conversation": text}},
            )
            with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
                with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                    send.return_value = True
                    await process_expense_message(incoming)
            extract_expense.assert_not_awaited()
            return send.await_args.args[1]

        flats = await ask("total de flats")
        self.assertIn("300,00", flats)
        self.assertIn("Flats (Condomínio / IPTU / Pool / Reforma)", flats)
        self.assertNotIn("450,50", flats)

        consorcios = await ask("quanto paguei de consórcios?")
        self.assertIn("450,50", consorcios)
        self.assertIn("Consórcios (Parcelas / Lances)", consorcios)
        self.assertNotIn("300,00", consorcios)


if __name__ == "__main__":
    unittest.main()
