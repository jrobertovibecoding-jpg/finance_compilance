import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel

from ..core import config
from ..core.database import get_connection
from ..core.audit import registrar_auditoria
from ..core.security import (
    gerar_hash_senha,
    verificar_senha,
    precisa_rehash,
    criar_token_jwt,
    obter_usuario_logado,
    login_rate_limiter,
)

logger = logging.getLogger("jrfincloud.auth")

router = APIRouter(tags=["Autenticação & Sessão"])


class LoginEntrada(BaseModel):
    email: str
    senha: str


class AlterarSenhaEntrada(BaseModel):
    senha_atual: str
    nova_senha: str


def _extrair_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def garantir_admin_inicial() -> None:
    """Provisiona usuário administrador inicial apenas se tabela vazia E env configurada."""
    email = config.ADMIN_INITIAL_EMAIL
    senha = config.ADMIN_INITIAL_PASSWORD
    if not email or not senha or len(senha) < 8:
        return

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM usuarios")
        row = cur.fetchone()
        total = row["total"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
        if total == 0:
            senha_hash = gerar_hash_senha(senha)
            cur.execute(
                "INSERT INTO usuarios (nome, email, senha_hash, perfil, ativo) VALUES (?, ?, ?, ?, ?)",
                ("Administrador", email, senha_hash, "MASTER", 1),
            )
            registrar_auditoria(
                conn,
                "Sistema",
                "BOOTSTRAP_ADMIN",
                "usuarios",
                email,
                "Administrador inicial provisionado via variável de ambiente",
            )
            conn.commit()
            logger.info("Administrador inicial provisionado para %s", email)
    finally:
        conn.close()


@router.post("/login")
def login(dados: LoginEntrada, request: Request):
    email_clean = (dados.email or "").strip().lower()
    ip = _extrair_ip(request)
    chave_rate = f"{ip}:{email_clean}"

    bloqueio_restante = login_rate_limiter.verificar_bloqueio(chave_rate)
    if bloqueio_restante is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Muitas tentativas de login inválidas. Aguarde {bloqueio_restante} segundos.",
        )

    if not email_clean or not dados.senha:
        login_rate_limiter.registrar_falha(chave_rate)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="E-mail ou senha incorretos.",
        )

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, nome, email, senha_hash, perfil, ativo FROM usuarios WHERE LOWER(email) = ?",
            (email_clean,),
        )
        user = cur.fetchone()

        if not user or not user["ativo"]:
            login_rate_limiter.registrar_falha(chave_rate)
            registrar_auditoria(
                conn,
                email_clean,
                "LOGIN_FALHA",
                "usuarios",
                str(user["id"]) if user else "",
                "Usuário inexistente ou inativo",
                ip=ip,
            )
            conn.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="E-mail ou senha incorretos.",
            )

        if not verificar_senha(dados.senha, user["senha_hash"]):
            login_rate_limiter.registrar_falha(chave_rate)
            registrar_auditoria(
                conn,
                email_clean,
                "LOGIN_FALHA",
                "usuarios",
                str(user["id"]),
                "Senha inválida",
                ip=ip,
            )
            conn.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="E-mail ou senha incorretos.",
            )

        login_rate_limiter.limpar(chave_rate)

        hoje = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if precisa_rehash(user["senha_hash"]):
            novo_hash = gerar_hash_senha(dados.senha)
            cur.execute(
                "UPDATE usuarios SET ultimo_login = ?, senha_hash = ? WHERE id = ?",
                (hoje, novo_hash, user["id"]),
            )
        else:
            cur.execute("UPDATE usuarios SET ultimo_login = ? WHERE id = ?", (hoje, user["id"]))

        usuario_info = {
            "id": user["id"],
            "nome": user["nome"],
            "email": user["email"],
            "perfil": user["perfil"],
        }

        registrar_auditoria(
            conn,
            usuario_info,
            "LOGIN_SUCESSO",
            "usuarios",
            str(user["id"]),
            "Login autenticado com sucesso",
            ip=ip,
        )
        conn.commit()
    finally:
        conn.close()

    token = criar_token_jwt(usuario_info)
    return {
        "success": True,
        "access_token": token,
        "token_type": "Bearer",
        "usuario": usuario_info,
    }


@router.get("/me")
def obter_me(usuario: dict = Depends(obter_usuario_logado)):
    return {"success": True, "usuario": usuario}


@router.post("/alterar-senha")
def alterar_senha(
    dados: AlterarSenhaEntrada,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    if len(dados.nova_senha or "") < 8:
        raise HTTPException(status_code=400, detail="A nova senha deve ter no mínimo 8 caracteres.")

    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT senha_hash FROM usuarios WHERE id = ?", (usuario["id"],))
        row = cur.fetchone()

        if not row or not verificar_senha(dados.senha_atual, row["senha_hash"]):
            raise HTTPException(status_code=400, detail="Senha atual incorreta.")

        novo_hash = gerar_hash_senha(dados.nova_senha)
        cur.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?", (novo_hash, usuario["id"]))
        registrar_auditoria(
            conn,
            usuario,
            "SENHA_ALTERADA",
            "usuarios",
            str(usuario["id"]),
            "Senha alterada pelo usuário",
            ip=ip,
        )
        conn.commit()
    finally:
        conn.close()

    return {"success": True, "message": "Senha alterada com sucesso!"}
