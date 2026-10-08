import requests

import config


def _headers():
    return {
        "apikey": config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
    }


def get_condominios():
    """Retorna os condomínios cadastrados na tabela `condominio` do Supabase."""
    resp = requests.get(
        f"{config.SUPABASE_URL}/rest/v1/condominio",
        headers=_headers(),
        params={
            "select": "idcontominio,ds_condominio,status,unidades",
            "order": "ds_condominio",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return [
        {
            "id": row["idcontominio"],
            "nome": row["ds_condominio"],
            "status": row.get("status"),
            "total_unidades": row.get("unidades"),
        }
        for row in resp.json()
        if row.get("idcontominio")
    ]


def listar_condominios_cadastro():
    """Retorna os condomínios com os campos usados na tela de cadastro
    (inclui o `id` interno, usado nas rotas de editar/excluir)."""
    resp = requests.get(
        f"{config.SUPABASE_URL}/rest/v1/condominio",
        headers=_headers(),
        params={
            "select": "id,idcontominio,ds_condominio,cnpj,cidade,uf,status,unidades",
            "order": "ds_condominio",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_condominio_cadastro(id_interno):
    """Retorna um condomínio pelo `id` interno (chave primária da tabela),
    usado na tela de edição do cadastro."""
    resp = requests.get(
        f"{config.SUPABASE_URL}/rest/v1/condominio",
        headers=_headers(),
        params={
            "id": f"eq.{id_interno}",
            "select": "id,idcontominio,ds_condominio,cnpj,endereco,end_numero,bairro,cidade,uf,status,unidades",
            "limit": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def criar_condominio(dados):
    resp = requests.post(
        f"{config.SUPABASE_URL}/rest/v1/condominio",
        headers={**_headers(), "Content-Type": "application/json"},
        json=dados,
        timeout=15,
    )
    resp.raise_for_status()


def atualizar_condominio(id_interno, dados):
    resp = requests.patch(
        f"{config.SUPABASE_URL}/rest/v1/condominio",
        headers={**_headers(), "Content-Type": "application/json"},
        params={"id": f"eq.{id_interno}"},
        json=dados,
        timeout=15,
    )
    resp.raise_for_status()


def excluir_condominio(id_interno):
    resp = requests.delete(
        f"{config.SUPABASE_URL}/rest/v1/condominio",
        headers=_headers(),
        params={"id": f"eq.{id_interno}"},
        timeout=15,
    )
    resp.raise_for_status()


def get_condominio(condominio_id):
    """Retorna um único condomínio pelo id da Condomob (idcontominio)."""
    resp = requests.get(
        f"{config.SUPABASE_URL}/rest/v1/condominio",
        headers=_headers(),
        params={
            "idcontominio": f"eq.{condominio_id}",
            "select": "idcontominio,ds_condominio,status,unidades",
            "limit": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    row = rows[0]
    return {
        "id": row["idcontominio"],
        "nome": row["ds_condominio"],
        "status": row.get("status"),
        "total_unidades": row.get("unidades"),
    }


def _deduplicar_unidades(unidades):
    """Alguns condomínios têm registros duplicados pra mesma unidade (mesmo
    id_condominio + unidade). Processar os duplicados em dobro foi o que
    esgotou a memória do dyno da Heroku num condomínio com 700 linhas pra
    348 unidades reais. Mantém, de cada grupo, a linha mais completa
    (com mais campos preenchidos)."""
    por_chave = {}
    for u in unidades:
        chave = (u.get("unidade") or "").strip()
        atual = por_chave.get(chave)
        if atual is None or sum(1 for v in u.values() if v) > sum(1 for v in atual.values() if v):
            por_chave[chave] = u
    return list(por_chave.values())


def get_unidades(condominio_id):
    """Retorna as unidades cadastradas para um condomínio, direto do
    Supabase, já sem duplicatas (mesma unidade com mais de um registro)."""
    resp = requests.get(
        f"{config.SUPABASE_URL}/rest/v1/unidades",
        headers=_headers(),
        params={
            "id_condominio": f"eq.{condominio_id}",
            "select": "*",
            "order": "bloco,apto",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return _deduplicar_unidades(resp.json())


def get_unidade(condominio_id, unidade):
    """Retorna uma única unidade pelo código (ex: '03-LT-003') dentro de um condomínio."""
    resp = requests.get(
        f"{config.SUPABASE_URL}/rest/v1/unidades",
        headers=_headers(),
        params={
            "id_condominio": f"eq.{condominio_id}",
            "unidade": f"eq.{unidade}",
            "select": "*",
            "limit": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def get_unidade_por_id(id_unidade):
    resp = requests.get(
        f"{config.SUPABASE_URL}/rest/v1/unidades",
        headers=_headers(),
        params={"id": f"eq.{id_unidade}", "select": "*", "limit": 1},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def criar_unidades(lista_dados):
    if not lista_dados:
        return
    colunas = set().union(*(d.keys() for d in lista_dados))
    lista_dados = [{c: d.get(c) for c in colunas} for d in lista_dados]
    resp = requests.post(
        f"{config.SUPABASE_URL}/rest/v1/unidades",
        headers={**_headers(), "Content-Type": "application/json"},
        json=lista_dados,
        timeout=30,
    )
    resp.raise_for_status()


def atualizar_unidade_por_id(id_unidade, dados):
    resp = requests.patch(
        f"{config.SUPABASE_URL}/rest/v1/unidades",
        headers={**_headers(), "Content-Type": "application/json"},
        params={"id": f"eq.{id_unidade}"},
        json=dados,
        timeout=15,
    )
    resp.raise_for_status()


def excluir_unidade_por_id(id_unidade):
    resp = requests.delete(
        f"{config.SUPABASE_URL}/rest/v1/unidades",
        headers=_headers(),
        params={"id": f"eq.{id_unidade}"},
        timeout=15,
    )
    resp.raise_for_status()


def atualizar_unidade(condominio_id, unidade, proprietario, email, fone1=None, fone2=None):
    """Atualiza nome do proprietário, email e telefones de uma unidade. Se
    houver registros duplicados pra essa unidade (condominio_id + unidade),
    todos são atualizados juntos."""
    resp = requests.patch(
        f"{config.SUPABASE_URL}/rest/v1/unidades",
        headers={**_headers(), "Content-Type": "application/json"},
        params={
            "id_condominio": f"eq.{condominio_id}",
            "unidade": f"eq.{unidade}",
        },
        json={"proprietario": proprietario, "email": email, "fone1": fone1, "fone2": fone2},
        timeout=15,
    )
    resp.raise_for_status()


def registrar_envio(
    condominio_id,
    nome_condominio,
    unidade,
    mes_referencia,
    sucesso,
    contato,
    tipo="email",
    mensagem_erro=None,
    valor=None,
    link=None,
    linha_digitavel=None,
    vencimento=None,
):
    """Registra o disparo (sucesso ou falha) na tabela `enviowhatapp`, usada
    como log de todos os envios (WhatsApp e, agora, email). `tipo` indica o
    canal ('email' ou, no futuro, 'whatsapp') e `contato` guarda o email ou
    telefone usado no disparo."""
    resp = requests.post(
        f"{config.SUPABASE_URL}/rest/v1/enviowhatapp",
        headers={**_headers(), "Content-Type": "application/json"},
        json={
            "condominio": condominio_id,
            "nomecondominio": nome_condominio,
            "unidade": unidade,
            "mesref": mes_referencia,
            "log": "Boleto Enviado" if sucesso else f"Falha no envio: {mensagem_erro}",
            "valor": valor,
            "link": link,
            "linhaDigitavel": linha_digitavel,
            "vencimento": vencimento,
            "contato": contato,
            "tipo": tipo,
        },
        timeout=15,
    )
    resp.raise_for_status()
