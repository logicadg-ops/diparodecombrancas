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


def get_unidades(condominio_id):
    """Retorna as unidades cadastradas para um condomínio, direto do Supabase."""
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
    return resp.json()


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
