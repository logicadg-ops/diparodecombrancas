import requests

import config


def login(email, senha):
    """Autentica via Supabase Auth (GoTrue). Retorna o dict do usuário em
    caso de sucesso, ou None se o email/senha estiverem errados."""
    resp = requests.post(
        f"{config.SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={
            "apikey": config.SUPABASE_SERVICE_KEY,
            "Content-Type": "application/json",
        },
        json={"email": email, "password": senha},
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("user")


def criar_usuario(email, senha):
    """Cria um usuário direto no Supabase Auth (uso administrativo, via
    service_role key). O email já sai confirmado, sem precisar de link de
    verificação."""
    resp = requests.post(
        f"{config.SUPABASE_URL}/auth/v1/admin/users",
        headers={
            "apikey": config.SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
        },
        json={"email": email, "password": senha, "email_confirm": True},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
