from fastapi import APIRouter

from . import auth_workspaces, cadastros, operacional, relatorios, storage, titulos

router = APIRouter(tags=["JR FinCloud v2 (SaaS Multi-Tenant)"])

router.include_router(auth_workspaces.router)
router.include_router(cadastros.router)
router.include_router(titulos.router)
router.include_router(operacional.router)
router.include_router(relatorios.router)
router.include_router(storage.router)
