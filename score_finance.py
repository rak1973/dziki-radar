"""Dziki Radar 1.4: ostrożna, częściowa ocena kondycji finansowej (0–12 pkt).

Brak danych != 0 punktów. Algorytm jest prototypem i wymaga oceny analityka.
Nie wystawia ocen za wzrost organiczny, kontrakty, skalowanie, wycenę ani rozwodnienie.
"""
from datetime import date

FORMULARZE = {"10-Q", "10-K", "10-Q/A", "10-K/A"}
METRYKI = {
    "aktywa_obrotowe": ("AssetsCurrent",),
    "zobowiazania_krotkie": ("LiabilitiesCurrent",),
    "gotowka": ("CashAndCashEquivalentsAtCarryingValue",),
    "przeplywy_operacyjne": ("NetCashProvidedByUsedInOperatingActivities",),
}


def _wejscia(facts, tag):
    return facts.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD", [])


def _data(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _inst(facts, tag, target):
    """Bilans (instant) na datę raportu, nie wartość z innego okresu."""
    rows = [r for r in _wejscia(facts, tag)
            if r.get("form") in FORMULARZE and _data(r.get("end")) == target
            and not r.get("start") and type(r.get("val")) in (int, float)]
    return max(rows, key=lambda r: (r.get("filed", ""), r.get("accn", ""))) if rows else None


def _cashflow_ttm(facts, end):
    """TTM = poprzedni rok obrotowy + bieżące YTD - poprzednie YTD.

    Nie sumujemy czterech przypadkowych rekordów, żeby nie dublować przepływów YTD.
    Dla końca roku bierze się bezpośrednio pełen rok.
    """
    entries = []
    for r in _wejscia(facts, METRYKI["przeplywy_operacyjne"][0]):
        a, b = _data(r.get("start")), _data(r.get("end"))
        if r.get("form") not in FORMULARZE or a is None or b is None or type(r.get("val")) not in (int, float):
            continue
        entries.append((a, b, r))

    def best(rows):
        return max(rows, key=lambda row: (row[2].get("filed", ""), row[2].get("accn", ""))) if rows else None

    years = [(a, b, r) for a, b, r in entries if b == end and 330 <= (b-a).days <= 380]
    annual = best(years)
    if annual:
        return {"wartosc": annual[2]["val"], "metoda": "roczna", "koniec": str(end), "tag": METRYKI["przeplywy_operacyjne"][0]}

    # Bieżące YTD (>= 1 pełny kwartał, < rok obrotowy)
    current = [(a, b, r) for a, b, r in entries if b == end and 75 <= (b-a).days <= 315]
    cur = best(current)
    if not cur:
        return None
    # Roczny okres finansowy kończący się przed aktualnym YTD.
    prev_years = [(a, b, r) for a, b, r in entries
                  if 330 <= (b-a).days <= 380 and 75 <= (end-b).days <= 315
                  and abs((cur[0]-b).days) <= 14]
    fy = best(prev_years)
    if not fy:
        return None
    prev_ytd_rows = [(a,b,r) for a,b,r in entries
                     if 350 <= (cur[0]-a).days <= 380
                     and 350 <= (end-b).days <= 380
                     and abs((b-a).days-(end-cur[0]).days) <= 14]
    prev_ytd = best(prev_ytd_rows)
    if not prev_ytd:
        return None
    value = fy[2]["val"] + cur[2]["val"] - prev_ytd[2]["val"]
    return {"wartosc": value, "metoda": "TTM: FY + YTD - YTD r/r", "koniec": str(end), "tag": METRYKI["przeplywy_operacyjne"][0]}


def _points(value, cutoffs):
    return next((points for cutoff, points in cutoffs if value >= cutoff), 0)


def evaluate_financial_health(facts, target_end):
    """Zwraca dane źródłowe, wyliczenia i ocenę cząstkową; nie tworzy rankingu 100 pkt."""
    end = _data(target_end)
    if not end:
        return {"punkty": None, "oceniono_z": 0, "maks": 12, "kompletna": False, "skladniki": []}

    extracted = {}
    for name, (tag,) in METRYKI.items():
        if name == "przeplywy_operacyjne":
            continue
        row = _inst(facts, tag, end)
        extracted[name] = row["val"] if row is not None else None
    current_assets = extracted["aktywa_obrotowe"]
    current_liabs = extracted["zobowiazania_krotkie"]
    cash = extracted["gotowka"]
    operating = _cashflow_ttm(facts, end)
    cashflow = operating["wartosc"] if operating else None

    items = []
    def add(name, points, value, unit, comment, tags):
        items.append({"nazwa": name, "punkty": points, "maks": 4, "wartosc": value,
                      "jednostka": unit, "uzasadnienie": comment,
                      "zrodlo": "SEC EDGAR Company Facts", "tagi": list(tags), "koniec": str(end)})

    if current_assets is not None and current_liabs is not None and current_liabs > 0 and current_assets >= 0:
        ratio = current_assets/current_liabs
        p = _points(ratio, [(3,4),(2,3),(1.2,2),(1,1)])
        add("Płynność bieżąca",p,round(ratio,3),"x",
            f"Aktywa obrotowe / zobowiązania krótkoterminowe = {ratio:.2f}.",
            ["AssetsCurrent","LiabilitiesCurrent"])
    else:
        add("Płynność bieżąca",None,None,"x","Brak porównywalnych pozycji bilansowych na dzień kwartału.",
            ["AssetsCurrent","LiabilitiesCurrent"])

    if cash is not None and current_liabs is not None and current_liabs > 0 and cash >= 0:
        ratio = cash/current_liabs
        p = _points(ratio, [(1,4),(.5,3),(.25,2),(.1,1)])
        add("Pokrycie zobowiązań gotówką",p,round(ratio,3),"x",
            f"Gotówka i ekwiwalenty / zobowiązania krótkoterminowe = {ratio:.2f}.",
            ["CashAndCashEquivalentsAtCarryingValue","LiabilitiesCurrent"])
    else:
        add("Pokrycie zobowiązań gotówką",None,None,"x","Brak wystarczających danych lub gotówka nie jest raportowana pod standardowym tagiem.",
            ["CashAndCashEquivalentsAtCarryingValue","LiabilitiesCurrent"])

    if cashflow is None:
        add("Przepływy operacyjne / zapas gotówki",None,None,"mies.",
            "Nie udało się wiarygodnie wyznaczyć przepływów z działalności operacyjnej za 12 miesięcy.",
            ["NetCashProvidedByUsedInOperatingActivities"])
    elif cashflow >= 0:
        add("Przepływy operacyjne / zapas gotówki",4,None,"mies.",
            f"Przepływy operacyjne za 12 miesięcy są nieujemne ({cashflow/1e6:.2f} mln USD).",
            ["NetCashProvidedByUsedInOperatingActivities"])
    elif cash is not None and cash >= 0:
        runway = 12*cash/(-cashflow)
        p = _points(runway, [(36,4),(24,3),(12,2),(6,1)])
        add("Przepływy operacyjne / zapas gotówki",p,round(runway,1),"mies.",
            f"Szacunkowy czas finansowania: gotówka / roczny ujemny CFO; to uproszczenie, nie prognoza.",
            ["CashAndCashEquivalentsAtCarryingValue","NetCashProvidedByUsedInOperatingActivities"])
    else:
        add("Przepływy operacyjne / zapas gotówki",None,None,"mies.",
            "Ujemny CFO, ale brak aktualnej kwoty gotówki do wyznaczenia zapasu finansowania.",
            ["CashAndCashEquivalentsAtCarryingValue","NetCashProvidedByUsedInOperatingActivities"])

    scored = [x for x in items if x["punkty"] is not None]
    return {
        "punkty": sum(x["punkty"] for x in scored) if scored else None,
        "oceniono_z": 4*len(scored), "maks": 12, "kompletna": len(scored) == 3,
        "skladniki": items,
        "metoda": "WSTĘPNA: trzy wskaźniki po maks. 4 pkt; punktacja do przeglądu przez analityka.",
        "uwaga": "Nie jest oceną jakości technologii ani pełnym Dziki Score 100. Brak danych ≠ 0 pkt. Wskaźnik runway ignoruje przyszłe finansowanie i inwestycje.",
    }
