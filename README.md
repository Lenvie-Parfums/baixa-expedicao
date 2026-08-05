# baixa-expedicao

Serviço FastAPI no Render que recebe a bipagem do AppSheet,
resolve o pedido no Omie e muda a etapa para **70 (Pedido Enviado)**.
Grava log completo no Google Sheets.

## Fluxo

```
AppSheet (bipa NF) → POST /baixa → Render → TrocarEtapaPedido (→ 70) Omie
                                       ↓
                               Sheets aba Log_Baixas
```

---

## 1. Variáveis de ambiente (Render → Environment)

| Variável | Descrição |
|---|---|
| `OMIE_APP_KEY` | app_key da conta Omie correta |
| `OMIE_APP_SECRET` | app_secret correspondente |
| `TOKEN_BAIXA` | segredo gerado por você (ex: `openssl rand -hex 32`) |
| `SHEETS_ID` | ID da planilha de log (da URL do Sheets) |
| `SHEETS_ABA` | nome da aba (padrão: `Log_Baixas`) |
| `GOOGLE_CREDS_JSON` | JSON completo da service account (cole como string) |

---

## 2. Google Sheets — Service Account

1. Google Cloud Console → IAM → Service Accounts → criar conta
2. Gerar chave JSON → copiar conteúdo inteiro
3. Colar como valor de `GOOGLE_CREDS_JSON` no Render
4. Na planilha de log, compartilhar com o e-mail da service account (Editor)

A aba `Log_Baixas` é criada automaticamente no primeiro uso.

---

## 3. AppSheet — configurar o POST para o Render

No AppSheet, crie uma **Automation** (ou Action do tipo "Call a webhook"):

**URL:** `https://baixa-expedicao.onrender.com/baixa`  
**Method:** POST  
**Headers:**
```
Content-Type: application/json
X-Token: <valor do TOKEN_BAIXA>
```

**Body (JSON):**
```json
{
  "nf_chave": "<<NF>>",
  "nf_numero": "<<NF_Tela>>",
  "transportadora": "<<Transportadora>>",
  "nome_motorista": "<<Nome_Motorista>>",
  "cpf_motorista": "<<CPF>>",
  "placa": "<<Placa>>",
  "operador": "<<USEREMAIL()>>"
}
```

Substitua `<<campo>>` pelos nomes exatos das colunas no seu AppSheet.

**Quando disparar:** no Event Action `Form Saved` do formulário de bipagem
(o mesmo gatilho do `Finalizar_Bipagem_Auto` que você já tem).

---

## 4. Deploy

```bash
# 1 clique via render.yaml, ou manualmente:
# Render → New Web Service → conectar repo → Start Command:
uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
```

> `--workers 1` é obrigatório — dedup em memória não sobrevive a múltiplos workers.

---

## 5. Endpoints

| Endpoint | Método | Descrição |
|---|---|---|
| `/health` | GET | health check (sem auth) |
| `/baixa` | POST | recebe bipagem, processa em background |

---

## 6. Log_Baixas — colunas gravadas

| Coluna | Descrição |
|---|---|
| Timestamp | data/hora da baixa |
| NF Chave | chave de acesso completa (44 dígitos) |
| NF Número | número da NF |
| nIdPedido | ID interno Omie |
| Etapa | sempre 70 |
| Transportadora | vinda do AppSheet |
| Motorista | nome do motorista |
| CPF | CPF do motorista |
| Placa | placa do veículo |
| Operador | e-mail de quem bipou |
| Status | OK ou ERRO |
| Obs | detalhe do erro (se houver) |

---

## 7. Checklist antes de ir pra produção

- [ ] Testar `/health` — deve retornar `{"status":"ok"}`
- [ ] Fazer POST manual com Postman/curl na `/baixa` com uma NF de teste
- [ ] Confirmar que a etapa mudou para 70 no Omie
- [ ] Confirmar que a linha apareceu na aba Log_Baixas
- [ ] Configurar o webhook no AppSheet apontando para a URL do Render
- [ ] Testar bipagem real no celular com uma NF de homologação
