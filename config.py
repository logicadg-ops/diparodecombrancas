import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

CONDOMOB_BASE_URL = os.environ["CONDOMOB_BASE_URL"].rstrip("/")
CONDOMOB_ADMINISTRADORA_ID = os.environ["CONDOMOB_ADMINISTRADORA_ID"]
CONDOMOB_AUTH_TOKEN = os.environ["CONDOMOB_AUTH_TOKEN"]

LOCAWEB_SMTP_URL = os.environ["LOCAWEB_SMTP_URL"]
LOCAWEB_SMTP_TOKEN = os.environ["LOCAWEB_SMTP_TOKEN"]
EMAIL_REMETENTE = os.environ["EMAIL_REMETENTE"]
EMAIL_REMETENTE_NOME = os.environ.get("EMAIL_REMETENTE_NOME", EMAIL_REMETENTE)

R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]
R2_ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev")


def load_condominios():
    with open(BASE_DIR / "config" / "condominios.json", encoding="utf-8") as f:
        return json.load(f)


def get_condominio(condominio_id):
    condominio_id = int(condominio_id)
    for c in load_condominios():
        if c["id"] == condominio_id:
            return c
    return None
