"""Cliente mínimo para la API pública de Buda.com (solo lectura, sin autenticación)."""
from __future__ import annotations

import asyncio
import ssl
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BUDA_API_BASE = "https://www.buda.com/api/v2"

CLP_MARKETS = [
    "BTC-CLP",
    "ETH-CLP",
    "BCH-CLP",
    "LTC-CLP",
    "USDC-CLP",
    "USDT-CLP",
    "SOL-CLP",
]

_CERT_BUNDLE = Path(__file__).resolve().parent.parent / "certs" / "corporate-ca-bundle.pem"


def _make_ssl_context() -> ssl.SSLContext | None:
    """En esta red corporativa el tráfico HTTPS pasa por un proxy de inspección
    (Forcepoint) que re-firma los certificados con su propia CA. Si el bundle
    combinado existe lo usamos; si no (otra máquina/red), httpx usa los certs
    del sistema por defecto."""
    if _CERT_BUNDLE.exists():
        return ssl.create_default_context(cafile=str(_CERT_BUNDLE))
    return None


def get_http_client() -> httpx.AsyncClient:
    ctx = _make_ssl_context()
    return httpx.AsyncClient(base_url=BUDA_API_BASE, verify=ctx or True, timeout=10.0)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def number(value, *, nonnegative=False) -> float:
    if isinstance(value, bool):
        raise ValueError("Número inválido")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError("Número inválido")
    return result


def amount(value, currency: str) -> float:
    if not isinstance(value, list) or len(value) != 2 or value[1] != currency:
        raise ValueError("Moneda o monto inválido")
    return number(value[0], nonnegative=True)


# Buda no expone variación 1h/4h directamente (solo 24h y 7d en el ticker).
# La aproximamos: pedimos trades con timestamp = "hace Nh" (la API pagina hacia
# atrás desde ese punto) y usamos el precio del trade más cercano como referencia.
# Es una aproximación por trade real, no una vela cerrada — puede diferir un poco
# de un exchange que sí calcule OHLC. Cacheada 5 min: esta ventana no necesita
# la misma frescura que el ticker (que se consulta cada 15s) y así no gastamos
# el límite de 120 req/min con estas llamadas extra.
_VARIATION_CACHE: dict[str, dict] = {}
_VARIATION_TTL_SECONDS = 300
_LOOKBACK_WINDOWS = {"price_variation_1h": 1, "price_variation_4h": 4}


async def _fetch_price_before(client: httpx.AsyncClient, market_id: str, target_ms: int) -> float | None:
    resp = await client.get(
        f"/markets/{market_id}/trades", params={"timestamp": target_ms, "limit": 1}
    )
    resp.raise_for_status()
    entries = resp.json()["trades"]["entries"]
    if not entries:
        return None
    # entrada: [timestamp_ms, monto, precio, lado, id]
    return number(entries[0][2], nonnegative=True)


async def _fetch_short_term_variations(
    client: httpx.AsyncClient, market_id: str, last_price: float
) -> dict:
    cached = _VARIATION_CACHE.get(market_id)
    now_epoch = time.time()
    if cached and cached["expires"] > now_epoch:
        return {field: ((last_price - ref) / ref) if ref else None
                for field, ref in zip(_LOOKBACK_WINDOWS, cached["references"])}

    now_ms = int(now_epoch * 1000)
    reference_prices = await asyncio.gather(
        *(
            _fetch_price_before(client, market_id, now_ms - hours * 3600_000)
            for hours in _LOOKBACK_WINDOWS.values()
        ), return_exceptions=True
    )
    reference_prices = [None if isinstance(ref, Exception) else ref for ref in reference_prices]
    data = {
        field: ((last_price - ref) / ref) if ref else None
        for field, ref in zip(_LOOKBACK_WINDOWS, reference_prices)
    }
    if all(ref is not None for ref in reference_prices):
        _VARIATION_CACHE[market_id] = {"expires": now_epoch + _VARIATION_TTL_SECONDS, "references": reference_prices}
    return data


async def fetch_ticker(client: httpx.AsyncClient, market_id: str) -> dict:
    resp = await client.get(f"/markets/{market_id}/ticker")
    resp.raise_for_status()
    ticker = resp.json()["ticker"]
    if ticker["market_id"] != market_id:
        raise ValueError("Mercado inesperado")
    base, quote = ticker["market_id"].split("-")
    last_price = amount(ticker["last_price"], quote)
    short_term = await _fetch_short_term_variations(client, market_id, last_price)

    return {
        "market_id": ticker["market_id"],
        "base_currency": base,
        "quote_currency": quote,
        "last_price": last_price,
        "min_ask": amount(ticker["min_ask"], quote),
        "max_bid": amount(ticker["max_bid"], quote),
        "volume": amount(ticker["volume"], base),
        **short_term,
        "price_variation_24h": number(ticker["price_variation_24h"]),
        "price_variation_7d": number(ticker["price_variation_7d"]),
        "fetched_at": now(),
    }


async def fetch_clp_tickers() -> dict:
    async with get_http_client() as client:
        results = await asyncio.gather(
            *(fetch_ticker(client, m) for m in CLP_MARKETS), return_exceptions=True
        )
    markets, errors = [], []
    for market_id, result in zip(CLP_MARKETS, results):
        if not isinstance(result, Exception):
            markets.append(result)
            continue
        code, message = "invalid_data", "Respuesta de mercado inválida"
        if isinstance(result, httpx.TimeoutException):
            code, message = "timeout", "Buda no respondió a tiempo"
        elif isinstance(result, httpx.HTTPStatusError):
            status = result.response.status_code
            code = "unavailable" if status == 404 else "upstream_error"
            message = f"Buda respondió HTTP {status}"
        elif isinstance(result, httpx.RequestError):
            code, message = "connection_error", "No fue posible conectar con Buda"
        errors.append({"market_id": market_id, "code": code, "message": message})
    return {"markets": markets, "errors": errors, "fetched_at": now(),
            "source": "Buda public API", "status": "ok" if not errors else "partial" if markets else "unavailable"}
