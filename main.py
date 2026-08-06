"""
baixa-expedicao - Render (Flask)
Recebe POST do Apps Script (bipagem de NF), resolve o pedido no Omie,
muda a etapa para 70 (Pedido Enviado), gera PDF no Drive e loga no Sheets.
"""

import os
import io
import logging
import threading
import re
import json
import requests
from collections import deque
from typing import Optional
from datetime import datetime

from flask import Flask, request, jsonify
import httpx
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Image, Table, TableStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OMIE_APP_KEY      = os.environ["OMIE_APP_KEY"]
OMIE_APP_SECRET   = os.environ["OMIE_APP_SECRET"]
OMIE_BASE_URL     = "https://app.omie.com.br/api/v1"
TOKEN_BAIXA       = os.environ["TOKEN_BAIXA"]
SHEETS_ID         = os.environ["SHEETS_ID"]
SHEETS_ABA        = os.environ.get("SHEETS_ABA", "Log_Baixas")
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
DRIVE_FOLDER_ID   = os.environ.get("DRIVE_FOLDER_ID", "")

ETAPA_DESTINO = 70
LOGO_URL = "https://www.lenvie.com.br/cdn/shop/files/Logo_Lenvie_2x_750x.png"

_processadas: deque = deque(maxlen=2000)
app = Flask(__name__)

# Google credentials
def get_credentials():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    return Credentials.from_service_account_info(creds_dict, scopes=scopes)

_sheets_client = None
def get_sheets_client():
    global _sheets_client
    if _sheets_client is None:
        _sheets_client = gspread.authorize(get_credentials())
    return _sheets_client

# Omie: retry wrapper
def omie_call(endpoint: str, call: str, param: dict) -> dict:
    import time
    url = "{}/{}/".format(OMIE_BASE_URL, endpoint)
    body = {
        "call": call,
        "app_key": OMIE_APP_KEY,
        "app_secret": OMIE_APP_SECRET,
        "param": [param],
    }
    for tentativa in range(4):
        try:
            r = httpx.post(url, json=body, timeout=60)
            data = r.json()
            fault = data.get("faultstring", "")
            if not fault:
                return data
            match = re.search(r"Aguarde (\d+) segundos", fault)
            wait = int(match.group(1)) + 5 if match else 56
            if "REDUNDANT" in fault or "Consumo redundante" in fault:
                log.warning("Rate limit Omie - aguardando %ds", wait)
                time.sleep(wait)
                continue
            if "MISUSE_API_PROCESS" in fault or "bloqueada" in fault.lower():
                raise RuntimeError("Omie bloqueado: {}".format(fault))
            raise RuntimeError("Omie faultstring: {}".format(fault))
        except (httpx.TimeoutException, httpx.ReadError) as e:
            log.warning("Timeout Omie (tentativa %d): %s", tentativa + 1, e)
            time.sleep(10 * (tentativa + 1))
    raise RuntimeError("Omie nao respondeu apos 4 tentativas")

# Omie: resolver nIdPedido
def resolver_pedido_por_nf(nf_numero: str) -> Optional[int]:
    import time
    log.info("ConsultarNF com nNF=%s", nf_numero)
    data = omie_call("produtos/nfconsultar", "ConsultarNF", {"nNF": str(nf_numero)})
    pedido_id = (
        data.get("compl", {}).get("nIdPedido")
        or data.get("nIdPedido")
        or data.get("cabecalho", {}).get("nIdPedido")
        or data.get("nfEmitInt", {}).get("nIdPedido")
    )
    if pedido_id and int(pedido_id) > 0:
        log.info("nIdPedido %s resolvido via ConsultarNF (NF %s)", pedido_id, nf_numero)
        return int(pedido_id)

    log.warning("ConsultarNF nao retornou nIdPedido para NF %s - tentando ListarNF", nf_numero)
    time.sleep(1)

    from datetime import date, timedelta
    hoje = date.today().strftime("%d/%m/%Y")
    ontem = (date.today() - timedelta(days=1)).strftime("%d/%m/%Y")

    for data_ini in [hoje, ontem]:
        resp = omie_call("produtos/nfconsultar", "ListarNF", {
            "pagina": 1, "registros_por_pagina": 50,
            "tpNF": "1", "dEmiInicial": data_ini, "dEmiFinal": hoje,
        })
        for nf in resp.get("nfCadastro", []):
            num = nf.get("ide", {}).get("nNF") or nf.get("compl", {}).get("nNF") or ""
            if str(num).strip() == str(nf_numero).strip():
                pid = nf.get("compl", {}).get("nIdPedido") or nf.get("nIdPedido")
                if pid and int(pid) > 0:
                    log.info("nIdPedido %s resolvido via ListarNF", pid)
                    return int(pid)
        time.sleep(1)

    log.error("nIdPedido nao resolvido para NF %s", nf_numero)
    return None

