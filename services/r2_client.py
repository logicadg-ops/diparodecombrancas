import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

import config


class BoletoNaoEncontrado(Exception):
    """Não há PDF salvo no R2 para essa unidade."""


_client = None


def _s3():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=config.R2_ENDPOINT_URL,
            aws_access_key_id=config.R2_ACCESS_KEY_ID,
            aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    return _client


def _chave(condominio_id, unidade):
    return f"{condominio_id}/{unidade}.pdf"


def boleto_existe(condominio_id, unidade):
    try:
        _s3().head_object(
            Bucket=config.R2_BUCKET_NAME, Key=_chave(condominio_id, unidade)
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def gerar_url_download(condominio_id, unidade, expira_em=3600):
    """Gera uma URL assinada temporária apontando para o PDF mais atual no R2.

    Deve ser chamada sob demanda (ex: numa rota que redireciona pra ela),
    nunca armazenada, já que o arquivo pode ser substituído a qualquer
    momento por uma nova emissão do boleto.
    """
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": config.R2_BUCKET_NAME, "Key": _chave(condominio_id, unidade)},
        ExpiresIn=expira_em,
    )


def listar_arquivos(condominio_id):
    """Lista os PDFs salvos pra um condomínio, com unidade, tamanho e data
    da última modificação (útil pra saber se um boleto já foi atualizado)."""
    arquivos = []
    paginator = _s3().get_paginator("list_objects_v2")
    for pagina in paginator.paginate(
        Bucket=config.R2_BUCKET_NAME, Prefix=f"{condominio_id}/"
    ):
        for obj in pagina.get("Contents", []):
            chave = obj["Key"]
            if not chave.endswith(".pdf"):
                continue
            nome_arquivo = chave.rsplit("/", 1)[-1]
            unidade = nome_arquivo[: -len(".pdf")]
            arquivos.append(
                {
                    "unidade": unidade,
                    "chave": chave,
                    "tamanho": obj["Size"],
                    "modificado_em": obj["LastModified"],
                }
            )
    arquivos.sort(key=lambda a: a["unidade"])
    return arquivos


def enviar_arquivo(condominio_id, unidade, arquivo_bytes, content_type="application/pdf"):
    """Envia (ou substitui) o PDF de uma unidade no R2."""
    _s3().put_object(
        Bucket=config.R2_BUCKET_NAME,
        Key=_chave(condominio_id, unidade),
        Body=arquivo_bytes,
        ContentType=content_type,
    )


def deletar_arquivo(condominio_id, unidade):
    _s3().delete_object(Bucket=config.R2_BUCKET_NAME, Key=_chave(condominio_id, unidade))


def deletar_todos_arquivos(condominio_id):
    """Exclui TODOS os PDFs salvos pra um condomínio (toda a pasta
    {condominio_id}/ no bucket). Irreversível. Retorna quantos foram
    excluídos."""
    s3 = _s3()
    total_excluidos = 0
    paginator = s3.get_paginator("list_objects_v2")
    for pagina in paginator.paginate(
        Bucket=config.R2_BUCKET_NAME, Prefix=f"{condominio_id}/"
    ):
        chaves = [{"Key": obj["Key"]} for obj in pagina.get("Contents", [])]
        if not chaves:
            continue
        s3.delete_objects(Bucket=config.R2_BUCKET_NAME, Delete={"Objects": chaves})
        total_excluidos += len(chaves)
    return total_excluidos
