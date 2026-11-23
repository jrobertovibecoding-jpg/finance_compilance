from contextlib import asynccontextmanager
import logging
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .core.config import PROJECT_NAME, VERSION, API_PREFIX, API_V2_PREFIX, CORS_ORIGINS
from .core.database import get_connection, init_db
from .core.security import obter_usuario_logado
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


@app.get("/")
def root():
    return {
        "app": PROJECT_NAME,
        "version": VERSION,
        "status": "online",
        "docs": "/docs",
    }


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
