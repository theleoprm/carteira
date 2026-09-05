#!/usr/bin/env python3
"""
Gera e envia o boletim diario de fechamento (saldo do dia, maiores altas/baixas).

Limitacao assumida: a secao de noticias do boletim (que antes vinha de busca na
web feita pelo Claude) nao tem equivalente automatico aqui, pelo mesmo motivo do
sync de dividendos - fica de fora da versao automatizada.
"""
import os
import time
import smtplib
import requests
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from common import get_db, HEADERS

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
MB_URL = "https://www.mercadobitcoin.net/api/{coin}/ticker/"
ENVS = ["investimentos", "reserva"]
ENV_LABELS = {"investimentos": "Investimentos", "reserva": "Reserva de emergência"}
_fx_cache = {}


def usd_brl():
    if "rate" not in _fx_cache:
        r = requests.get(YAHOO_URL.format(ticker="BRL=X"), headers=HEADERS, timeout=20)
        r.raise_for_status()
        _fx_cache["rate"] = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    return _fx_cache["rate"]


def brt_today_str():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%d/%m/%Y")


def coletar_variacoes(db):
    linhas = []
    for env in ENVS:
        doc = db.collection("portfolio").document(env).get()
        if not doc.exists:
            continue
        for item in doc.to_dict().get("items", []):
            classe = item.get("classe")
            if classe not in ("Ação", "FII", "ETF", "Cripto"):
                continue
            nome = item["nome"]
            moeda = item.get("moeda", "BRL")
            qtd = item.get("qtd", 0)
            try:
                if classe == "Cripto":
                    r = requests.get(MB_URL.format(coin=nome.upper()), headers=HEADERS, timeout=20)
                    r.raise_for_status()
                    t = r.json()["ticker"]
                    atual, anterior = float(t["last"]), float(t["open"])
                else:
                    ticker = nome.upper() if (classe in ("Ação", "ETF") and moeda == "USD") else nome.upper() + ".SA"
                    r = requests.get(YAHOO_URL.format(ticker=ticker), headers=HEADERS, timeout=20)
                    r.raise_for_status()
                    meta = r.json()["chart"]["result"][0]["meta"]
                    atual = meta["regularMarketPrice"]
                    anterior = meta.get("previousClose") or meta.get("chartPreviousClose")
                    if moeda == "USD":
                        fx = usd_brl()
                        atual *= fx
                        anterior *= fx
                if not anterior:
                    continue
                pct = (atual / anterior - 1) * 100
                valor_rs = qtd * (atual - anterior)
                linhas.append({"env": env, "nome": nome, "pct": pct, "valor": valor_rs, "anterior_total": qtd * anterior})
            except Exception as e:
                print(f"{nome}: falhou ({e})")
            time.sleep(0.3)
    return linhas


def montar_texto(linhas):
    data_str = brt_today_str()
    if not linhas:
        return None
    total_valor = sum(l["valor"] for l in linhas)
    total_base = sum(l["anterior_total"] for l in linhas)
    total_pct = (total_valor / total_base * 100) if total_base else 0
    ordenado = sorted(linhas, key=lambda l: l["pct"], reverse=True)
    altas = ordenado[:3]
    baixas = ordenado[-3:][::-1]

    partes = [f"Boletim da carteira — {data_str}", ""]
    sinal = "+" if total_valor >= 0 else ""
    partes.append(f"Saldo do dia: {sinal}R$ {total_valor:,.2f} ({sinal}{total_pct:.2f}%)")
    partes.append("(cobre apenas Ação/FII/ETF/Cripto — Renda Fixa não tem variação de mercado diária)")
    partes.append("")
    partes.append("Maiores altas:")
    for l in altas:
        partes.append(f"  {l['nome']} ({ENV_LABELS[l['env']]}): {l['pct']:+.2f}% / R$ {l['valor']:+,.2f}")
    partes.append("")
    partes.append("Maiores baixas:")
    for l in baixas:
        partes.append(f"  {l['nome']} ({ENV_LABELS[l['env']]}): {l['pct']:+.2f}% / R$ {l['valor']:+,.2f}")
    partes.append("")
    partes.append("Preços do momento em que este boletim foi gerado — não é cotação ao vivo contínua.")
    return "\n".join(partes)


def enviar_email(texto, data_str):
    user = os.environ.get("GMAIL_USER")
    senha = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not senha:
        print("GMAIL_USER/GMAIL_APP_PASSWORD não configurados — pulando envio de e-mail.")
        return False
    msg = MIMEText(texto, "plain", "utf-8")
    msg["Subject"] = f"Boletim da carteira — {data_str}"
    msg["From"] = user
    msg["To"] = user
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, senha)
        server.sendmail(user, [user], msg.as_string())
    return True


def main():
    db = get_db()
    linhas = coletar_variacoes(db)
    texto = montar_texto(linhas)
    if not texto:
        print("Nenhum ativo de renda variável — nada a fazer.")
        return
    db.collection("portfolio").document("boletim").set({"texto": texto, "geradoEm": int(time.time() * 1000)})
    enviado = enviar_email(texto, brt_today_str())
    print(texto)
    print("\nE-mail enviado:" if enviado else "\nE-mail NÃO enviado (ver aviso acima).")


if __name__ == "__main__":
    main()
