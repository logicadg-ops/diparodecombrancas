from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests
from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, abort, session, Response
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from services import (
    auth_client,
    condomob,
    email_reports,
    email_service,
    importacao_unidades,
    r2_client,
    relatorio_emails,
    supabase_client,
    whatsapp_service,
)

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
# Atrás do proxy da Heroku (TLS termina lá), sem isso url_for(_external=True)
# geraria links http:// em vez de https://.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

ROTAS_PUBLICAS = {
    "login",
    "static",
    # Link clicado pelo morador a partir do email — ele não está (e não
    # deve precisar estar) logado no sistema.
    "boleto_2via_arquivo",
}


@app.template_filter("data_br")
def _filtro_data_br(valor_iso):
    if not valor_iso:
        return "—"
    try:
        return datetime.fromisoformat(valor_iso).strftime("%d/%m/%y %H:%M:%S")
    except (TypeError, ValueError):
        return valor_iso


@app.before_request
def exigir_login():
    if request.endpoint in ROTAS_PUBLICAS or request.endpoint is None:
        return
    if not session.get("usuario_email"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        senha = request.form.get("senha", "")
        usuario = auth_client.login(email, senha)
        if not usuario:
            flash("Email ou senha inválidos.", "error")
            return render_template("login.html")

        session["usuario_email"] = usuario["email"]
        destino = request.args.get("next") or url_for("index")
        return redirect(destino)

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Você saiu do sistema.", "success")
    return redirect(url_for("login"))


def contato_da_unidade(unidade_row):
    """Sempre usa os dados do proprietário, mesmo se a unidade estiver alugada."""
    return {
        "nome": unidade_row.get("proprietario") or "Condômino",
        "email": unidade_row.get("email"),
        "cpf_cnpj": unidade_row.get("cpfcnpj"),
    }


def emails_da_unidade(unidade_row):
    """O campo email pode conter vários endereços separados por ';'
    (ex: 'a@x.com;b@y.com') — retorna a lista já limpa, sem vazios."""
    bruto = unidade_row.get("email") or ""
    return [e.strip() for e in bruto.split(";") if e.strip()]


def _cobranca_da_unidade(condominio_id, unidade_row):
    contato = contato_da_unidade(unidade_row)
    try:
        return condomob.consultar_cobranca_aberta(
            condominio_id, unidade_row.get("unidade"), contato["cpf_cnpj"]
        )
    except condomob.CobrancaNaoEncontrada:
        return None
    except requests.RequestException:
        # Falha pontual da Condomob (timeout, 500, etc) numa unidade não
        # deve derrubar a lista inteira — essa unidade só fica sem dados.
        app.logger.warning(
            "Condomob falhou pra unidade %s (condomínio %s)",
            unidade_row.get("unidade"), condominio_id, exc_info=True,
        )
        return None


def marcar_unidades_com_cobranca(condominio_id, unidades):
    """Consulta a Condomob em paralelo para saber quais unidades têm boleto
    em aberto, marcando cada dict com `tem_cobranca` e `vencimento_mes`
    (usado para o filtro por mês de vencimento na tela)."""
    with ThreadPoolExecutor(max_workers=10) as pool:
        cobrancas = pool.map(
            lambda u: _cobranca_da_unidade(condominio_id, u), unidades
        )
    for unidade_row, cobranca in zip(unidades, cobrancas):
        unidade_row["tem_cobranca"] = cobranca is not None
        vencimento = cobranca.get("vencimento") if cobranca else None
        unidade_row["vencimento"] = vencimento
        unidade_row["vencimento_mes"] = vencimento[:7] if vencimento else None
    return unidades


def _mes_arquivo(vencimento):
    """Deriva o mês (YYYY-MM) usado como parte da chave do PDF no R2, a
    partir de uma data de vencimento ISO ('YYYY-MM-DD')."""
    return (vencimento or "")[:7] or None


def _dados_cobranca_2via(condominio_id, unidade, contato, vencimento_override=None):
    """Busca os dados de vencimento/valor/pix na Condomob (best-effort) e
    verifica se há PDF no R2 pra essa unidade NO MÊS do vencimento (ou no
    mês informado em `vencimento_override`, quando o envio já tem uma data
    escolhida manualmente). Isso evita servir o PDF de um mês antigo quando
    o boleto do mês atual ainda não foi re-enviado pro R2. Não usa url_for,
    então pode ser chamada de fora do contexto de request (ex: thread pool)."""
    try:
        cobranca = dict(
            condomob.consultar_cobranca_aberta(condominio_id, unidade, contato["cpf_cnpj"])
        )
    except condomob.CobrancaNaoEncontrada:
        cobranca = {"vencimento": None, "valor": None, "pix": "", "linhaDigitavel": ""}

    mes = _mes_arquivo(vencimento_override) or _mes_arquivo(cobranca.get("vencimento"))
    if not mes or not r2_client.boleto_existe(condominio_id, unidade, mes):
        raise r2_client.BoletoNaoEncontrado(
            f"Sem boleto salvo para a unidade {unidade}" + (f" no mês {mes}" if mes else "")
        )
    cobranca["mes_arquivo"] = mes
    return cobranca


def _montar_cobranca_2via(condominio_id, unidade, contato, vencimento_override=None):
    """Igual a `_dados_cobranca_2via`, mas já inclui o link estável do
    próprio sistema — só pode ser chamada dentro de um contexto de request."""
    cobranca = _dados_cobranca_2via(condominio_id, unidade, contato, vencimento_override)
    cobranca["link"] = url_for(
        "boleto_2via_arquivo", condominio_id=condominio_id, unidade=unidade,
        mes=cobranca["mes_arquivo"], _external=True,
    )
    return cobranca


def _dados_unidade_2via(condominio_id, unidade_row):
    contato = contato_da_unidade(unidade_row)
    try:
        cobranca = _dados_cobranca_2via(condominio_id, unidade_row.get("unidade"), contato)
        return True, cobranca.get("vencimento")
    except requests.RequestException:
        # Falha pontual da Condomob numa unidade: mantém o PDF visível
        # (já sabemos que existe no R2), só sem os dados de vencimento.
        app.logger.warning(
            "Condomob falhou pra unidade %s (condomínio %s)",
            unidade_row.get("unidade"), condominio_id, exc_info=True,
        )
        return True, None
    except r2_client.BoletoNaoEncontrado:
        return False, None


def marcar_unidades_2via(condominio_id, unidades):
    """Consulta o R2 (e, quando dá, a Condomob) em paralelo pra saber quais
    unidades têm PDF de 2ª via salvo, marcando `tem_boleto_2via` e
    `vencimento_mes` (usado no filtro por mês)."""
    with ThreadPoolExecutor(max_workers=10) as pool:
        resultados = pool.map(
            lambda u: _dados_unidade_2via(condominio_id, u), unidades
        )
    for unidade_row, (tem_boleto, vencimento) in zip(unidades, resultados):
        unidade_row["tem_boleto_2via"] = tem_boleto
        unidade_row["vencimento"] = vencimento
        unidade_row["vencimento_mes"] = vencimento[:7] if vencimento else None
    return unidades


MESES_NOMES = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


@app.route("/")
def index():
    condominios = supabase_client.get_condominios()
    ano_atual = date.today().year
    anos = list(range(ano_atual - 1, ano_atual + 2))
    return render_template(
        "index.html",
        condominios=condominios,
        meses_nomes=MESES_NOMES,
        anos=anos,
        ano_atual=ano_atual,
    )


@app.route("/condominios")
def listar_condominios():
    condominios = supabase_client.listar_condominios_cadastro()
    return render_template("condominios_lista.html", condominios=condominios)


@app.route("/condominios/novo", methods=["GET", "POST"])
def novo_condominio():
    if request.method == "POST":
        idcontominio_bruto = request.form.get("idcontominio", "").strip()
        nome = request.form.get("ds_condominio", "").strip()
        dados_form = {"idcontominio": idcontominio_bruto, "ds_condominio": nome}

        if not idcontominio_bruto.isdigit() or not nome:
            flash("ID (Condomob) precisa ser numérico, e o nome é obrigatório.", "error")
            return render_template("condominio_form.html", condominio=dados_form, modo="novo")

        unidades_bruto = request.form.get("unidades", "").strip()
        if unidades_bruto and not unidades_bruto.isdigit():
            flash("Número de unidades precisa ser um número inteiro.", "error")
            return render_template(
                "condominio_form.html", condominio=dict(request.form), modo="novo"
            )
        dados = {
            "idcontominio": int(idcontominio_bruto),
            "ds_condominio": nome,
            "unidades": int(unidades_bruto) if unidades_bruto else None,
            "cnpj": request.form.get("cnpj", "").strip() or None,
            "endereco": request.form.get("endereco", "").strip() or None,
            "end_numero": request.form.get("end_numero", "").strip() or None,
            "bairro": request.form.get("bairro", "").strip() or None,
            "cidade": request.form.get("cidade", "").strip() or None,
            "uf": request.form.get("uf", "").strip() or None,
            "status": request.form.get("status", "ATIVO").strip(),
        }
        supabase_client.criar_condominio(dados)
        flash(f"Condomínio \"{dados['ds_condominio']}\" cadastrado.", "success")
        return redirect(url_for("listar_condominios"))

    return render_template("condominio_form.html", condominio={}, modo="novo")


@app.route("/condominios/<int:id_interno>/editar", methods=["GET", "POST"])
def editar_condominio(id_interno):
    condominio = supabase_client.get_condominio_cadastro(id_interno)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("listar_condominios"))

    if request.method == "POST":
        unidades_bruto = request.form.get("unidades", "").strip()
        if unidades_bruto and not unidades_bruto.isdigit():
            flash("Número de unidades precisa ser um número inteiro.", "error")
            return render_template(
                "condominio_form.html", condominio={**condominio, **request.form.to_dict()}, modo="editar"
            )
        dados = {
            "ds_condominio": request.form.get("ds_condominio", "").strip(),
            "unidades": int(unidades_bruto) if unidades_bruto else None,
            "cnpj": request.form.get("cnpj", "").strip() or None,
            "endereco": request.form.get("endereco", "").strip() or None,
            "end_numero": request.form.get("end_numero", "").strip() or None,
            "bairro": request.form.get("bairro", "").strip() or None,
            "cidade": request.form.get("cidade", "").strip() or None,
            "uf": request.form.get("uf", "").strip() or None,
            "status": request.form.get("status", "ATIVO").strip(),
        }
        if not dados["ds_condominio"]:
            flash("Nome é obrigatório.", "error")
            return render_template(
                "condominio_form.html", condominio={**condominio, **dados}, modo="editar"
            )

        supabase_client.atualizar_condominio(id_interno, dados)
        flash(f"Condomínio \"{dados['ds_condominio']}\" atualizado.", "success")
        return redirect(url_for("listar_condominios"))

    return render_template("condominio_form.html", condominio=condominio, modo="editar")


