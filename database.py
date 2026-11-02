import os
from pathlib import Path

from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine, select

load_dotenv()

# No Docker/Easypanel o banco fica em /app/data/vorcaro.db (volume persistente).
DEFAULT_DATABASE_URL = "sqlite:////app/data/vorcaro.db"


def sqlite_path(url: str) -> Path | None:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if not raw or raw.startswith(":memory:"):
        return None
    return Path(raw)


def _prepare_url(url: str) -> str:
    path = sqlite_path(url)
    if path is not None and str(path.parent) not in {"", "."}:
        path.parent.mkdir(parents=True, exist_ok=True)
    return url


def resolve_database_url() -> str:
    configured = os.getenv("DATABASE_URL")
    if configured:
        return _prepare_url(configured)
    # Container Linux com WORKDIR /app. No Windows local, usa ./data.
    if os.name != "nt" and Path("/app").is_dir():
        return _prepare_url(DEFAULT_DATABASE_URL)
    fallback = Path("data") / "vorcaro.db"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + fallback.resolve().as_posix()


DATABASE_URL = resolve_database_url()
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)


def ensure_data_dir() -> None:
    """Cria o diretório do SQLite antes de abrir o banco.

    No container o caminho é /app/data. Um volume montado pelo Easypanel
    pode chegar vazio, então a pasta precisa existir de novo na subida.
    """
    directories: list[Path] = []
    db_path = sqlite_path(DATABASE_URL)
    if db_path is not None:
        directories.append(db_path.parent)
    if os.name != "nt":
        directories.append(Path("/app/data"))

    seen: set[str] = set()
    for directory in directories:
        key = str(directory)
        if not key or key in {".", ""} or key in seen:
            continue
        seen.add(key)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o777)
        except OSError:
            pass


def ensure_expense_columns() -> None:
    """Acrescenta colunas novas em bases SQLite já criadas. create_all não altera tabela existente."""
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.begin() as connection:
        tables = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'expenses'"
        ).fetchall()
        if not tables:
            return
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(expenses)").fetchall()}
        if "alert_message_id" not in columns:
            connection.exec_driver_sql("ALTER TABLE expenses ADD COLUMN alert_message_id VARCHAR")


def init_db() -> None:
    from models import CONFIRMATION_BODY, CONFIRMATION_TEMPLATE_KEY, MessageTemplate

    ensure_data_dir()
    SQLModel.metadata.create_all(engine)
    ensure_expense_columns()
    with Session(engine) as session:
        existing = session.exec(
            select(MessageTemplate).where(MessageTemplate.key == CONFIRMATION_TEMPLATE_KEY)
        ).first()
        if existing is None:
            session.add(MessageTemplate(key=CONFIRMATION_TEMPLATE_KEY, body=CONFIRMATION_BODY))
            session.commit()
