import logging
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..core.audit import registrar_auditoria
from ..core.database import get_connection, transaction
from ..core.security import obter_usuario_logado

logger = logging.getLogger("jrfincloud.adiantamentos")

router = APIRouter(prefix="/adiantamentos", tags=["Adiantamentos & Prestação de Contas"])


class AdiantamentoEntrada(BaseModel):
    solicitante: str
    ccusto: Optional[str] = "Geral"
    data: str
    valor: float
    forma: Optional[str] = "PIX"
    motivo: Optional[str] = "Adiantamento para despesas operacionais"


class ComprovanteGastoEntrada(BaseModel):
    adiantamento_id: int
    data: str
    tipo: str
    descricao: str
    valor: float


class FechamentoEntrada(BaseModel):
    tipo_fechamento: str = Field(..., description="DEVOLUCAO, REEMBOLSO, EXATO")
    saldo: float
    gerar_reembolso_cp: Optional[bool] = False


def _extrair_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.get("")
def listar_adiantamentos():
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT id, fornecedor, data, valor, status, observacao, ccusto, forma, motivo, data_fechamento FROM adiantamentos")
        rows_ad = cur.fetchall()

        cur.execute("SELECT id, adiantamento_id, data, tipo_doc, descricao, valor FROM adiantamento_comprovantes")
        rows_comp = cur.fetchall()
        conn.close()

        comprovantes_map = {}
        for c in rows_comp:
            aid = c["adiantamento_id"]
            if aid not in comprovantes_map:
                comprovantes_map[aid] = []
            comprovantes_map[aid].append({
                "id": c["id"],
                "data": c["data"],
                "tipo": c["tipo_doc"],
                "desc": c["descricao"],
                "valor": c["valor"],
            })

        lista = []
        for a in rows_ad:
            ad = dict(a)
            aid = ad["id"]
            gastos = comprovantes_map.get(aid, [])
            tot_gastos = sum(g["valor"] for g in gastos)
            lista.append({
                "id": aid,
                "solicitante": ad["fornecedor"],
                "ccusto": ad.get("ccusto") or "Geral",
                "dataLib": ad["data"],
                "valor": ad["valor"],
                "forma": ad.get("forma") or "PIX",
                "motivo": ad.get("motivo") or ad.get("observacao") or "",
                "status": ad.get("status") or "ABERTO",
                "dataFechamento": ad.get("data_fechamento"),
                "totalComprovado": tot_gastos,
                "saldo": ad["valor"] - tot_gastos,
                "gastos": gastos,
            })

        return {"success": True, "total": len(lista), "adiantamentos": lista}
    except Exception as e:
        logger.exception("Erro ao listar adiantamentos: %s", e)
        return {"success": False, "error": str(e), "adiantamentos": []}


@router.post("")
def criar_adiantamento(
    ad: AdiantamentoEntrada,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        with transaction(conn):
            cur.execute("""
                INSERT INTO adiantamentos (fornecedor, data, valor, status, observacao, ccusto, forma, motivo)
                VALUES (?, ?, ?, 'ABERTO', ?, ?, ?, ?)
            """, (ad.solicitante, ad.data, ad.valor, ad.motivo, ad.ccusto, ad.forma, ad.motivo))
            ad_id = cur.lastrowid
            registrar_auditoria(
                conn,
                usuario,
                "CRIAR_ADIANTAMENTO",
                "adiantamentos",
                str(ad_id),
                f"Adiantamento para {ad.solicitante}",
                ad.valor,
                ip=ip,
            )

        return {"success": True, "id": ad_id, "message": "Adiantamento concedido e registrado com sucesso!"}
    except Exception as e:
        logger.exception("Erro ao criar adiantamento: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.post("/comprovantes")
def lancar_comprovante(
    comp: ComprovanteGastoEntrada,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        with transaction(conn):
            cur.execute("SELECT id FROM adiantamentos WHERE id = ?", (comp.adiantamento_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Adiantamento não encontrado.")
            cur.execute("""
                INSERT INTO adiantamento_comprovantes (adiantamento_id, data, tipo_doc, descricao, valor, data_criacao)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (comp.adiantamento_id, comp.data, comp.tipo, comp.descricao, comp.valor, datetime.now().strftime("%Y-%m-%d")))
            comp_id = cur.lastrowid
            registrar_auditoria(
                conn,
                usuario,
                "LANCAR_COMPROVANTE",
                "adiantamento_comprovantes",
                str(comp_id),
                f"Adiantamento #{comp.adiantamento_id} - {comp.descricao}",
                comp.valor,
                ip=ip,
            )

        return {"success": True, "id": comp_id, "message": "Comprovante registrado com sucesso!"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao lançar comprovante: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.post("/{id}/fechar")
def fechar_adiantamento(
    id: int,
    fechamento: FechamentoEntrada,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        hoje = datetime.now().strftime("%Y-%m-%d")

        cur.execute("SELECT fornecedor, ccusto FROM adiantamentos WHERE id = ?", (id,))
        row_ad = cur.fetchone()
        if not row_ad:
            raise HTTPException(status_code=404, detail="Adiantamento não encontrado.")

        status_final = f"QUITADO_{fechamento.tipo_fechamento.upper()}"
        with transaction(conn):
            cur.execute("""
                UPDATE adiantamentos
                SET status = ?, data_fechamento = ?
                WHERE id = ?
            """, (status_final, hoje, id))

            if fechamento.gerar_reembolso_cp and fechamento.saldo < 0:
                dif = abs(fechamento.saldo)
                tx = f"REEMB_{int(datetime.now().timestamp())}"
                cur.execute("""
                    INSERT INTO notas (
                        numero_tx, tipo, fornecedor, cnpj, dt_emissao, dt_vencimento,
                        valor_bruto, valor_liquido, status, categoria, observacao,
                        responsavel, filial, is_previsao, numero_nf
                    ) VALUES (?, 'FORNECEDOR', ?, '00.000.000/0000-00', ?, ?, ?, ?, 'PENDENTE', 'DESPESA_INTERNA', ?, ?, ?, 0, ?)
                """, (
                    tx,
                    row_ad["fornecedor"],
                    hoje,
                    hoje,
                    dif,
                    dif,
                    f"Reembolso de saldo complementar do adiantamento #{id}",
                    row_ad["fornecedor"],
                    row_ad["ccusto"],
                    f"REEMB-{id}",
                ))

            registrar_auditoria(
                conn,
                usuario,
                "FECHAR_ADIANTAMENTO",
                "adiantamentos",
                str(id),
                f"Encerrado como {status_final}",
                abs(fechamento.saldo),
                ip=ip,
            )

        return {"success": True, "status": status_final, "message": "Prestação de contas encerrada com sucesso!"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao fechar adiantamento: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
