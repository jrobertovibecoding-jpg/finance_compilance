import io
from decimal import Decimal
from fastapi.testclient import TestClient


def _registrar(client: TestClient, nome: str, cnpj: str, email: str):
    resp = client.post(
        "/api/v2/auth/register-tenant",
        json={
            "nome_empresa": nome,
            "cnpj": cnpj,
            "admin_nome": f"Admin {nome}",
            "admin_email": email,
            "admin_senha": "SenhaSaaS@2026",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_motor_fiscal_simples_vs_lucro_presumido_e_reforma_2026(client):
    reg = _registrar(client, "Varejo Fiscal", "11.222.333/0001-81", "fiscal@varejo.com.br")
    hdr = {"Authorization": f"Bearer {reg['access_token']}"}

    # Plano de Contas (criação e bloqueio de duplicidade)
    r_pc = client.post(
        "/api/v2/plano-contas",
        headers=hdr,
        json={"codigo": "31101", "nome": "Serviços de TI", "macro_grupo": "DESPESAS FIXAS"},
    )
    assert r_pc.status_code == 201
    r_pc_dup = client.post(
        "/api/v2/plano-contas",
        headers=hdr,
        json={"codigo": "31101", "nome": "Duplicado", "macro_grupo": "DESPESAS FIXAS"},
    )
    assert r_pc_dup.status_code == 409

    # Fornecedor Lucro Presumido vs Simples Nacional
    r_lp = client.post(
        "/api/v2/fornecedores",
        headers=hdr,
        json={
            "razao_social": "Consultoria Tech Presumido Ltda",
            "cnpj_cpf": "45.997.418/0001-53",
            "regime_tributario": "LUCRO_PRESUMIDO",
            "optante_simples": False,
        },
    )
    assert r_lp.status_code == 201, r_lp.text
    lp_id = r_lp.json()["id"]

    r_sn = client.post(
        "/api/v2/fornecedores",
        headers=hdr,
        json={
            "razao_social": "Atelie Costura Simples ME",
            "cnpj_cpf": "33.000.167/0001-01",
            "regime_tributario": "SIMPLES_NACIONAL",
            "optante_simples": True,
        },
    )
    assert r_sn.status_code == 201, r_sn.text
    sn_id = r_sn.json()["id"]

    # Cálculo para Lucro Presumido (R$ 10.000,00 com ISS 5%)
    calc_lp = client.post(
        "/api/v2/fiscal/calcular-retencoes",
        headers=hdr,
        json={
            "valor_bruto": "10000.00",
            "fornecedor_id": lp_id,
            "iss_aliquota": "5.00",
            "reter_iss": True,
            "reter_federais": True,
            "incluir_reforma_2026": True,
        },
    )
    assert calc_lp.status_code == 200, calc_lp.text
    d_lp = calc_lp.json()
    assert d_lp["retencoes"] == {
        "irrf": "150.00",
        "pis": "65.00",
        "cofins": "300.00",
        "csll": "100.00",
        "iss": "500.00",
    }
    assert d_lp["total_retencoes"] == "1115.00"
    assert d_lp["valor_liquido"] == "8885.00"
    assert d_lp["reforma_tributaria_2026_informativo"]["cbs_0_90"] == "90.00"
    assert d_lp["reforma_tributaria_2026_informativo"]["ibs_0_10"] == "10.00"

    # Cálculo para Simples Nacional -> dispensa retenções federais automaticamente!
    calc_sn = client.post(
        "/api/v2/fiscal/calcular-retencoes",
        headers=hdr,
        json={
            "valor_bruto": "10000.00",
            "fornecedor_id": sn_id,
            "iss_aliquota": "5.00",
            "reter_iss": True,
            "reter_federais": True,
        },
    )
    assert calc_sn.status_code == 200
    d_sn = calc_sn.json()
    assert d_sn["optante_simples_ou_mei"] is True
    assert d_sn["dispensa_federal_motivo"] is not None
    assert d_sn["retencoes"] == {
        "irrf": "0.00",
        "pis": "0.00",
        "cofins": "0.00",
        "csll": "0.00",
        "iss": "500.00",
    }
    assert d_sn["valor_liquido"] == "9500.00"


def test_workflow_alcadas_aprovacao_rbac_e_edicao_preservando_imposto(client):
    reg = _registrar(client, "Empresa Alcadas", "11.222.333/0001-81", "dono@alcadas.com.br")
    hdr_owner = {"Authorization": f"Bearer {reg['access_token']}"}

    # Owner cria colaborador FINANCEIRO
    r_usr = client.post(
        "/api/v2/usuarios",
        headers=hdr_owner,
        json={
            "nome": "Operador Financeiro",
            "email": "fin@alcadas.com.br",
            "senha": "SenhaFin@2026",
            "role": "FINANCEIRO",
        },
    )
    assert r_usr.status_code == 201, r_usr.text

    # Login como FINANCEIRO
    r_login_fin = client.post(
        "/api/v2/auth/login",
        json={"email": "fin@alcadas.com.br", "senha": "SenhaFin@2026"},
    )
    assert r_login_fin.status_code == 200
    hdr_fin = {"Authorization": f"Bearer {r_login_fin.json()['access_token']}"}

    # FINANCEIRO lança título com retenção de ISS de R$ 50,00
    r_tit = client.post(
        "/api/v2/titulos",
        headers=hdr_fin,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "numero_documento": "NF-ALC-1",
            "favorecido_nome": "Fornecedor Servicos",
            "dt_emissao": "2026-10-01",
            "dt_vencimento": "2026-10-20",
            "valor_bruto": "1000.00",
            "impostos": [{"tipo": "ISS", "aliquota": "5.0", "valor": "50.00"}],
        },
    )
    assert r_tit.status_code == 201
    tit_id = r_tit.json()["id"]
    assert r_tit.json()["valor_liquido"] == "950.00"

    # Edição do valor bruto para 2000.00 preserva a retenção de 50.00 -> liquido 1950.00
    r_edit = client.put(
        f"/api/v2/titulos/{tit_id}",
        headers=hdr_fin,
        json={"valor_bruto": "2000.00"},
    )
    assert r_edit.status_code == 200
    assert r_edit.json()["titulo"]["valor_liquido"] == "1950.00"

    # Submete para aprovação
    r_sub = client.post(f"/api/v2/titulos/{tit_id}/submeter-aprovacao", headers=hdr_fin)
    assert r_sub.status_code == 200
    assert r_sub.json()["status"] == "AGUARDANDO_APROVACAO"

    # FINANCEIRO NÃO pode aprovar -> 403
    r_forbid = client.post(
        f"/api/v2/titulos/{tit_id}/aprovar",
        headers=hdr_fin,
        json={"canal": "WEB"},
    )
    assert r_forbid.status_code == 403

    # OWNER aprova via WHATSAPP -> 200
    r_apr = client.post(
        f"/api/v2/titulos/{tit_id}/aprovar",
        headers=hdr_owner,
        json={"canal": "WHATSAPP", "justificativa": "Aprovado diretoria"},
    )
    assert r_apr.status_code == 200
    assert r_apr.json()["status"] == "APROVADO"

    # Reprovação exige justificativa
    r_tit2 = client.post(
        "/api/v2/titulos",
        headers=hdr_fin,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "numero_documento": "NF-ALC-2",
            "favorecido_nome": "Fornecedor Divergente",
            "dt_emissao": "2026-10-01",
            "dt_vencimento": "2026-10-20",
            "valor_bruto": "500.00",
        },
    )
    tit2_id = r_tit2.json()["id"]
    r_rep_422 = client.post(
        f"/api/v2/titulos/{tit2_id}/reprovar",
        headers=hdr_owner,
        json={"canal": "WEB", "justificativa": "   "},
    )
    assert r_rep_422.status_code == 422

    r_rep_ok = client.post(
        f"/api/v2/titulos/{tit2_id}/reprovar",
        headers=hdr_owner,
        json={"canal": "WEB", "justificativa": "Valor diverge do pedido"},
    )
    assert r_rep_ok.status_code == 200
    assert r_rep_ok.json()["status"] == "REPROVADO"


