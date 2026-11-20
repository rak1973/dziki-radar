
import requests
import json
from datetime import datetime, timezone
from datetime import date

CIK = "0001824920"
BASE = "https://data.sec.gov"
HEADERS = {
    "User-Agent": "DzikiRadar daniel.pachuta73@gmail.com",
    "Accept": "application/json"
}

def pobierz(url):
    r = requests.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    return r.json()

def przychody(facts):
    gaap = facts.get("facts", {}).get("us-gaap", {})
    tagi = [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax"
    ]
    for tag in tagi:
        if tag in gaap:
            dane = gaap[tag].get("units", {}).get("USD", [])
            if dane:
                return dane, tag
    return [], None

def okresy(dane, roczny=False):
    wynik = {}
    for x in dane:
        if x.get("form") not in ("10-Q", "10-K"):
            continue
        if not x.get("start") or not x.get("end"):
            continue
        dni = (date.fromisoformat(x["end"]) -
               date.fromisoformat(x["start"])).days
        if roczny:
            if not 330 <= dni <= 380:
                continue
        else:
            if not 75 <= dni <= 105:
                continue
        klucz = (x["start"], x["end"])
        if klucz not in wynik or x.get("filed", "") > wynik[klucz].get("filed", ""):
            wynik[klucz] = x
    return sorted(wynik.values(), key=lambda x: x["end"])

def wzrost(teraz, dawniej):
    if not dawniej or dawniej <= 0:
        return None
    return round((teraz / dawniej - 1) * 100, 2)

sub = pobierz(BASE + "/submissions/CIK" + CIK + ".json")
facts = pobierz(BASE + "/api/xbrl/companyfacts/CIK" + CIK + ".json")

dane, tag = przychody(facts)
kwartaly = okresy(dane)
lata = okresy(dane, roczny=True)

ostatni = kwartaly[-1] if kwartaly else None
poprzedni_rok = None

if ostatni:
    cel = date.fromisoformat(ostatni["end"])
    for x in reversed(kwartaly[:-1]):
        koniec = date.fromisoformat(x["end"])
        if 350 <= (cel - koniec).days <= 380:
            poprzedni_rok = x
            break

roczny = lata[-1] if lata else None

wynik = {
    "spolka": "IONQ",
    "nazwa_sec": sub.get("name"),
    "aktualizacja": datetime.now(timezone.utc).isoformat(),
    "status": "SEC odpowiada",
    "zrodlo": "SEC EDGAR Company Facts",
    "tag_przychodow": tag,
    "przychod_kwartalny_usd": ostatni["val"] if ostatni else None,
    "koniec_kwartalu": ostatni["end"] if ostatni else None,
    "przychod_kwartalny_rok_wczesniej_usd": (
        poprzedni_rok["val"] if poprzedni_rok else None
    ),
    "wzrost_przychodow_rr_proc": (
        wzrost(ostatni["val"], poprzedni_rok["val"])
        if ostatni and poprzedni_rok else None
    ),
    "przychod_roczny_usd": roczny["val"] if roczny else None,
    "koniec_roku": roczny["end"] if roczny else None,
    "wzrost_organiczny_proc": None,
    "uwaga": "Wzrost organiczny wymaga analizy przejec"
}

with open("radar_data.json", "w", encoding="utf-8") as f:
    json.dump(wynik, f, ensure_ascii=False, indent=2)

print(json.dumps(wynik, ensure_ascii=False, indent=2))
