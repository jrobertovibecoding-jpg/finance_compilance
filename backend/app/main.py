from contextlib import asynccontextmanager
import logging
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from .core.config import BASE_DIR, PROJECT_NAME, VERSION, API_PREFIX, API_V2_PREFIX, CORS_ORIGINS, CORS_ORIGIN_REGEX
from .core.database import get_connection, init_db
from .core.security import obter_usuario_logado
from .db.models import TituloItem
from .db.session import get_engine
from .api import titulos, previsoes, adiantamentos, auth, fornecedores
from .api.auth import garantir_admin_inicial
from .api.v2 import router as v2_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("jrfincloud.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        TituloItem.__table__.create(bind=get_engine(), checkfirst=True)
    except Exception as exc:
        logger.warning("Aviso ao garantir tabela titulo_itens: %s", exc)
    garantir_admin_inicial()
    yield


app = FastAPI(
    title=PROJECT_NAME,
    version=VERSION,
    description="Backend Oficial JR Solutions (JR FinCloud) - Gestão Financeira, Contas a Pagar e Retenções Tributárias",
    lifespan=lifespan,
)

allow_all_origins = "*" in CORS_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all_origins else CORS_ORIGINS,
    allow_origin_regex=None if allow_all_origins else (CORS_ORIGIN_REGEX or None),
    allow_credentials=not allow_all_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Tenant-Id", "Accept"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


auth_dependencies = [Depends(obter_usuario_logado)]

app.include_router(auth.router, prefix=f"{API_PREFIX}/auth")
app.include_router(auth.router, prefix="/api/contas-a-pagar/auth")

app.include_router(titulos.router, prefix=API_PREFIX, dependencies=auth_dependencies)
app.include_router(previsoes.router, prefix=API_PREFIX, dependencies=auth_dependencies)
app.include_router(adiantamentos.router, prefix=API_PREFIX, dependencies=auth_dependencies)
app.include_router(fornecedores.router, prefix=API_PREFIX, dependencies=auth_dependencies)

app.include_router(titulos.router, prefix="/api/contas-a-pagar", dependencies=auth_dependencies)
app.include_router(previsoes.router, prefix="/api/contas-a-pagar", dependencies=auth_dependencies)
app.include_router(adiantamentos.router, prefix="/api/contas-a-pagar", dependencies=auth_dependencies)
app.include_router(fornecedores.router, prefix="/api/contas-a-pagar", dependencies=auth_dependencies)

app.include_router(v2_router, prefix=API_V2_PREFIX)

_CANDIDATOS_FRONTEND = [
    BASE_DIR / "frontend",
    BASE_DIR.parent / "frontend",
]
FRONTEND_DIR = next((p for p in _CANDIDATOS_FRONTEND if (p / "index.html").exists()), None)


def _servir_asset_pwa(filename: str, media_type: str, fallback: str | None = None):
    if FRONTEND_DIR is None:
        return JSONResponse(status_code=404, content={"detail": "Frontend não disponível."})
    alvo = FRONTEND_DIR / filename
    if not alvo.exists() and fallback:
        alvo = FRONTEND_DIR / fallback
    if not alvo.exists():
        return JSONResponse(status_code=404, content={"detail": "Arquivo não encontrado."})
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"} if filename in ("index.html", "sw.js") else None
    return FileResponse(alvo, media_type=media_type, headers=headers)


@app.get("/api/info")
def api_info():
    return {
        "app": PROJECT_NAME,
        "version": VERSION,
        "status": "online",
        "docs": "/docs",
    }


@app.get("/")
def root():
    if FRONTEND_DIR is not None:
        return FileResponse(
            FRONTEND_DIR / "index.html",
            media_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    return {
        "app": PROJECT_NAME,
        "version": VERSION,
        "status": "online",
        "docs": "/docs",
    }


@app.get("/index.html")
def pwa_index():
    return _servir_asset_pwa("index.html", "text/html")


@app.get("/manifest.json")
def pwa_manifest():
    return _servir_asset_pwa("manifest.json", "application/manifest+json")


@app.get("/sw.js")
def pwa_sw():
    return _servir_asset_pwa("sw.js", "application/javascript")


@app.get("/grupos_categorias.json")
def pwa_grupos_categorias():
    return _servir_asset_pwa("grupos_categorias.json", "application/json")


@app.get("/icon-192.png")
def pwa_icon_192():
    return _servir_asset_pwa("icon-192.png", "image/png")


@app.get("/icon-512.png")
def pwa_icon_512():
    return _servir_asset_pwa("icon-512.png", "image/png")


@app.get("/favicon.png")
def pwa_favicon():
    return _servir_asset_pwa("favicon.png", "image/png", fallback="icon-192.png")


@app.get("/health")
def health_check():
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        with get_engine().connect() as sa_conn:
            sa_conn.execute(text("SELECT 1"))
        return {"status": "healthy", "service": "jr-fincloud-api", "db": "ok", "db_v2": "ok"}
    except Exception as exc:
        logger.error("Healthcheck falhou ao conectar ao banco: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "service": "jr-fincloud-api", "db": "error"},
        )
