#!/usr/bin/env python3
"""
Sincroniza dividendos/JCP pagos e eventos societarios (desdobramento, grupamento,
possivel bonificacao) dos ativos de renda variavel, usando o historico oficial do
Yahoo Finance. So considera fatos a partir de CUTOFF_DATE (pedido do usuario: nada
retroativo a antes da migracao).

Limitacao assumida: a deteccao de dividendos ANUNCIADOS mas ainda nao pagos (que
antes dependia de busca na web feita pelo Claude) nao tem equivalente automatico
aqui - um script Python nao tem uma ferramenta de busca web geral. Essa parte fica
de fora da automacao; dividendos anunciados devem ser lancados manualmente no
painel quando anunciados, ou perguntados ao Claude sob demanda.
"""
import time
import uuid
import requests
from datetime import datetime, timezone
from common import get_db, HEADERS

CUTOFF_DATE = "2026-09-01"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1y&interval=1d&events=div,split"
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


def fetch_events(ticker):
    r = requests.get(YAHOO_URL.format(ticker=ticker), headers=HEADERS, timeout=20)
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    events = result.get("events", {})
    return events.get("dividends", {}), events.get("splits", {})


def unix_to_date(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def process_env(db, env):
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

    for item in items:
        if item.get("classe") not in ("Ação", "FII", "ETF"):
            continue
        nome = item["nome"]
        ticker = ticker_for(item)
        moeda = item.get("moeda", "BRL")
        try:
            dividends, splits = fetch_events(ticker)
        except Exception as e:
            print(f"{env}/{nome}: falhou ao buscar eventos ({e})")
            time.sleep(0.3)
            continue

        for ts, d in dividends.items():
            data = unix_to_date(d.get("date", ts))
            if data < CUTOFF_DATE:
                continue
            if (nome, data) in existentes_div:
                continue
            valor_unit = d.get("amount", 0)
            if moeda == "USD":
                valor_unit *= usd_brl()
            valor_total = round(valor_unit * item.get("qtd", 0), 2)
            registros.append({
                "id": uuid.uuid4().hex[:8], "ativo": nome, "valor": valor_total,
                "data": data, "tipo": "Dividendo", "status": "recebido", "origem": "auto",
            })
            eventos.append({
                "id": uuid.uuid4().hex[:8], "tipo": "dividendo", "ativo": nome, "data": data,
                "detalhes": f"Dividendo pago: {valor_unit:.4f}/unidade", "valor": valor_total,
                "qtdDelta": None, "origem": "auto",
            })
            div_changed = True
            mov_changed = True
            print(f"{env}/{nome}: dividendo {data} = R$ {valor_total}")

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
    for env in ENVS:
        process_env(db, env)


if __name__ == "__main__":
    main()
