"""Obliczenia EOD dla mapy sektorów i Dzikiego Odkrywcy."""

from collections.abc import Iterable, Mapping, Set
from datetime import date
from math import isfinite
from statistics import fmean


SCHEMA_VERSION = 1
METHODOLOGY_VERSION = 2
_COMPARISON_DECIMALS = 10
_ZERO_TOLERANCE = 10 ** -_COMPARISON_DECIMALS
BENCHMARK = "SPY"
WINDOWS = (5, 10, 20)
# Identyfikatory sektorów GICS są niezależne od tickera i języka widoku.
SECTORS = (
    ("10", "XLE", "Energia"),
    ("15", "XLB", "Materiały"),
    ("20", "XLI", "Przemysł"),
    ("25", "XLY", "Dobra konsumpcyjne uznaniowe"),
    ("30", "XLP", "Dobra konsumpcyjne podstawowe"),
    ("35", "XLV", "Ochrona zdrowia"),
    ("40", "XLF", "Finanse"),
    ("45", "XLK", "Technologie informacyjne"),
    ("50", "XLC", "Usługi komunikacyjne"),
    ("55", "XLU", "Usługi użyteczności publicznej"),
    ("60", "XLRE", "Nieruchomości"),
)


def calculate_rotation(
    series_by_symbol, sessions, *, source, price_basis="split_adjusted_close", shortened_sessions=()
):
    """Zwraca migawkę zgodną z kontraktem JSON v1.

    series_by_symbol mapuje ticker na rekordy z polami date, close i volume.
    close musi używać tej samej podstawy cenowej dla wszystkich tickerów.
    Opcjonalne is_complete=False wyłącza cały rekord. Kolejność rekordów
    jest dowolna. Duplikat danej sesji wyłącza jej dane dla danego tickera.

    sessions musi pochodzić z pełnego, niezależnego kalendarza giełdowego.
    Podaj rosnące daty ISO wszystkich zakończonych sesji analizowanego
    okresu, także tych bez notowań u dostawcy. Nie wyprowadzaj kalendarza
    z rekordów ETF-ów ani SPY. Moduł sprawdza format i kolejność dat,
    a kompletność kalendarza i zamknięcie sesji zapewnia wywołujący.
    Ostatnia data jest wspólna dla wszystkich obliczeń. shortened_sessions
    wskazuje sesje skrócone, z wolumenem bez przeskalowania.
    Nieznane wskaźniki są zapisywane jako null, a okna mają klucze tekstowe.
    Ranking wymaga porównywalnych cen SPY i 11 sektorów. Braki samego
    wolumenu są raportowane osobno i nie blokują rankingu cenowego.
    """
    if isinstance(sessions, Set):
        raise ValueError("sessions wymaga dat w kolejności rosnącej.")
    sessions = _calendar_dates(sessions, "sessions")
    if any(left > right for left, right in zip(sessions, sessions[1:])):
        raise ValueError("sessions wymaga dat w kolejności rosnącej.")
    shortened_sessions = set(_calendar_dates(shortened_sessions, "shortened_sessions"))
    if not isinstance(series_by_symbol, Mapping):
        raise ValueError("Podaj mapę tickerów na rekordy EOD.")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Podaj nazwę źródła danych.")
    if price_basis not in ("split_adjusted_close", "split_and_dividend_adjusted_close"):
        raise ValueError("Podaj obsługiwaną podstawę cenową.")

    sessions = sessions[-2 * max(WINDOWS):]
    shortened = [session for session in sessions if session in shortened_sessions]
    benchmark = _metrics(series_by_symbol.get(BENCHMARK), sessions)
    sectors = []
    for sector_id, ticker, name in SECTORS:
        metrics = _metrics(series_by_symbol.get(ticker), sessions)
        for window, values in metrics["windows"].items():
            values["relative_strength_pp"] = _difference(
                values["return_pct"], benchmark["windows"][window]["return_pct"]
            )
        previous_rs = _difference(metrics.pop("previous_return_5"), benchmark["previous_return_5"])
        metrics["acceleration_a5_pp"] = _difference(
            metrics["windows"]["5"]["relative_strength_pp"], previous_rs
        )
        sectors.append({
            "sector_id": sector_id,
            "ticker": ticker,
            "name": name,
            "classification": {"status": "unclassified", "rank": None, "score": None, "reasons": []},
            **metrics,
        })
    del benchmark["previous_return_5"]

    benchmark_ready = all(values["return_pct"] is not None for values in benchmark["windows"].values())
    comparable = {
        sector["sector_id"] for sector in sectors
        if all(values["relative_strength_pp"] is not None for values in sector["windows"].values())
        and sector["acceleration_a5_pp"] is not None
    }
    volume_ready = [
        sector["sector_id"] for sector in sectors
        if all(values["volume_change_pct"] is not None for values in sector["windows"].values())
    ]
    ranking_ready = benchmark_ready and len(comparable) == len(SECTORS)
    dominant, emerging = _classify(sectors) if ranking_ready else ([], [])

    return {
        "schema_version": SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "as_of_session": sessions[-1] if sessions else None,
        "source": source.strip(),
        "price_basis": price_basis,
        "sector_taxonomy": "GICS",
        "windows_sessions": list(WINDOWS),
        "shortened_sessions": shortened,
        "volume_measure": "traded_shares",
        "benchmark": {"ticker": BENCHMARK, **benchmark},
        "coverage": {
            "benchmark_ready": benchmark_ready,
            "ranking_ready": ranking_ready,
            "sessions_considered": len(sessions),
            "expected_sector_count": len(SECTORS),
            "comparable_sector_count": len(comparable),
            "unavailable_sector_ids": [
                sector["sector_id"] for sector in sectors if sector["sector_id"] not in comparable
            ],
            "volume_ready_sector_ids": volume_ready,
        },
        "sectors": sectors,
        "dominant_sector_ids": dominant,
        "emerging_sector_ids": emerging,
    }


