"""Plano de contas fechado do Gerente Financeiro."""

import unicodedata

CHART: dict[str, tuple[str, ...]] = {
    "Filho (Gabriel)": (
        "Educação (Marista / Material / Cursos)",
        "Saúde & Terapias",
        "Pensão Alimentícia",
        "Esporte (Natação / Personal)",
        "Alimentação & Lanche",
        "Lazer & Jogos",
        "Vestuário & Calçados",
    ),
    "Ex-Cônjuges": (
        "Gabriela (Mãe Gabriel)",
        "Mirella (Temporária)",
    ),
    "Moradia & Imóveis Pessoais": (
        "Aluguel Residencial",
        "Condomínio",
        "IPTU & Taxas",
        "Utilidades (Neoenergia / Água / Internet)",
        "Manutenção Residencial",
    ),
    "Investimentos & Patrimônio": (
        "Flats (Condomínio / IPTU / Pool / Reforma)",
        "Consórcios (Parcelas / Lances)",
        "Aportes & Imóveis",
    ),
    "Operação Empresas (Grupo Lisboa)": (
        "Combustível / Carga",
        "Mercadoria Revenda (Loja / Food)",
        "Manutenção & Equipamentos",
        "Folha & Pessoal",
        "Utilidades Operacionais",
        "Tributos & Taxas Operacionais",
    ),
    "Despesas Pessoais & Outros": (
        "Alimentação Pessoal",
        "Transporte & Veículos",
        "Saúde Pessoal / Academia",
        "Impostos PF",
        "Outros",
    ),
}

DOCUMENT_TYPES = ("PIX", "Boleto", "TED", "Cartão", "Cupom", "Dinheiro")
COST_CENTERS = (
    "Gabriel",
    "Posto VIP",
    "Posto Real",
    "Casa Caiada",
    "Restaurante",
    "Conveniência",
    "Flat",
    "Pessoal",
)


def fold(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    without = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return without.lower()


def taxonomy_prompt() -> str:
    lines = ["Classifique OBRIGATORIAMENTE em uma categoria e uma subcategoria desta lista:"]
    for index, (principal, subs) in enumerate(CHART.items(), start=1):
        lines.append(f"{index}. {principal}")
        for sub in subs:
            lines.append(f"   - {sub}")
    lines.extend(
        [
            "Regras de classificação:",
            "- Menções a Gabriel, escola, Marista, natação, filho ou despesa infantil: Filho (Gabriel).",
            "- Pagamentos, pensões, acordos ou transferências para Gabriela: Ex-Cônjuges » Gabriela (Mãe Gabriel).",
            "- O mesmo para Mirella: Ex-Cônjuges » Mirella (Temporária).",
            "- Flat, pool de flat ou reforma de flat: Investimentos & Patrimônio » Flats (Condomínio / IPTU / Pool / Reforma).",
            "- Parcela ou lance de consórcio: Investimentos & Patrimônio » Consórcios (Parcelas / Lances).",
            "- Operação do Grupo Lisboa deve indicar a unidade quando aparecer: Posto VIP, Posto Real, Casa Caiada, Restaurante ou Conveniência.",
            "origem_recurso é a conta de onde o dinheiro saiu: Itaú, Bradesco, Santander, Banco do Brasil, Nubank, cartão, dinheiro ou caixa da loja.",
            "Procure 'Conta Debitada', 'Instituição Emissora' ou falas como 'paguei pelo Bradesco' e 'saiu do caixa da loja'.",
            "tipo_documento só pode ser: PIX, Boleto, TED, Cartão, Cupom ou Dinheiro.",
            "Se faltar categoria, unidade ou a origem do dinheiro, use status_confirmacao PENDENTE_INFO e explique em duvida_pendente.",
            "Se estiver completo e inequívoco, use status_confirmacao CONFIRMADO e duvida_pendente null.",
        ]
    )
    return "\n".join(lines)


def canonical_principal(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    folded = fold(value)
    for principal in CHART:
        if fold(principal) == folded:
            return principal
    return None


def canonical_subcategory(value: object, principal: str | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    folded = fold(value)
    options = CHART.get(principal, ()) if principal else tuple(sub for subs in CHART.values() for sub in subs)
    for sub in options:
        if fold(sub) == folded:
            return sub
    for sub in options:
        if folded and (folded in fold(sub) or fold(sub) in folded):
            return sub
    return None


def principal_of(subcategory: str) -> str | None:
    for principal, subs in CHART.items():
        if subcategory in subs:
            return principal
    return None


def canonical_document(value: object, default: str = "Cupom") -> str:
    if not isinstance(value, str):
        return default
    folded = fold(value)
    for option in DOCUMENT_TYPES:
        if fold(option) == folded:
            return option
    if "pix" in folded:
        return "PIX"
    if "boleto" in folded:
        return "Boleto"
    if "ted" in folded:
        return "TED"
    if "cartao" in folded or "credito" in folded or "debito" in folded:
        return "Cartão"
    if "dinheiro" in folded or "especie" in folded or "caixa" in folded:
        return "Dinheiro"
    if "cupom" in folded or "nota" in folded or "recibo" in folded:
        return "Cupom"
    return default


def canonical_center(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    folded = fold(value)
    for option in COST_CENTERS:
        if fold(option) == folded:
            return option
    aliases = {
        "vip": "Posto VIP",
        "posto vip": "Posto VIP",
        "real": "Posto Real",
        "posto real": "Posto Real",
        "casa caiada": "Casa Caiada",
        "restaurante": "Restaurante",
        "conveniencia": "Conveniência",
        "flat": "Flat",
        "flats": "Flat",
        "gabriel": "Gabriel",
        "pessoal": "Pessoal",
    }
    return aliases.get(folded)


def hint_from_text(text: str) -> tuple[str | None, str | None, str | None]:
    """Aplica as regras fixas de pessoa, flat e consórcio sobre o texto original."""
    folded = fold(text)
    if _has_word(folded, "gabriela"):
        return "Ex-Cônjuges", "Gabriela (Mãe Gabriel)", None
    if _has_word(folded, "mirella"):
        return "Ex-Cônjuges", "Mirella (Temporária)", None
    if any(_has_word(folded, word) for word in ("gabriel", "marista", "natacao", "filho", "filha")):
        sub = None
        if any(word in folded for word in ("marista", "escola", "colegio", "curso", "material")):
            sub = "Educação (Marista / Material / Cursos)"
        elif "natacao" in folded or "personal" in folded or "esporte" in folded:
            sub = "Esporte (Natação / Personal)"
        elif "pensao" in folded:
            sub = "Pensão Alimentícia"
        return "Filho (Gabriel)", sub, "Gabriel"
    if "flat" in folded:
        return (
            "Investimentos & Patrimônio",
            "Flats (Condomínio / IPTU / Pool / Reforma)",
            "Flat",
        )
    if "consorcio" in folded:
        return (
            "Investimentos & Patrimônio",
            "Consórcios (Parcelas / Lances)",
            None,
        )
    return None, None, None


def bank_from_text(text: str) -> str | None:
    folded = fold(text)
    banks = (
        ("banco do brasil", "Banco do Brasil"),
        ("bradesco", "Bradesco"),
        ("santander", "Santander"),
        ("nubank", "Nubank"),
        ("itau", "Itaú"),
        ("caixa da loja", "Caixa da loja"),
        ("dinheiro", "Dinheiro"),
    )
    for needle, label in banks:
        if needle in folded:
            return label
    return None


def _has_word(folded: str, word: str) -> bool:
    import re

    return re.search(rf"\b{re.escape(word)}\b", folded) is not None