# Omie: trocar etapa
def trocar_etapa(n_id_pedido: int, etapa: int) -> dict:
    return omie_call("produtos/pedido", "TrocarEtapaPedido", {
        "codigo_pedido": n_id_pedido,
        "etapa": str(etapa).zfill(2),
    })

# Gerar PDF comprovante
def gerar_pdf(dados: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm
    )

    styles = getSampleStyleSheet()
    verde = colors.HexColor("#5a7a5a")
    cinza = colors.HexColor("#f5f5f5")

    style_title = ParagraphStyle(
        "title", parent=styles["Normal"],
        fontSize=18, textColor=verde,
        fontName="Helvetica-Bold", alignment=TA_CENTER, spaceAfter=4
    )
    style_sub = ParagraphStyle(
        "sub", parent=styles["Normal"],
        fontSize=10, textColor=colors.grey,
        alignment=TA_CENTER, spaceAfter=12
    )
    style_label = ParagraphStyle(
        "label", parent=styles["Normal"],
        fontSize=8, textColor=colors.grey,
        fontName="Helvetica", spaceAfter=2
    )
    style_value = ParagraphStyle(
        "value", parent=styles["Normal"],
        fontSize=11, textColor=colors.black,
        fontName="Helvetica-Bold", spaceAfter=8
    )
    style_chave = ParagraphStyle(
        "chave", parent=styles["Normal"],
        fontSize=7, textColor=colors.HexColor("#555555"),
        fontName="Helvetica", alignment=TA_CENTER, spaceAfter=4
    )

    elements = []

    # Logo
    try:
        resp_logo = requests.get(LOGO_URL, timeout=10)
        if resp_logo.status_code == 200:
            logo_buf = io.BytesIO(resp_logo.content)
            logo = Image(logo_buf, width=6*cm, height=1.5*cm)
            logo.hAlign = "CENTER"
            elements.append(logo)
            elements.append(Spacer(1, 0.3*cm))
    except Exception:
        elements.append(Paragraph("LENVIE PARFUMS", style_title))

    elements.append(Paragraph("COMPROVANTE DE EXPEDICAO", style_title))
    elements.append(Paragraph(
        dados.get("data_hora", datetime.now().strftime("%d/%m/%Y %H:%M:%S")),
        style_sub
    ))
    elements.append(HRFlowable(width="100%", thickness=1, color=verde, spaceAfter=12))

    # Tabela principal de dados
    def campo(label, valor):
        return [
            Paragraph(label, style_label),
            Paragraph(str(valor) if valor else "---", style_value)
        ]

    tabela_dados = [
        campo("NF NUMERO", dados.get("nf_numero", "")),
        campo("PEDIDO OMIE", dados.get("n_id_pedido", "")),
        campo("ETAPA", "70 - Pedido Enviado"),
        campo("TRANSPORTADORA", dados.get("transportadora", "")),
        campo("MOTORISTA", dados.get("nome_motorista", "")),
        campo("CPF", dados.get("cpf_motorista", "")),
        campo("PLACA DO VEICULO", dados.get("placa", "")),
        campo("OPERADOR", dados.get("operador", "")),
    ]

    for row in tabela_dados:
        elements.append(row[0])
        elements.append(row[1])

    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey, spaceAfter=8))

    # Chave de acesso
    if dados.get("nf_chave"):
        elements.append(Paragraph("CHAVE DE ACESSO", style_label))
        elements.append(Paragraph(dados["nf_chave"], style_chave))
        elements.append(Spacer(1, 0.3*cm))

    elements.append(HRFlowable(width="100%", thickness=1, color=verde, spaceAfter=8))
    elements.append(Paragraph(
        "Documento gerado automaticamente pelo sistema de expedicao LENVIE",
        ParagraphStyle("rodape", parent=styles["Normal"],
                       fontSize=7, textColor=colors.grey, alignment=TA_CENTER)
    ))

    doc.build(elements)
    buf.seek(0)
    return buf.read()