def _calendar_dates(values, parameter):
    message = f"{parameter} wymaga kolekcji unikalnych dat w formacie RRRR-MM-DD."
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        raise ValueError(message)
    try:
        dates = tuple(values)
    except TypeError:
        raise ValueError(message) from None
    if not all(_iso_date(value) for value in dates) or len(set(dates)) != len(dates):
        raise ValueError(message)
    return dates


def _iso_date(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _number(value, *, positive=False):
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    if not isfinite(value) or value < 0 or (positive and value == 0):
        return None
    return value


def _change(current, previous):
    if current is None or previous is None or previous == 0:
        return None
    result = 100 * (current / previous - 1)
    return _normalized_metric(result)


def _difference(current, previous):
    if current is None or previous is None:
        return None
    result = current - previous
    return _normalized_metric(result)


def _normalized_metric(value):
    if not isfinite(value):
        return None
    return 0.0 if abs(value) <= _ZERO_TOLERANCE else value


def _return(prices, window, *, offset=0):
    if len(prices) <= window + offset:
        return None
    segment = prices[len(prices) - window - offset - 1:len(prices) - offset]
    if None in segment:
        return None
    return _change(segment[-1], segment[0])


def _volume_change(volumes, window):
    if len(volumes) < 2 * window or None in volumes[-2 * window:]:
        return None
    try:
        return _change(fmean(volumes[-window:]), fmean(volumes[-2 * window:-window]))
    except OverflowError:
        return None


def _direction(value):
    if value is None:
        return None
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _metrics(rows, sessions):
    records = {}
    expected = set(sessions)
    if not isinstance(rows, Iterable):
        rows = ()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        session = row.get("date")
        if not isinstance(session, str) or session not in expected:
            continue
        records[session] = row if session not in records and row.get("is_complete", True) is True else {}

    prices = [_number(records.get(day, {}).get("close"), positive=True) for day in sessions]
    volumes = [_number(records.get(day, {}).get("volume")) for day in sessions]
    windows = {}
    for window in WINDOWS:
        price_return = _return(prices, window)
        volume_change = _volume_change(volumes, window)
        windows[str(window)] = {
            "return_pct": price_return,
            "volume_change_pct": volume_change,
            "price_direction": _direction(price_return),
            "volume_direction": _direction(volume_change),
        }
    return {
        "windows": windows,
        "previous_return_5": _return(prices, 5, offset=5),
        "missing_price_sessions": [day for day, price in zip(sessions, prices) if price is None],
        "missing_volume_sessions": [day for day, volume in zip(sessions, volumes) if volume is None],
    }


def _classify(sectors):
    dominant = []
    for sector in sectors:
        windows = sector["windows"]
        rs5 = windows["5"]["relative_strength_pp"]
        rs10 = windows["10"]["relative_strength_pp"]
        rs20 = windows["20"]["relative_strength_pp"]
        sector["classification"]["status"] = "unranked"
        if rs10 > 0 and rs20 > 0:
            score = 0.50 * rs20 + 0.30 * rs10 + 0.20 * rs5
            dominant.append((score, sector["ticker"], sector))
    # Stała precyzja klucza zachowuje przechodniość porządku przy remisach.
    dominant.sort(key=lambda item: (-round(item[0], _COMPARISON_DECIMALS), item[1]))
    dominant = dominant[:3]
    for rank, (score, _, sector) in enumerate(dominant, 1):
        sector["classification"] = {
            "status": "dominant", "rank": rank, "score": score,
            "reasons": ["positive_rs_10", "positive_rs_20"],
        }

    emerging = [
        sector for sector in sectors
        if sector["classification"]["status"] != "dominant"
        and sector["windows"]["5"]["relative_strength_pp"] > 0
        and sector["acceleration_a5_pp"] > 0
    ]
    emerging.sort(key=lambda sector: (
        -round(sector["acceleration_a5_pp"], _COMPARISON_DECIMALS),
        -round(sector["windows"]["5"]["relative_strength_pp"], _COMPARISON_DECIMALS),
        sector["ticker"],
    ))
    emerging = emerging[:2]
    for rank, sector in enumerate(emerging, 1):
        sector["classification"] = {
            "status": "emerging", "rank": rank, "score": None,
            "reasons": ["positive_rs_5", "positive_acceleration_a5"],
        }
    return [sector["sector_id"] for _, _, sector in dominant], [sector["sector_id"] for sector in emerging]
