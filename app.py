from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, abort, session, Response
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from services import auth_client, condomob, email_service, importacao_unidades, r2_client, supabase_client

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
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


def _dados_cobranca_2via(condominio_id, unidade, contato):
    """Verifica se há PDF no R2 pra essa unidade e, quando dá, busca os dados
    de vencimento/valor/pix na Condomob (best-effort). Não usa url_for, então
    pode ser chamada de fora do contexto de request (ex: numa thread pool)."""
    if not r2_client.boleto_existe(condominio_id, unidade):
        raise r2_client.BoletoNaoEncontrado(f"Sem boleto salvo para a unidade {unidade}")

    try:
        return dict(
            condomob.consultar_cobranca_aberta(condominio_id, unidade, contato["cpf_cnpj"])
        )
    except condomob.CobrancaNaoEncontrada:
        return {"vencimento": None, "valor": None, "pix": "", "linhaDigitavel": ""}


def _montar_cobranca_2via(condominio_id, unidade, contato):
    """Igual a `_dados_cobranca_2via`, mas já inclui o link estável do
    próprio sistema — só pode ser chamada dentro de um contexto de request."""
    cobranca = _dados_cobranca_2via(condominio_id, unidade, contato)
    cobranca["link"] = url_for(
        "boleto_2via_arquivo", condominio_id=condominio_id, unidade=unidade, _external=True
    )
    return cobranca


