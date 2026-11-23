from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...core.auth_v2 import (
    ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_COOKIE_NAME,
    TenantContext,
    _hash_refresh_token,
    _resolver_role_no_tenant,
    clear_refresh_cookie,
    emitir_par_tokens,
    get_tenant_context,
    require_v2_roles,
    rotacionar_refresh_token,
    set_refresh_cookie,
)
from ...core.security import (
    gerar_hash_senha,
    login_rate_limiter,
    precisa_rehash,
    verificar_senha,
)
from ...core.validators import limpar_documento, validar_cnpj
from ...db.models import (
    CentroCusto,
    Empresa,
    RefreshToken,
    Tenant,
    Usuario,
    UsuarioTenantRole,
)
from ...db.session import get_db, set_rls_context
from .deps import extrair_ip, registrar_audit_v2, slugify

router = APIRouter(tags=["JR FinCloud v2 - Auth & Workspaces"])


class RegisterTenantRequest(BaseModel):
    nome_empresa: str
    razao_social: Optional[str] = None
    cnpj: str
    regime_tributario: str = "LUCRO_PRESUMIDO"
    is_bpo_partner: bool = False
    parent_bpo_tenant_id: Optional[str] = None
    admin_nome: str
    admin_email: str
    admin_senha: str


class LoginV2Request(BaseModel):
    email: str
    senha: str
    tenant_id: Optional[str] = None


class RefreshV2Request(BaseModel):
    refresh_token: Optional[str] = None


class SwitchTenantRequest(BaseModel):
    target_tenant_id: str


class EmpresaCreateRequest(BaseModel):
    razao_social: str
    nome_fantasia: str
    cnpj: str
    regime_tributario: str = "LUCRO_PRESUMIDO"
    cidade: str = "SALVADOR"
    uf: str = "BA"
    cep: str = "40000000"


class CentroCustoCreateRequest(BaseModel):
    empresa_id: str
    codigo: str
    nome: str
    tipo_unidade: str = "LOJA_SHOPPING"


ROLES_VALIDOS = {"OWNER", "DIRETOR_APROVADOR", "FINANCEIRO", "CONTADOR_LEITURA", "SOLICITANTE"}


class UsuarioTenantCreateRequest(BaseModel):
    nome: str
    email: str
    senha: str
    role: str = "FINANCEIRO"


