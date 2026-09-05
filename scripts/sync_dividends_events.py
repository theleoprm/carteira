#!/usr/bin/env python3
"""
Sincroniza dividendos/JCP (com data de pagamento quando disponivel) e eventos
societarios (desdobramento, grupamento, possivel bonificacao) dos ativos de
renda variavel. So considera fatos a partir de CUTOFF_DATE.

Fontes de dividendos (raspagem de HTML de sites independentes - nao sao APIs
oficiais, podem quebrar se o layout mudar):
- Acao BRL (B3): dadosdemercado.com.br -> tem data-com, tipo, valor E pagamento.
- Acao USD (EUA): statusinvest.com.br/acoes/eua/{ticker} -> idem, com pagamento.
- FII e ETF (BRL ou USD): nenhuma das duas fontes acima expoe a tabela no HTML
  server-side para esses tipos (carregam via JS) -> fica no Yahoo Finance, que
  so tem a data-com/ex, sem data de pagamento. Status sempre "a receber".

Eventos societarios (splits): sempre via Yahoo Finance, para BRL e USD.
"""
import time
import uuid
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from common import get_db, HEADERS

CUTOFF_DATE = "2026-09-01"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1y&interval=1d&events=div,split"
DADOS_URL = "https://www.dadosdemercado.com.br/acoes/{ticker}/dividendos"
STATUSINVEST_EUA_URL = "https://statusinvest.com.br/acoes/eua/{ticker}"
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
ENVS = ["investimentos", "reserva"]
_fx_cache = {}


def ticker_for(item):
    nome = item["nome"].upper()
    if item.get("classe") in ("Ação", "ETF") and item.get("moeda") == "USD":
        return nome
    return nome + ".SA"


def usd_brl():
    if "rate" not in _fx_cache:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/BRL=X", headers=HEADERS, timeout=20)
        r.raise_for_status()
        _fx_cache["rate"] = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
    return _fx_cache["rate"]


def fetch_events_yahoo(ticker):
    r = requests.get(YAHOO_URL.format(ticker=ticker), headers=HEADERS, timeout=20)
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    events = result.get("events", {})
    return events.get("dividends", {}), events.get("splits", {})


def unix_to_date(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def br_date_to_iso(d):
    if not d or "/" not in d:
        return None
    dia, mes, ano = d.split("/")
    return f"{ano}-{mes}-{dia}"


def parse_valor_br(valor_str):
    valor_str = valor_str.lstrip("*").strip().replace(".", "").replace(",", ".")
    try:
        return float(valor_str)
    except ValueError:
        return None


def fetch_dividends_dadosdemercado(nome):
    """Acoes BRL. Lista de {tipo, valor_unit, data_ex, data_pagamento}."""
    url = DADOS_URL.format(ticker=nome.lower())
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="normal-table")
    if not table or not table.find("tbody"):
        return []
    resultado = []
    for tr in table.find("tbody").find_all("tr"):
        cols = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cols) < 5:
            continue
        tipo, valor_str, _registro, ex, pagamento = cols[:5]
        valor_unit = parse_valor_br(valor_str)
        data_ex = br_date_to_iso(ex)
        if valor_unit is None or not data_ex:
            continue
        resultado.append({"tipo": tipo or "Dividendo", "valor_unit": valor_unit, "data_ex": data_ex, "data_pagamento": br_date_to_iso(pagamento)})
    return resultado


def fetch_dividends_statusinvest_eua(nome):
    """Acoes USD (EUA). Lista de {tipo, valor_unit, data_ex, data_pagamento}."""
    url = STATUSINVEST_EUA_URL.format(ticker=nome.lower())
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    tabela = None
    for t in soup.find_all("table"):
        header_text = t.get_text(" ", strip=True).upper()
        if "DATA EX" in header_text and "PAGAMENTO" in header_text:
            tabela = t
            break
    if not tabela or not tabela.find("tbody"):
        return []
    resultado = []
    for tr in tabela.find("tbody").find_all("tr"):
        cols = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cols) < 4:
            continue
        tipo, ex, pagamento, valor_str = cols[:4]
        valor_unit = parse_valor_br(valor_str)
        data_ex = br_date_to_iso(ex)
        if valor_unit is None or not data_ex:
            continue
        resultado.append({"tipo": tipo or "Dividendo", "valor_unit": valor_unit, "data_ex": data_ex, "data_pagamento": br_date_to_iso(pagamento)})
    return resultado


