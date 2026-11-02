import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EvolutionConfig:
    base_url: str
    api_key: str
    instance: str

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.instance)


def evolution_config() -> EvolutionConfig:
    return EvolutionConfig(
        base_url=os.getenv("EVOLUTION_API_URL", "").strip().rstrip("/"),
        api_key=os.getenv("EVOLUTION_API_KEY", "").strip(),
        instance=(
            os.getenv("EVOLUTION_INSTANCE_NAME") or os.getenv("EVOLUTION_INSTANCE") or ""
        ).strip(),
    )


def expense_group_jids() -> set[str]:
    raw = os.getenv("GRUPOS_DESPESAS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_expense_group(remote_jid: str) -> bool:
    jid = remote_jid.strip()
    return bool(jid) and jid in expense_group_jids()


def gemini_api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip()


def gemini_model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip() or "gemini-1.5-flash"