def test_remessa_cnab240_multi_empresa_v2(client):
    reg = _registrar(client, "Empresa CNAB Multi", "11.222.333/0001-81", "cnab@multi.com.br")
    hdr = {"Authorization": f"Bearer {reg['access_token']}"}

    r_cb = client.post(
        "/api/v2/contas-bancarias",
        headers=hdr,
        json={"empresa_id": reg["empresa_padrao_id"], "agencia": "4321", "conta": "98765", "dac": "2"},
    )
    assert r_cb.status_code == 201
    conta_id = r_cb.json()["id"]

    barra_44 = "34191000000000250001750001234567890123456789"
    # Título 1: PENDENTE (não deve ir quando somente_aprovados=True)
    r_t1 = client.post(
        "/api/v2/titulos",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "numero_documento": "BOL-PEND",
            "favorecido_nome": "Fornecedor Pendente",
            "dt_emissao": "2026-10-01",
            "dt_vencimento": "2026-10-15",
            "valor_bruto": "250.00",
            "cod_barras": barra_44,
        },
    )
    assert r_t1.status_code == 201

    # Título 2: APROVADO (deve integrar o CNAB 240)
    r_t2 = client.post(
        "/api/v2/titulos",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "numero_documento": "BOL-APROV",
            "favorecido_nome": "Fornecedor Aprovado",
            "dt_emissao": "2026-10-01",
            "dt_vencimento": "2026-10-15",
            "valor_bruto": "420.00",
            "cod_barras": barra_44,
        },
    )
    tit2_id = r_t2.json()["id"]
    client.post(f"/api/v2/titulos/{tit2_id}/aprovar", headers=hdr, json={"canal": "WEB"})

    r_rem = client.post(
        "/api/v2/remessas/itau-cnab240",
        headers=hdr,
        json={"conta_bancaria_id": conta_id, "data_pagamento": "2026-10-15", "somente_aprovados": True},
    )
    assert r_rem.status_code == 200, r_rem.text
    linhas = [ln for ln in r_rem.text.splitlines() if ln]
    assert len(linhas) == 5  # apenas 1 detalhe aprovado
    assert all(len(ln) == 240 for ln in linhas)
    assert "11222333000181" in linhas[0]
    assert "04321" in linhas[0]
    assert "000000098765" in linhas[0]
    assert linhas[3][23:41] == "000000000000042000"


