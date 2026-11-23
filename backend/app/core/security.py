import os
import time
import json
import uuid
import base64
import hmac
import hashlib
import logging
import secrets
import threading
from typing import Optional
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .config import ENV

logger = logging.getLogger("jrfincloud.security")

PBKDF2_ITERATIONS = 600_000
LEGACY_PBKDF2_ITERATIONS = 100_000

_env_secret = os.getenv("JWT_SECRET_KEY", "").strip()
if ENV == "production":
    if len(_env_secret) < 32:
        raise RuntimeError(
            "JWT_SECRET_KEY deve estar configurada com pelo menos 32 caracteres em ambiente de produção."
        )
    SECRET_KEY = _env_secret
else:
    if not _env_secret:
        logger.warning(
            "JWT_SECRET_KEY não definida; usando chave efêmera apenas para desenvolvimento/testes."
        )
        SECRET_KEY = secrets.token_hex(32)
    else:
        SECRET_KEY = _env_secret

security_bearer = HTTPBearer(auto_error=False)


class LoginRateLimiter:
    """Limitador de taxa em memória para proteção contra força bruta no login."""

    def __init__(self, max_tentativas: int = 5, janela_segundos: int = 60, bloqueio_segundos: int = 300):
        self.max_tentativas = max_tentativas
        self.janela_segundos = janela_segundos
        self.bloqueio_segundos = bloqueio_segundos
        self._lock = threading.Lock()
        self._tentativas: dict[str, list[float]] = {}
        self._bloqueados_ate: dict[str, float] = {}

    def verificar_bloqueio(self, chave: str) -> Optional[int]:
        agora = time.time()
        with self._lock:
            bloqueio_fim = self._bloqueados_ate.get(chave)
            if bloqueio_fim:
                if agora < bloqueio_fim:
                    return max(1, int(bloqueio_fim - agora))
                del self._bloqueados_ate[chave]
                self._tentativas.pop(chave, None)
        return None

    def registrar_falha(self, chave: str) -> None:
        agora = time.time()
        corte = agora - self.janela_segundos
        with self._lock:
            lista = [t for t in self._tentativas.get(chave, []) if t >= corte]
            lista.append(agora)
            self._tentativas[chave] = lista
            if len(lista) >= self.max_tentativas:
                self._bloqueados_ate[chave] = agora + self.bloqueio_segundos

    def limpar(self, chave: str) -> None:
        with self._lock:
            self._tentativas.pop(chave, None)
            self._bloqueados_ate.pop(chave, None)

    def reset(self) -> None:
        with self._lock:
            self._tentativas.clear()
            self._bloqueados_ate.clear()


login_rate_limiter = LoginRateLimiter()


def gerar_hash_senha(senha: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${key.hex()}"


def verificar_senha(senha: str, hash_salvo: str) -> bool:
    try:
        if not hash_salvo:
            return False
        if hash_salvo.startswith("pbkdf2_sha256$"):
            parts = hash_salvo.split("$")
            if len(parts) != 4:
                return False
            _, iters_str, salt_hex, key_hex = parts
            iters = int(iters_str)
            salt = bytes.fromhex(salt_hex)
            key = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, iters)
            return hmac.compare_digest(key.hex(), key_hex)
        if ":" in hash_salvo:
            salt_hex, key_hex = hash_salvo.split(":", 1)
            salt = bytes.fromhex(salt_hex)
            key = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, LEGACY_PBKDF2_ITERATIONS)
            return hmac.compare_digest(key.hex(), key_hex)
        return False
    except Exception:
        return False


def precisa_rehash(hash_salvo: str) -> bool:
    if not hash_salvo or not hash_salvo.startswith("pbkdf2_sha256$"):
        return True
    try:
        iters = int(hash_salvo.split("$")[1])
        return iters < PBKDF2_ITERATIONS
    except Exception:
        return True


def _b64_url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64_url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def criar_token_jwt(payload: dict, exp_segundos: int = 86400, token_type: str = "access") -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    agora = int(time.time())
    payload_copy = payload.copy()
    payload_copy["exp"] = agora + exp_segundos
    payload_copy["iat"] = agora
    payload_copy.setdefault("jti", uuid.uuid4().hex)
    payload_copy["token_type"] = token_type

    h_b64 = _b64_url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64_url_encode(json.dumps(payload_copy, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}"

    signature = hmac.new(SECRET_KEY.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64_url_encode(signature)
    return f"{signing_input}.{sig_b64}"


def decodificar_token_jwt(token: str, expected_type: Optional[str] = "access") -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        h_b64, p_b64, sig_b64 = parts
        header = json.loads(_b64_url_decode(h_b64).decode("utf-8"))
        if header.get("alg") != "HS256":
            return None

        signing_input = f"{h_b64}.{p_b64}"
        expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64_url_encode(expected_sig), sig_b64):
            return None

        payload = json.loads(_b64_url_decode(p_b64).decode("utf-8"))
        if payload.get("exp", 0) < time.time():
            return None
        if expected_type is not None and payload.get("token_type", "access") != expected_type:
            return None
        return payload
    except Exception:
        return None


def obter_usuario_logado(credentials: Optional[HTTPAuthorizationCredentials] = Security(security_bearer)) -> dict:
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado. Faça login para continuar.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decodificar_token_jwt(credentials.credentials, expected_type="access")
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão expirada ou token inválido. Faça login novamente.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def require_roles(*perfis_permitidos: str):
    allowed = {p.upper() for p in perfis_permitidos}

    def _role_guard(usuario: dict = Depends(obter_usuario_logado)) -> dict:
        perfil = str(usuario.get("perfil") or usuario.get("role") or "").upper()
        if perfil not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permissão insuficiente para executar esta operação.",
            )
        return usuario

    return _role_guard
