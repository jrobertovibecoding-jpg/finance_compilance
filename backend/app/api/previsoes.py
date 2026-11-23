import logging
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..core.audit import registrar_auditoria
from ..core.database import get_connection, transaction
from ..core.security import obter_usuario_logado

logger = logging.getLogger("jrfincloud.previsoes")

router = APIRouter(prefix="/previsoes", tags=["Despesas Fixas & Projeções"])


class PrevisaoEntrada(BaseModel):
    nome: str
    favorecido: str
    cnpj: Optional[str] = ""
    mesRef: str
    vencimento: str
    valor: float
    tipoValor: Optional[str] = "FIXO"
    ccusto: Optional[str] = "Administrativo"
    obs: Optional[str] = ""


class EfetivacaoEntrada(BaseModel):
    id: int
    valor_real: float
    numero_doc: str
    linha_digitavel: Optional[str] = ""


def _extrair_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.get("")
def listar_previsoes():
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, numero_tx, fornecedor, cnpj, dt_vencimento, valor_bruto,
                   descricao, filial, observacao, status, is_previsao
            FROM notas
            WHERE is_previsao = 1 OR status = 'PREVISAO'
            ORDER BY dt_vencimento ASC
        """)
        rows = cur.fetchall()
        conn.close()

        previsoes = []
        for r in rows:
            d = dict(r)
            previsoes.append({
                "id": d["id"],
                "nome": d.get("descricao") or d.get("fornecedor"),
                "favorecido": d.get("fornecedor"),
                "cnpj": d.get("cnpj") or "",
                "mesRef": (d.get("dt_vencimento") or "")[:7],
                "vencimento": d.get("dt_vencimento"),
                "valor": d.get("valor_bruto") or 0.0,
                "tipoValor": "VARIAVEL" if "Variavel" in (d.get("observacao") or "") else "FIXO",
                "ccusto": d.get("filial") or "Geral",
                "status": d.get("status") or "PREVISTO",
                "obs": d.get("observacao") or "",
            })
        return {"success": True, "total": len(previsoes), "previsoes": previsoes}
    except Exception as e:
        logger.exception("Erro ao listar previsões: %s", e)
        return {"success": False, "error": str(e), "previsoes": []}


@router.post("")
def criar_previsao(
    prev: PrevisaoEntrada,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        tx_code = f"PREV_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        with transaction(conn):
            cur.execute("""
                INSERT INTO notas (
                    numero_tx, tipo, fornecedor, cnpj, dt_emissao, dt_vencimento,
                    valor_bruto, valor_liquido, status, categoria, observacao,
                    filial, descricao, is_previsao
                ) VALUES (?, 'PREVISAO', ?, ?, ?, ?, ?, ?, 'PREVISTO', 'DESPESA_FIXA', ?, ?, ?, 1)
            """, (
                tx_code,
                prev.favorecido,
                prev.cnpj,
                datetime.now().strftime("%Y-%m-%d"),
                prev.vencimento,
                prev.valor,
                prev.valor,
                f"{prev.tipoValor} | {prev.obs}",
                prev.ccusto,
                prev.nome,
            ))
            prev_id = cur.lastrowid
            registrar_auditoria(
                conn,
                usuario,
                "CRIAR_PREVISAO",
                "notas",
                str(prev_id),
                f"Previsão {prev.nome} - {prev.favorecido}",
                prev.valor,
                ip=ip,
            )

        return {"success": True, "id": prev_id, "message": "Previsão cadastrada com sucesso!"}
    except Exception as e:
        logger.exception("Erro ao criar previsão: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.post("/efetivar")
def efetivar_previsao(
    efet: EfetivacaoEntrada,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        with transaction(conn):
            cur.execute("SELECT id FROM notas WHERE id = ? AND is_previsao = 1", (efet.id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Previsão não encontrada.")

            cur.execute("""
                UPDATE notas
                SET is_previsao = 0, status = 'PENDENTE', valor_bruto = ?,
                    valor_liquido = ?, numero_nf = ?, cod_barras = ?
                WHERE id = ?
            """, (
                efet.valor_real,
                efet.valor_real,
                efet.numero_doc,
                efet.linha_digitavel,
                efet.id,
            ))
            registrar_auditoria(
                conn,
                usuario,
                "EFETIVAR_PREVISAO",
                "notas",
                str(efet.id),
                f"Efetivada como doc {efet.numero_doc}",
                efet.valor_real,
                ip=ip,
            )

        return {"success": True, "message": "Previsão efetivada com sucesso em conta real a pagar!"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao efetivar previsão: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
