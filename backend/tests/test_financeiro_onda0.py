import io
from decimal import Decimal
from app.core.database import get_connection


def _criar_doc(client, auth_headers, numero_doc, valor_bruto, vencimento, linha_dig="", impostos=None):
    payload = {
        "tipo_doc": "NFSE",
        "numero_doc": numero_doc,
        "favorecido": f"Fornecedor {numero_doc}",
        "cnpj": "11.222.333/0001-81",
        "data_emissao": "2026-10-01",
        "vencimento": vencimento,
        "valor_bruto": valor_bruto,
        "linha_dig": linha_dig,
        "impostos": impostos or [],
    }
    resp = client.post("/api/v1/gravar-documento", headers=auth_headers, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["nota_id"]


def test_exportar_lote_itau_sem_fallback_perigoso_e_linhas_240(client, auth_headers):
    # Título em outra data COM código de barras válido (44 dígitos)
    barra_44 = "34191000000000150001750001234567890123456789"
    _criar_doc(client, auth_headers, "NF-01", 150.00, "2026-10-10", linha_dig=barra_44)

    # Pedir lote para uma data SEM títulos -> deve retornar 404 (NÃO pode puxar títulos de outra data!)
    r_vazio = client.get(
        "/api/v1/exportar-lote-itau",
        headers=auth_headers,
        params={
            "data_pagamento": "2026-10-25",
            "cnpj_empresa": "11.222.333/0001-81",
            "razao_social": "EMPRESA TESTE LTDA",
            "agencia": "1234",
            "conta": "56789",
            "dac": "1",
        },
    )
    assert r_vazio.status_code == 404

    # Pedir lote para data correta sem configurar conta -> deve retornar 400
    r_sem_conta = client.get(
        "/api/v1/exportar-lote-itau",
        headers=auth_headers,
        params={"data_pagamento": "2026-10-10"},
    )
    assert r_sem_conta.status_code == 400

    # Pedir lote para data correta com dados bancários -> 200 e todas linhas com exatamente 240 chars
    r_ok = client.get(
        "/api/v1/exportar-lote-itau",
        headers=auth_headers,
        params={
            "data_pagamento": "2026-10-10",
            "cnpj_empresa": "11.222.333/0001-81",
            "razao_social": "EMPRESA TESTE LTDA",
            "agencia": "1234",
            "conta": "56789",
            "dac": "1",
        },
    )
    assert r_ok.status_code == 200, r_ok.text
    linhas = [ln for ln in r_ok.text.splitlines() if ln]
    assert len(linhas) == 5  # header arq, header lote, 1 detalhe J, trailler lote, trailler arq
    for ln in linhas:
        assert len(ln) == 240
    # Valor no trailler de lote (posições 24..41 em 1-indexed -> [23:41]) == 15000 centavos
    assert linhas[3][23:41] == "000000000000015000"


def test_conciliar_grupo_n1_rateio_sem_multiplicacao(client, auth_headers):
    id1 = _criar_doc(client, auth_headers, "NF-101", 100.00, "2026-10-10")
    id2 = _criar_doc(client, auth_headers, "NF-102", 200.00, "2026-10-10")
    id3 = _criar_doc(client, auth_headers, "NF-103", 300.00, "2026-10-10")

    # Extrato pagou R$ 630,00 (soma = 600,00 + juros = 30,00)
    resp = client.post(
        "/api/v1/conciliar-grupo",
        headers=auth_headers,
        json={
            "titulo_ids": [id1, id2, id3],
            "extrato_fitid": "FITID_GRP_001",
            "extrato_valor": 630.00,
            "extrato_data": "2026-10-10",
            "extrato_memo": "PAGTO LOTE FORNECEDORES",
        },
    )
    assert resp.status_code == 200, resp.text

    conn = get_connection()
    rows = conn.cursor().execute(
        "SELECT id, valor_pago, juros_multa, desconto FROM notas WHERE id IN (?, ?, ?) ORDER BY id",
        (id1, id2, id3),
    ).fetchall()
    conn.close()

    soma_pago = sum(Decimal(str(r["valor_pago"])) for r in rows)
    soma_juros = sum(Decimal(str(r["juros_multa"])) for r in rows)
    soma_desc = sum(Decimal(str(r["desconto"])) for r in rows)

    assert soma_pago == Decimal("630.00")
    assert soma_juros == Decimal("30.00")
    assert soma_desc == Decimal("0.00")

    # Reenvio do mesmo FITID deve ser bloqueado (409)
    id4 = _criar_doc(client, auth_headers, "NF-104", 50.00, "2026-10-10")
    resp_dup = client.post(
        "/api/v1/conciliar-grupo",
        headers=auth_headers,
        json={
            "titulo_ids": [id4],
            "extrato_fitid": "FITID_GRP_001",
            "extrato_valor": 50.00,
            "extrato_data": "2026-10-10",
        },
    )
    assert resp_dup.status_code == 409


def test_conciliar_ofx_deduplicacao_fitid(client, auth_headers):
    _criar_doc(client, auth_headers, "NF-OFX-1", 250.00, "2026-10-15")
    _criar_doc(client, auth_headers, "NF-OFX-2", 250.00, "2026-10-20")

    ofx_content = """OFXHEADER:100
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
          <ACCTID>12345</ACCTID>
          <ACCTTYPE>CHECKING</ACCTTYPE>
        </BANKACCTFROM>
        <BANKTRANLIST>
          <DTSTART>20261001000000</DTSTART>
          <DTEND>20261031000000</DTEND>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20261015120000</DTPOSTED>
            <TRNAMT>-250.00</TRNAMT>
            <FITID>FITID_UNICO_250</FITID>
            <MEMO>PAGTO FORNECEDOR</MEMO>
          </STMTTRN>
        </BANKTRANLIST>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
""".encode("utf-8")

    r1 = client.post(
        "/api/v1/conciliar-ofx",
        headers=auth_headers,
        files={"arquivo": ("extrato.ofx", io.BytesIO(ofx_content), "application/x-ofx")},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["total_conciliados"] == 1

    # Reimportar o mesmo OFX não pode baixar o segundo título de R$ 250,00!
    r2 = client.post(
        "/api/v1/conciliar-ofx",
        headers=auth_headers,
        files={"arquivo": ("extrato.ofx", io.BytesIO(ofx_content), "application/x-ofx")},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["total_conciliados"] == 0
    assert r2.json()["total_ja_conciliados"] == 1


def test_dashboard_contabiliza_dia_31_e_edicao_preserva_retencoes(client, auth_headers):
    nota_id = _criar_doc(
        client,
        auth_headers,
        "NF-31",
        1000.00,
        "2026-10-31",
        impostos=[{"tipo": "ISS", "aliquota": 5.0, "valor": 50.00}],
    )

    dash = client.get("/api/v1/dashboard", headers=auth_headers, params={"ref_date": "2026-10-15"})
    assert dash.status_code == 200, dash.text
    kpis = dash.json()["kpis"]
    # Valor líquido = 950.00, vencimento dia 31 DEVE entrar no total_mes
    assert kpis["total_mes"] == 950.00

    # Editar valor bruto para 2000.00 -> deve manter retenção de 50.00 -> valor_liquido = 1950.00
    r_edit = client.put(
        f"/api/v1/titulos/NOTA_{nota_id}",
        headers=auth_headers,
        json={"valor": 2000.00},
    )
    assert r_edit.status_code == 200, r_edit.text

    conn = get_connection()
    row = conn.cursor().execute("SELECT valor_bruto, valor_liquido FROM notas WHERE id = ?", (nota_id,)).fetchone()
    conn.close()
    assert row["valor_bruto"] == 2000.00
    assert row["valor_liquido"] == 1950.00
