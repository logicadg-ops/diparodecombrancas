import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

COLUNAS = ["Enviado em", "Destinatário", "Unidade", "Condomínio", "Assunto", "Status", "Aberto em"]


def _fmt_data_hora(valor_iso):
    if not valor_iso:
        return "-"
    try:
        return datetime.fromisoformat(valor_iso).strftime("%d/%m/%y %H:%M:%S")
    except (TypeError, ValueError):
        return valor_iso


def _linhas(mensagens):
    linhas = []
    for m in mensagens:
        linhas.append([
            _fmt_data_hora(m.get("sent_at")),
            m.get("recipient") or "-",
            m.get("unidade") or "-",
            m.get("condominio_nome") or "-",
            m.get("subject") or "-",
            m.get("status") or "-",
            _fmt_data_hora(m.get("opened_at")),
        ])
    return linhas


def gerar_pdf(mensagens, titulo="Histórico de emails enviados"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
    )
    estilos = getSampleStyleSheet()
    elementos = [
        Paragraph(titulo, estilos["Title"]),
        Paragraph(f"{len(mensagens)} registro(s)", estilos["Normal"]),
        Spacer(1, 0.5 * cm),
    ]

    dados = [COLUNAS] + _linhas(mensagens)
    tabela = Table(dados, repeatRows=1)
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1c3d5a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d9dde2")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7fa")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elementos.append(tabela)

    doc.build(elementos)
    buffer.seek(0)
    return buffer


def gerar_excel(mensagens):
    wb = Workbook()
    ws = wb.active
    ws.title = "Histórico de emails"

    ws.append(COLUNAS)
    for celula in ws[1]:
        celula.font = Font(bold=True)

    for linha in _linhas(mensagens):
        ws.append(linha)

    larguras = [16, 28, 12, 28, 45, 14, 16]
    for idx, largura in enumerate(larguras, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = largura

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
