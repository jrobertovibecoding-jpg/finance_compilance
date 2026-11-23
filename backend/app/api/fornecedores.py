import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..core.audit import registrar_auditoria
from ..core.database import get_connection, transaction
from ..core.security import obter_usuario_logado
from ..core.validators import validar_documento

logger = logging.getLogger("jrfincloud.fornecedores")

router = APIRouter(prefix="/fornecedores", tags=["Fornecedores & Prestadores"])


class FornecedorModel(BaseModel):
    id: Optional[int] = None
    razao_social: str
    nome_fantasia: Optional[str] = ""
    cnpj_cpf: Optional[str] = ""
    tipo: Optional[str] = "FORNECEDOR"
    categoria: Optional[str] = ""
    ccusto: Optional[str] = ""
    responsavel: Optional[str] = ""
    forma_pgto: Optional[str] = ""
    pix_chave: Optional[str] = ""
    dados_banco: Optional[str] = ""
    cod_operacao: Optional[str] = ""
    cnae: Optional[str] = ""
    item_lc116: Optional[str] = ""
    ativo: Optional[int] = 1


def _extrair_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.get("")
@router.get("/")
def listar_fornecedores(busca: Optional[str] = Query(None, description="Busca por Razao Social, Nome Fantasia, CNPJ ou ID")):
    try:
        conn = get_connection()
        cur = conn.cursor()

        query = """
            SELECT id, razao_social, nome_fantasia, cnpj_cpf, tipo, categoria,
                   ccusto, responsavel, forma_pgto, pix_chave, dados_banco,
                   cod_operacao, cnae, item_lc116, ativo, criado_em, atualizado_em
            FROM fornecedores
            WHERE ativo = 1
        """
        params = []

        if busca and busca.strip():
            termo = f"%{busca.strip()}%"
            if busca.strip().isdigit():
                query += " AND (id = ? OR cnpj_cpf LIKE ? OR razao_social LIKE ? OR nome_fantasia LIKE ?)"
                params.extend([int(busca.strip()), termo, termo, termo])
            else:
                query += " AND (razao_social LIKE ? OR nome_fantasia LIKE ? OR cnpj_cpf LIKE ?)"
                params.extend([termo, termo, termo])

        query += " ORDER BY razao_social ASC"
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        lista = []
        for r in rows:
            d = dict(r)
            lista.append({
                "id": d["id"],
                "razao_social": d["razao_social"] or "",
                "nome_fantasia": d["nome_fantasia"] or d["razao_social"] or "",
                "cnpj_cpf": d["cnpj_cpf"] or "",
                "tipo": d["tipo"] or "FORNECEDOR",
                "categoria": d["categoria"] or "",
                "ccusto": d["ccusto"] or "",
                "responsavel": d["responsavel"] or "",
                "forma_pgto": d["forma_pgto"] or "",
                "pix_chave": d["pix_chave"] or "",
                "dados_banco": d["dados_banco"] or "",
                "cod_operacao": d["cod_operacao"] or "",
                "cnae": d["cnae"] or "",
                "item_lc116": d["item_lc116"] or "",
                "ativo": d.get("ativo", 1),
            })

        return {"success": True, "total": len(lista), "fornecedores": lista}
    except Exception as e:
        logger.exception("Erro ao listar fornecedores: %s", e)
        return {"success": False, "error": str(e), "fornecedores": []}