# Upload PDF para o Drive (suporta Shared Drive)
def upload_drive(pdf_bytes: bytes, nome_arquivo: str) -> str:
    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    file_metadata = {
        "name": nome_arquivo,
        "parents": [DRIVE_FOLDER_ID] if DRIVE_FOLDER_ID else [],
        "mimeType": "application/pdf",
    }
    media = MediaIoBaseUpload(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        resumable=False
    )
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()

    link = file.get("webViewLink", "")
    log.info("PDF gerado no Drive: %s", link)
    return link

# Gravar log no Sheets
def gravar_log(payload: dict, n_id_pedido: Optional[int], status: str, obs: str = "", link_pdf: str = ""):
    try:
        gc = get_sheets_client()
        sh = gc.open_by_key(SHEETS_ID)
        try:
            ws = sh.worksheet(SHEETS_ABA)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=SHEETS_ABA, rows=1000, cols=13)
            ws.append_row([
                "Timestamp", "NF Chave", "NF Numero", "nIdPedido",
                "Etapa", "Transportadora", "Motorista", "CPF", "Placa",
                "Operador", "Status", "Obs", "Comprovante"
            ])
        ws.append_row([
            datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            payload.get("nf_chave", ""),
            payload.get("nf_numero", ""),
            str(n_id_pedido or ""),
            str(ETAPA_DESTINO),
            payload.get("transportadora", ""),
            payload.get("nome_motorista", ""),
            payload.get("cpf_motorista", ""),
            payload.get("placa", ""),
            payload.get("operador", ""),
            status,
            obs,
            link_pdf,
        ])
        log.info("Log gravado: NF %s -> %s", payload.get("nf_numero"), status)
    except Exception as e:
        log.error("Falha ao gravar log no Sheets: %s", e, exc_info=True)

# Processamento em background
def processar_baixa(payload: dict):
    nf_chave  = payload.get("nf_chave", "")
    nf_numero = payload.get("nf_numero") or (
        str(int(nf_chave[25:34])) if len(nf_chave) >= 34 else None
    )

    chave_dedup = nf_chave.strip() or str(nf_numero)
    if chave_dedup in _processadas:
        log.info("NF %s ja processada - ignorando", nf_numero)
        return
    _processadas.append(chave_dedup)

    n_id_pedido = None
    link_pdf = ""
    try:
        n_id_pedido = resolver_pedido_por_nf(nf_numero)
        if not n_id_pedido:
            msg = "nIdPedido nao encontrado para NF {}".format(nf_numero)
            log.error(msg)
            gravar_log(payload, None, "ERRO", msg)
            return

        trocar_etapa(n_id_pedido, ETAPA_DESTINO)
        log.info("Pedido %s -> etapa %d OK (NF %s)", n_id_pedido, ETAPA_DESTINO, nf_numero)

        # Gerar PDF e subir no Drive
        agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        nome_pdf = "Comprovante_NF{}_{}.pdf".format(
            nf_numero, datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        pdf_bytes = gerar_pdf({
            "nf_numero":      nf_numero,
            "nf_chave":       nf_chave,
            "n_id_pedido":    n_id_pedido,
            "transportadora": payload.get("transportadora", ""),
            "nome_motorista": payload.get("nome_motorista", ""),
            "cpf_motorista":  payload.get("cpf_motorista", ""),
            "placa":          payload.get("placa", ""),
            "operador":       payload.get("operador", ""),
            "data_hora":      agora,
        })
        link_pdf = upload_drive(pdf_bytes, nome_pdf)

        gravar_log(payload, n_id_pedido, "OK", "", link_pdf)

    except Exception as e:
        log.error("Erro ao processar NF %s: %s", nf_numero, e, exc_info=True)
        gravar_log(payload, n_id_pedido, "ERRO", str(e), link_pdf)

# Endpoints
@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.post("/baixa")
def baixa():
    token = request.headers.get("X-Token", "")
    if token != TOKEN_BAIXA:
        return jsonify({"erro": "Token invalido"}), 401

    payload = request.get_json(force=True)
    if not payload or (not payload.get("nf_chave") and not payload.get("nf_numero")):
        return jsonify({"erro": "nf_chave ou nf_numero obrigatorio"}), 400

    t = threading.Thread(target=processar_baixa, args=(payload,), daemon=True)
    t.start()

    nf_ref = payload.get("nf_numero") or (payload.get("nf_chave", "")[:10] + "...") or "sem-nf"
    return jsonify({"status": "recebido", "nf": nf_ref})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