def test_despesas_recorrentes_geracao_idempotente_fevereiro(client):
    reg = _registrar(client, "Empresa Recorrente", "11.222.333/0001-81", "rec@empresa.com.br")
    hdr = {"Authorization": f"Bearer {reg['access_token']}"}

    r_rec = client.post(
        "/api/v2/despesas-recorrentes",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "nome": "Aluguel Loja Dia 31",
            "valor_estimado": "3500.00",
            "dia_vencimento": 31,
        },
    )
    assert r_rec.status_code == 201

    r_g1 = client.post(
        "/api/v2/despesas-recorrentes/gerar-competencia",
        headers=hdr,
        json={"ano": 2026, "mes": 2},
    )
    assert r_g1.status_code == 200
    assert r_g1.json()["criados"] == 1
    assert r_g1.json()["ignorados_existentes"] == 0

    # Verifica vencimento ajustado para 2026-02-28
    r_list = client.get("/api/v2/titulos", headers=hdr)
    assert r_list.json()["titulos"][0]["dt_vencimento"] == "2026-02-28"

    # 2a execução na mesma competência não duplica!
    r_g2 = client.post(
        "/api/v2/despesas-recorrentes/gerar-competencia",
        headers=hdr,
        json={"ano": 2026, "mes": 2},
    )
    assert r_g2.status_code == 200
    assert r_g2.json()["criados"] == 0
    assert r_g2.json()["ignorados_existentes"] == 1