@router.post("/auth/register-tenant", status_code=status.HTTP_201_CREATED)
def register_tenant(
    req: RegisterTenantRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    if len(req.admin_senha or "") < 8:
        raise HTTPException(status_code=422, detail="A senha deve ter no mínimo 8 caracteres.")
    if not validar_cnpj(req.cnpj):
        raise HTTPException(status_code=422, detail="CNPJ da empresa inválido.")

    email_clean = req.admin_email.strip().lower()
    if db.query(Usuario).filter(Usuario.email == email_clean).first():
        raise HTTPException(status_code=409, detail="Já existe usuário cadastrado com este e-mail.")

    base_slug = slugify(req.nome_empresa)
    slug = base_slug
    idx = 1
    while db.query(Tenant).filter(Tenant.slug == slug).first():
        idx += 1
        slug = f"{base_slug}-{idx}"

    cnpj_limpo = limpar_documento(req.cnpj)
    tenant = Tenant(
        nome_fantasia=req.nome_empresa.strip(),
        slug=slug,
        documento=cnpj_limpo,
        plano="PRO",
        status_assinatura="ACTIVE",
        is_bpo_partner=req.is_bpo_partner,
        parent_bpo_tenant_id=req.parent_bpo_tenant_id,
    )
    db.add(tenant)
    db.flush()
    set_rls_context(db, tenant.id)

    empresa = Empresa(
        tenant_id=tenant.id,
        razao_social=(req.razao_social or req.nome_empresa).strip(),
        nome_fantasia=req.nome_empresa.strip(),
        cnpj=cnpj_limpo,
        regime_tributario=req.regime_tributario,
    )
    db.add(empresa)
    db.flush()

    ccusto = CentroCusto(
        tenant_id=tenant.id,
        empresa_id=empresa.id,
        codigo="MATRIZ",
        nome=f"{req.nome_empresa.strip()} - Matriz",
        tipo_unidade="MATRIZ",
    )
    db.add(ccusto)

    usuario = Usuario(
        nome=req.admin_nome.strip(),
        email=email_clean,
        senha_hash=gerar_hash_senha(req.admin_senha),
        ativo=True,
    )
    db.add(usuario)
    db.flush()

    db.add(
        UsuarioTenantRole(
            tenant_id=tenant.id,
            usuario_id=usuario.id,
            role="OWNER",
            ativo=True,
        )
    )

    ip = extrair_ip(request)
    ua = request.headers.get("user-agent", "")
    access_token, raw_refresh, _ = emitir_par_tokens(
        db=db,
        usuario=usuario,
        tenant_id=tenant.id,
        role="OWNER",
        user_agent=ua,
        ip_origem=ip,
    )
    registrar_audit_v2(
        db,
        tenant.id,
        usuario,
        "REGISTER_TENANT",
        "tenants",
        tenant.id,
        dados_depois={"slug": slug, "cnpj": cnpj_limpo, "is_bpo_partner": req.is_bpo_partner},
        ip=ip,
    )
    db.commit()

    set_refresh_cookie(response, raw_refresh)
    return {
        "success": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "usuario": {"id": usuario.id, "nome": usuario.nome, "email": usuario.email},
        "tenant": {
            "id": tenant.id,
            "nome_fantasia": tenant.nome_fantasia,
            "slug": tenant.slug,
            "is_bpo_partner": tenant.is_bpo_partner,
        },
        "empresa_padrao_id": empresa.id,
        "role": "OWNER",
    }


@router.post("/auth/login")
def login_v2(
    req: LoginV2Request,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    email_clean = (req.email or "").strip().lower()
    ip = extrair_ip(request)
    chave_rate = f"v2:{ip}:{email_clean}"

    bloqueio = login_rate_limiter.verificar_bloqueio(chave_rate)
    if bloqueio is not None:
        raise HTTPException(status_code=429, detail=f"Muitas tentativas inválidas. Aguarde {bloqueio}s.")

    usuario = db.query(Usuario).filter(Usuario.email == email_clean, Usuario.ativo.is_(True)).first()
    if not usuario or not verificar_senha(req.senha, usuario.senha_hash):
        login_rate_limiter.registrar_falha(chave_rate)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas.")

    login_rate_limiter.limpar(chave_rate)
    if precisa_rehash(usuario.senha_hash):
        usuario.senha_hash = gerar_hash_senha(req.senha)
    usuario.ultimo_login = datetime.now(timezone.utc)

    tenant_id = req.tenant_id
    if tenant_id:
        role_str = _resolver_role_no_tenant(db, usuario.id, tenant_id)
        if not role_str:
            raise HTTPException(status_code=403, detail="Usuário sem acesso ao workspace informado.")
    else:
        membership = (
            db.query(UsuarioTenantRole)
            .filter(UsuarioTenantRole.usuario_id == usuario.id, UsuarioTenantRole.ativo.is_(True))
            .first()
        )
        if not membership:
            raise HTTPException(status_code=403, detail="Usuário não vinculado a nenhum workspace ativo.")
        tenant_id = membership.tenant_id
        role_str = membership.role

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    set_rls_context(db, tenant_id)
    access_token, raw_refresh, _ = emitir_par_tokens(
        db=db,
        usuario=usuario,
        tenant_id=tenant_id,
        role=role_str,
        user_agent=request.headers.get("user-agent", ""),
        ip_origem=ip,
    )
    registrar_audit_v2(db, tenant_id, usuario, "LOGIN_V2", "usuarios", usuario.id, ip=ip)
    db.commit()

    set_refresh_cookie(response, raw_refresh)
    return {
        "success": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "usuario": {"id": usuario.id, "nome": usuario.nome, "email": usuario.email},
        "tenant": {"id": tenant.id, "nome_fantasia": tenant.nome_fantasia, "slug": tenant.slug} if tenant else None,
        "role": role_str,
    }


@router.post("/auth/refresh")
def refresh_v2(
    request: Request,
    response: Response,
    body: Optional[RefreshV2Request] = None,
    db: Session = Depends(get_db),
):
    raw_token = request.cookies.get(REFRESH_COOKIE_NAME) or (body.refresh_token if body else None)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token ausente.")

    access_token, novo_raw_refresh, usuario, tenant_id, role_str = rotacionar_refresh_token(
        db=db,
        raw_refresh_token=raw_token,
        user_agent=request.headers.get("user-agent", ""),
        ip_origem=extrair_ip(request),
    )
    set_refresh_cookie(response, novo_raw_refresh)
    return {
        "success": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "tenant_id": tenant_id,
        "role": role_str,
        "usuario": {"id": usuario.id, "nome": usuario.nome, "email": usuario.email},
    }


@router.post("/auth/logout")
def logout_v2(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_token:
        token_hash = _hash_refresh_token(raw_token)
        rt = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
        if rt and rt.revoked_at is None:
            rt.revoked_at = datetime.now(timezone.utc)
            db.commit()
    clear_refresh_cookie(response)
    return {"success": True, "message": "Sessão encerrada com sucesso."}


@router.get("/workspaces")
def listar_workspaces(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """Lista workspaces diretos e carteiras de clientes BPO gerenciadas pelo usuário."""
    diretos = (
        db.query(UsuarioTenantRole, Tenant)
        .join(Tenant, Tenant.id == UsuarioTenantRole.tenant_id)
        .filter(UsuarioTenantRole.usuario_id == ctx.usuario.id, UsuarioTenantRole.ativo.is_(True))
        .all()
    )

    workspaces_map: dict[str, dict] = {}
    bpo_ids = []
    for mem, ten in diretos:
        workspaces_map[ten.id] = {
            "tenant_id": ten.id,
            "nome_fantasia": ten.nome_fantasia,
            "slug": ten.slug,
            "role": mem.role,
            "via_bpo": False,
            "is_bpo_partner": ten.is_bpo_partner,
            "ativo_atual": ten.id == ctx.tenant_id,
        }
        if ten.is_bpo_partner and mem.role in ("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO", "CONTADOR_LEITURA"):
            bpo_ids.append((ten.id, mem.role))

    for bpo_id, bpo_role in bpo_ids:
        clientes = db.query(Tenant).filter(Tenant.parent_bpo_tenant_id == bpo_id).all()
        for cli in clientes:
            if cli.id not in workspaces_map:
                workspaces_map[cli.id] = {
                    "tenant_id": cli.id,
                    "nome_fantasia": cli.nome_fantasia,
                    "slug": cli.slug,
                    "role": bpo_role,
                    "via_bpo": True,
                    "is_bpo_partner": cli.is_bpo_partner,
                    "ativo_atual": cli.id == ctx.tenant_id,
                }

    return {"success": True, "workspaces": list(workspaces_map.values())}


@router.post("/auth/switch-tenant")
def switch_tenant(
    req: SwitchTenantRequest,
    request: Request,
    response: Response,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    role_alvo = _resolver_role_no_tenant(db, ctx.usuario.id, req.target_tenant_id)
    if not role_alvo:
        raise HTTPException(status_code=403, detail="Você não possui acesso autorizado ao workspace solicitado.")

    tenant = db.query(Tenant).filter(Tenant.id == req.target_tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Workspace não encontrado.")

    set_rls_context(db, tenant.id)
    ip = extrair_ip(request)
    access_token, raw_refresh, _ = emitir_par_tokens(
        db=db,
        usuario=ctx.usuario,
        tenant_id=tenant.id,
        role=role_alvo,
        user_agent=request.headers.get("user-agent", ""),
        ip_origem=ip,
    )
    registrar_audit_v2(
        db,
        tenant.id,
        ctx.usuario,
        "SWITCH_TENANT",
        "tenants",
        tenant.id,
        dados_antes={"tenant_id": ctx.tenant_id},
        dados_depois={"tenant_id": tenant.id, "role": role_alvo},
        ip=ip,
    )
    db.commit()

    set_refresh_cookie(response, raw_refresh)
    return {
        "success": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "tenant": {"id": tenant.id, "nome_fantasia": tenant.nome_fantasia, "slug": tenant.slug},
        "role": role_alvo,
    }


@router.get("/empresas")
def listar_empresas_v2(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    empresas = db.query(Empresa).filter(Empresa.tenant_id == ctx.tenant_id).order_by(Empresa.nome_fantasia).all()
    return {
        "success": True,
        "empresas": [
            {
                "id": e.id,
                "razao_social": e.razao_social,
                "nome_fantasia": e.nome_fantasia,
                "cnpj": e.cnpj,
                "regime_tributario": e.regime_tributario,
                "cidade": e.cidade,
                "uf": e.uf,
            }
            for e in empresas
        ],
    }


@router.post("/empresas", status_code=status.HTTP_201_CREATED)
def criar_empresa_v2(
    req: EmpresaCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    if not validar_cnpj(req.cnpj):
        raise HTTPException(status_code=422, detail="CNPJ inválido.")
    cnpj_limpo = limpar_documento(req.cnpj)

    if db.query(Empresa).filter(Empresa.tenant_id == ctx.tenant_id, Empresa.cnpj == cnpj_limpo).first():
        raise HTTPException(status_code=409, detail="CNPJ já cadastrado neste workspace.")

    empresa = Empresa(
        tenant_id=ctx.tenant_id,
        razao_social=req.razao_social.strip(),
        nome_fantasia=req.nome_fantasia.strip(),
        cnpj=cnpj_limpo,
        regime_tributario=req.regime_tributario,
        cidade=req.cidade,
        uf=req.uf,
        cep=req.cep,
    )
    db.add(empresa)
    db.flush()
    registrar_audit_v2(db, ctx.tenant_id, ctx.usuario, "CRIAR_EMPRESA", "empresas", empresa.id, ip=extrair_ip(request))
    db.commit()
    return {"success": True, "id": empresa.id, "cnpj": empresa.cnpj}


@router.get("/centros-custo")
def listar_centros_custo_v2(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    itens = db.query(CentroCusto).filter(CentroCusto.tenant_id == ctx.tenant_id).order_by(CentroCusto.codigo).all()
    return {
        "success": True,
        "centros_custo": [
            {"id": c.id, "empresa_id": c.empresa_id, "codigo": c.codigo, "nome": c.nome, "tipo_unidade": c.tipo_unidade}
            for c in itens
        ],
    }


@router.post("/centros-custo", status_code=status.HTTP_201_CREATED)
def criar_centro_custo_v2(
    req: CentroCustoCreateRequest,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    emp = db.query(Empresa).filter(Empresa.id == req.empresa_id, Empresa.tenant_id == ctx.tenant_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Empresa não encontrada neste workspace.")

    cc = CentroCusto(
        tenant_id=ctx.tenant_id,
        empresa_id=emp.id,
        codigo=req.codigo.strip().upper(),
        nome=req.nome.strip(),
        tipo_unidade=req.tipo_unidade,
    )
    db.add(cc)
    db.commit()
    return {"success": True, "id": cc.id, "codigo": cc.codigo}


@router.get("/usuarios")
def listar_usuarios_tenant(
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR")),
    db: Session = Depends(get_db),
):
    membros = (
        db.query(UsuarioTenantRole, Usuario)
        .join(Usuario, Usuario.id == UsuarioTenantRole.usuario_id)
        .filter(UsuarioTenantRole.tenant_id == ctx.tenant_id)
        .order_by(Usuario.nome.asc())
        .all()
    )
    return {
        "success": True,
        "usuarios": [
            {
                "id": usr.id,
                "nome": usr.nome,
                "email": usr.email,
                "role": mem.role,
                "ativo": mem.ativo and usr.ativo,
            }
            for mem, usr in membros
        ],
    }


@router.post("/usuarios", status_code=status.HTTP_201_CREATED)
def criar_usuario_tenant(
    req: UsuarioTenantCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER")),
    db: Session = Depends(get_db),
):
    role_norm = req.role.strip().upper()
    if role_norm not in ROLES_VALIDOS:
        raise HTTPException(status_code=422, detail=f"Papel inválido. Permitidos: {sorted(ROLES_VALIDOS)}")
    if len(req.senha or "") < 8:
        raise HTTPException(status_code=422, detail="A senha deve ter no mínimo 8 caracteres.")

    email_clean = req.email.strip().lower()
    if db.query(Usuario).filter(Usuario.email == email_clean).first():
        raise HTTPException(status_code=409, detail="E-mail já cadastrado no sistema.")

    usr = Usuario(
        nome=req.nome.strip(),
        email=email_clean,
        senha_hash=gerar_hash_senha(req.senha),
        ativo=True,
    )
    db.add(usr)
    db.flush()

    db.add(
        UsuarioTenantRole(
            tenant_id=ctx.tenant_id,
            usuario_id=usr.id,
            role=role_norm,
            ativo=True,
        )
    )
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CRIAR_USUARIO_TENANT",
        "usuarios",
        usr.id,
        dados_depois={"email": email_clean, "role": role_norm},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": usr.id, "email": usr.email, "role": role_norm}