def process_env(db, env, hoje_str):
    port_ref = db.collection("portfolio").document(env)
    port_doc = port_ref.get()
    if not port_doc.exists:
        return
    items = port_doc.to_dict().get("items", [])
    if not items:
        return

    div_ref = db.collection("portfolio").document(f"dividendos_{env}")
    div_doc = div_ref.get()
    div_data = div_doc.to_dict() if div_doc.exists else {}
    registros = div_data.get("registros", [])
    sugestoes = div_data.get("sugestoes", [])

    mov_ref = db.collection("portfolio").document(f"movimentacoes_{env}")
    mov_doc = mov_ref.get()
    eventos = mov_doc.to_dict().get("eventos", []) if mov_doc.exists else []

    existentes_div = {(r["ativo"], r["data"]) for r in registros if r.get("origem") == "auto"}
    existentes_evt = {(e["ativo"], e["data"], e["tipo"]) for e in eventos if e.get("origem") == "auto"}

    items_changed = False
    div_changed = False
    mov_changed = False

    def registrar_com_pagamento(nome, divs, fonte):
        nonlocal div_changed, mov_changed
        qtd_item = next(i.get("qtd", 0) for i in items if i["nome"] == nome)
        for d in divs:
            if d["data_ex"] < CUTOFF_DATE or (nome, d["data_ex"]) in existentes_div:
                continue
            valor_total = round(d["valor_unit"] * qtd_item, 2)
            pago = bool(d["data_pagamento"] and d["data_pagamento"] <= hoje_str)
            registros.append({
                "id": uuid.uuid4().hex[:8], "ativo": nome, "valor": valor_total,
                "data": d["data_ex"], "dataPagamento": d["data_pagamento"],
                "tipo": d["tipo"], "status": "recebido" if pago else "a receber", "origem": "auto",
            })
            eventos.append({
                "id": uuid.uuid4().hex[:8], "tipo": "jcp" if "JCP" in d["tipo"].upper() else "dividendo",
                "ativo": nome, "data": d["data_ex"],
                "detalhes": f"{d['tipo']} anunciado ({fonte}): {d['valor_unit']:.4f}/unidade"
                            + (f" — pagamento previsto {d['data_pagamento']}" if d["data_pagamento"] else ""),
                "valor": valor_total, "qtdDelta": None, "origem": "auto",
            })
            div_changed = mov_changed = True
            print(f"{env}/{nome}: dividendo novo {d['data_ex']} = R$ {valor_total} (pagamento {d['data_pagamento']})")
        # enriquece/confirma registros existentes que ainda nao tinham data de pagamento
        for r in registros:
            if r.get("ativo") != nome or r.get("origem") != "auto" or r.get("status") == "recebido":
                continue
            match = next((d for d in divs if d["data_ex"] == r.get("data")), None)
            if not match or not match["data_pagamento"]:
                continue
            if r.get("dataPagamento") != match["data_pagamento"]:
                r["dataPagamento"] = match["data_pagamento"]
                div_changed = True
            if match["data_pagamento"] <= hoje_str:
                r["status"] = "recebido"
                div_changed = True
                print(f"{env}/{nome}: confirmado recebido automaticamente (pagamento {match['data_pagamento']})")

    for item in items:
        classe = item.get("classe")
        if classe not in ("Ação", "FII", "ETF"):
            continue
        nome = item["nome"]
        moeda = item.get("moeda", "BRL")
        qtd = item.get("qtd", 0)

        # ---- Dividendos, por tipo de ativo ----
        if classe == "Ação" and moeda != "USD":
            try:
                divs = fetch_dividends_dadosdemercado(nome)
                registrar_com_pagamento(nome, divs, "dadosdemercado.com.br")
            except Exception as e:
                print(f"{env}/{nome}: dadosdemercado.com.br falhou ({e})")
            time.sleep(0.4)
        elif classe == "Ação" and moeda == "USD":
            try:
                divs = fetch_dividends_statusinvest_eua(nome)
                registrar_com_pagamento(nome, divs, "statusinvest.com.br")
            except Exception as e:
                print(f"{env}/{nome}: statusinvest.com.br falhou ({e})")
            time.sleep(0.4)

        # ---- Eventos societarios (splits) + dividendos via Yahoo p/ FII/ETF (sem data de pagamento) ----
        ticker = ticker_for(item)
        try:
            dividends_yahoo, splits = fetch_events_yahoo(ticker)
        except Exception as e:
            print(f"{env}/{nome}: Yahoo falhou ({e})")
            time.sleep(0.3)
            continue

        if classe in ("FII", "ETF"):
            for ts, d in dividends_yahoo.items():
                data = unix_to_date(d.get("date", ts))
                if data < CUTOFF_DATE or (nome, data) in existentes_div:
                    continue
                valor_unit = d.get("amount", 0) * (usd_brl() if moeda == "USD" else 1)
                valor_total = round(valor_unit * qtd, 2)
                registros.append({
                    "id": uuid.uuid4().hex[:8], "ativo": nome, "valor": valor_total,
                    "data": data, "dataPagamento": None,
                    "tipo": "Dividendo", "status": "a receber", "origem": "auto",
                })
                eventos.append({
                    "id": uuid.uuid4().hex[:8], "tipo": "dividendo", "ativo": nome, "data": data,
                    "detalhes": f"Dividendo anunciado (sem data de pagamento disponível): {valor_unit:.4f}/unidade",
                    "valor": valor_total, "qtdDelta": None, "origem": "auto",
                })
                div_changed = mov_changed = True

        for ts, s in splits.items():
            data = unix_to_date(s.get("date", ts))
            if data < CUTOFF_DATE:
                continue
            tipo = "desdobramento" if s.get("numerator", 1) >= s.get("denominator", 1) else "grupamento"
            if (nome, data, tipo) in existentes_evt:
                continue
            ratio = s.get("numerator", 1) / s.get("denominator", 1)
            old_qtd = item.get("qtd", 0)
            new_qtd = old_qtd * ratio
            item["qtd"] = new_qtd
            item["pm"] = item.get("pm", 0) / ratio
            items_changed = True
            eventos.append({
                "id": uuid.uuid4().hex[:8], "tipo": tipo, "ativo": nome, "data": data,
                "detalhes": f"Proporção {s.get('numerator')}:{s.get('denominator')} (pode incluir bonificação, o Yahoo não distingue) — quantidade ajustada de {old_qtd:g} para {new_qtd:g}",
                "valor": None, "qtdDelta": new_qtd - old_qtd, "origem": "auto",
            })
            mov_changed = True
            print(f"{env}/{nome}: {tipo} em {data}, {old_qtd:g} -> {new_qtd:g}")

        time.sleep(0.3)

    if items_changed:
        port_ref.set({"items": items, "updatedAt": int(time.time() * 1000)}, merge=True)
    if div_changed:
        div_ref.set({"registros": registros, "sugestoes": sugestoes, "updatedAt": int(time.time() * 1000)}, merge=True)
    if mov_changed:
        mov_ref.set({"eventos": eventos, "updatedAt": int(time.time() * 1000)}, merge=True)


def main():
    db = get_db()
    hoje_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for env in ENVS:
        process_env(db, env, hoje_str)


if __name__ == "__main__":
    main()
