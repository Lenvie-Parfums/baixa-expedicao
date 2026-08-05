"""
baixa-expedicao — Render (Flask)
Recebe POST do AppSheet (bipagem de NF), resolve o pedido no Omie
e muda a etapa para 70 (Pedido Enviado). Grava log no Google Sheets.
"""

import os
import logging
import asyncio
import threading
import re
import json
from collections import deque
from typing import Optional
from datetime import datetime

from flask import Flask, request, jsonify
import httpx
import gspread
from google.oauth2.service_account import Credentials

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config via env ────────────────────────────────────────────────────────────
OMIE_APP_KEY      = os.environ["OMIE_APP_KEY"]
OMIE_APP_SECRET   = os.environ["OMIE_APP_SECRET"]
OMIE_BASE_URL     = "https://app.omie.com.br/api/v1"
TOKEN_BAIXA       = os.environ["TOKEN_BAIXA"]
SHEETS_ID         = os.environ["SHEETS_ID"]
SHEETS_ABA        = os.environ.get("SHEETS_ABA", "Log_Baixas")
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

ETAPA_DESTINO = 70  # Pedido Enviado

# ── Dedup em memória ──────────────────────────────────────────────────────────
_processadas: deque = deque(maxlen=2000)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Google Sheets client (lazy) ───────────────────────────────────────────────
_sheets_client = None

def get_sheets_client():
    global _sheets_client
    if _sheets_client is None:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        _sheets_client = gspread.authorize(creds)
    return _sheets_client

# ── Omie: retry wrapper (síncrono) ────────────────────────────────────────────
def omie_call(endpoint: str, call: str, param: dict) -> dict:
    import time
    url = f"{OMIE_BASE_URL}/{endpoint}/"
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
                log.warning("Rate limit Omie — aguardando %ds", wait)
                time.sleep(wait)
                continue
            if "MISUSE_API_PROCESS" in fault or "bloqueada" in fault.lower():
                raise RuntimeError(f"Omie bloqueado: {fault}")
            raise RuntimeError(f"Omie faultstring: {fault}")
        except (httpx.TimeoutException, httpx.ReadError) as e:
            log.warning("Timeout Omie (tentativa %d): %s", tentativa + 1, e)
            time.sleep(10 * (tentativa + 1))
    raise RuntimeError("Omie não respondeu após 4 tentativas")

# ── Omie: resolver nIdPedido pela chave NF ───────────────────────────────────
def resolver_pedido_por_nf(nf_numero: str) -> Optional[int]:
    data = omie_call("produtos/nfconsultar", "ObterNf", {"nNFe": int(nf_numero)})
    pedido_id = data.get("compl", {}).get("nIdPedido") or data.get("nIdPedido")
    return int(pedido_id) if pedido_id else None

# ── Omie: trocar etapa ────────────────────────────────────────────────────────
def trocar_etapa(n_id_pedido: int, etapa: int) -> dict:
    return omie_call("produtos/pedido", "TrocarEtapaPedido", {
        "codigo_pedido": n_id_pedido,
        "etapa": str(etapa).zfill(2),
    })

# ── Google Sheets: gravar log ─────────────────────────────────────────────────
def gravar_log(payload: dict, n_id_pedido: Optional[int], status: str, obs: str = ""):
    try:
        gc = get_sheets_client()
        sh = gc.open_by_key(SHEETS_ID)
        try:
            ws = sh.worksheet(SHEETS_ABA)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=SHEETS_ABA, rows=1000, cols=12)
            ws.append_row([
                "Timestamp", "NF Chave", "NF Número", "nIdPedido",
                "Etapa", "Transportadora", "Motorista", "CPF", "Placa",
                "Operador", "Status", "Obs"
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
        ])
        log.info("Log gravado: NF %s → %s", payload.get("nf_numero"), status)
    except Exception as e:
        log.error("Falha ao gravar log no Sheets: %s", e)

# ── Processamento em background (thread) ──────────────────────────────────────
def processar_baixa(payload: dict):
    nf_chave  = payload.get("nf_chave", "")
    nf_numero = payload.get("nf_numero") or (
        str(int(nf_chave[25:34])) if len(nf_chave) >= 34 else None
    )

    chave_dedup = nf_chave.strip()
    if chave_dedup in _processadas:
        log.info("NF %s já processada — ignorando", nf_numero)
        return
    _processadas.append(chave_dedup)

    n_id_pedido = None
    try:
        n_id_pedido = resolver_pedido_por_nf(nf_numero)
        if not n_id_pedido:
            msg = f"nIdPedido não encontrado para NF {nf_numero}"
            log.error(msg)
            gravar_log(payload, None, "ERRO", msg)
            return
        trocar_etapa(n_id_pedido, ETAPA_DESTINO)
        log.info("Pedido %s → etapa %d OK (NF %s)", n_id_pedido, ETAPA_DESTINO, nf_numero)
        gravar_log(payload, n_id_pedido, "OK")
    except Exception as e:
        log.error("Erro ao processar NF %s: %s", nf_numero, e)
        gravar_log(payload, n_id_pedido, "ERRO", str(e))

# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.post("/baixa")
def baixa():
    token = request.headers.get("X-Token", "")
    if token != TOKEN_BAIXA:
        return jsonify({"erro": "Token inválido"}), 401

    payload = request.get_json(force=True)
    if not payload or not payload.get("nf_chave"):
        return jsonify({"erro": "nf_chave obrigatório"}), 400

    # responde 200 imediatamente — processa em thread
    t = threading.Thread(target=processar_baixa, args=(payload,), daemon=True)
    t.start()

    nf_ref = payload.get("nf_numero") or payload["nf_chave"][:10] + "..."
    return jsonify({"status": "recebido", "nf": nf_ref})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))