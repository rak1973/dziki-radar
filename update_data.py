"""Dziki Radar 1.3 — bezpłatny, weryfikowalny przesiew spółek z SEC EDGAR.

Uruchamianie: python update_data.py
Wyjście: radar_data.json (10 kart, statusy i źródła danych).

Ta wersja NIE ocenia wzrostu organicznego i NIE korzysta z płatnego API.
Później można dodać dostawcę FMP, zachowując ten sam format wyjścia.
"""

import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

SYMBOLS = ("IONQ", "BKSY", "RKLB", "QNC", "XNDU", "ONDS", "RGTI", "QBTS", "RDW", "VRT")
SEC_BASE = "https://data.sec.gov"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
OUTPUT = Path(__file__).with_name("radar_data.json")
# SEC wymaga identyfikowalnego User-Agent. Ten adres jest już w publicznym repo.
HEADERS = {
    "User-Agent": "DzikiRadar research daniel.pachuta73@gmail.com",
    "Accept": "application/json",
}
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)


def get_json(session, url):
    # Umiarkowane tempo dla publicznego API SEC.
    time.sleep(0.45)
    response = session.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def ticker_to_cik(document):
    """Mapowanie publicznych tickerów SEC bez zgadywania numerów CIK."""
    rows = document.values() if isinstance(document, dict) else document
    mapping = {}
    for row in rows:
        if isinstance(row, dict) and row.get("ticker") and row.get("cik_str") is not None:
            mapping[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    return mapping


def periods(items, min_days, max_days):
    """Tylko okresy raportowane, bez mylenia wartości YTD z kwartałem."""
    by_period = {}
    for entry in items:
        if entry.get("form") not in ("10-Q", "10-K"):
            continue
        if not isinstance(entry.get("val"), (int, float)):
            continue
        try:
            start = date.fromisoformat(entry["start"])
            end = date.fromisoformat(entry["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if not min_days <= (end - start).days <= max_days:
            continue
        key = (start.isoformat(), end.isoformat())
        if key not in by_period or entry.get("filed", "") > by_period[key].get("filed", ""):
            by_period[key] = entry
    return sorted(by_period.values(), key=lambda row: row["end"])


def revenue_periods(facts):
    gaap = facts.get("facts", {}).get("us-gaap", {})
    candidates = []
    for tag in REVENUE_TAGS:
        quarters = periods(gaap.get(tag, {}).get("units", {}).get("USD", []), 75, 105)
        years = periods(gaap.get(tag, {}).get("units", {}).get("USD", []), 330, 380)
        if quarters:
            candidates.append((quarters[-1]["end"], tag, quarters, years))
    if not candidates:
        return None, [], []
    # Wybierz tag z najnowszym kwartałem, przy remisie zachowaj kolejność preferencji.
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, tag, quarters, years = candidates[0]
    return tag, quarters, years


def same_quarter_last_year(quarters):
    if not quarters:
        return None
    latest_end = date.fromisoformat(quarters[-1]["end"])
    for item in reversed(quarters[:-1]):
        distance = (latest_end - date.fromisoformat(item["end"])).days
        if 350 <= distance <= 380:
            return item
    return None


def growth_pct(current, previous):
    if previous is None or previous <= 0 or current is None:
        return None
    return round((current / previous - 1) * 100, 2)


def empty_card(symbol, status, reason):
    return {
        "spolka": symbol,
        "status": status,
        "uwaga": reason,
        "zrodlo": "SEC EDGAR",
        "wzrost_organiczny_proc": None,
        "przychod_kwartalny_usd": None,
        "przychod_kwartalny_rok_wczesniej_usd": None,
        "wzrost_przychodow_rr_proc": None,
        "przychod_roczny_usd": None,
        "koniec_kwartalu": None,
    }


def build_card(symbol, facts, cik):
    tag, quarters, years = revenue_periods(facts)
    if not quarters:
        card = empty_card(symbol, "brak_danych", "W SEC nie znaleziono porównywalnych kwartalnych przychodów w standardowych tagach US-GAAP.")
        card["cik"] = cik
        return card
    latest = quarters[-1]
    previous = same_quarter_last_year(quarters)
    return {
        "spolka": symbol,
        "nazwa_sec": facts.get("entityName"),
        "cik": cik,
        "status": "ok",
        "zrodlo": "SEC EDGAR Company Facts",
        "tag_przychodow": tag,
        "przychod_kwartalny_usd": latest["val"],
        "przychod_kwartalny_rok_wczesniej_usd": previous["val"] if previous else None,
        "wzrost_przychodow_rr_proc": growth_pct(latest["val"], previous["val"] if previous else None),
        "koniec_kwartalu": latest["end"],
        "data_zlozenia_raportu": latest.get("filed"),
        "przychod_roczny_usd": years[-1]["val"] if years else None,
        "koniec_roku": years[-1]["end"] if years else None,
        "wzrost_organiczny_proc": None,
        "uwaga": "To wzrost przychodów skonsolidowanych, NIE zweryfikowany wzrost organiczny. Analiza przejęć wymagana.",
    }


def main():
    with requests.Session() as session:
        # Błąd pobrania mapy oznacza przerwanie aktualizacji — nie nadpisujemy dobrych danych pustymi.
        mapping = ticker_to_cik(get_json(session, SEC_TICKERS))
        if not mapping:
            raise RuntimeError("SEC zwrócił pustą mapę tickerów; plik danych pozostaje bez zmian")
        cards = {}
        for symbol in SYMBOLS:
            cik = mapping.get(symbol)
            if not cik:
                cards[symbol] = empty_card(symbol, "poza_mapa_sec", "Ticker niewystępujący w mapie SEC. Brak danych nie oznacza zerowych przychodów.")
                continue
            try:
                facts = get_json(session, f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json")
                cards[symbol] = build_card(symbol, facts, cik)
            except (requests.RequestException, ValueError, KeyError) as exc:
                cards[symbol] = empty_card(symbol, "blad_pobierania", f"Nie udało się pobrać danych SEC ({type(exc).__name__}).")

    result = {
        "wersja_schematu": 2,
        "aktualizacja": datetime.now(timezone.utc).isoformat(),
        "zrodlo": "SEC EDGAR; bezpłatny poziom bazowy",
        "spolki": cards,
        "nota": "Status ok oznacza dostępność danych, nie ocenę inwestycyjną. Brak wyliczeń wzrostu organicznego bez weryfikacji przejęć.",
    }
    # Zapis atomowy — nie pozostawiaj uszkodzonego pliku przy przerwaniu.
    temp_path = OUTPUT.with_suffix(".tmp")
    temp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(OUTPUT)
    print(f"Zapisano {OUTPUT.name}: {sum(c['status'] == 'ok' for c in cards.values())}/{len(cards)} kart z przychodami")


if __name__ == "__main__":
    main()
