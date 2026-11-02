import atexit
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

if "database" not in sys.modules:
    _DB_DIR = tempfile.TemporaryDirectory()
    _DB_FILE = Path(_DB_DIR.name) / "vorcaro.db"
    os.environ["DATABASE_URL"] = "sqlite:///" + _DB_FILE.resolve().as_posix()

from sqlmodel import Session, select

from database import engine, init_db
from models import Expense, InboundReceipt
from services.financeiro import process_expense_message
from services.parser import IncomingMessage

atexit.register(engine.dispose)

_GROUP = "120363411915877448@g.us"
_SP = ZoneInfo("America/Sao_Paulo")


def _clear() -> None:
    with Session(engine) as session:
        for model in (Expense, InboundReceipt):
            for row in session.exec(select(model)).all():
                session.delete(row)
        session.commit()


def _message(text: str, external_id: str, raw: dict | None = None) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        remote_jid=_GROUP,
        external_id=external_id,
        from_me=False,
        raw=raw
        or {
            "key": {"id": external_id, "remoteJid": _GROUP},
            "message": {"conversation": text},
        },
    )


def _extracted(valor: float, when: str, favorecido: str) -> dict:
    return {
        "valor": valor,
        "data_hora": when,
        "categoria_principal": "Despesas Pessoais & Outros",
        "subcategoria": "Alimentação Pessoal",
        "favorecido": favorecido,
        "tipo_documento": "PIX",
        "status_confirmacao": "CONFIRMADO",
    }


class PendingFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        _clear()

    async def _launch(self, external_id: str, valor: float, when: str, favorecido: str) -> int:
        incoming = _message(f"PIX de {valor} para {favorecido}", external_id)
        with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
            extract_expense.return_value = _extracted(valor, when, favorecido)
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(incoming)
        body = send.await_args.args[1]
        self.assertIn("DESPESA REGISTRADA COM DÚVIDA", body)
        self.assertIn("Informe a conta de origem", body)
        with Session(engine) as session:
            row = session.exec(select(Expense).where(Expense.external_id == external_id)).one()
            self.assertIn(f"#{row.id}", body)
            return row.id

    async def test_correcao_sem_reply_atualiza_a_ultima_pendencia(self):
        first = await self._launch("P1", 10, "2026-10-01T10:00:00-03:00", "Ana")
        last = await self._launch("P2", 20, "2026-10-02T11:00:00-03:00", "Bruno")

        correction = _message("foi pelo Itaú", "P3")
        with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(correction)
        extract_expense.assert_not_awaited()
        body = send.await_args.args[1]
        self.assertIn("Lançamento conferido e salvo na base de dados.", body)
        self.assertIn("Itaú", body)
        self.assertIn("Bruno", body)

        with Session(engine) as session:
            older = session.get(Expense, first)
            newer = session.get(Expense, last)
        self.assertEqual(older.status_confirmacao, "PENDENTE_INFO")
        self.assertIsNone(older.origem_recurso)
        self.assertEqual(newer.status_confirmacao, "CONFIRMADO")
        self.assertEqual(newer.origem_recurso, "Itaú")
        self.assertIsNone(newer.duvida_pendente)

    async def test_reply_atualiza_a_primeira_pendencia(self):
        first = await self._launch("R1", 10, "2026-10-01T10:00:00-03:00", "Ana")
        second = await self._launch("R2", 20, "2026-10-02T11:00:00-03:00", "Bruno")
        reply = _message(
            "foi pelo Bradesco",
            "R3",
            raw={
                "key": {"id": "R3", "remoteJid": _GROUP},
                "message": {
                    "extendedTextMessage": {
                        "text": "foi pelo Bradesco",
                        "contextInfo": {
                            "stanzaId": "WAMID-QUE-NAO-EXISTE",
                            "quotedMessage": {
                                "conversation": f"⚠️ *DESPESA REGISTRADA COM DÚVIDA*\n🔖 *Lançamento:* #{first}"
                            },
                        },
                    }
                },
            },
        )
        with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(reply)
        extract_expense.assert_not_awaited()
        body = send.await_args.args[1]
        self.assertIn("Ana", body)
        self.assertIn("Bradesco", body)
        self.assertNotIn("Bruno", body)

        with Session(engine) as session:
            cited = session.get(Expense, first)
            other = session.get(Expense, second)
        self.assertEqual(cited.status_confirmacao, "CONFIRMADO")
        self.assertEqual(cited.origem_recurso, "Bradesco")
        self.assertEqual(other.status_confirmacao, "PENDENTE_INFO")
        self.assertIsNone(other.origem_recurso)

    async def test_virada_de_fuso_conta_outubro_e_separa_pendente(self):
        stored = datetime(2026, 11, 1, 2, 55, tzinfo=timezone.utc)
        with Session(engine) as session:
            session.add(
                Expense(
                    valor=70,
                    data_hora=stored,
                    categoria_principal="Despesas Pessoais & Outros",
                    subcategoria="Alimentação Pessoal",
                    origem_recurso="Nubank",
                    favorecido="Padaria",
                    tipo_documento="PIX",
                    status_confirmacao="CONFIRMADO",
                    group_jid=_GROUP,
                    external_id="TZ1",
                )
            )
            session.add(
                Expense(
                    valor=15,
                    data_hora=stored,
                    categoria_principal="Despesas Pessoais & Outros",
                    subcategoria="Outros",
                    favorecido="Alguém",
                    tipo_documento="PIX",
                    status_confirmacao="PENDENTE_INFO",
                    duvida_pendente="Informe a conta de origem (Bradesco, Itaú, Cartão, etc.)",
                    group_jid=_GROUP,
                    external_id="TZ2",
                )
            )
            session.commit()

        october = datetime(2026, 10, 31, 23, 50, tzinfo=_SP)
        with patch("services.financeiro.now_local", return_value=october):
            with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
                with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                    send.return_value = True
                    await process_expense_message(_message("gastos este mês", "TZQ"))
        extract_expense.assert_not_awaited()
        body = send.await_args.args[1]
        self.assertIn("R$ 70,00", body)
        self.assertIn("31/10/2026 23:55", body)
        self.assertIn("Existem 1 lançamentos pendentes de confirmação neste período que não foram somados.", body)
        self.assertNotIn("15,00", body)

        november = datetime(2026, 11, 2, 10, 0, tzinfo=_SP)
        with patch("services.financeiro.now_local", return_value=november):
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(_message("gastos este mês", "TZQ2"))
        self.assertNotIn("70,00", send.await_args.args[1])

    async def test_gabriela_nao_mapeia_favorecido_para_gabriel(self):
        incoming = _message("pagamento da Gabriela pelo Bradesco", "GAB1")
        with patch("services.financeiro.extract_expense", new_callable=AsyncMock) as extract_expense:
            extract_expense.return_value = {
                "valor": 200,
                "data_hora": "2026-10-15T09:00:00-03:00",
                "categoria_principal": "Filho (Gabriel)",
                "subcategoria": "Pensão Alimentícia",
                "favorecido": "Gabriel",
                "origem_recurso": "Bradesco",
                "tipo_documento": "PIX",
                "status_confirmacao": "CONFIRMADO",
                "transcricao": "pagamento da Gabriela pelo Bradesco",
            }
            with patch("services.financeiro.send_text", new_callable=AsyncMock) as send:
                send.return_value = True
                await process_expense_message(incoming)
        with Session(engine) as session:
            row = session.exec(select(Expense)).one()
        self.assertEqual(row.favorecido, "Gabriela")
        self.assertNotEqual(row.favorecido, "Gabriel")
        self.assertEqual(row.categoria_principal, "Ex-Cônjuges")
        self.assertEqual(row.subcategoria, "Gabriela (Mãe Gabriel)")
        self.assertEqual(row.origem_recurso, "Bradesco")
        self.assertEqual(row.status_confirmacao, "CONFIRMADO")


if __name__ == "__main__":
    unittest.main()
