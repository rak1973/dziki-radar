"""Samodzielny klient odczytu Financial Modeling Prep Stable API."""

import http.client
import json
import math
import os
import ssl
import time
from contextlib import closing
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import quote, urlencode


_FMP_HOST = "financialmodelingprep.com"
_FISCAL_PERIODS = ("Q1", "Q2", "Q3", "Q4", "FY")
_STATEMENT_METRICS = {
    "income-statement": ("revenue",),
    "balance-sheet-statement": ("totalAssets",),
    "cash-flow-statement": ("operatingCashFlow", "netCashProvidedByOperatingActivities"),
}


class FMPError(RuntimeError):
    """Błąd klienta FMP."""


class FMPConfigurationError(FMPError):
    """Brak wymaganej konfiguracji."""


class FMPConnectionError(FMPError):
    """Błąd transportu HTTP."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class FMPResponseError(FMPError):
    """Nieprawidłowa odpowiedź API."""


class FMPRequestBudgetError(FMPError):
    """Wyczerpano budżet prób HTTP dla tej instancji klienta."""


class FMPHTTPError(FMPError):
    """Odpowiedź HTTP z kodem błędu."""

    def __init__(self, status_code, *, retry_after=None):
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"FMP zwróciło HTTP {status_code}.")


class FMPRateLimitError(FMPHTTPError):
    """FMP ograniczyło liczbę zapytań. retry_after jest podane w sekundach."""

    def __init__(self, retry_after):
        super().__init__(429, retry_after=retry_after)


def _retry_after(header):
    if not header:
        return None
    try:
        header = header.strip()
        if header.isascii() and header.isdecimal():
            return float(header)
        deadline = parsedate_to_datetime(header)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, OverflowError):
        return None


def _request(endpoint, params, timeout):
    """Wykonaj HTTPS bez logowania URL i zwróć wynik lub bezpieczny wyjątek.

    Ramka z kluczem istnieje tylko podczas transportu. Surowe wyjątki,
    obiekty połączeń i odpowiedzi zostają wewnątrz tej funkcji.
    """
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        return FMPConfigurationError("Ustaw zmienną środowiskową FMP_API_KEY.")
    try:
        path = f"/stable/{endpoint}?{urlencode({**params, 'apikey': key})}"
        with closing(http.client.HTTPSConnection(_FMP_HOST, timeout=timeout)) as connection:
            connection.set_debuglevel(0)
            connection.request("GET", path, headers={"Accept": "application/json"})
            with connection.getresponse() as response:
                status = response.status
                if not 200 <= status < 300:
                    retry_after = None
                    if status in (429, 500, 502, 503, 504):
                        retry_after = _retry_after(response.getheader("Retry-After"))
                    if status == 429:
                        return FMPRateLimitError(retry_after)
                    return FMPHTTPError(status, retry_after=retry_after)
                payload = json.loads(response.read())
        # Odrzuć także odpowiedź 2xx, gdy serwer odbije sekret w polu JSON.
        serialized = json.dumps(payload)
        encoded = quote(key, safe="")
        spellings = (json.dumps(key)[1:-1], encoded, encoded.replace("%20", "+"))
        if any(spelling in serialized for spelling in spellings):
            return FMPResponseError("FMP zwróciło niedozwolone dane w odpowiedzi.")
    except ssl.SSLError:
        return FMPConnectionError("Nie udało się ustanowić bezpiecznego połączenia z FMP.")
    except (OSError, http.client.HTTPException):
        return FMPConnectionError("Nie udało się pobrać odpowiedzi FMP.", retryable=True)
    except ValueError:
        return FMPResponseError("FMP zwróciło nieprawidłowy JSON.")
    except Exception:
        # Nie przekazuj diagnostyce wyjątku z ramkami transportu.
        return FMPConnectionError("Żądanie do FMP zakończyło się błędem.")
    return payload


def _iso_date(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _finite_number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _valid_row(row, endpoint, params):
    if not isinstance(row, dict) or row.get("symbol") != params["symbol"]:
        return False
    if endpoint == "quote":
        return _finite_number(row.get("price"))
    if not _iso_date(row.get("date")):
        return False
    if endpoint == "historical-price-eod/full":
        return all(_finite_number(row.get(field)) for field in ("open", "high", "low", "close", "volume"))

    requested = params["period"]
    period = row.get("period")
    if requested == "quarter":
        valid_period = period in _FISCAL_PERIODS[:4]
    else:
        valid_period = period == ("FY" if requested == "annual" else requested)
    currency = row.get("reportedCurrency")
    metrics = [row[field] for field in _STATEMENT_METRICS[endpoint] if field in row]
    return (
        valid_period
        and isinstance(currency, str) and len(currency) == 3
        and currency.isascii() and currency.isalpha() and currency.isupper()
        and bool(metrics)
        and all(value is None or _finite_number(value) for value in metrics)
    )


class FMPClient:
    """Zwraca listy rekordów FMP, w tym pustą listę przy braku danych.

    max_requests ogranicza łączną liczbę prób HTTP (w tym ponowień) w
    całym życiu instancji. None oznacza brak lokalnego limitu. Użyj jednej
    instancji dla całej serii tickerów, żeby dzielić budżet i przerwę po 429.
    max_retry_delay ogranicza pojedyncze oczekiwanie. Dłuższa przerwa po
    429 jest raportowana wywołującemu i nadal obowiązuje inne tickery.
    """

    def __init__(self, *, timeout=10, max_retries=2, max_retry_delay=30, max_requests=None):
        if not os.environ.get("FMP_API_KEY", "").strip():
            raise FMPConfigurationError("Ustaw zmienną środowiskową FMP_API_KEY.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout musi być dodatni i skończony.")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries musi być nieujemną liczbą całkowitą.")
        if not math.isfinite(max_retry_delay) or max_retry_delay < 0:
            raise ValueError("max_retry_delay musi być nieujemny i skończony.")
        if max_requests is not None and (type(max_requests) is not int or max_requests < 1):
            raise ValueError("max_requests musi być dodatnią liczbą całkowitą albo None.")
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_retry_delay = max_retry_delay
        self._remaining_requests = max_requests
        self._cooldown_until = 0
        self._lock = Lock()

    def income_statement(self, symbol, *, period="annual", limit=5):
        """Pobierz rachunki zysków i strat."""
        return self._statement("income-statement", symbol, period, limit)

    def balance_sheet_statement(self, symbol, *, period="annual", limit=5):
        """Pobierz bilanse."""
        return self._statement("balance-sheet-statement", symbol, period, limit)

    def cash_flow_statement(self, symbol, *, period="annual", limit=5):
        """Pobierz rachunki przepływów pieniężnych."""
        return self._statement("cash-flow-statement", symbol, period, limit)

    def quote(self, symbol):
        """Pobierz bieżące notowanie."""
        return self._get("quote", symbol)

    def historical_prices(self, symbol, *, start=None, end=None):
        """Pobierz dzienne notowania EOD. Daty podaj w formacie RRRR-MM-DD."""
        params = {}
        for name, value in (("from", start), ("to", end)):
            if value is not None:
                if not _iso_date(value):
                    raise ValueError("Daty muszą mieć format RRRR-MM-DD.")
                params[name] = value
        if start is not None and end is not None and start > end:
            raise ValueError("Data początkowa musi poprzedzać końcową lub być jej równa.")
        return self._get("historical-price-eod/full", symbol, **params)

    def _statement(self, endpoint, symbol, period, limit):
        if not isinstance(period, str):
            raise ValueError("Nieobsługiwany okres sprawozdawczy.")
        period = period.strip()
        if period.upper() in _FISCAL_PERIODS:
            period = period.upper()
        else:
            period = period.lower()
        if period not in (*_FISCAL_PERIODS, "annual", "quarter"):
            raise ValueError("Nieobsługiwany okres sprawozdawczy.")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit musi być liczbą całkowitą od 1 do 1000.")
        return self._get(endpoint, symbol, period=period, limit=limit)

    def _attempt(self, endpoint, params, fallback):
        # Serializacja prób gwarantuje, że żaden ticker nie ominie właśnie
        # odebranego 429. Oczekiwanie na przerwę odbywa się poza blokadą.
        while True:
            with self._lock:
                if self._remaining_requests == 0:
                    raise FMPRequestBudgetError("Wyczerpano budżet zapytań do FMP.")
                delay = self._cooldown_until - time.monotonic()
                if delay <= 0:
                    result = _request(endpoint, params, self._timeout)
                    if self._remaining_requests is not None and not isinstance(result, FMPConfigurationError):
                        self._remaining_requests -= 1
                    if isinstance(result, FMPRateLimitError):
                        if result.retry_after is None:
                            result = FMPRateLimitError(fallback)
                        self._cooldown_until = time.monotonic() + result.retry_after
                    return result
                if delay > self._max_retry_delay:
                    raise FMPRateLimitError(delay)
            time.sleep(delay)

    def _get(self, endpoint, symbol, **params):
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Podaj symbol giełdowy.")
        params["symbol"] = symbol.strip().upper()
        fallback = 1
        for attempt in range(self._max_retries + 1):
            result = self._attempt(endpoint, params, fallback)
            if not isinstance(result, FMPError):
                if isinstance(result, list) and all(_valid_row(row, endpoint, params) for row in result):
                    return result
                raise FMPResponseError("FMP zwróciło rekordy niezgodne ze schematem endpointu.")

            retryable = (
                isinstance(result, FMPConnectionError) and result.retryable
                or isinstance(result, FMPHTTPError) and result.status_code in (429, 500, 502, 503, 504)
            )
            if not retryable or attempt == self._max_retries:
                raise result

            delay = getattr(result, "retry_after", None)
            if delay is not None and delay > self._max_retry_delay:
                raise result
            if not isinstance(result, FMPRateLimitError):
                with self._lock:
                    if self._remaining_requests == 0:
                        raise FMPRequestBudgetError("Wyczerpano budżet zapytań do FMP.")
                time.sleep(delay if delay is not None else min(fallback, self._max_retry_delay))
            fallback = min(fallback * 2, max(1, self._max_retry_delay))
