from app.core.database import get_connection
from app.core.security import gerar_hash_senha, verificar_senha


def test_rotas_negocio_exigem_autenticacao(client):
    endpoints = [
        ("GET", "/api/v1/titulos"),
        ("GET", "/api/contas-a-pagar/titulos"),
        ("GET", "/api/v1/dashboard"),
        ("GET", "/api/v1/fornecedores"),
        ("GET", "/api/v1/previsoes"),
        ("GET", "/api/v1/adiantamentos"),
        ("GET", "/api/v1/exportar-lote-itau"),
        ("POST", "/api/v1/gravar-documento"),
        ("POST", "/api/v1/fornecedores"),
        ("POST", "/api/v1/conciliar-grupo"),
    ]
    for method, url in endpoints:
        resp = client.request(method, url)
        assert resp.status_code == 401, f"Rota {method} {url} deveria retornar 401 sem token, retornou {resp.status_code}"


def test_backdoor_removido_nao_cria_usuario_master(client):
    resp = client.post("/api/v1/auth/login", json={"email": "roberto_atacante@evil.com", "senha": "Bimer@2026"})
    assert resp.status_code == 401

    resp_vazio = client.post("/api/v1/auth/login", json={"email": "", "senha": "Bimer@2026"})
    assert resp_vazio.status_code == 401

    conn = get_connection()
    total = conn.cursor().execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
    conn.close()
    assert total == 0


def test_rate_limit_login_429(client):
    conn = get_connection()
    conn.cursor().execute(
        "INSERT INTO usuarios (nome, email, senha_hash, perfil, ativo) VALUES (?, ?, ?, ?, ?)",
        ("Admin", "admin@empresa.com.br", gerar_hash_senha("SenhaCorreta@123"), "MASTER", 1),
    )
    conn.commit()
    conn.close()

    for _ in range(5):
        r = client.post("/api/v1/auth/login", json={"email": "admin@empresa.com.br", "senha": "errada"})
        assert r.status_code == 401

    bloqueado = client.post("/api/v1/auth/login", json={"email": "admin@empresa.com.br", "senha": "SenhaCorreta@123"})
    assert bloqueado.status_code == 429


def test_headers_seguranca_e_healthcheck(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_validacao_cpf_cnpj_422_e_audit_log(client, auth_headers):
    # CNPJ inválido deve retornar 422
    r_inv = client.post(
        "/api/v1/fornecedores",
        headers=auth_headers,
        json={"razao_social": "Fornecedor Teste", "cnpj_cpf": "11.111.111/1111-11"},
    )
    assert r_inv.status_code == 422

    # CNPJ válido (11.222.333/0001-81) deve gravar e gerar audit_log
    r_ok = client.post(
        "/api/v1/fornecedores",
        headers=auth_headers,
        json={"razao_social": "Fornecedor Válido Ltda", "cnpj_cpf": "11.222.333/0001-81"},
    )
    assert r_ok.status_code == 200, r_ok.text

    # Gravar documento com CNPJ inválido -> 422
    r_doc_inv = client.post(
        "/api/v1/gravar-documento",
        headers=auth_headers,
        json={
            "tipo_doc": "NFSE",
            "numero_doc": "NF-100",
            "favorecido": "Prestador Teste",
            "cnpj": "99.999.999/9999-99",
            "data_emissao": "2026-10-01",
            "vencimento": "2026-10-15",
            "valor_bruto": 1000.0,
        },
    )
    assert r_doc_inv.status_code == 422

    conn = get_connection()
    logs = conn.cursor().execute("SELECT acao, entidade FROM audit_log").fetchall()
    conn.close()
    acoes = {(row["acao"], row["entidade"]) for row in logs}
    assert ("LOGIN_SUCESSO", "usuarios") in acoes
    assert ("SALVAR_FORNECEDOR", "fornecedores") in acoes


def test_upgrade_hash_legado_no_login(client):
    import hashlib
    import os
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", b"SenhaAntiga@123", salt, 100_000)
    legacy_hash = f"{salt.hex()}:{key.hex()}"
    assert verificar_senha("SenhaAntiga@123", legacy_hash)

    conn = get_connection()
    conn.cursor().execute(
        "INSERT INTO usuarios (nome, email, senha_hash, perfil, ativo) VALUES (?, ?, ?, ?, ?)",
        ("Legado", "legado@empresa.com.br", legacy_hash, "MASTER", 1),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/v1/auth/login", json={"email": "legado@empresa.com.br", "senha": "SenhaAntiga@123"})
    assert resp.status_code == 200

    conn = get_connection()
    novo_hash = conn.cursor().execute("SELECT senha_hash FROM usuarios WHERE email = ?", ("legado@empresa.com.br",)).fetchone()[0]
    conn.close()
    assert novo_hash.startswith("pbkdf2_sha256$600000$")
