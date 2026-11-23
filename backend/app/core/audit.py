import logging
from typing import Any, Optional, Union
from .database import get_connection

logger = logging.getLogger("jrfincloud.audit")


def _formatar_usuario(usuario: Optional[Union[dict, str]]) -> str:
    if not usuario:
        return "Sistema"
    if isinstance(usuario, str):
        return usuario
    if isinstance(usuario, dict):
        nome = str(usuario.get("nome") or "").strip()
        email = str(usuario.get("email") or "").strip()
        if nome and email:
            return f"{nome} <{email}>"
        return email or nome or str(usuario.get("sub") or usuario.get("id") or "Sistema")
    return str(usuario)


def registrar_auditoria(
    conn: Any,
    usuario: Optional[Union[dict, str]],
    acao: str,
    entidade: str,
    entidade_id: Any = "",
    descricao: str = "",
    valor: float = 0.0,
    ip: str = "",
) -> None:
    """Registra evento imutável de auditoria na tabela audit_log."""
    own_conn = False
    try:
        if conn is None:
            conn = get_connection()
            own_conn = True

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO audit_log (usuario, acao, entidade, entidade_id, descricao, valor, ip)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _formatar_usuario(usuario),
                str(acao or "").upper(),
                str(entidade or ""),
                str(entidade_id or ""),
                str(descricao or "")[:1000],
                float(valor or 0.0),
                str(ip or "")[:64],
            ),
        )
        if own_conn:
            conn.commit()
    except Exception as exc:
        logger.warning("Falha ao registrar audit_log (%s/%s): %s", acao, entidade, exc)
    finally:
        if own_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