@app.route("/condominios/<int:id_interno>/excluir", methods=["POST"])
def excluir_condominio(id_interno):
    supabase_client.excluir_condominio(id_interno)
    flash("Condomínio excluído.", "success")
    return redirect(url_for("listar_condominios"))


def _dados_unidade_do_formulario():
    dados = {}
    for campo in importacao_unidades.CAMPOS_TEXTO:
        dados[campo] = request.form.get(campo, "").strip() or None
    dados[importacao_unidades.CAMPO_BOOLEANO] = request.form.get("alugado") == "on"
    return dados


@app.route("/condominios/<int:condominio_id>/unidades")
def listar_unidades_cadastro(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("listar_condominios"))
    unidades = supabase_client.get_unidades(condominio_id)
    return render_template("unidades_cadastro.html", condominio=condominio, unidades=unidades)


@app.route("/condominios/<int:condominio_id>/unidades/modelo.xlsx")
def modelo_unidades(condominio_id):
    conteudo = importacao_unidades.gerar_modelo()
    return Response(
        conteudo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=modelo_unidades.xlsx"},
    )


@app.route("/condominios/<int:condominio_id>/unidades/nova", methods=["GET", "POST"])
def nova_unidade_cadastro(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("listar_condominios"))

    if request.method == "POST":
        dados = _dados_unidade_do_formulario()
        if not dados["unidade"]:
            flash("O código da unidade é obrigatório.", "error")
            return render_template(
                "unidade_form.html", condominio=condominio, unidade=dados, modo="nova"
            )
        dados["id_condominio"] = condominio_id
        dados["nome_condominio"] = condominio["nome"]
        supabase_client.criar_unidades([dados])
        flash(f"Unidade {dados['unidade']} cadastrada.", "success")
        return redirect(url_for("listar_unidades_cadastro", condominio_id=condominio_id))

    return render_template("unidade_form.html", condominio=condominio, unidade={}, modo="nova")