def test_adiantamentos_v2_comprovantes_e_reembolso_automatico(client):
    reg = _registrar(client, "Empresa Adiantamentos", "11.222.333/0001-81", "ad@emp.com.br")
    hdr = {"Authorization": f"Bearer {reg['access_token']}"}

    r_ad = client.post(
        "/api/v2/adiantamentos",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "solicitante": "Luiza Marketing",
            "data_liberacao": "2026-10-01",
            "valor_concedido": "500.00",
            "motivo": "Producao Lookbook",
        },
    )
    assert r_ad.status_code == 201
    ad_id = r_ad.json()["id"]

    r_c1 = client.post(
        f"/api/v2/adiantamentos/{ad_id}/comprovantes",
        headers=hdr,
        json={"data_gasto": "2026-10-02", "tipo_doc": "NFCE", "descricao": "Lanche equipe", "valor": "220.00"},
    )
    assert r_c1.status_code == 201
    r_c2 = client.post(
        f"/api/v2/adiantamentos/{ad_id}/comprovantes",
        headers=hdr,
        json={"data_gasto": "2026-10-03", "tipo_doc": "RECIBO", "descricao": "Transporte material", "valor": "400.00"},
    )
    assert r_c2.status_code == 201

    r_fech = client.post(
        f"/api/v2/adiantamentos/{ad_id}/fechar",
        headers=hdr,
        json={"gerar_reembolso_cp": True},
    )
    assert r_fech.status_code == 200, r_fech.text
    d = r_fech.json()
    assert d["status"] == "QUITADO_REEMBOLSO"
    assert d["total_comprovado"] == "620.00"
    assert d["saldo"] == "-120.00"
    assert d["reembolso_titulo_id"] is not None

    # Título de reembolso criado automaticamente
    r_tit = client.get(f"/api/v2/titulos/{d['reembolso_titulo_id']}", headers=hdr)
    assert r_tit.status_code == 200
    assert r_tit.json()["titulo"]["valor_bruto"] == "120.00"

    # Não aceita mais comprovante após encerramento -> 409
    r_c3 = client.post(
        f"/api/v2/adiantamentos/{ad_id}/comprovantes",
        headers=hdr,
        json={"data_gasto": "2026-10-04", "tipo_doc": "RECIBO", "descricao": "Extra", "valor": "10.00"},
    )
    assert r_c3.status_code == 409


def test_conciliacao_bancaria_persistente_n1_e_1n_parcial(client):
    reg = _registrar(client, "Empresa Conciliacao", "11.222.333/0001-81", "conc@emp.com.br")
    hdr = {"Authorization": f"Bearer {reg['access_token']}"}

    r_cb = client.post(
        "/api/v2/contas-bancarias",
        headers=hdr,
        json={"empresa_id": reg["empresa_padrao_id"], "agencia": "1111", "conta": "22222", "dac": "3"},
    )
    conta_id = r_cb.json()["id"]

    t1 = client.post(
        "/api/v2/titulos",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "numero_documento": "T-400",
            "favorecido_nome": "Fornecedor A",
            "dt_emissao": "2026-10-01",
            "dt_vencimento": "2026-10-10",
            "valor_bruto": "400.00",
        },
    ).json()["id"]
    t2 = client.post(
        "/api/v2/titulos",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "numero_documento": "T-600",
            "favorecido_nome": "Fornecedor B",
            "dt_emissao": "2026-10-01",
            "dt_vencimento": "2026-10-10",
            "valor_bruto": "600.00",
        },
    ).json()["id"]
    t_parcial = client.post(
        "/api/v2/titulos",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "numero_documento": "T-1000",
            "favorecido_nome": "Fornecedor Parcial",
            "dt_emissao": "2026-10-01",
            "dt_vencimento": "2026-10-20",
            "valor_bruto": "1000.00",
        },
    ).json()["id"]

    ofx_bytes = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <STMTRS>
        <CURDEF>BRL</CURDEF>
        <BANKACCTFROM>
          <BANKID>341</BANKID>
          <ACCTID>22222</ACCTID>
          <ACCTTYPE>CHECKING</ACCTTYPE>
        </BANKACCTFROM>
        <BANKTRANLIST>
          <DTSTART>20261001000000</DTSTART>
          <DTEND>20261031000000</DTEND>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20261010120000</DTPOSTED>
            <TRNAMT>-1000.00</TRNAMT>
            <FITID>FITID_V2_1000</FITID>
            <MEMO>PAGTO LOTE A E B</MEMO>
          </STMTTRN>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20261015120000</DTPOSTED>
            <TRNAMT>-300.00</TRNAMT>
            <FITID>FITID_V2_300</FITID>
            <MEMO>SINAL FORNECEDOR PARCIAL</MEMO>
          </STMTTRN>
        </BANKTRANLIST>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
