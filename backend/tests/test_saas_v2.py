import sqlite3
import sys
from pathlib import Path
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.core.storage import ObjectStorageService
from app.db.session import get_engine
from app.main import app

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.migrar_sqlite_para_postgres_saas import migrar_para_saas_v2


def _registrar_tenant(tc: TestClient, nome: str, cnpj: str, email: str, is_bpo: bool = False, parent_bpo_id: str | None = None):
    resp = tc.post(
        "/api/v2/auth/register-tenant",
        json={
            "nome_empresa": nome,
            "cnpj": cnpj,
            "is_bpo_partner": is_bpo,
            "parent_bpo_tenant_id": parent_bpo_id,
            "admin_nome": f"Admin {nome}",
            "admin_email": email,
            "admin_senha": "SenhaSaaS@2026",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_alembic_criou_todas_tabelas_saas(isolated_env):
    insp = inspect(get_engine())
    tabelas = set(insp.get_table_names())
    esperadas = {
        "tenants", "empresas", "centros_custo", "usuarios", "usuario_tenant_roles",
        "refresh_tokens", "plano_contas", "contas_bancarias", "fornecedores",
        "titulos", "titulo_impostos", "titulo_rateios", "titulo_aprovacoes",
        "despesas_recorrentes", "adiantamentos", "adiantamento_comprovantes",
        "extratos_bancarios", "extrato_transacoes", "conciliacao_itens",
        "documentos_anexos", "audit_logs", "alembic_version",
    }
    assert esperadas.issubset(tabelas)


def test_isolamento_multi_tenant_estrito(client):
    reg_a = _registrar_tenant(client, "Loja Moda Alpha", "11.222.333/0001-81", "alpha@loja.com.br")
    reg_b = _registrar_tenant(client, "Loja Moda Beta", "45.997.418/0001-53", "beta@loja.com.br")

    hdr_a = {"Authorization": f"Bearer {reg_a['access_token']}"}
    hdr_b = {"Authorization": f"Bearer {reg_b['access_token']}"}

    # Tenant A cria título
    r_tit = client.post(
        "/api/v2/titulos",
        headers=hdr_a,
        json={
            "empresa_id": reg_a["empresa_padrao_id"],
            "numero_documento": "NF-ALPHA-1",
            "favorecido_nome": "Shopping da Bahia",
            "dt_emissao": "2026-10-01",
            "dt_vencimento": "2026-10-15",
            "valor_bruto": "18450.00",
        },
    )
    assert r_tit.status_code == 201, r_tit.text
    tit_a_id = r_tit.json()["id"]

    # Tenant B NÃO enxerga título de Tenant A
    r_list_b = client.get("/api/v2/titulos", headers=hdr_b)
    assert r_list_b.status_code == 200
    assert r_list_b.json()["total"] == 0

    # Tenant B tentando ratear título do Tenant A -> 404
    r_cross_rateio = client.post(
        f"/api/v2/titulos/{tit_a_id}/rateios",
        headers=hdr_b,
        json={"itens": [{"rubrica": "ALUGUEL", "valor": "18450.00"}]},
    )
    assert r_cross_rateio.status_code == 404

    # Spoofing de X-Tenant-Id deve ser rejeitado com 403
    r_spoof = client.get(
        "/api/v2/empresas",
        headers={**hdr_b, "X-Tenant-Id": reg_a["tenant"]["id"]},
    )
    assert r_spoof.status_code == 403


def test_bpo_workspaces_e_switch_tenant(client):
    bpo = _registrar_tenant(client, "Contabilidade BPO Top", "11.222.333/0001-81", "bpo@contabil.com.br", is_bpo=True)
    cli = _registrar_tenant(
        client,
        "Cliente Varejo Sul",
        "45.997.418/0001-53",
        "dono@varejosul.com.br",
        is_bpo=False,
        parent_bpo_id=bpo["tenant"]["id"],
    )
    terceiro = _registrar_tenant(client, "Terceiro Isolado", "33.000.167/0001-01", "terceiro@outro.com.br")

    hdr_bpo = {"Authorization": f"Bearer {bpo['access_token']}"}
    r_ws = client.get("/api/v2/workspaces", headers=hdr_bpo)
    assert r_ws.status_code == 200
    ids_ws = {w["tenant_id"] for w in r_ws.json()["workspaces"]}
    assert bpo["tenant"]["id"] in ids_ws
    assert cli["tenant"]["id"] in ids_ws
    assert terceiro["tenant"]["id"] not in ids_ws

    # BPO troca para o workspace do cliente e cria centro de custo nele
    r_sw = client.post(
        "/api/v2/auth/switch-tenant",
        headers=hdr_bpo,
        json={"target_tenant_id": cli["tenant"]["id"]},
    )
    assert r_sw.status_code == 200, r_sw.text
    hdr_bpo_cli = {"Authorization": f"Bearer {r_sw.json()['access_token']}"}

    r_cc = client.post(
        "/api/v2/centros-custo",
        headers=hdr_bpo_cli,
        json={"empresa_id": cli["empresa_padrao_id"], "codigo": "SDB", "nome": "Loja Shopping da Bahia"},
    )
    assert r_cc.status_code == 201, r_cc.text

    # Terceiro não autorizado tentando switch-tenant para cliente -> 403
    r_denied = client.post(
        "/api/v2/auth/switch-tenant",
        headers={"Authorization": f"Bearer {terceiro['access_token']}"},
        json={"target_tenant_id": cli["tenant"]["id"]},
    )
    assert r_denied.status_code == 403


def test_refresh_token_rotacao_e_deteccao_de_reuso(isolated_env):
    with TestClient(app) as tc:
        reg = _registrar_tenant(tc, "Empresa Refresh", "11.222.333/0001-81", "refresh@empresa.com.br")
        rt1 = tc.cookies.get("jr_refresh_token")
        assert rt1

        # 1a rotação legítima: rt1 -> rt2
        r_ref1 = tc.post("/api/v2/auth/refresh")
        assert r_ref1.status_code == 200, r_ref1.text
        rt2 = tc.cookies.get("jr_refresh_token")
        assert rt2 and rt2 != rt1

        # Ataque de reuso com o rt1 já rotacionado -> 401 e revoga família inteira!
        tc.cookies.clear()
        r_reuse = tc.post("/api/v2/auth/refresh", json={"refresh_token": rt1})
        assert r_reuse.status_code == 401

        # Agora nem mesmo rt2 pode mais ser usado porque a família foi revogada
        tc.cookies.clear()
        r_after = tc.post("/api/v2/auth/refresh", json={"refresh_token": rt2})
        assert r_after.status_code == 401


def test_rateio_boleto_shopping_trava_decimal_exata(client):
    reg = _registrar_tenant(client, "Rede Shopping", "11.222.333/0001-81", "shopping@rede.com.br")
    hdr = {"Authorization": f"Bearer {reg['access_token']}"}

    r_cc = client.post(
        "/api/v2/centros-custo",
        headers=hdr,
        json={"empresa_id": reg["empresa_padrao_id"], "codigo": "LOJA-SDB", "nome": "Shopping da Bahia"},
    )
    cc_id = r_cc.json()["id"]

    r_tit = client.post(
        "/api/v2/titulos",
        headers=hdr,
        json={
            "empresa_id": reg["empresa_padrao_id"],
            "centro_custo_id": cc_id,
            "numero_documento": "BOL-SHOP-09",
            "favorecido_nome": "Condomínio Shopping da Bahia",
            "dt_emissao": "2026-09-01",
            "dt_vencimento": "2026-09-10",
            "valor_bruto": "18450.00",
        },
    )
    tit_id = r_tit.json()["id"]

    # Soma errada por 1 centavo -> 422
    r_err = client.post(
        f"/api/v2/titulos/{tit_id}/rateios",
        headers=hdr,
        json={
            "itens": [
                {"centro_custo_id": cc_id, "rubrica": "ALUGUEL", "valor": "10000.00"},
                {"centro_custo_id": cc_id, "rubrica": "CONDOMINIO", "valor": "8449.99"},
            ]
        },
    )
    assert r_err.status_code == 422

    # Soma exata = 18450.00 -> 200
    r_ok = client.post(
        f"/api/v2/titulos/{tit_id}/rateios",
        headers=hdr,
        json={
            "itens": [
                {"centro_custo_id": cc_id, "rubrica": "ALUGUEL", "valor": "7200.00"},
                {"centro_custo_id": cc_id, "rubrica": "CONDOMINIO", "valor": "4500.00"},
                {"centro_custo_id": cc_id, "rubrica": "FPP", "valor": "800.00"},
                {"centro_custo_id": cc_id, "rubrica": "IPTU", "valor": "1250.00"},
                {"centro_custo_id": cc_id, "rubrica": "ENERGIA", "valor": "3500.00"},
                {"centro_custo_id": cc_id, "rubrica": "AR_CONDICIONADO", "valor": "1200.00"},
            ]
        },
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["total_rateado"] == "18450.00"
    assert len(r_ok.json()["itens"]) == 6


def test_object_storage_r2_sigv4_e_isolamento_tenant(client):
    svc = ObjectStorageService(
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        bucket="jr-fincloud-docs",
        access_key_id="AKIA_TEST",
        secret_access_key="SECRET_TEST",
        region="auto",
    )
    with pytest.raises(ValueError):
        svc.validar_arquivo("malware.exe", "application/x-msdownload", 1024)
    with pytest.raises(ValueError):
        svc.validar_arquivo("gigante.pdf", "application/pdf", 20 * 1024 * 1024)

    reg_a = _registrar_tenant(client, "Storage Alpha", "11.222.333/0001-81", "st_a@emp.com")
    reg_b = _registrar_tenant(client, "Storage Beta", "45.997.418/0001-53", "st_b@emp.com")
    hdr_a = {"Authorization": f"Bearer {reg_a['access_token']}"}
    hdr_b = {"Authorization": f"Bearer {reg_b['access_token']}"}

    r_up = client.post(
        "/api/v2/storage/presign-upload",
        headers=hdr_a,
        json={
            "empresa_id": reg_a["empresa_padrao_id"],
            "entidade_tipo": "TITULO",
            "entidade_id": "tit-123",
            "nome_original": "danfe_nota_123.pdf",
            "mime_type": "application/pdf",
            "tamanho_bytes": 12450,
            "sha256": "a" * 64,
        },
    )
    assert r_up.status_code == 200, r_up.text
    anexo_id = r_up.json()["anexo_id"]
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in r_up.json()["upload_url"]
    assert "X-Amz-Signature=" in r_up.json()["upload_url"]
    assert r_up.json()["storage_key"].startswith(f"tenants/{reg_a['tenant']['id']}/")

    r_dl_a = client.get(f"/api/v2/storage/{anexo_id}/presign-download", headers=hdr_a)
    assert r_dl_a.status_code == 200
    assert "X-Amz-Signature=" in r_dl_a.json()["download_url"]

    # Tenant B não pode gerar URL de download do anexo de Tenant A
    r_dl_b = client.get(f"/api/v2/storage/{anexo_id}/presign-download", headers=hdr_b)
    assert r_dl_b.status_code == 404


def test_etl_migracao_sqlite_para_saas_v2(tmp_path):
    src_db = tmp_path / "origem.db"
    dst_db = tmp_path / "destino_v2.db"

    conn = sqlite3.connect(src_db)
    conn.executescript(
        """
        CREATE TABLE usuarios (id INTEGER PRIMARY KEY, nome TEXT, email TEXT, senha_hash TEXT, perfil TEXT, ativo INTEGER);
        CREATE TABLE fornecedores (id INTEGER PRIMARY KEY, razao_social TEXT, nome_fantasia TEXT, cnpj_cpf TEXT, tipo TEXT, categoria TEXT, ccusto TEXT, responsavel TEXT, forma_pgto TEXT, pix_chave TEXT, dados_banco TEXT, cod_operacao TEXT, cnae TEXT, item_lc116 TEXT, ativo INTEGER);
        CREATE TABLE notas (id INTEGER PRIMARY KEY, numero_tx TEXT, tipo TEXT, fornecedor TEXT, cnpj TEXT, dt_emissao TEXT, dt_vencimento TEXT, dt_pagamento TEXT, valor_bruto REAL, valor_liquido REAL, status TEXT, categoria TEXT, observacao TEXT, responsavel TEXT, descricao TEXT, filial TEXT, forma_pgto TEXT, cod_barras TEXT, pix_chave TEXT, numero_nf TEXT, is_previsao INTEGER, conciliada INTEGER, cod_operacao TEXT, cnae TEXT, item_lc116 TEXT, valor_pago REAL, juros_multa REAL, desconto REAL);
        CREATE TABLE nota_impostos (id INTEGER PRIMARY KEY, nota_id INTEGER, tipo TEXT, aliquota REAL, valor REAL, dt_venc_imp TEXT, status TEXT, numero_doc TEXT);
        INSERT INTO usuarios VALUES (1, 'Gestor', 'gestor@emp.com', 'hash', 'MASTER', 1);
        INSERT INTO fornecedores VALUES (1, 'Tecidos Sul Ltda', 'Tecidos Sul', '11.222.333/0001-81', 'FORNECEDOR', 'MP', 'MATRIZ', 'Ana', 'PIX', 'pix@sul', '', '1102', '', '', 1);
        INSERT INTO notas VALUES (1, 'TX_1', 'FORNECEDOR', 'Tecidos Sul Ltda', '11.222.333/0001-81', '2026-10-01', '2026-10-20', NULL, 1500.55, 1425.55, 'PENDENTE', 'NFSE', '', 'Ana', 'Compra tecidos', 'MATRIZ', 'PIX', '', '', 'NF-500', 0, 0, '1102', '', '', 0, 0, 0);
        INSERT INTO nota_impostos VALUES (1, 1, 'ISS', 5.0, 75.00, '2026-11-10', 'PENDENTE', 'NF-500');
        """
    )
    conn.commit()
    conn.close()

    stats = migrar_para_saas_v2(
        sqlite_path=str(src_db),
        target_url=f"sqlite:///{dst_db}",
        tenant_nome="Empresa ETL",
        tenant_slug="empresa-etl",
        empresa_cnpj="11222333000181",
        plano_contas_json=str(ROOT_DIR / "frontend" / "grupos_categorias.json"),
    )
    assert stats["titulos"] == 1
    assert stats["impostos"] == 1
    assert stats["fornecedores"] == 1
    assert stats["plano_contas"] > 50
    assert stats["soma_origem_bruto"] == Decimal("1500.55")
    assert stats["soma_destino_bruto"] == Decimal("1500.55")

    # Rodar 2a vez deve ser idempotente (0 duplicações)
    stats2 = migrar_para_saas_v2(
        sqlite_path=str(src_db),
        target_url=f"sqlite:///{dst_db}",
        tenant_nome="Empresa ETL",
        tenant_slug="empresa-etl",
        empresa_cnpj="11222333000181",
        plano_contas_json=str(ROOT_DIR / "frontend" / "grupos_categorias.json"),
    )
    assert stats2["titulos"] == 0
    assert stats2["fornecedores"] == 0
