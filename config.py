import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _env(nome, padrao=None):
    """Lê uma variável de ambiente já removendo espaços/quebras de linha
    acidentais (ex: um Enter sobrando ao colar o valor num painel como o de
    Config Vars da Heroku, que quebra os headers HTTP)."""
    valor = os.environ.get(nome, padrao)
    return valor.strip() if isinstance(valor, str) else valor


def _env_obrigatoria(nome):
    valor = _env(nome)
    if not valor:
        raise RuntimeError(f"Variável de ambiente obrigatória não definida: {nome}")
    return valor


SUPABASE_URL = _env_obrigatoria("SUPABASE_URL").rstrip("/")
SUPABASE_SERVICE_KEY = _env_obrigatoria("SUPABASE_SERVICE_KEY")

CONDOMOB_BASE_URL = _env_obrigatoria("CONDOMOB_BASE_URL").rstrip("/")
CONDOMOB_ADMINISTRADORA_ID = _env_obrigatoria("CONDOMOB_ADMINISTRADORA_ID")
CONDOMOB_AUTH_TOKEN = _env_obrigatoria("CONDOMOB_AUTH_TOKEN")

LOCAWEB_SMTP_URL = _env_obrigatoria("LOCAWEB_SMTP_URL")
LOCAWEB_SMTP_TOKEN = _env_obrigatoria("LOCAWEB_SMTP_TOKEN")
EMAIL_REMETENTE = _env_obrigatoria("EMAIL_REMETENTE")
EMAIL_REMETENTE_NOME = _env("EMAIL_REMETENTE_NOME", EMAIL_REMETENTE)

R2_ACCOUNT_ID = _env_obrigatoria("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = _env_obrigatoria("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = _env_obrigatoria("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = _env_obrigatoria("R2_BUCKET_NAME")
R2_ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

FLASK_SECRET_KEY = _env("FLASK_SECRET_KEY", "dev")


def load_condominios():
    with open(BASE_DIR / "config" / "condominios.json", encoding="utf-8") as f:
        return json.load(f)


def get_condominio(condominio_id):
    condominio_id = int(condominio_id)
    for c in load_condominios():
        if c["id"] == condominio_id:
            return c
    return None