@app.route("/condominios/<int:condominio_id>/unidades/<int:id_unidade>/editar", methods=["GET", "POST"])
def editar_unidade_cadastro(condominio_id, id_unidade):
    condominio = supabase_client.get_condominio(condominio_id)
    unidade = supabase_client.get_unidade_por_id(id_unidade)
    if not condominio or not unidade or unidade.get("id_condominio") != condominio_id:
        flash("Unidade não encontrada.", "error")
        return redirect(url_for("listar_unidades_cadastro", condominio_id=condominio_id))

    if request.method == "POST":
        dados = _dados_unidade_do_formulario()
        if not dados["unidade"]:
            flash("O código da unidade é obrigatório.", "error")
            return render_template(
                "unidade_form.html", condominio=condominio,
                unidade={**unidade, **dados}, modo="editar",
            )
        supabase_client.atualizar_unidade_por_id(id_unidade, dados)
        flash(f"Unidade {dados['unidade']} atualizada.", "success")
        return redirect(url_for("listar_unidades_cadastro", condominio_id=condominio_id))

    return render_template("unidade_form.html", condominio=condominio, unidade=unidade, modo="editar")


@app.route("/condominios/<int:condominio_id>/unidades/importar", methods=["GET", "POST"])
def importar_unidades(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("listar_condominios"))

    if request.method == "GET":
        return render_template("unidades_importar.html", condominio=condominio, relatorio=None)

    arquivo = request.files.get("planilha")
    if not arquivo or not arquivo.filename.lower().endswith(".xlsx"):
        flash("Envie uma planilha .xlsx.", "error")
        return redirect(url_for("importar_unidades", condominio_id=condominio_id))

    simular = request.form.get("simular") == "on"
    linhas, erros_formato, avisos = importacao_unidades.ler_planilha(arquivo.stream)
    unidades_existentes = supabase_client.get_unidades(condominio_id)
    plano = importacao_unidades.planejar(
        condominio_id, condominio["nome"], linhas, unidades_existentes
    )
    erros = erros_formato + plano["erros"]

    aplicado = False
    if not simular and not erros:
        importacao_unidades.aplicar(plano)
        aplicado = True
        flash("Importação aplicada no cadastro.", "success")

    relatorio = {
        "simulado": simular,
        "aplicado": aplicado,
        "criar": len(plano["criar"]),
        "atualizar": len(plano["atualizar"]),
        "sem_alteracao": len(plano["sem_alteracao"]),
        "amostra_criar": [d["unidade"] for d in plano["criar"][:50]],
        "amostra_atualizar": [item["unidade"] for item in plano["atualizar"][:50]],
        "avisos": avisos,
        "erros": erros,
    }
    return render_template("unidades_importar.html", condominio=condominio, relatorio=relatorio)


