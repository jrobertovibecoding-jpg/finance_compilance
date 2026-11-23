import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from . import config
from .security import criar_token_jwt, decodificar_token_jwt
from ..db.models import RefreshToken, Tenant, Usuario, UsuarioTenantRole
from ..db.session import get_db, set_rls_context

ACCESS_TOKEN_TTL_SECONDS = 900  # 15 minutos
REFRESH_TOKEN_TTL_DAYS = 30
REFRESH_COOKIE_NAME = "jr_refresh_token"

bearer_v2 = HTTPBearer(auto_error=False)


@dataclass
class TenantContext:
    usuario: Usuario
    tenant_id: str
    role: str
    membership: Optional[UsuarioTenantRole] = None


def _hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def emitir_par_tokens(
    db: Session,
    usuario: Usuario,
    tenant_id: str,
    role: str,
    family_id: Optional[str] = None,
    user_agent: str = "",
    ip_origem: str = "",
) -> tuple[str, str, RefreshToken]:
    raw_refresh = secrets.token_urlsafe(48)
    token_hash = _hash_refresh_token(raw_refresh)
    fam = family_id or secrets.token_hex(16)
    agora = datetime.now(timezone.utc)

    rt = RefreshToken(
        tenant_id=tenant_id,
        usuario_id=usuario.id,
        family_id=fam,
        token_hash=token_hash,
        expires_at=agora + timedelta(days=REFRESH_TOKEN_TTL_DAYS),
        user_agent=(user_agent or "")[:255],
        ip_origem=(ip_origem or "")[:64],
    )
    db.add(rt)
    db.flush()

    access_token = criar_token_jwt(
        {
            "sub": usuario.id,
            "email": usuario.email,
            "nome": usuario.nome,
            "tenant_id": tenant_id,
            "role": role,
            "v": 2,
        },
        exp_segundos=ACCESS_TOKEN_TTL_SECONDS,
        token_type="access",
    )
    return access_token, raw_refresh, rt


def rotacionar_refresh_token(
    db: Session,
    raw_refresh_token: str,
    user_agent: str = "",
    ip_origem: str = "",
) -> tuple[str, str, Usuario, str, str]:
    token_hash = _hash_refresh_token(raw_refresh_token)
    rt = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
    if not rt:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token inválido.")

    agora = datetime.now(timezone.utc)

    # Detecção de reuso: um refresh token já rotacionado foi reapresentado -> revoga toda a família!
    if rt.revoked_at is not None:
        db.query(RefreshToken).filter(
            RefreshToken.family_id == rt.family_id,
            RefreshToken.revoked_at.is_(None),
        ).update({"revoked_at": agora}, synchronize_session=False)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Reuso de sessão detectado. Todas as sessões associadas foram revogadas.",
        )

    if _ensure_aware_utc(rt.expires_at) < agora:
        rt.revoked_at = agora
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expirado.")

    usuario = db.query(Usuario).filter(Usuario.id == rt.usuario_id, Usuario.ativo.is_(True)).first()
    if not usuario:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário inativo ou inexistente.")

    role_str = _resolver_role_no_tenant(db, usuario.id, rt.tenant_id)
    if not role_str:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso ao workspace revogado.")

    rt.revoked_at = agora
    access_token, novo_raw_refresh, novo_rt = emitir_par_tokens(
        db=db,
        usuario=usuario,
        tenant_id=rt.tenant_id,
        role=role_str,
        family_id=rt.family_id,
        user_agent=user_agent,
        ip_origem=ip_origem,
    )
    rt.replaced_by_token_id = novo_rt.id
    db.commit()
    return access_token, novo_raw_refresh, usuario, rt.tenant_id, role_str


def _resolver_role_no_tenant(db: Session, usuario_id: str, tenant_id: str) -> Optional[str]:
    direct = (
        db.query(UsuarioTenantRole)
        .filter(
            UsuarioTenantRole.usuario_id == usuario_id,
            UsuarioTenantRole.tenant_id == tenant_id,
            UsuarioTenantRole.ativo.is_(True),
        )
        .first()
    )
    if direct:
        return direct.role

    # Verifica acesso via parceiro BPO pai (Parent BPO Tenant)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant and tenant.parent_bpo_tenant_id:
        bpo_role = (
            db.query(UsuarioTenantRole)
            .filter(
                UsuarioTenantRole.usuario_id == usuario_id,
                UsuarioTenantRole.tenant_id == tenant.parent_bpo_tenant_id,
                UsuarioTenantRole.ativo.is_(True),
            )
            .first()
        )
        if bpo_role and bpo_role.role in ("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO", "CONTADOR_LEITURA"):
            return bpo_role.role
    return None


def set_refresh_cookie(response: Response, raw_refresh_token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw_refresh_token,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        max_age=REFRESH_TOKEN_TTL_DAYS * 86400,
        path="/api/v2/auth",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path="/api/v2/auth")


def get_tenant_context(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_v2),
    db: Session = Depends(get_db),
) -> TenantContext:
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decodificar_token_jwt(credentials.credentials, expected_type="access")
    if not payload or payload.get("v") != 2:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token v2 inválido ou expirado.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    usuario_id = payload.get("sub")
    tenant_id = payload.get("tenant_id")
    if not usuario_id or not tenant_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Contexto de tenant ausente no token.")

    header_tid = (request.headers.get("X-Tenant-Id") or "").strip()
    if header_tid and header_tid != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cabeçalho X-Tenant-Id diverge do workspace autenticado. Utilize /auth/switch-tenant.",
        )

    usuario = db.query(Usuario).filter(Usuario.id == usuario_id, Usuario.ativo.is_(True)).first()
    if not usuario:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário inativo ou inexistente.")

    role = _resolver_role_no_tenant(db, usuario.id, tenant_id)
    if not role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem permissão neste workspace.")

    set_rls_context(db, tenant_id)
    return TenantContext(usuario=usuario, tenant_id=tenant_id, role=role)


def require_v2_roles(*allowed_roles: str):
    allowed = {r.upper() for r in allowed_roles}

    def _guard(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if ctx.role.upper() not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permissão insuficiente no workspace.")
        return ctx

    return _guard
