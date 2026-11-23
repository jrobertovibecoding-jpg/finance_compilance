import os
import sys
import tempfile
from pathlib import Path

# Configura isolamento ANTES de qualquer import do app
_TEST_TMP_DIR = Path(tempfile.mkdtemp(prefix="jrfincloud_tests_"))
os.environ["TESTING"] = "1"
os.environ["ENV"] = "test"
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["TURSO_AUTH_TOKEN"] = ""
os.environ["JWT_SECRET_KEY"] = "chave-secreta-de-teste-com-mais-de-32-caracteres-2026"
os.environ["DATABASE_PATH"] = str(_TEST_TMP_DIR / "onda0_init.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_TMP_DIR / 'onda1_init.db'}"
os.environ["ADMIN_INITIAL_EMAIL"] = ""
os.environ["ADMIN_INITIAL_PASSWORD"] = ""

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from app.core.database import get_connection, init_db
from app.core.security import gerar_hash_senha, login_rate_limiter
from app.db.session import reset_engine_cache
from app.main import app


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """Garante banco SQLite Onda 0 e Onda 1 isolados em tmp_path."""
    db_file = tmp_path / "test_onda0.db"
    db_v2_file = tmp_path / "test_onda1.db"
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("TURSO_DATABASE_URL", "")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "")
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_v2_file}")
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "https://example-account.r2.cloudflarestorage.com")
    monkeypatch.setenv("STORAGE_BUCKET", "jr-fincloud-test")
    monkeypatch.setenv("STORAGE_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("STORAGE_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("STORAGE_REGION", "auto")

    reset_engine_cache()
    login_rate_limiter.reset()
    init_db()

    alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_v2_file}")
    alembic_command.upgrade(alembic_cfg, "head")

    yield {"db_v1": db_file, "db_v2": db_v2_file}

    reset_engine_cache()
    login_rate_limiter.reset()


@pytest.fixture()
def client(isolated_env):
    with TestClient(app) as tc:
        yield tc


@pytest.fixture()
def auth_headers(isolated_env):
    """Cria usuário teste no banco Onda 0 isolado e retorna header Bearer autenticado."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO usuarios (nome, email, senha_hash, perfil, ativo) VALUES (?, ?, ?, ?, ?)",
        ("Gestor Teste", "gestor@jrfincloud.com.br", gerar_hash_senha("SenhaForte@2026"), "MASTER", 1),
    )
    conn.commit()
    conn.close()

    with TestClient(app) as tc:
        resp = tc.post(
            "/api/v1/auth/login",
            json={"email": "gestor@jrfincloud.com.br", "senha": "SenhaForte@2026"},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}
