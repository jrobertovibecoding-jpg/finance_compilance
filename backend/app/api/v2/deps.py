import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ...db.models import (
    AuditLog,
    CentroCusto,
    ContaBancaria,
    Empresa,
    Fornecedor,
    PlanoConta,
    Titulo,
    Usuario,
)


def extrair_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def slugify(texto: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (texto or "").lower()).strip("-")
    return slug or "tenant"


def to_dec(val) -> Decimal:
    return Decimal(str(val or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def registrar_audit_v2(
    db: Session,
    tenant_id: str,
    usuario: Optional[Usuario],
    acao: str,
    entidade: str,
    entidade_id: str,
    dados_antes: Optional[dict] = None,
    dados_depois: Optional[dict] = None,
    ip: str = "",
) -> None:
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            usuario_id=usuario.id if usuario else None,
            usuario_email=usuario.email if usuario else None,
            acao=acao,
            entidade=entidade,
            entidade_id=str(entidade_id),
            dados_antes=dados_antes,
            dados_depois=dados_depois,
            ip_origem=(ip or "")[:64],
        )
    )


def obter_empresa_tenant(db: Session, tenant_id: str, empresa_id: str) -> Empresa:
    emp = db.query(Empresa).filter(Empresa.id == empresa_id, Empresa.tenant_id == tenant_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Empresa não encontrada neste workspace.")
    return emp


def obter_ccusto_tenant(db: Session, tenant_id: str, ccusto_id: str) -> CentroCusto:
    cc = db.query(CentroCusto).filter(CentroCusto.id == ccusto_id, CentroCusto.tenant_id == tenant_id).first()
    if not cc:
        raise HTTPException(status_code=404, detail="Centro de custo não encontrado neste workspace.")
    return cc


def obter_conta_tenant(db: Session, tenant_id: str, conta_id: str) -> ContaBancaria:
    conta = db.query(ContaBancaria).filter(ContaBancaria.id == conta_id, ContaBancaria.tenant_id == tenant_id).first()
    if not conta:
        raise HTTPException(status_code=404, detail="Conta bancária não encontrada neste workspace.")
    return conta


def obter_fornecedor_tenant(db: Session, tenant_id: str, fornecedor_id: str) -> Fornecedor:
    forn = db.query(Fornecedor).filter(Fornecedor.id == fornecedor_id, Fornecedor.tenant_id == tenant_id).first()
    if not forn:
        raise HTTPException(status_code=404, detail="Fornecedor não encontrado neste workspace.")
    return forn


def obter_plano_conta_tenant(db: Session, tenant_id: str, plano_conta_id: str) -> PlanoConta:
    pc = db.query(PlanoConta).filter(PlanoConta.id == plano_conta_id, PlanoConta.tenant_id == tenant_id).first()
    if not pc:
        raise HTTPException(status_code=404, detail="Conta do plano de contas não encontrada neste workspace.")
    return pc


def obter_titulo_tenant(db: Session, tenant_id: str, titulo_id: str) -> Titulo:
    tit = db.query(Titulo).filter(Titulo.id == titulo_id, Titulo.tenant_id == tenant_id).first()
    if not tit:
        raise HTTPException(status_code=404, detail="Título não encontrado neste workspace.")
    return tit