@app.route("/condominio/<int:condominio_id>")
def ver_condominio(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    unidades = supabase_client.get_unidades(condominio_id)
    marcar_unidades_com_cobranca(condominio_id, unidades)
    meses = sorted({u["vencimento_mes"] for u in unidades if u["vencimento_mes"]})
    mes_selecionado = request.args.get("mes", "")
    return render_template(
        "condominio.html",
        condominio=condominio,
        unidades=unidades,
        meses=meses,
        mes_selecionado=mes_selecionado,
        condominios=supabase_client.get_condominios(),
        whatsapp_ativo=whatsapp_service.whatsapp_configurado(),
    )


@app.route("/condominio/<int:condominio_id>/unidade/<unidade>/boleto")
def ver_boleto(condominio_id, unidade):
    condominio = supabase_client.get_condominio(condominio_id)
    unidade_row = supabase_client.get_unidade(condominio_id, unidade)
    if not condominio or not unidade_row:
        flash("Unidade não encontrada.", "error")
        return redirect(url_for("ver_condominio", condominio_id=condominio_id))

    contato = contato_da_unidade(unidade_row)
    erro = None
    cobranca = None
    try:
        cobranca = condomob.consultar_cobranca_aberta(
            condominio_id, unidade, contato["cpf_cnpj"]
        )
    except condomob.CobrancaNaoEncontrada as e:
        erro = str(e)

    return render_template(
        "boleto.html",
        condominio=condominio,
        unidade=unidade_row,
        contato=contato,
        cobranca=cobranca,
        erro=erro,
    )


def _quer_json():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@app.route("/unidade/<int:condominio_id>/<unidade>/editar", methods=["GET", "POST"])
def editar_unidade(condominio_id, unidade):
    condominio = supabase_client.get_condominio(condominio_id)
    unidade_row = supabase_client.get_unidade(condominio_id, unidade)
    if not condominio or not unidade_row:
        flash("Unidade não encontrada.", "error")
        return redirect(url_for("index"))

    origem = request.values.get("origem", "padrao")
    destino = "ver_condominio_2via" if origem == "2via" else "ver_condominio"

    if request.method == "POST":
        proprietario = request.form.get("proprietario", "").strip()
        email = request.form.get("email", "").strip()
        fone1 = request.form.get("fone1", "").strip()
        fone2 = request.form.get("fone2", "").strip()
        supabase_client.atualizar_unidade(condominio_id, unidade, proprietario, email, fone1, fone2)
        flash(f"Dados da unidade {unidade} atualizados.", "success")
        return redirect(url_for(destino, condominio_id=condominio_id))

    return render_template(
        "editar_unidade.html",
        condominio=condominio,
        unidade=unidade_row,
        origem=origem,
        destino=destino,
    )


def _mes_referencia_de_data(data_iso):
    """Deriva 'MM/YYYY' a partir de uma data ISO ('YYYY-MM-DD'), pro log de
    envio quando não há um mês de referência explícito (fluxo ao vivo)."""
    try:
        return datetime.strptime(data_iso, "%Y-%m-%d").strftime("%m/%Y")
    except (TypeError, ValueError):
        return None


def _registrar_log_envio(condominio, unidade, contato, sucesso, mensagem_erro=None,
                          mes_referencia=None, cobranca=None, tipo="email"):
    cobranca = cobranca or {}
    try:
        supabase_client.registrar_envio(
            condominio_id=condominio["id"],
            nome_condominio=condominio["nome"],
            unidade=unidade,
            mes_referencia=mes_referencia or _mes_referencia_de_data(cobranca.get("vencimento")),
            sucesso=sucesso,
            contato=contato,
            tipo=tipo,
            mensagem_erro=mensagem_erro,
            valor=cobranca.get("valor"),
            link=cobranca.get("link"),
            linha_digitavel=cobranca.get("linhaDigitavel"),
            vencimento=cobranca.get("vencimento"),
        )
    except Exception:
        app.logger.exception("Falha ao registrar log de envio na tabela enviowhatapp")


# ---------------------------------------------------------------------------
# Canal de envio: email, WhatsApp ou os dois
# ---------------------------------------------------------------------------

CANAIS = ("email", "whatsapp", "ambos")


def _canal_da_requisicao():
    canal = (request.values.get("canal") or "email").strip().lower()
    return canal if canal in CANAIS else "email"


def _usa_email(canal):
    return canal in ("email", "ambos")


def _usa_whatsapp(canal):
    return canal in ("whatsapp", "ambos")


def _validar_destinos(canal, unidade, emails, telefones):
    """Confere se a unidade tem para onde enviar no canal escolhido.
    Devolve a mensagem de erro, ou None se estiver tudo certo."""
    if _usa_whatsapp(canal) and not whatsapp_service.whatsapp_configurado():
        return "Envio por WhatsApp não configurado (falta a variável COEXY_CHANNEL_TOKEN)."
    if canal == "email" and not emails:
        return f"Unidade {unidade} não tem email cadastrado."
    if canal == "whatsapp" and not telefones:
        return f"Unidade {unidade} não tem telefone (fone1/fone2) válido cadastrado."
    if canal == "ambos" and not emails and not telefones:
        return f"Unidade {unidade} não tem email nem telefone cadastrado."
    return None


def _enviar_whatsapp_unidade(condominio, unidade, telefones, contato, obter_pdf,
                             referencia, vencimento, valor, linha_digitavel,
                             mes_referencia_log=None, cobranca_log=None):
    """Sobe o PDF uma vez para a Coexy e envia o template para cada telefone.
    Devolve (enviados, ja_enviados, falhas)."""
    enviados, ja_enviados, falhas = [], [], []
    cobranca_log = cobranca_log or {}
    try:
        nome_arquivo = whatsapp_service.nome_arquivo_boleto(unidade, vencimento)
        media_id = whatsapp_service.subir_pdf(obter_pdf(), nome_arquivo)
    except (whatsapp_service.EnvioWhatsappFalhou, r2_client.BoletoNaoEncontrado,
            requests.RequestException) as e:
        for telefone in telefones:
            _registrar_log_envio(condominio, unidade, telefone, False, mensagem_erro=str(e),
                                 mes_referencia=mes_referencia_log, cobranca=cobranca_log, tipo="whatsapp")
            falhas.append((whatsapp_service.formatar_telefone(telefone), str(e)))
        return enviados, ja_enviados, falhas

    for telefone in telefones:
        legivel = whatsapp_service.formatar_telefone(telefone)
        try:
            whatsapp_service.enviar_boleto(
                telefone, media_id, nome_arquivo, contato["nome"], condominio["nome"],
                referencia, unidade, valor, vencimento, linha_digitavel,
            )
            _registrar_log_envio(condominio, unidade, telefone, True,
                                 mes_referencia=mes_referencia_log, cobranca=cobranca_log, tipo="whatsapp")
            enviados.append(legivel)
        except whatsapp_service.BoletoJaEnviado:
            ja_enviados.append(legivel)
        except (whatsapp_service.EnvioWhatsappFalhou, requests.RequestException) as e:
            _registrar_log_envio(condominio, unidade, telefone, False, mensagem_erro=str(e),
                                 mes_referencia=mes_referencia_log, cobranca=cobranca_log, tipo="whatsapp")
            falhas.append((legivel, str(e)))
    return enviados, ja_enviados, falhas


def _resumo_envio(unidade, enviados, ja_enviados, falhas):
    """Monta (sucesso, mensagem) a partir do resultado dos dois canais."""
    partes = []
    if enviados:
        partes.append(f"Boleto enviado para {', '.join(enviados)} (unidade {unidade}).")
    if ja_enviados:
        partes.append(f"Já tinha sido enviado antes para {', '.join(ja_enviados)} (não reenviado).")
    if falhas:
        partes.append("Falha para: " + "; ".join(f"{destino}: {erro}" for destino, erro in falhas))
    sucesso = bool(enviados or ja_enviados) and not falhas
    return sucesso, " ".join(partes) or f"Nada enviado para a unidade {unidade}."


def _responder_envio(sucesso, mensagem, status_erro, rota_volta, condominio_id):
    if _quer_json():
        return (jsonify(sucesso=True, mensagem=mensagem) if sucesso
                else (jsonify(sucesso=False, mensagem=mensagem), status_erro))
    flash(mensagem, "success" if sucesso else "error")
    return redirect(url_for(rota_volta, condominio_id=condominio_id))


def _processar_envio_ao_vivo(condominio, unidade_row, canal):
    """Envia o boleto em aberto (Condomob) de uma unidade pelos canais
    escolhidos. Devolve (sucesso, mensagem, status_http_se_erro)."""
    unidade = unidade_row.get("unidade")
    contato = contato_da_unidade(unidade_row)
    emails = emails_da_unidade(unidade_row) if _usa_email(canal) else []
    telefones = whatsapp_service.telefones_da_unidade(unidade_row) if _usa_whatsapp(canal) else []

    erro = _validar_destinos(canal, unidade, emails, telefones)
    if erro:
        return False, erro, 400

    try:
        cobranca = condomob.consultar_cobranca_aberta(condominio["id"], unidade, contato["cpf_cnpj"])
    except condomob.CobrancaNaoEncontrada:
        for email in emails:
            _registrar_log_envio(condominio, unidade, email, False, mensagem_erro="Sem cobrança em aberto")
        for telefone in telefones:
            _registrar_log_envio(condominio, unidade, telefone, False,
                                 mensagem_erro="Sem cobrança em aberto", tipo="whatsapp")
        return False, f"Unidade {unidade} não possui cobrança em aberto.", 400

    enviados, ja_enviados, falhas = [], [], []
    for email in emails:
        try:
            email_service.enviar_boleto(email, contato["nome"], unidade, cobranca, condominio["nome"])
            _registrar_log_envio(condominio, unidade, email, True, cobranca=cobranca)
            enviados.append(email)
        except email_service.EnvioEmailFalhou as e:
            _registrar_log_envio(condominio, unidade, email, False, mensagem_erro=str(e), cobranca=cobranca)
            falhas.append((email, str(e)))

    if telefones:
        w_env, w_ja, w_falhas = _enviar_whatsapp_unidade(
            condominio, unidade, telefones, contato,
            obter_pdf=lambda: whatsapp_service.baixar_pdf(cobranca.get("link")),
            referencia=_mes_referencia_de_data(cobranca.get("vencimento")) or "-",
            vencimento=cobranca.get("vencimento"),
            valor=cobranca.get("valor"),
            linha_digitavel=cobranca.get("linhaDigitavel"),
            cobranca_log=cobranca,
        )
        enviados += w_env
        ja_enviados += w_ja
        falhas += w_falhas

    sucesso, mensagem = _resumo_envio(unidade, enviados, ja_enviados, falhas)
    return sucesso, mensagem, 502


@app.route("/condominio/<int:condominio_id>/unidade/<unidade>/enviar", methods=["POST"])
def enviar_individual(condominio_id, unidade):
    condominio = supabase_client.get_condominio(condominio_id)
    unidade_row = supabase_client.get_unidade(condominio_id, unidade)
    if not condominio or not unidade_row:
        return _responder_envio(False, "Unidade não encontrada.", 404, "ver_condominio", condominio_id)

    sucesso, mensagem, status = _processar_envio_ao_vivo(condominio, unidade_row, _canal_da_requisicao())
    return _responder_envio(sucesso, mensagem, status, "ver_condominio", condominio_id)


@app.route("/condominio/<int:condominio_id>/enviar-massa", methods=["POST"])
def enviar_massa(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    canal = _canal_da_requisicao()
    resultados = []
    for unidade_row in supabase_client.get_unidades(condominio_id):
        _, mensagem, _ = _processar_envio_ao_vivo(condominio, unidade_row, canal)
        resultados.append({"unidade": unidade_row.get("unidade"), "status": mensagem})

    return render_template(
        "resultado_envio.html", condominio=condominio, resultados=resultados
    )


@app.route("/2via/condominio/<int:condominio_id>")
def ver_condominio_2via(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    unidades = supabase_client.get_unidades(condominio_id)
    marcar_unidades_2via(condominio_id, unidades)
    meses = sorted({u["vencimento_mes"] for u in unidades if u["vencimento_mes"]})
    mes_selecionado = request.args.get("mes", "")
    return render_template(
        "condominio_2via.html",
        condominio=condominio,
        unidades=unidades,
        meses=meses,
        mes_selecionado=mes_selecionado,
        administradora_padrao=config.EMAIL_REMETENTE_NOME,
        condominios=supabase_client.get_condominios(),
        whatsapp_ativo=whatsapp_service.whatsapp_configurado(),
    )


@app.route("/2via/condominio/<int:condominio_id>/unidade/<unidade>/arquivo")
def boleto_2via_arquivo(condominio_id, unidade):
    """Rota estável: busca no R2 o PDF do mês indicado em `mes` (YYYY-MM) e
    redireciona pra ele. É esse link (e não a URL assinada) que vai no email
    e no botão 'Ver boleto'. Sem `mes` na URL (links enviados antes do PDF
    passar a ser versionado por mês), cai pra chave antiga sem mês."""
    mes = request.args.get("mes") or None
    if not r2_client.boleto_existe(condominio_id, unidade, mes):
        abort(404)
    return redirect(r2_client.gerar_url_download(condominio_id, unidade, mes))


@app.route("/2via/condominio/<int:condominio_id>/unidade/<unidade>/boleto")
def ver_boleto_2via(condominio_id, unidade):
    condominio = supabase_client.get_condominio(condominio_id)
    unidade_row = supabase_client.get_unidade(condominio_id, unidade)
    if not condominio or not unidade_row:
        flash("Unidade não encontrada.", "error")
        return redirect(url_for("ver_condominio_2via", condominio_id=condominio_id))

    contato = contato_da_unidade(unidade_row)
    erro = None
    cobranca = None
    try:
        cobranca = _montar_cobranca_2via(condominio_id, unidade, contato)
    except r2_client.BoletoNaoEncontrado as e:
        erro = str(e)

    return render_template(
        "boleto_2via.html",
        condominio=condominio,
        unidade=unidade_row,
        contato=contato,
        cobranca=cobranca,
        erro=erro,
    )


def _processar_envio_2via(condominio, unidade_row, canal, mes_referencia, administradora, vencimento):
    """Envia o PDF de 2ª via salvo no R2 pelos canais escolhidos.
    Devolve (sucesso, mensagem, status_http_se_erro). Precisa de contexto de
    request (o link do email usa url_for)."""
    unidade = unidade_row.get("unidade")
    condominio_id = condominio["id"]
    contato = contato_da_unidade(unidade_row)
    emails = emails_da_unidade(unidade_row) if _usa_email(canal) else []
    telefones = whatsapp_service.telefones_da_unidade(unidade_row) if _usa_whatsapp(canal) else []

    erro = _validar_destinos(canal, unidade, emails, telefones)
    if erro:
        return False, erro, 400

    try:
        cobranca = _montar_cobranca_2via(condominio_id, unidade, contato, vencimento_override=vencimento)
    except r2_client.BoletoNaoEncontrado:
        for email in emails:
            _registrar_log_envio(condominio, unidade, email, False, mensagem_erro="Sem boleto de 2ª via salvo",
                                 mes_referencia=mes_referencia, cobranca={"vencimento": vencimento})
        for telefone in telefones:
            _registrar_log_envio(condominio, unidade, telefone, False, mensagem_erro="Sem boleto de 2ª via salvo",
                                 mes_referencia=mes_referencia, cobranca={"vencimento": vencimento}, tipo="whatsapp")
        return False, f"Unidade {unidade} não possui boleto de 2ª via salvo.", 400

    link_arquivo_real = r2_client.gerar_url_download(condominio_id, unidade)
    cobranca_log = {**cobranca, "vencimento": vencimento, "link": link_arquivo_real}

    enviados, ja_enviados, falhas = [], [], []
    for email in emails:
        try:
            email_service.enviar_boleto_2via(
                email, contato["nome"], unidade, cobranca["link"],
                mes_referencia, vencimento, condominio["nome"], administradora,
            )
            _registrar_log_envio(condominio, unidade, email, True, mes_referencia=mes_referencia,
                                 cobranca=cobranca_log)
            enviados.append(email)
        except email_service.EnvioEmailFalhou as e:
            _registrar_log_envio(condominio, unidade, email, False, mensagem_erro=str(e),
                                 mes_referencia=mes_referencia, cobranca=cobranca_log)
            falhas.append((email, str(e)))

    if telefones:
        # Valor e linha digitável só entram se a cobrança do Condomob for a
        # mesma do PDF (mesmo vencimento); senão o morador vê "-" e usa o PDF.
        mesma_cobranca = (cobranca.get("vencimento") or "")[:10] == (vencimento or "")[:10]
        w_env, w_ja, w_falhas = _enviar_whatsapp_unidade(
            condominio, unidade, telefones, contato,
            obter_pdf=lambda: r2_client.baixar_arquivo(condominio_id, unidade, cobranca["mes_arquivo"]),
            referencia=mes_referencia,
            vencimento=vencimento,
            valor=cobranca.get("valor") if mesma_cobranca else None,
            linha_digitavel=cobranca.get("linhaDigitavel") if mesma_cobranca else None,
            mes_referencia_log=mes_referencia,
            cobranca_log=cobranca_log,
        )
        enviados += w_env
        ja_enviados += w_ja
        falhas += w_falhas

    sucesso, mensagem = _resumo_envio(unidade, enviados, ja_enviados, falhas)
    return sucesso, mensagem, 502


def _campos_2via():
    return (
        (request.values.get("mes_referencia") or "").strip(),
        (request.values.get("administradora") or "").strip(),
        (request.values.get("vencimento") or "").strip(),
    )


@app.route("/2via/condominio/<int:condominio_id>/unidade/<unidade>/enviar", methods=["POST"])
def enviar_individual_2via(condominio_id, unidade):
    condominio = supabase_client.get_condominio(condominio_id)
    unidade_row = supabase_client.get_unidade(condominio_id, unidade)
    if not condominio or not unidade_row:
        return _responder_envio(False, "Unidade não encontrada.", 404, "ver_condominio_2via", condominio_id)

    mes_referencia, administradora, vencimento = _campos_2via()
    if not mes_referencia or not administradora or not vencimento:
        return _responder_envio(
            False, "Preencha o mês de referência, a administradora e o vencimento antes de enviar.",
            400, "ver_condominio_2via", condominio_id,
        )

    sucesso, mensagem, status = _processar_envio_2via(
        condominio, unidade_row, _canal_da_requisicao(), mes_referencia, administradora, vencimento
    )
    return _responder_envio(sucesso, mensagem, status, "ver_condominio_2via", condominio_id)


@app.route("/2via/condominio/<int:condominio_id>/enviar-massa", methods=["POST"])
def enviar_massa_2via(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    mes_referencia, administradora, vencimento = _campos_2via()
    if not mes_referencia or not administradora or not vencimento:
        flash("Preencha o mês de referência, a administradora e o vencimento antes de enviar.", "error")
        return redirect(url_for("ver_condominio_2via", condominio_id=condominio_id))

    canal = _canal_da_requisicao()
    resultados = []
    for unidade_row in supabase_client.get_unidades(condominio_id):
        _, mensagem, _ = _processar_envio_2via(
            condominio, unidade_row, canal, mes_referencia, administradora, vencimento
        )
        resultados.append({"unidade": unidade_row.get("unidade"), "status": mensagem})

    return render_template(
        "resultado_envio.html", condominio=condominio, resultados=resultados
    )


def _mes_selecionado_arquivos():
    mes = (request.args.get("mes") or request.form.get("mes") or "").strip()
    return mes or date.today().strftime("%Y-%m")


@app.route("/2via/condominio/<int:condominio_id>/arquivos")
def arquivos_2via(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    mes = _mes_selecionado_arquivos()
    arquivos = r2_client.listar_arquivos(condominio_id, mes)

    unidades_por_codigo = {
        u.get("unidade"): u for u in supabase_client.get_unidades(condominio_id)
    }
    for arquivo in arquivos:
        unidade_row = unidades_por_codigo.get(arquivo["unidade"])
        arquivo["proprietario"] = unidade_row.get("proprietario") if unidade_row else None

    return render_template(
        "arquivos_2via.html",
        condominio=condominio,
        arquivos=arquivos,
        mes=mes,
        condominios=supabase_client.get_condominios(),
    )


@app.route("/2via/condominio/<int:condominio_id>/arquivos/upload", methods=["POST"])
def upload_arquivo_2via(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    mes = _mes_selecionado_arquivos()
    arquivos_enviados = request.files.getlist("arquivos")
    if not arquivos_enviados or all(f.filename == "" for f in arquivos_enviados):
        flash("Selecione ao menos um arquivo PDF pra enviar.", "error")
        return redirect(url_for("arquivos_2via", condominio_id=condominio_id, mes=mes))

    enviados, ignorados = [], []
    for arquivo in arquivos_enviados:
        if not arquivo.filename:
            continue
        if not arquivo.filename.lower().endswith(".pdf"):
            ignorados.append(f"{arquivo.filename} (não é PDF)")
            continue

        unidade = arquivo.filename.rsplit(".", 1)[0].strip()
        try:
            r2_client.enviar_arquivo(condominio_id, unidade, mes, arquivo.stream.read())
            enviados.append(unidade)
        except Exception as e:
            ignorados.append(f"{arquivo.filename} (erro: {e})")

    if enviados:
        flash(f"Enviado(s) com sucesso pro mês {mes}: {', '.join(enviados)}.", "success")
    if ignorados:
        flash(f"Não enviado(s): {', '.join(ignorados)}.", "error")

    return redirect(url_for("arquivos_2via", condominio_id=condominio_id, mes=mes))


@app.route("/2via/condominio/<int:condominio_id>/arquivos/<unidade>/excluir", methods=["POST"])
def excluir_arquivo_2via(condominio_id, unidade):
    mes = _mes_selecionado_arquivos()
    r2_client.deletar_arquivo(condominio_id, unidade, mes)
    flash(f"Boleto da unidade {unidade} (mês {mes}) removido do armazenamento.", "success")
    return redirect(url_for("arquivos_2via", condominio_id=condominio_id, mes=mes))


@app.route("/2via/condominio/<int:condominio_id>/arquivos/excluir-todos", methods=["POST"])
def excluir_todos_arquivos_2via(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    mes = _mes_selecionado_arquivos()
    confirmacao = (request.form.get("confirmacao") or "").strip()
    if confirmacao != condominio["nome"]:
        flash(
            "Exclusão cancelada: o nome digitado não confere com o do condomínio.",
            "error",
        )
        return redirect(url_for("arquivos_2via", condominio_id=condominio_id, mes=mes))

    total = r2_client.deletar_todos_arquivos(condominio_id, mes)
    flash(f"{total} arquivo(s) do mês {mes} excluído(s) da pasta de {condominio['nome']}.", "success")
    return redirect(url_for("arquivos_2via", condominio_id=condominio_id, mes=mes))


def _cruzar_unidade_envios(mensagens, envios):
    """Para cada mensagem retornada pela SMTP API, procura entre os `envios`
    (registros da tabela `enviowhatapp`) o de mesmo destinatário e horário
    mais próximo, e preenche unidade/condomínio de origem."""
    por_contato = {}
    for envio in envios:
        try:
            quando = datetime.fromisoformat(envio["created_at"])
        except (TypeError, ValueError, KeyError):
            continue
        por_contato.setdefault((envio.get("contato") or "").strip().lower(), []).append((quando, envio))

    for m in mensagens:
        candidatos = por_contato.get((m.get("recipient") or "").strip().lower())
        if not candidatos or not m.get("sent_at"):
            continue
        try:
            enviado_em = datetime.fromisoformat(m["sent_at"])
        except (TypeError, ValueError):
            continue

        melhor_idx, melhor_diff = None, None
        for idx, (quando, _envio) in enumerate(candidatos):
            diff = abs((quando - enviado_em).total_seconds())
            if melhor_diff is None or diff < melhor_diff:
                melhor_idx, melhor_diff = idx, diff

        if melhor_idx is not None and melhor_diff <= 300:
            _quando, envio = candidatos.pop(melhor_idx)
            m["unidade"] = envio.get("unidade")
            m["condominio_nome"] = envio.get("nomecondominio")

    return mensagens


def _ler_filtros_historico():
    status = request.args.get("status", "all")
    recipient = (request.args.get("recipient") or "").strip() or None
    subject = (request.args.get("subject") or "").strip() or None
    unidade = (request.args.get("unidade") or "").strip() or None
    condominio_id = (request.args.get("condominio") or "").strip() or None
    start_date = request.args.get("start_date") or (date.today() - timedelta(days=30)).isoformat()
    end_date = request.args.get("end_date") or date.today().isoformat()
    return status, recipient, subject, unidade, condominio_id, start_date, end_date


def _buscar_todas_mensagens_historico(
    status, recipient, subject, unidade, condominio_id, start_date, end_date,
    por_pagina_api=500, limite_paginas_api=50,
):
    """Busca TODAS as mensagens que casam com os filtros (sem paginação na
    tela), usada tanto para listar quando há filtro de unidade/condomínio
    quanto para gerar os relatórios (PDF/Excel)."""
    periodo_fim = f"{end_date}T23:59:59"

    if condominio_id:
        # Um condomínio pode ter centenas de unidades/emails — em vez de uma
        # chamada por destinatário, varremos a SMTP API uma única vez no
        # período e filtramos localmente pelos emails daquele condomínio.
        envios = supabase_client.listar_envios_email(
            start_date, periodo_fim, unidade=unidade, condominio=condominio_id
        )
        destinatarios = {(e.get("contato") or "").strip().lower() for e in envios if e.get("contato")}

        todas = []
        pagina_api = 1
        while True:
            msgs, total_pag_api = email_reports.listar_mensagens(
                status=status, start_date=start_date, end_date=end_date,
                recipient=recipient, subject=subject, page=pagina_api, per=por_pagina_api,
            )
            todas.extend(msgs)
            if pagina_api >= total_pag_api or pagina_api >= limite_paginas_api:
                break
            pagina_api += 1

        todas = [m for m in todas if (m.get("recipient") or "").strip().lower() in destinatarios]
    elif unidade:
        # Unidade isolada tem poucos emails associados: compensa consultar
        # a SMTP API só para eles.
        envios = supabase_client.listar_envios_email(start_date, periodo_fim, unidade=unidade)
        destinatarios = sorted({(e.get("contato") or "").strip().lower() for e in envios if e.get("contato")})

        todas = []
        for email_destino in destinatarios:
            if recipient and recipient.lower() not in email_destino:
                continue
            pagina_api = 1
            while True:
                msgs, total_pag_api = email_reports.listar_mensagens(
                    status=status, start_date=start_date, end_date=end_date,
                    recipient=email_destino, subject=subject, page=pagina_api, per=por_pagina_api,
                )
                todas.extend(msgs)
                if pagina_api >= total_pag_api or pagina_api >= limite_paginas_api:
                    break
                pagina_api += 1
    else:
        envios = supabase_client.listar_envios_email(start_date, periodo_fim, contato=recipient)
        todas = []
        pagina_api = 1
        while True:
            msgs, total_pag_api = email_reports.listar_mensagens(
                status=status, start_date=start_date, end_date=end_date,
                recipient=recipient, subject=subject, page=pagina_api, per=por_pagina_api,
            )
            todas.extend(msgs)
            if pagina_api >= total_pag_api or pagina_api >= limite_paginas_api:
                break
            pagina_api += 1

    todas.sort(key=lambda m: m.get("sent_at") or "", reverse=True)
    return _cruzar_unidade_envios(todas, envios)


@app.route("/emails/historico")
def historico_emails():
    status, recipient, subject, unidade, condominio_id, start_date, end_date = _ler_filtros_historico()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    mensagens = []
    total_paginas = 1
    erro = None

    try:
        if unidade or condominio_id:
            todas = _buscar_todas_mensagens_historico(
                status, recipient, subject, unidade, condominio_id, start_date, end_date
            )
            total_paginas = max(1, -(-len(todas) // 30))
            mensagens = todas[(page - 1) * 30: page * 30]
        else:
            periodo_fim = f"{end_date}T23:59:59"
            mensagens, total_paginas = email_reports.listar_mensagens(
                status=status, start_date=start_date, end_date=end_date,
                recipient=recipient, subject=subject, page=page, per=30,
            )
            envios = supabase_client.listar_envios_email(start_date, periodo_fim, contato=recipient)
            mensagens = _cruzar_unidade_envios(mensagens, envios)
    except requests.RequestException as e:
        erro = f"Não foi possível consultar o histórico de envios agora: {e}"

    try:
        condominios = supabase_client.get_condominios()
    except requests.RequestException:
        condominios = []

    return render_template(
        "historico_emails.html",
        mensagens=mensagens,
        status=status,
        recipient=recipient or "",
        subject=subject or "",
        unidade=unidade or "",
        condominio_id=condominio_id or "",
        condominios=condominios,
        start_date=start_date,
        end_date=end_date,
        page=page,
        total_paginas=total_paginas,
        erro=erro,
    )


@app.route("/emails/historico/exportar.pdf")
def historico_emails_pdf():
    status, recipient, subject, unidade, condominio_id, start_date, end_date = _ler_filtros_historico()
    mensagens = _buscar_todas_mensagens_historico(
        status, recipient, subject, unidade, condominio_id, start_date, end_date
    )
    buffer = relatorio_emails.gerar_pdf(
        mensagens, titulo=f"Histórico de emails — {start_date} a {end_date}"
    )
    return Response(
        buffer.read(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=historico_emails_{start_date}_a_{end_date}.pdf"},
    )


@app.route("/emails/historico/exportar.xlsx")
def historico_emails_excel():
    status, recipient, subject, unidade, condominio_id, start_date, end_date = _ler_filtros_historico()
    mensagens = _buscar_todas_mensagens_historico(
        status, recipient, subject, unidade, condominio_id, start_date, end_date
    )
    buffer = relatorio_emails.gerar_excel(mensagens)
    return Response(
        buffer.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=historico_emails_{start_date}_a_{end_date}.xlsx"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
