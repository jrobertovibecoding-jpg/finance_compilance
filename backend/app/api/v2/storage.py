from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...core.auth_v2 import TenantContext, get_tenant_context, require_v2_roles
from ...core.storage import ObjectStorageService
from ...db.models import DocumentoAnexo, Empresa
from ...db.session import get_db
from .deps import extrair_ip, registrar_audit_v2

router = APIRouter(tags=["JR FinCloud v2 - Storage"])


class PresignUploadRequest(BaseModel):
    empresa_id: Optional[str] = None
    entidade_tipo: str = "TITULO"
    entidade_id: str
    nome_original: str
    mime_type: str
    tamanho_bytes: int
    sha256: str = Field(..., min_length=64, max_length=64)


@router.post("/storage/presign-upload")
def presign_upload(
    req: PresignUploadRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO", "SOLICITANTE")),
    db: Session = Depends(get_db),
):
    if req.empresa_id:
        emp = db.query(Empresa).filter(Empresa.id == req.empresa_id, Empresa.tenant_id == ctx.tenant_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="Empresa não encontrada neste workspace.")

    storage = ObjectStorageService()
    try:
        ext = storage.validar_arquivo(req.nome_original, req.mime_type, req.tamanho_bytes)
        key = storage.construir_chave(ctx.tenant_id, req.empresa_id, req.entidade_tipo, ext)
        upload_url = storage.gerar_presigned_url("PUT", key, expires_in=300)
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except RuntimeError as re_err:
        raise HTTPException(status_code=503, detail=str(re_err))

    anexo = DocumentoAnexo(
        tenant_id=ctx.tenant_id,
        empresa_id=req.empresa_id,
        entidade_tipo=req.entidade_tipo.upper(),
        entidade_id=req.entidade_id,
        storage_key=key,
        nome_original=req.nome_original[:255],
        mime_type=req.mime_type.lower(),
        tamanho_bytes=req.tamanho_bytes,
        sha256=req.sha256.lower(),
        uploaded_by=ctx.usuario.id,
    )
    db.add(anexo)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "PRESIGN_UPLOAD",
        "documentos_anexos",
        anexo.id,
        dados_depois={"storage_key": key, "tamanho_bytes": req.tamanho_bytes},
        ip=extrair_ip(request),
    )
    db.commit()

    return {
        "success": True,
        "anexo_id": anexo.id,
        "storage_key": key,
        "upload_url": upload_url,
        "expires_in": 300,
    }


@router.get("/storage/{anexo_id}/presign-download")
def presign_download(
    anexo_id: str,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    anexo = (
        db.query(DocumentoAnexo)
        .filter(DocumentoAnexo.id == anexo_id, DocumentoAnexo.tenant_id == ctx.tenant_id)
        .first()
    )
    if not anexo:
        raise HTTPException(status_code=404, detail="Documento não encontrado neste workspace.")

    storage = ObjectStorageService()
    try:
        download_url = storage.gerar_presigned_url("GET", anexo.storage_key, expires_in=300)
    except RuntimeError as re_err:
        raise HTTPException(status_code=503, detail=str(re_err))

    return {
        "success": True,
        "anexo_id": anexo.id,
        "nome_original": anexo.nome_original,
        "download_url": download_url,
        "expires_in": 300,
    }