def _dados_unidade_2via(condominio_id, unidade_row):
    contato = contato_da_unidade(unidade_row)
    try:
        cobranca = _dados_cobranca_2via(condominio_id, unidade_row.get("unidade"), contato)
        return True, cobranca.get("vencimento")
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

        dados = {
            "idcontominio": int(idcontominio_bruto),
            "ds_condominio": nome,
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
        dados = {
            "ds_condominio": request.form.get("ds_condominio", "").strip(),
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


def _registrar_log_envio(condominio, unidade, contato_email, sucesso, mensagem_erro=None,
                          mes_referencia=None, cobranca=None):
    cobranca = cobranca or {}
    try:
        supabase_client.registrar_envio(
            condominio_id=condominio["id"],
            nome_condominio=condominio["nome"],
            unidade=unidade,
            mes_referencia=mes_referencia or _mes_referencia_de_data(cobranca.get("vencimento")),
            sucesso=sucesso,
            contato=contato_email,
            tipo="email",
            mensagem_erro=mensagem_erro,
            valor=cobranca.get("valor"),
            link=cobranca.get("link"),
            linha_digitavel=cobranca.get("linhaDigitavel"),
            vencimento=cobranca.get("vencimento"),
        )
    except Exception:
        app.logger.exception("Falha ao registrar log de envio na tabela enviowhatapp")


@app.route("/condominio/<int:condominio_id>/unidade/<unidade>/enviar", methods=["POST"])
def enviar_individual(condominio_id, unidade):
    condominio = supabase_client.get_condominio(condominio_id)
    unidade_row = supabase_client.get_unidade(condominio_id, unidade)
    if not condominio or not unidade_row:
        msg = "Unidade não encontrada."
        if _quer_json():
            return jsonify(sucesso=False, mensagem=msg), 404
        flash(msg, "error")
        return redirect(url_for("ver_condominio", condominio_id=condominio_id))

    contato = contato_da_unidade(unidade_row)
    emails = emails_da_unidade(unidade_row)
    if not emails:
        msg = f"Unidade {unidade} não tem email cadastrado."
        if _quer_json():
            return jsonify(sucesso=False, mensagem=msg), 400
        flash(msg, "error")
        return redirect(url_for("ver_condominio", condominio_id=condominio_id))

    try:
        cobranca = condomob.consultar_cobranca_aberta(
            condominio_id, unidade, contato["cpf_cnpj"]
        )
    except condomob.CobrancaNaoEncontrada:
        for email in emails:
            _registrar_log_envio(
                condominio, unidade, email, False, mensagem_erro="Sem cobrança em aberto"
            )
        msg = f"Unidade {unidade} não possui cobrança em aberto."
        if _quer_json():
            return jsonify(sucesso=False, mensagem=msg), 400
        flash(msg, "error")
        return redirect(url_for("ver_condominio", condominio_id=condominio_id))

    falhas = []
    for email in emails:
        try:
            email_service.enviar_boleto(email, contato["nome"], unidade, cobranca, condominio["nome"])
            _registrar_log_envio(condominio, unidade, email, True, cobranca=cobranca)
        except email_service.EnvioEmailFalhou as e:
            _registrar_log_envio(condominio, unidade, email, False, mensagem_erro=str(e), cobranca=cobranca)
            falhas.append((email, str(e)))

    if not falhas:
        msg = f"Boleto enviado para {', '.join(emails)} (unidade {unidade})."
        if _quer_json():
            return jsonify(sucesso=True, mensagem=msg)
        flash(msg, "success")
    else:
        detalhe = "; ".join(f"{email}: {erro}" for email, erro in falhas)
        msg = f"Falha ao enviar email da unidade {unidade} para: {detalhe}"
        if _quer_json():
            return jsonify(sucesso=False, mensagem=msg), 502
        flash(msg, "error")

    return redirect(url_for("ver_condominio", condominio_id=condominio_id))


@app.route("/condominio/<int:condominio_id>/enviar-massa", methods=["POST"])
def enviar_massa(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    unidades = supabase_client.get_unidades(condominio_id)
    resultados = []

    for unidade_row in unidades:
        unidade = unidade_row.get("unidade")
        contato = contato_da_unidade(unidade_row)

        if not contato["email"]:
            resultados.append({"unidade": unidade, "status": "sem email cadastrado"})
            continue

        try:
            cobranca = condomob.consultar_cobranca_aberta(
                condominio_id, unidade, contato["cpf_cnpj"]
            )
            email_service.enviar_boleto(
                contato["email"], contato["nome"], unidade, cobranca, condominio["nome"]
            )
            resultados.append({"unidade": unidade, "status": "enviado"})
        except condomob.CobrancaNaoEncontrada:
            resultados.append({"unidade": unidade, "status": "sem cobrança em aberto"})
        except email_service.EnvioEmailFalhou as e:
            resultados.append({"unidade": unidade, "status": f"erro: {e}"})

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
    )


@app.route("/2via/condominio/<int:condominio_id>/unidade/<unidade>/arquivo")
def boleto_2via_arquivo(condominio_id, unidade):
    """Rota estável: sempre busca o PDF mais atual no R2 e redireciona pra
    ele. É esse link (e não a URL assinada) que vai no email e no botão
    'Ver boleto', então nunca muda mesmo quando o arquivo é substituído."""
    if not r2_client.boleto_existe(condominio_id, unidade):
        abort(404)
    return redirect(r2_client.gerar_url_download(condominio_id, unidade))


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


@app.route("/2via/condominio/<int:condominio_id>/unidade/<unidade>/enviar", methods=["POST"])
def enviar_individual_2via(condominio_id, unidade):
    condominio = supabase_client.get_condominio(condominio_id)
    unidade_row = supabase_client.get_unidade(condominio_id, unidade)
    if not condominio or not unidade_row:
        msg = "Unidade não encontrada."
        if _quer_json():
            return jsonify(sucesso=False, mensagem=msg), 404
        flash(msg, "error")
        return redirect(url_for("ver_condominio_2via", condominio_id=condominio_id))

    contato = contato_da_unidade(unidade_row)
    emails = emails_da_unidade(unidade_row)
    if not emails:
        msg = f"Unidade {unidade} não tem email cadastrado."
        if _quer_json():
            return jsonify(sucesso=False, mensagem=msg), 400
        flash(msg, "error")
        return redirect(url_for("ver_condominio_2via", condominio_id=condominio_id))

    mes_referencia = (request.values.get("mes_referencia") or "").strip()
    administradora = (request.values.get("administradora") or "").strip()
    vencimento = (request.values.get("vencimento") or "").strip()
    if not mes_referencia or not administradora or not vencimento:
        msg = "Preencha o mês de referência, a administradora e o vencimento antes de enviar."
        if _quer_json():
            return jsonify(sucesso=False, mensagem=msg), 400
        flash(msg, "error")
        return redirect(url_for("ver_condominio_2via", condominio_id=condominio_id))

    try:
        cobranca = _montar_cobranca_2via(condominio_id, unidade, contato)
    except r2_client.BoletoNaoEncontrado:
        for email in emails:
            _registrar_log_envio(
                condominio, unidade, email, False,
                mensagem_erro="Sem boleto de 2ª via salvo", mes_referencia=mes_referencia,
                cobranca={"vencimento": vencimento},
            )
        msg = f"Unidade {unidade} não possui boleto de 2ª via salvo."
        if _quer_json():
            return jsonify(sucesso=False, mensagem=msg), 400
        flash(msg, "error")
        return redirect(url_for("ver_condominio_2via", condominio_id=condominio_id))

    link_arquivo_real = r2_client.gerar_url_download(condominio_id, unidade)

    falhas = []
    for email in emails:
        try:
            email_service.enviar_boleto_2via(
                email, contato["nome"], unidade, cobranca["link"],
                mes_referencia, vencimento, condominio["nome"], administradora,
            )
            _registrar_log_envio(
                condominio, unidade, email, True, mes_referencia=mes_referencia,
                cobranca={**cobranca, "vencimento": vencimento, "link": link_arquivo_real},
            )
        except email_service.EnvioEmailFalhou as e:
            _registrar_log_envio(
                condominio, unidade, email, False, mensagem_erro=str(e),
                mes_referencia=mes_referencia,
                cobranca={**cobranca, "vencimento": vencimento, "link": link_arquivo_real},
            )
            falhas.append((email, str(e)))

    if not falhas:
        msg = f"Boleto enviado para {', '.join(emails)} (unidade {unidade})."
        if _quer_json():
            return jsonify(sucesso=True, mensagem=msg)
        flash(msg, "success")
    else:
        detalhe = "; ".join(f"{email}: {erro}" for email, erro in falhas)
        msg = f"Falha ao enviar email da unidade {unidade} para: {detalhe}"
        if _quer_json():
            return jsonify(sucesso=False, mensagem=msg), 502
        flash(msg, "error")

    return redirect(url_for("ver_condominio_2via", condominio_id=condominio_id))


@app.route("/2via/condominio/<int:condominio_id>/enviar-massa", methods=["POST"])
def enviar_massa_2via(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    mes_referencia = (request.form.get("mes_referencia") or "").strip()
    administradora = (request.form.get("administradora") or "").strip()
    vencimento = (request.form.get("vencimento") or "").strip()
    if not mes_referencia or not administradora or not vencimento:
        flash("Preencha o mês de referência, a administradora e o vencimento antes de enviar.", "error")
        return redirect(url_for("ver_condominio_2via", condominio_id=condominio_id))

    unidades = supabase_client.get_unidades(condominio_id)
    resultados = []

    for unidade_row in unidades:
        unidade = unidade_row.get("unidade")
        contato = contato_da_unidade(unidade_row)

        if not contato["email"]:
            resultados.append({"unidade": unidade, "status": "sem email cadastrado"})
            continue

        try:
            cobranca = _montar_cobranca_2via(condominio_id, unidade, contato)
            email_service.enviar_boleto_2via(
                contato["email"],
                contato["nome"],
                unidade,
                cobranca["link"],
                mes_referencia,
                vencimento,
                condominio["nome"],
                administradora,
            )
            resultados.append({"unidade": unidade, "status": "enviado"})
        except r2_client.BoletoNaoEncontrado:
            resultados.append({"unidade": unidade, "status": "sem boleto de 2ª via salvo"})
        except email_service.EnvioEmailFalhou as e:
            resultados.append({"unidade": unidade, "status": f"erro: {e}"})

    return render_template(
        "resultado_envio.html", condominio=condominio, resultados=resultados
    )


@app.route("/2via/condominio/<int:condominio_id>/arquivos")
def arquivos_2via(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    arquivos = r2_client.listar_arquivos(condominio_id)

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
        condominios=supabase_client.get_condominios(),
    )


@app.route("/2via/condominio/<int:condominio_id>/arquivos/upload", methods=["POST"])
def upload_arquivo_2via(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    arquivos_enviados = request.files.getlist("arquivos")
    if not arquivos_enviados or all(f.filename == "" for f in arquivos_enviados):
        flash("Selecione ao menos um arquivo PDF pra enviar.", "error")
        return redirect(url_for("arquivos_2via", condominio_id=condominio_id))

    enviados, ignorados = [], []
    for arquivo in arquivos_enviados:
        if not arquivo.filename:
            continue
        if not arquivo.filename.lower().endswith(".pdf"):
            ignorados.append(f"{arquivo.filename} (não é PDF)")
            continue

        unidade = arquivo.filename.rsplit(".", 1)[0].strip()
        try:
            r2_client.enviar_arquivo(condominio_id, unidade, arquivo.stream.read())
            enviados.append(unidade)
        except Exception as e:
            ignorados.append(f"{arquivo.filename} (erro: {e})")

    if enviados:
        flash(f"Enviado(s) com sucesso: {', '.join(enviados)}.", "success")
    if ignorados:
        flash(f"Não enviado(s): {', '.join(ignorados)}.", "error")

    return redirect(url_for("arquivos_2via", condominio_id=condominio_id))


@app.route("/2via/condominio/<int:condominio_id>/arquivos/<unidade>/excluir", methods=["POST"])
def excluir_arquivo_2via(condominio_id, unidade):
    r2_client.deletar_arquivo(condominio_id, unidade)
    flash(f"Boleto da unidade {unidade} removido do armazenamento.", "success")
    return redirect(url_for("arquivos_2via", condominio_id=condominio_id))


@app.route("/2via/condominio/<int:condominio_id>/arquivos/excluir-todos", methods=["POST"])
def excluir_todos_arquivos_2via(condominio_id):
    condominio = supabase_client.get_condominio(condominio_id)
    if not condominio:
        flash("Condomínio não encontrado.", "error")
        return redirect(url_for("index"))

    confirmacao = (request.form.get("confirmacao") or "").strip()
    if confirmacao != condominio["nome"]:
        flash(
            "Exclusão cancelada: o nome digitado não confere com o do condomínio.",
            "error",
        )
        return redirect(url_for("arquivos_2via", condominio_id=condominio_id))

    total = r2_client.deletar_todos_arquivos(condominio_id)
    flash(f"{total} arquivo(s) excluído(s) da pasta de {condominio['nome']}.", "success")
    return redirect(url_for("arquivos_2via", condominio_id=condominio_id))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
