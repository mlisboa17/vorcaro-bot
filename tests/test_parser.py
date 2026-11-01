import unittest
from datetime import date

from services.parser import extract_messages, parse_expense

TODAY = date(2026, 11, 1)


class ParserTests(unittest.TestCase):
    def test_almoco(self):
        parsed = parse_expense("Almoço 85", today=TODAY)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.amount, 85)
        self.assertEqual(parsed.description, "Almoço")
        self.assertEqual(parsed.status, "PENDING")
        self.assertEqual(parsed.category, "Outros")
        self.assertEqual(parsed.due_date, TODAY)

    def test_posto(self):
        parsed = parse_expense("Posto 250", today=TODAY)
        self.assertEqual(parsed.amount, 250)
        self.assertEqual(parsed.description, "Posto")
        self.assertEqual(parsed.status, "PENDING")

    def test_pago_morevie(self):
        parsed = parse_expense("Pago Morevie 1140", today=TODAY)
        self.assertEqual(parsed.amount, 1140)
        self.assertEqual(parsed.description, "Morevie")
        self.assertEqual(parsed.status, "PAID")
        self.assertEqual(parsed.category, "Outros")

    def test_paguei_com_centavos(self):
        parsed = parse_expense("Paguei mercado 152,30", today=TODAY)
        self.assertEqual(parsed.amount, 152.30)
        self.assertEqual(parsed.description, "mercado")
        self.assertEqual(parsed.status, "PAID")

    def test_milhar_brasileiro_e_categoria(self):
        parsed = parse_expense("R$ 1.140,50 cartão nubank", today=TODAY)
        self.assertEqual(parsed.amount, 1140.50)
        self.assertEqual(parsed.category, "Cartão")
        self.assertIn("cartão", parsed.description)

    def test_vencimento_e_imobiliario(self):
        parsed = parse_expense("Aluguel 2500 vence 10/11", today=TODAY)
        self.assertEqual(parsed.amount, 2500)
        self.assertEqual(parsed.description, "Aluguel")
        self.assertEqual(parsed.category, "Imobiliário")
        self.assertEqual(parsed.due_date, date(2026, 11, 10))
        self.assertEqual(parsed.status, "PENDING")

    def test_vence_dia(self):
        parsed = parse_expense("Internet 120 vence dia 5", today=TODAY)
        self.assertEqual(parsed.amount, 120)
        self.assertEqual(parsed.description, "Internet")
        self.assertEqual(parsed.category, "Fixos")
        self.assertEqual(parsed.due_date, date(2026, 11, 5))

    def test_pagamento_nao_marca_como_pago(self):
        parsed = parse_expense("pagamento 80", today=TODAY)
        self.assertEqual(parsed.status, "PENDING")
        self.assertEqual(parsed.description, "pagamento")

    def test_consorcio(self):
        parsed = parse_expense("Consórcio 300", today=TODAY)
        self.assertEqual(parsed.category, "Consórcios")
        self.assertEqual(parsed.amount, 300)

    def test_sem_valor(self):
        self.assertIsNone(parse_expense("somente texto", today=TODAY))
        self.assertIsNone(parse_expense("   ", today=TODAY))

    def test_payload_conversation(self):
        payload = {
            "event": "messages.upsert",
            "data": {
                "key": {
                    "remoteJid": "5511999999999@s.whatsapp.net",
                    "fromMe": False,
                    "id": "ABC",
                },
                "message": {"conversation": "Posto 250"},
            },
        }
        parsed = parse_expense(payload, today=TODAY)
        self.assertEqual(parsed.amount, 250)
        messages = extract_messages(payload)
        self.assertEqual(messages[0].remote_jid, "5511999999999@s.whatsapp.net")
        self.assertEqual(messages[0].external_id, "ABC")
        self.assertFalse(messages[0].from_me)

    def test_payload_extended_text(self):
        payload = {
            "data": {
                "key": {"remoteJid": "5511888888888@s.whatsapp.net", "fromMe": False, "id": "Z"},
                "message": {"extendedTextMessage": {"text": "Almoço 85"}},
            }
        }
        messages = extract_messages(payload)
        self.assertEqual(messages[0].text, "Almoço 85")

    def test_from_me_nao_entra_no_parse_de_payload(self):
        payload = {
            "data": {
                "key": {"remoteJid": "5511999999999@s.whatsapp.net", "fromMe": True, "id": "ME"},
                "message": {"conversation": "Almoço 85"},
            }
        }
        self.assertIsNone(parse_expense(payload, today=TODAY))
        self.assertTrue(extract_messages(payload)[0].from_me)


if __name__ == "__main__":
    unittest.main()
