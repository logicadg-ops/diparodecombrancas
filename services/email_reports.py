import re

import requests

import config


def _total_paginas(links):
    ultimo = (links or {}).get("last", "")
    m = re.search(r"page=(\d+)", ultimo)
    return int(m.group(1)) if m else 1


def listar_mensagens(status="all", start_date=None, end_date=None, recipient=None, subject=None, page=1, per=25):
    """Consulta o histórico de envios na SMTP API da Locaweb (GET /v1/messages).

    Retorna (mensagens, total_paginas).
    """
    params = {
        "status": status,
        "start_date": start_date,
        "end_date": end_date,
        "page": page,
        "per": per,
    }
    if recipient:
        params["recipient"] = recipient
    if subject:
        params["subject"] = subject

    resp = requests.get(
        config.LOCAWEB_SMTP_URL,
        headers={"x-auth-token": config.LOCAWEB_SMTP_TOKEN},
        params=params,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    mensagens = data.get("data", {}).get("messages", [])
    total_paginas = _total_paginas(data.get("links"))
    return mensagens, total_paginas
