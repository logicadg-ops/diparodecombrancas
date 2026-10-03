import requests

import config


class CobrancaNaoEncontrada(Exception):
    """Não há cobrança em aberto para a unidade/CPF informados."""


def consultar_cobranca_aberta(condominio_id, unidade, cpf_cnpj):
    """Consulta a última cobrança em aberto de uma unidade na API da Condomob.

    Retorna um dict com: documento, unidade, vencimento, valor, link, pix,
    linhaDigitavel. Levanta CobrancaNaoEncontrada se não houver taxa em aberto.
    """
    resp = requests.get(
        f"{config.CONDOMOB_BASE_URL}/ws/chatbot/cobranca/latest/cpfCnpj",
        headers={
            "accept": "*/*",
            "administradora": config.CONDOMOB_ADMINISTRADORA_ID,
            "Authorization": config.CONDOMOB_AUTH_TOKEN,
        },
        params={
            "administradora": config.CONDOMOB_ADMINISTRADORA_ID,
            "condominio": condominio_id,
            "cpfCnpj": cpf_cnpj,
            "unidade": unidade,
        },
        timeout=20,
    )

    if resp.status_code in (400, 404):
        raise CobrancaNaoEncontrada(f"Sem cobrança em aberto para a unidade {unidade}")

    resp.raise_for_status()
    data = resp.json()

    if not data:
        raise CobrancaNaoEncontrada(f"Sem cobrança em aberto para a unidade {unidade}")

    data["link"] = _link_download_direto(data.get("link", ""))
    return data


def _link_download_direto(link):
    """Insere /download antes da querystring para pular a tela de checkout
    e exibir o boleto diretamente."""
    if not link or "/boleto/download" in link:
        return link
    return link.replace("/boleto?", "/boleto/download?", 1)
