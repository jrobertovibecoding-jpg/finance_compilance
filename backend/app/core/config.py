import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

if os.getenv("TESTING", "").strip().lower() not in ("1", "true"):
    load_dotenv(BASE_DIR / ".env", override=False)

ENV = os.getenv("ENV", "development").strip().lower()

# Procura o banco no caminho relativo local, na raiz do backend ou via variável de ambiente
_default_legacy_db = (
    BASE_DIR / "data" / "bimer.db"
    if BASE_DIR.parent == Path(BASE_DIR.anchor)
    else BASE_DIR.parent / "database" / "bimer.db"
)

candidatos_db = [
    os.getenv("DATABASE_PATH"),
    str(BASE_DIR / "data" / "bimer.db"),
    str(BASE_DIR.parent / "database" / "bimer.db"),
    str(BASE_DIR / "bimer.db"),
    str(BASE_DIR / "database" / "bimer.db"),
]

DATABASE_PATH = next(
    (p for p in candidatos_db if p and Path(p).exists()),
    str(_default_legacy_db),
)

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if origin.strip()
]
CORS_ORIGIN_REGEX = os.getenv(
    "CORS_ORIGIN_REGEX",
    r"^https://.*\.(vercel\.app|jrfincloud\.com\.br)$",
).strip()

API_PREFIX = "/api/v1"
API_V2_PREFIX = "/api/v2"
PROJECT_NAME = os.getenv("PROJECT_NAME", "JR FinCloud - Plataforma de Gestão Financeira").strip()
VERSION = "2.0.0"

# Banco Onda 1 (PostgreSQL SaaS Multi-Tenant) e Turso/SQLite (Onda 0)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

# Segurança & Sessão
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true" if ENV == "production" else "false").strip().lower() == "true"

# Bootstrap seguro de administrador inicial (opcional, apenas se tabela vazia)
ADMIN_INITIAL_EMAIL = os.getenv("ADMIN_INITIAL_EMAIL", "").strip().lower()
ADMIN_INITIAL_PASSWORD = os.getenv("ADMIN_INITIAL_PASSWORD", "").strip()

# Mensageria WhatsApp & Links Públicos
WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL", "").strip()
WHATSAPP_API_TOKEN = os.getenv("WHATSAPP_API_TOKEN", "").strip()
WHATSAPP_DEFAULT_NUMBER = os.getenv("WHATSAPP_DEFAULT_NUMBER", "").strip()
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "https://jrfincloud.com.br").strip()
COMPANY_DISPLAY_NAME = os.getenv("COMPANY_DISPLAY_NAME", "JR FinCloud").strip()

# Parâmetros Bancários CNAB 240 / SISPAG Itaú (sem hardcoding de cliente)
EMPRESA_CNPJ = os.getenv("EMPRESA_CNPJ", "").strip()
EMPRESA_RAZAO_SOCIAL = os.getenv("EMPRESA_RAZAO_SOCIAL", "").strip()
EMPRESA_CIDADE = os.getenv("EMPRESA_CIDADE", "SALVADOR").strip()
EMPRESA_CEP = os.getenv("EMPRESA_CEP", "40000000").strip()
EMPRESA_UF = os.getenv("EMPRESA_UF", "BA").strip()
ITAU_AGENCIA = os.getenv("ITAU_AGENCIA", "").strip()
ITAU_CONTA = os.getenv("ITAU_CONTA", "").strip()
ITAU_DAC = os.getenv("ITAU_DAC", "").strip()

# Object Storage (Cloudflare R2 / AWS S3)
STORAGE_ENDPOINT_URL = os.getenv("STORAGE_ENDPOINT_URL", "").strip()
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "jr-fincloud-docs").strip()
STORAGE_ACCESS_KEY_ID = os.getenv("STORAGE_ACCESS_KEY_ID", "").strip()
STORAGE_SECRET_ACCESS_KEY = os.getenv("STORAGE_SECRET_ACCESS_KEY", "").strip()
STORAGE_REGION = os.getenv("STORAGE_REGION", "auto").strip()
