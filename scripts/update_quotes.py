#!/usr/bin/env python3
"""Atualiza o preco atual (pa) dos ativos de renda variavel no Firestore."""
import time
import requests
from common import get_db, HEADERS

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
MB_URL = "https://www.mercadobitcoin.net/api/{coin}/ticker/"

ENVS = ["investimentos", "reserva"]
_fx_cache = {}


def yahoo_price(ticker):
    r = requests.get(YAHOO_URL.format(ticker=ticker), headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]


def usd_brl():
    if "rate" not in _fx_cache:
        _fx_cache["rate"] = yahoo_price("BRL=X")
    return _fx_cache["rate"]


def mb_price(coin):
    r = requests.get(MB_URL.format(coin=coin), headers=HEADERS, timeout=20)
    r.raise_for_status()
    return float(r.json()["ticker"]["last"])


def main():
    db = get_db()
    for env in ENVS:
        ref = db.collection("portfolio").document(env)
        doc = ref.get()
        if not doc.exists:
            continue
        items = doc.to_dict().get("items", [])
        if not items:
            continue
        changed = False
        falhas = []
        for item in items:
            classe = item.get("classe")
            nome = item.get("nome", "")
            moeda = item.get("moeda", "BRL")
            try:
                if classe in ("Ação", "ETF") and moeda == "USD":
                    preco = yahoo_price(nome.upper()) * usd_brl()
                elif classe in ("Ação", "FII", "ETF"):
                    preco = yahoo_price(nome.upper() + ".SA")
                elif classe == "Cripto":
                    preco = mb_price(nome.upper())
                else:
                    continue
                item["pa"] = round(preco, 4)
                changed = True
            except Exception as e:
                falhas.append(f"{nome} ({e})")
            time.sleep(0.3)
        if changed:
            ref.set({"items": items, "updatedAt": int(time.time() * 1000)}, merge=True)
        print(f"{env}: atualizado. Falhas: {', '.join(falhas) if falhas else 'nenhuma'}")


if __name__ == "__main__":
    main()
