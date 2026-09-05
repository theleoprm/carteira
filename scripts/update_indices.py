#!/usr/bin/env python3
"""Atualiza CDI, SELIC e IPCA no Firestore a partir da API do Banco Central (SGS)."""
import time
import requests
from common import get_db, HEADERS

SERIES = {
    "selic": 432,   # Meta Selic definida pelo Copom, % a.a.
    "cdi": 4389,    # CDI acumulado no mes, anualizado (base 252), % a.a.
    "ipca": 13522,  # IPCA acumulado em 12 meses, %
}
URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados/ultimos/1?formato=json"


def buscar(codigo):
    r = requests.get(URL.format(codigo=codigo), headers=HEADERS, timeout=20)
    r.raise_for_status()
    dados = r.json()
    return float(dados[-1]["valor"].replace(",", "."))


def main():
    db = get_db()
    resultado = {}
    for nome, codigo in SERIES.items():
        try:
            resultado[nome] = buscar(codigo)
            print(f"{nome}: {resultado[nome]}")
        except Exception as e:
            print(f"{nome}: falhou ({e})")
        time.sleep(1)

    if resultado:
        resultado["updatedAt"] = int(time.time() * 1000)
        db.collection("portfolio").document("referencias").set(resultado, merge=True)
        print("Gravado em portfolio/referencias.")


if __name__ == "__main__":
    main()