""".encode("utf-8")

    r_imp1 = client.post(
        "/api/v2/conciliacao/importar-ofx",
        headers=hdr,
        params={"conta_bancaria_id": conta_id},
        files={"arquivo": ("extrato.ofx", io.BytesIO(ofx_bytes), "application/x-ofx")},
    )
    assert r_imp1.status_code == 200, r_imp1.text
    assert r_imp1.json()["importadas"] == 2
    assert r_imp1.json()["duplicadas"] == 0

    # Reimportação do mesmo OFX deduplica FITIDs
    r_imp2 = client.post(
        "/api/v2/conciliacao/importar-ofx",
        headers=hdr,
        params={"conta_bancaria_id": conta_id},
        files={"arquivo": ("extrato.ofx", io.BytesIO(ofx_bytes), "application/x-ofx")},
    )
    assert r_imp2.status_code == 200
    assert r_imp2.json()["importadas"] == 0
    assert r_imp2.json()["duplicadas"] == 2

    txs = client.get("/api/v2/conciliacao/transacoes", headers=hdr, params={"conta_bancaria_id": conta_id}).json()["transacoes"]
    tx_1000 = next(t for t in txs if t["fitid"] == "FITID_V2_1000")
    tx_300 = next(t for t in txs if t["fitid"] == "FITID_V2_300")

    # N:1 -> 1 transação de 1000 liquida 2 títulos (400 + 600)
    r_vinc_n1 = client.post(
        "/api/v2/conciliacao/vincular",
        headers=hdr,
        json={
            "extrato_transacao_id": tx_1000["id"],
            "alocacoes": [
                {"titulo_id": t1, "valor_alocado": "400.00"},
                {"titulo_id": t2, "valor_alocado": "600.00"},
            ],
            "marcar_transacao_conciliada": True,
        },
    )
    assert r_vinc_n1.status_code == 200, r_vinc_n1.text
    assert r_vinc_n1.json()["transacao_conciliada"] is True
    assert all(item["status"] == "CONCILIADO" for item in r_vinc_n1.json()["titulos"])

    # 1:N parcial -> transação de 300 abate parcialmente título de 1000 -> status PARCIAL
    r_vinc_parcial = client.post(
        "/api/v2/conciliacao/vincular",
        headers=hdr,
        json={
            "extrato_transacao_id": tx_300["id"],
            "alocacoes": [{"titulo_id": t_parcial, "valor_alocado": "300.00"}],
            "marcar_transacao_conciliada": True,
        },
    )
    assert r_vinc_parcial.status_code == 200, r_vinc_parcial.text
    assert r_vinc_parcial.json()["titulos"][0]["status"] == "PARCIAL"
    assert r_vinc_parcial.json()["titulos"][0]["valor_pago"] == "300.00"


def test_dre_por_centro_custo_com_rateio_shopping_sem_duplicidade(client):
    reg = _registrar(client, "Rede Moda Bahia", "11.222.333/0001-81", "dre@redemoda.com.br")
    hdr = {"Authorization": f"Bearer {reg['access_token']}"}

    cc_sdb = client.post(
        "/api/v2/centros-custo",
        headers=hdr,
        json={"empresa_id": reg["empresa_padrao_id"], "codigo": "SDB", "nome": "Shopping da Bahia"},
    ).json()["id"]
    cc_ssa = client.post(
        "/api/v2/centros-custo",
        headers=hdr,
        json={"empresa_id": reg["empresa_padrao_id"], "codigo": "SSA", "nome": "Salvador Shopping"},
    ).json()["id"]

    # Boleto único shopping de R$ 18.450,00 rateado em 6 rubricas
    tit_shop = client.post(
        "/api/v2/titulos",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "centro_custo_id": cc_sdb,
            "tipo_documento": "FATURA",
            "numero_documento": "SDB-10-2026",
            "favorecido_nome": "Condominio Shopping da Bahia",
            "dt_emissao": "2026-10-01",
            "dt_vencimento": "2026-10-10",
            "valor_bruto": "18450.00",
        },
    ).json()["id"]

    r_rat = client.post(
        f"/api/v2/titulos/{tit_shop}/rateios",
        headers=hdr,
        json={
            "itens": [
                {"centro_custo_id": cc_sdb, "rubrica": "ALUGUEL", "valor": "7200.00"},
                {"centro_custo_id": cc_sdb, "rubrica": "CONDOMINIO", "valor": "4500.00"},
                {"centro_custo_id": cc_sdb, "rubrica": "FPP", "valor": "800.00"},
                {"centro_custo_id": cc_sdb, "rubrica": "IPTU", "valor": "1250.00"},
                {"centro_custo_id": cc_sdb, "rubrica": "ENERGIA", "valor": "3500.00"},
                {"centro_custo_id": cc_sdb, "rubrica": "AR_CONDICIONADO", "valor": "1200.00"},
            ]
        },
    )
    assert r_rat.status_code == 200

    # Despesa simples em outra loja de R$ 1.550,00
    client.post(
        "/api/v2/titulos",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "centro_custo_id": cc_ssa,
            "tipo_documento": "FRETE",
            "numero_documento": "CTE-99",
            "favorecido_nome": "Transportadora Bahia",
            "dt_emissao": "2026-10-05",
            "dt_vencimento": "2026-10-31",
            "valor_bruto": "1550.00",
        },
    )

    dre = client.get(
        "/api/v2/relatorios/dre-por-centro-custo",
        headers=hdr,
        params={"ano": 2026, "mes": 10},
    )
    assert dre.status_code == 200, dre.text
    d = dre.json()
    assert d["total_geral"] == "20000.00"
    assert d["por_rubrica"]["ALUGUEL"] == "7200.00"
    assert d["por_rubrica"]["ENERGIA"] == "3500.00"
    assert d["por_rubrica"]["FRETE"] == "1550.00"

    por_cc = {c["centro_custo_id"]: c for c in d["por_centro_custo"]}
    assert por_cc[cc_sdb]["total"] == "18450.00"
    assert por_cc[cc_ssa]["total"] == "1550.00"

    # Dashboard do mesmo mês contabiliza inclusive dia 31!
    dash = client.get("/api/v2/dashboard", headers=hdr, params={"ref_date": "2026-10-15"})
    assert dash.status_code == 200
    assert dash.json()["kpis"]["total_mes"] == "20000.00"


def test_audit_logs_e_isolamento_cross_tenant_novos_modulos(client):
    reg_a = _registrar(client, "Tenant Audit A", "11.222.333/0001-81", "aud_a@emp.com.br")
    reg_b = _registrar(client, "Tenant Audit B", "45.997.418/0001-53", "aud_b@emp.com.br")

    hdr_a = {"Authorization": f"Bearer {reg_a['access_token']}"}
    hdr_b = {"Authorization": f"Bearer {reg_b['access_token']}"}

    client.post(
        "/api/v2/fornecedores",
        headers=hdr_a,
        json={"razao_social": "Fornecedor Secreto A", "cnpj_cpf": "33.000.167/0001-01"},
    )
    client.post(
        "/api/v2/contas-bancarias",
        headers=hdr_a,
        json={"empresa_id": reg_a["empresa_padrao_id"], "agencia": "9999", "conta": "88888", "dac": "1"},
    )

    # Tenant B não vê nada de Tenant A
    assert client.get("/api/v2/fornecedores", headers=hdr_b).json()["total"] == 0
    assert len(client.get("/api/v2/contas-bancarias", headers=hdr_b).json()["contas"]) == 0
    assert len(client.get("/api/v2/conciliacao/transacoes", headers=hdr_b).json()["transacoes"]) == 0

    # Audit log de Tenant A mostra eventos de A, e Tenant B só vê o próprio register-tenant
    logs_a = client.get("/api/v2/audit-logs", headers=hdr_a).json()["logs"]
    logs_b = client.get("/api/v2/audit-logs", headers=hdr_b).json()["logs"]
    assert any(l["acao"] == "CRIAR_FORNECEDOR" for l in logs_a)
    assert all(l["acao"] == "REGISTER_TENANT" for l in logs_b)

    # FINANCEIRO não pode consultar audit-logs globais do tenant -> 403
    client.post(
        "/api/v2/usuarios",
        headers=hdr_a,
        json={"nome": "Fin A", "email": "fina@emp.com.br", "senha": "SenhaFin@2026", "role": "FINANCEIRO"},
    )
    tok_fin = client.post("/api/v2/auth/login", json={"email": "fina@emp.com.br", "senha": "SenhaFin@2026"}).json()["access_token"]
    r_aud_forbid = client.get("/api/v2/audit-logs", headers={"Authorization": f"Bearer {tok_fin}"})
    assert r_aud_forbid.status_code == 403
