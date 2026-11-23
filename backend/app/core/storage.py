import hashlib
import hmac
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlparse

from . import config

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

ALLOWED_MIME_EXTENSIONS: dict[str, set[str]] = {
    "application/pdf": {"pdf"},
    "application/xml": {"xml"},
    "text/xml": {"xml"},
    "application/x-ofx": {"ofx"},
    "application/vnd.intu.qbo": {"ofx", "qbo"},
    "text/plain": {"txt", "rem", "ret", "csv", "ofx"},
    "text/csv": {"csv"},
    "image/png": {"png"},
    "image/jpeg": {"jpg", "jpeg"},
}


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(secret_key: str, date_stamp: str, region_name: str, service_name: str) -> bytes:
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region_name)
    k_service = _sign(k_region, service_name)
    return _sign(k_service, "aws4_request")


def _safe_segment(value: str, fallback: str = "geral") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (value or "").strip())
    return cleaned.strip("_") or fallback


class ObjectStorageService:
    """
    Serviço S3-compatible / Cloudflare R2 usando AWS Signature V4 em stdlib pura.
    Emite Presigned PUT e GET URLs com isolamento por tenant_id e validação de MIME/tamanho.
    """

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        bucket: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self.endpoint_url = (endpoint_url if endpoint_url is not None else os.getenv("STORAGE_ENDPOINT_URL", config.STORAGE_ENDPOINT_URL)).strip().rstrip("/")
        self.bucket = (bucket if bucket is not None else os.getenv("STORAGE_BUCKET", config.STORAGE_BUCKET)).strip()
        self.access_key_id = (access_key_id if access_key_id is not None else os.getenv("STORAGE_ACCESS_KEY_ID", config.STORAGE_ACCESS_KEY_ID)).strip()
        self.secret_access_key = (secret_access_key if secret_access_key is not None else os.getenv("STORAGE_SECRET_ACCESS_KEY", config.STORAGE_SECRET_ACCESS_KEY)).strip()
        self.region = (region if region is not None else os.getenv("STORAGE_REGION", config.STORAGE_REGION)).strip() or "auto"

    def validar_arquivo(self, nome_original: str, mime_type: str, tamanho_bytes: int) -> str:
        if tamanho_bytes <= 0 or tamanho_bytes > MAX_UPLOAD_BYTES:
            raise ValueError(f"Tamanho do arquivo inválido. Máximo permitido: {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")

        mime_norm = (mime_type or "").split(";")[0].strip().lower()
        allowed_exts = ALLOWED_MIME_EXTENSIONS.get(mime_norm)
        if not allowed_exts:
            raise ValueError(f"Tipo MIME não permitido: {mime_type}")

        ext = ""
        if "." in (nome_original or ""):
            ext = nome_original.rsplit(".", 1)[1].strip().lower()
        if not ext:
            ext = sorted(allowed_exts)[0]
        elif ext not in allowed_exts:
            raise ValueError(f"Extensão '.{ext}' incompatível com MIME '{mime_norm}'.")
        return ext

    def construir_chave(
        self,
        tenant_id: str,
        empresa_id: Optional[str],
        entidade_tipo: str,
        ext: str,
        agora: Optional[datetime] = None,
    ) -> str:
        dt = agora or datetime.now(timezone.utc)
        t_seg = _safe_segment(tenant_id)
        e_seg = _safe_segment(empresa_id or "matriz")
        ent_seg = _safe_segment(entidade_tipo.lower(), "anexo")
        arq_id = uuid.uuid4().hex
        return f"tenants/{t_seg}/{e_seg}/{dt:%Y}/{dt:%m}/{ent_seg}/{arq_id}.{ext}"

    def _garantir_configurado(self) -> None:
        if not (self.endpoint_url and self.bucket and self.access_key_id and self.secret_access_key):
            raise RuntimeError(
                "Object Storage (Cloudflare R2/S3) não configurado. Defina STORAGE_ENDPOINT_URL, STORAGE_BUCKET, STORAGE_ACCESS_KEY_ID e STORAGE_SECRET_ACCESS_KEY."
            )

    def gerar_presigned_url(self, method: str, storage_key: str, expires_in: int = 300) -> str:
        self._garantir_configurado()
        method_upper = method.upper()
        if method_upper not in ("PUT", "GET"):
            raise ValueError("Método presigned deve ser PUT ou GET.")

        parsed = urlparse(self.endpoint_url)
        scheme = parsed.scheme or "https"
        host = parsed.netloc
        base_path = parsed.path.rstrip("/")
        object_path = f"{base_path}/{quote(self.bucket, safe='')}/{quote(storage_key.lstrip('/'), safe='/')}"

        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        service = "s3"
        credential_scope = f"{date_stamp}/{self.region}/{service}/aws4_request"

        query_params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key_id}/{credential_scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(int(expires_in)),
            "X-Amz-SignedHeaders": "host",
        }
        canonical_querystring = "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}"
            for k, v in sorted(query_params.items())
        )
        canonical_headers = f"host:{host}\n"
        canonical_request = "\n".join([
            method_upper,
            object_path,
            canonical_querystring,
            canonical_headers,
            "host",
            "UNSIGNED-PAYLOAD",
        ])

        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signing_key = _get_signature_key(self.secret_access_key, date_stamp, self.region, service)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        return f"{scheme}://{host}{object_path}?{canonical_querystring}&X-Amz-Signature={signature}"
