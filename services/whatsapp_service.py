"""Envio de boletos pelo WhatsApp Oficial (Coexy / Cloud API da Meta).

O boleto vai como template aprovado (`boleto_condominio_pdf`), com o PDF no
cabeçalho. Como template, pode ser enviado a qualquer hora, mesmo sem o
morador ter falado com o número nas últimas 24h.

O PDF nunca é enviado por link: a Meta exige resposta 200 do servidor do
arquivo e o Condomob responde 202 no download do boleto. Por isso o PDF é
baixado aqui, enviado para a Coexy (`/media`) e a mensagem usa o id da mídia.
"""
import re
from datetime import datetime

import requests

import config


class EnvioWhatsappFalhou(Exception):
    pass


class BoletoJaEnviado(Exception):
    """A Coexy recusou por repetição: este boleto já foi enviado para este
    telefone (mesma chave de idempotência nos últimos 7 dias)."""


def whatsapp_configurado():
    return bool(config.COEXY_CHANNEL_TOKEN)


def _base():
    return f"{config.COEXY_META_PROXY_URL}/{config.COEXY_PHONE_NUMBER_ID}"


def _auth():
    return {"Authorization": f"Bearer {config.COEXY_CHANNEL_TOKEN}"}


def normalizar_telefone(bruto):
    """Converte um telefone do cadastro para o formato da Cloud API
    (55 + DDD + número). Aceita com ou sem 55, com máscara, fixo ou celular.
    Retorna None se não parecer um telefone brasileiro válido."""
    digitos = re.sub(r"\D", "", bruto or "")
    if digitos.startswith("0"):
        digitos = digitos.lstrip("0")
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    if not digitos.startswith("55") or len(digitos) not in (12, 13):
        return None
    if len(digitos) == 12 and digitos[4] in "2345":
        return None  # telefone fixo: não tem WhatsApp
    return digitos


def telefones_da_unidade(unidade_row):
    """Telefones do proprietário (fone1 e fone2), já normalizados e sem
    repetição. Cada campo pode trazer mais de um número separado por
    ';', ',' ou '/'."""
    telefones = []
    for campo in ("fone1", "fone2"):
        for parte in re.split(r"[;,/]", unidade_row.get(campo) or ""):
            numero = normalizar_telefone(parte)
            if numero and numero not in telefones:
                telefones.append(numero)
    return telefones


def formatar_telefone(numero):
    m = re.match(r"^55(\d{2})(\d{4,5})(\d{4})$", numero or "")
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}" if m else (numero or "")


def baixar_pdf(url):
    """Baixa o PDF do boleto (link de download do Condomob ou URL do R2)."""
    resp = requests.get(url, timeout=30)
    if resp.status_code not in (200, 202) or not resp.content.startswith(b"%PDF"):
        raise EnvioWhatsappFalhou(f"Não foi possível baixar o PDF do boleto (HTTP {resp.status_code})")
    return resp.content


def subir_pdf(pdf_bytes, nome_arquivo):
    """Envia o PDF para a Coexy e devolve o id da mídia (vale para vários
    envios do mesmo boleto)."""
    resp = requests.post(
        f"{_base()}/media",
        headers=_auth(),
        data={"messaging_product": "whatsapp", "type": "application/pdf"},
        files={"file": (nome_arquivo, pdf_bytes, "application/pdf")},
        timeout=60,
    )
    if resp.status_code >= 300 or "id" not in (resp.json() if resp.content else {}):
        raise EnvioWhatsappFalhou(f"Coexy recusou o PDF ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["id"]


def _param(texto, padrao="-"):
    """Variável de template: a Meta não aceita vazio, quebra de linha, tab
    nem mais de 4 espaços seguidos."""
    texto = re.sub(r"[\r\n\t]+", " ", str(texto or "")).strip()
    texto = re.sub(r" {2,}", " ", texto)
    return (texto or padrao)[:1000]


def _formatar_valor(valor):
    if valor in (None, ""):
        return "-"
    try:
        return f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(valor)


def _formatar_data(data_iso):
    if not data_iso:
        return "-"
    try:
        return datetime.strptime(data_iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return data_iso


def nome_arquivo_boleto(unidade, vencimento):
    venc = _formatar_data(vencimento).replace("/", "-")
    unidade_limpa = re.sub(r"[^A-Za-z0-9_-]", "", str(unidade or "")) or "unidade"
    return f"Boleto {unidade_limpa} - venc {venc}.pdf"


def enviar_boleto(telefone, media_id, nome_arquivo, nome, condominio_nome, referencia,
                  unidade, valor, vencimento, linha_digitavel, condominio_id):
    """Envia o template de boleto com o PDF (já enviado à Coexy) para um
    telefone. Devolve o id da mensagem (wamid)."""
    # O condomínio entra na chave porque condomínios diferentes podem ter os
    # mesmos códigos de unidade (ex: RESERVA CANDEIAS e a TAXA EXTRA dele);
    # sem isso, a Coexy recusaria o 2º boleto como repetição do 1º.
    chave = re.sub(
        r"[^A-Za-z0-9_-]", "",
        f"boleto-{condominio_id}-{telefone}-{unidade}-{vencimento or referencia}",
    )
    corpo = [
        _param((nome or "").split(" ")[0].title() or "Condômino"),
        _param(condominio_nome),
        _param(referencia),
        _param(unidade),
        _param(_formatar_valor(valor)),
        _param(_formatar_data(vencimento)),
        _param(linha_digitavel, "consulte o boleto em PDF anexo"),
    ]
    payload = {
        "messaging_product": "whatsapp",
        "to": telefone,
        "type": "template",
        "template": {
            "name": config.COEXY_TEMPLATE_BOLETO,
            "language": {"code": "pt_BR"},
            "components": [
                {"type": "header", "parameters": [
                    {"type": "document", "document": {"id": media_id, "filename": nome_arquivo}}
                ]},
                {"type": "body", "parameters": [{"type": "text", "text": t} for t in corpo]},
            ],
        },
        "idempotency_key": chave[:200],
    }
    resp = requests.post(f"{_base()}/messages", headers=_auth(), json=payload, timeout=30)
    if resp.status_code == 422 and "idempotency_key_reused" in resp.text:
        raise BoletoJaEnviado("Este boleto já foi enviado para este telefone.")
    if resp.status_code >= 300:
        raise EnvioWhatsappFalhou(f"Coexy retornou {resp.status_code}: {resp.text[:300]}")
    dados = resp.json() if resp.content else {}
    mensagens = dados.get("messages") or []
    if not mensagens:
        raise EnvioWhatsappFalhou(f"Resposta inesperada da Coexy: {resp.text[:300]}")
    return mensagens[0].get("id")
