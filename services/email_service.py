from datetime import datetime

import requests
from flask import render_template

import config


class EnvioEmailFalhou(Exception):
    pass


def _formatar_valor(valor):
    if valor is None:
        return "-"
    try:
        return f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return valor


def _formatar_data(data_iso):
    if not data_iso:
        return "-"
    try:
        return datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return data_iso


def _enviar_email(destinatario_email, assunto, html):
    """Envia via API Locaweb SMTP LW (POST /v1/messages).

    Schema conforme https://api.smtplw.com.br/docs/v1/swagger.yaml:
    autenticação via header x-auth-token; corpo com subject/body/to/from,
    com headers.Content-Type indicando que o body é HTML.
    """
    payload = {
        "subject": assunto,
        "body": html,
        "to": destinatario_email,
        "from": config.EMAIL_REMETENTE,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
        },
    }

    resp = requests.post(
        config.LOCAWEB_SMTP_URL,
        headers={
            "x-auth-token": config.LOCAWEB_SMTP_TOKEN,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )

    if resp.status_code != 201:
        raise EnvioEmailFalhou(
            f"Locaweb SMTP retornou {resp.status_code}: {resp.text}"
        )

    return resp.json() if resp.content else {}


def montar_corpo_html(proprietario, unidade, cobranca, condominio_nome=""):
    return render_template(
        "email_boleto.html",
        proprietario=proprietario,
        unidade=unidade,
        condominio_nome=condominio_nome or "Administração Condominial",
        vencimento_formatado=_formatar_data(cobranca.get("vencimento")),
        valor_formatado=_formatar_valor(cobranca.get("valor")),
        link=cobranca.get("link", ""),
        pix=cobranca.get("pix", ""),
        linha_digitavel=cobranca.get("linhaDigitavel", ""),
    )


def enviar_boleto(destinatario_email, destinatario_nome, unidade, cobranca, condominio_nome=""):
    html = montar_corpo_html(destinatario_nome, unidade, cobranca, condominio_nome)
    return _enviar_email(
        destinatario_email, f"Boleto condominial - Unidade {unidade}", html
    )


def montar_corpo_html_2via(
    proprietario, unidade, link, mes_referencia, vencimento, condominio_nome="", administradora_nome=""
):
    return render_template(
        "email_boleto_2via.html",
        proprietario=proprietario,
        unidade=unidade,
        condominio_nome=condominio_nome or "Administração Condominial",
        administradora_nome=administradora_nome or "Administradora",
        mes_referencia=mes_referencia or "-",
        vencimento_formatado=_formatar_data(vencimento),
        link=link,
    )


def enviar_boleto_2via(
    destinatario_email,
    destinatario_nome,
    unidade,
    link,
    mes_referencia,
    vencimento,
    condominio_nome="",
    administradora_nome="",
):
    html = montar_corpo_html_2via(
        destinatario_nome,
        unidade,
        link,
        mes_referencia,
        vencimento,
        condominio_nome,
        administradora_nome,
    )
    assunto = (
        f"{condominio_nome or 'Administração Condominial'} - Boleto {mes_referencia} "
        f"- Vencimento {_formatar_data(vencimento)}."
    )
    return _enviar_email(destinatario_email, assunto, html)