@router.get("/{id_fornecedor}")
def obter_fornecedor(id_fornecedor: int):
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM fornecedores WHERE id = ?", (id_fornecedor,))
        row = cur.fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Fornecedor não encontrado.")
        return {"success": True, "fornecedor": dict(row)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
@router.post("/")
def salvar_fornecedor(
    f: FornecedorModel,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    razao = f.razao_social.strip()
    cnpj = (f.cnpj_cpf or "").strip()

    if not razao:
        raise HTTPException(status_code=400, detail="Razão Social é obrigatória.")
    if cnpj and not validar_documento(cnpj):
        raise HTTPException(status_code=422, detail=f"CNPJ/CPF inválido: {cnpj}")

    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()

        with transaction(conn):
            if f.id:
                cur.execute("SELECT id FROM fornecedores WHERE id = ?", (f.id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Fornecedor não encontrado para atualização.")
                cur.execute("""
                    UPDATE fornecedores SET
                        razao_social = ?,
                        nome_fantasia = ?,
                        cnpj_cpf = ?,
                        tipo = ?,
                        categoria = ?,
                        ccusto = ?,
                        responsavel = ?,
                        forma_pgto = ?,
                        pix_chave = ?,
                        dados_banco = ?,
                        cod_operacao = ?,
                        cnae = ?,
                        item_lc116 = ?,
                        ativo = ?,
                        atualizado_em = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (
                    razao,
                    f.nome_fantasia or razao,
                    cnpj if cnpj else None,
                    f.tipo or "FORNECEDOR",
                    f.categoria or "",
                    f.ccusto or "",
                    f.responsavel or "",
                    f.forma_pgto or "",
                    f.pix_chave or "",
                    f.dados_banco or "",
                    f.cod_operacao or "",
                    f.cnae or "",
                    f.item_lc116 or "",
                    f.ativo if f.ativo is not None else 1,
                    f.id,
                ))
                fornecedor_id = f.id
                msg = "Fornecedor gravado com sucesso!"
            else:
                if cnpj:
                    cur.execute("SELECT id FROM fornecedores WHERE cnpj_cpf = ?", (cnpj,))
                    exist = cur.fetchone()
                    if exist:
                        fornecedor_id = exist["id"]
                        cur.execute("""
                            UPDATE fornecedores SET
                                razao_social = ?,
                                nome_fantasia = ?,
                                categoria = COALESCE(NULLIF(?, ''), categoria),
                                ccusto = COALESCE(NULLIF(?, ''), ccusto),
                                responsavel = COALESCE(NULLIF(?, ''), responsavel),
                                forma_pgto = COALESCE(NULLIF(?, ''), forma_pgto),
                                pix_chave = COALESCE(NULLIF(?, ''), pix_chave),
                                dados_banco = COALESCE(NULLIF(?, ''), dados_banco),
                                cod_operacao = COALESCE(NULLIF(?, ''), cod_operacao),
                                cnae = COALESCE(NULLIF(?, ''), cnae),
                                item_lc116 = COALESCE(NULLIF(?, ''), item_lc116),
                                atualizado_em = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (
                            razao,
                            f.nome_fantasia or razao,
                            f.categoria or "",
                            f.ccusto or "",
                            f.responsavel or "",
                            f.forma_pgto or "",
                            f.pix_chave or "",
                            f.dados_banco or "",
                            f.cod_operacao or "",
                            f.cnae or "",
                            f.item_lc116 or "",
                            fornecedor_id,
                        ))
                        registrar_auditoria(
                            conn,
                            usuario,
                            "SALVAR_FORNECEDOR",
                            "fornecedores",
                            str(fornecedor_id),
                            f"Atualizado fornecedor {razao}",
                            ip=ip,
                        )
                        return {"success": True, "id": fornecedor_id, "message": "Fornecedor atualizado com sucesso!"}

                cur.execute("""
                    INSERT INTO fornecedores (
                        razao_social, nome_fantasia, cnpj_cpf, tipo, categoria,
                        ccusto, responsavel, forma_pgto, pix_chave, dados_banco,
                        cod_operacao, cnae, item_lc116, ativo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    razao,
                    f.nome_fantasia or razao,
                    cnpj if cnpj else None,
                    f.tipo or "FORNECEDOR",
                    f.categoria or "",
                    f.ccusto or "",
                    f.responsavel or "",
                    f.forma_pgto or "",
                    f.pix_chave or "",
                    f.dados_banco or "",
                    f.cod_operacao or "",
                    f.cnae or "",
                    f.item_lc116 or "",
                    f.ativo if f.ativo is not None else 1,
                ))
                fornecedor_id = cur.lastrowid
                msg = "Fornecedor gravado com sucesso!"

            registrar_auditoria(
                conn,
                usuario,
                "SALVAR_FORNECEDOR",
                "fornecedores",
                str(fornecedor_id),
                f"Fornecedor {razao}",
                ip=ip,
            )

        return {"success": True, "id": fornecedor_id, "message": msg}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao salvar fornecedor: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
