"""Predicción de dirección de precio usando JEV (TypeSafe), a varios horizontes.

Importante: esto no es una señal de trading. JEV es un modelo de decisión
estructurada, no un motor de forecasting financiero — a estos horizontes
(sobre todo 1h/4h) el precio de una cripto se comporta casi como un paseo
aleatorio y no hay garantía de que la probabilidad que devuelve tenga valor
predictivo real. Por eso cada predicción se guarda con su contexto y, al
cumplirse el horizonte, se compara contra el precio real: así queda un
historial verificable (tasa de acierto real) en vez de una promesa de certeza.

Dos archivos JSON planos, independientes entre sí, para poder abrirlos y
revisarlos directamente además de verlos en el panel:
- data/predictions.json: informativo, solo para la tabla "Predicción JEV"
  (una fila por mercado+horizonte, se reemplaza al volver a predecir).
- data/tracking.json: historial de Seguimiento, append-only — cada guardado
  agrega una fila nueva acá, sin importar qué pase después en predictions.json.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx2
import typesafe_sdk as ts

from . import buda, reliability, market_study

MAX_TRACKING_RECORDS = 1000
MAX_TRACKING_BYTES = 5 * 1024 * 1024

# Informativo, SOLO para la tabla "Predicción JEV": una fila por mercado +
# horizonte (upsert), sin acumular historial. La tabla de Seguimiento no lee
# de acá — tiene su propio archivo (TRACKING_PATH) completamente aparte.
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.json"
# Historial de Seguimiento: log independiente, append-only (nunca se
# sobreescribe por mercado+horizonte). Cada vez que se guarda una predicción
# queda acá una copia propia, sin importar qué pase después en predictions.json.
TRACKING_PATH = Path(__file__).resolve().parent.parent / "data" / "tracking.json"
_CERT_BUNDLE = Path(__file__).resolve().parent.parent / "certs" / "corporate-ca-bundle.pem"

HORIZONS = {
    "1h": {"hours": 1, "label": "1 hora"},
    "4h": {"hours": 4, "label": "4 horas"},
    "24h": {"hours": 24, "label": "24 horas"},
    "7d": {"hours": 24 * 7, "label": "7 días"},
}

# Un lock por archivo: varias corrutinas podrían escribir a la vez (crear
# predicciones en paralelo + resolver vencidas), y predictions.json /
# tracking.json ahora son independientes entre sí.
_FILE_LOCK = asyncio.Lock()
_TRACKING_LOCK = asyncio.Lock()


class JevNotConfigured(RuntimeError):
    """Falta TYPESAFE_API_KEY."""


def _load() -> list[dict]:
    if not DATA_PATH.exists():
        return []
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save(records: list[dict]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = DATA_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(DATA_PATH)


def _load_tracking() -> list[dict]:
    if not TRACKING_PATH.exists():
        return []
    try:
        return json.loads(TRACKING_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save_tracking(records: list[dict]) -> None:
    serialized = json.dumps(records, indent=2, ensure_ascii=False, allow_nan=False)
    if len(records) > MAX_TRACKING_RECORDS or len(serialized.encode('utf-8')) > MAX_TRACKING_BYTES:
        raise ValueError('Límite de seguimiento: 1000 filas o 5 MiB. Exporta y administra registros antes de guardar más.')
    TRACKING_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = TRACKING_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(serialized, encoding="utf-8")
    tmp_path.replace(TRACKING_PATH)


def _make_ssl_context() -> ssl.SSLContext | None:
    if _CERT_BUNDLE.exists():
        return ssl.create_default_context(cafile=str(_CERT_BUNDLE))
    return None


def _ensure_configured() -> str:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise JevNotConfigured("Falta TYPESAFE_API_KEY (configúrala en backend/.env)")
    return api_key


def _client(api_key: str) -> ts.AsyncTypeSafeClient:
    http_client = httpx2.AsyncClient(verify=_make_ssl_context() or True, timeout=20.0)
    return ts.AsyncTypeSafeClient(api_key=api_key, http_client=http_client)


def _instructions(horizon: str) -> str:
    label = HORIZONS[horizon]["label"]
    return (
        "Dado el estado reciente de este mercado cripto (variación de precio en "
        "las últimas 1h, 4h, 24h y 7d, volumen 24h, el impulso de corto vs largo "
        "plazo y la fuerza relativa contra BTC), ¿el precio será más alto "
        f"dentro de {label} que el precio actual? Pondera especialmente si el "
        "impulso de corto plazo confirma o revierte la tendencia de largo plazo, "
        "y si el mercado se mueve más fuerte o más débil que BTC (movimiento "
        "propio del activo, no solo el mercado cripto en general). "
        "Considera spread y recent_trade_sample cuando estén disponibles. "
        "La muestra tiene duración variable: no la confundas con velas ni volatilidad horaria. "
        "Estos indicadores no tienen poder predictivo demostrado; refleja la incertidumbre."
    )


def _momentum_label(price_variation_1h: float | None, price_variation_24h: float | None) -> str:
    """Compara el impulso de corto plazo (1h) contra la tendencia de largo
    plazo (24h) — señal simple de si el movimiento reciente confirma o
    revierte la tendencia, calculada de datos que ya teníamos (no pedimos
    nada nuevo a Buda)."""
    if price_variation_1h is None or price_variation_24h is None:
        return "sin datos suficientes"
    if price_variation_1h > 0 and price_variation_24h > 0:
        return "impulso alcista (1h y 24h ambos positivos, misma dirección)"
    if price_variation_1h < 0 and price_variation_24h < 0:
        return "impulso bajista (1h y 24h ambos negativos, misma dirección)"
    if price_variation_1h > 0 and price_variation_24h < 0:
        return "posible reversión al alza (1h positivo, 24h negativo)"
    if price_variation_1h < 0 and price_variation_24h > 0:
        return "posible reversión a la baja (1h negativo, 24h positivo)"
    return "mixto / sin impulso claro"


def _enrich_state(market: dict, btc_market: dict | None) -> dict:
    """Arma el resto del state que le mandamos a JEV: además de las 4
    variaciones y el volumen (lo que ya había), agrega impulso de corto vs
    largo plazo y fuerza relativa contra BTC — dos señales derivadas de
    datos que ya teníamos, pensadas para darle a JEV más contexto que un
    puñado de porcentajes sueltos."""
    state = {
        "last_price": market["last_price"],
        "price_variation_1h": market["price_variation_1h"],
        "price_variation_4h": market["price_variation_4h"],
        "price_variation_24h": market["price_variation_24h"],
        "price_variation_7d": market["price_variation_7d"],
        "volume_24h": market["volume"],
        "fetched_at": market.get('fetched_at'),
        "spread_pct": ((market['min_ask']-market['max_bid']) / ((market['min_ask']+market['max_bid'])/2)*100)
            if market.get('min_ask',0)>0 and market.get('max_bid',0)>0 else None,
        "spread_note": "Diferencia relativa entre mejor oferta y demanda; no es una señal predictiva validada",
        "momentum_corto_vs_largo_plazo": _momentum_label(
            market["price_variation_1h"], market["price_variation_24h"]
        ),
    }
    if market["market_id"] == "BTC-CLP":
        state["fuerza_relativa_vs_btc_24h"] = "es BTC — no aplica"
    elif btc_market is None:
        state["fuerza_relativa_vs_btc_24h"] = None
    elif btc_market["price_variation_24h"] is not None and market["price_variation_24h"] is not None:
        state["fuerza_relativa_vs_btc_24h"] = round(
            market["price_variation_24h"] - btc_market["price_variation_24h"], 4
        )
    else:
        state["fuerza_relativa_vs_btc_24h"] = None
    return state


# Bandas de magnitud (% de cambio) por horizonte — a mayor horizonte, mayor
# movimiento esperado. JEV elige en cuál banda cae (primitivo Score, sobre
# estos niveles ordenados) — no calcula un porcentaje continuo, así que lo
# que se muestra es exactamente la banda y el rango que JEV escogió, no un
# número interpolado por nosotros.
MAGNITUDE_BANDS = {
    "1h": [
        ("Baja fuerte", "caída mayor a 2.0%"),
        ("Baja leve", "caída entre 0.7% y 2.0%"),
        ("Estable", "variación menor a 0.7%"),
        ("Sube leve", "alza entre 0.7% y 2.0%"),
        ("Sube fuerte", "alza mayor a 2.0%"),
    ],
    "4h": [
        ("Baja fuerte", "caída mayor a 4.0%"),
        ("Baja leve", "caída entre 1.5% y 4.0%"),
        ("Estable", "variación menor a 1.5%"),
        ("Sube leve", "alza entre 1.5% y 4.0%"),
        ("Sube fuerte", "alza mayor a 4.0%"),
    ],
    "24h": [
        ("Baja fuerte", "caída mayor a 8.0%"),
        ("Baja leve", "caída entre 3.0% y 8.0%"),
        ("Estable", "variación menor a 3.0%"),
        ("Sube leve", "alza entre 3.0% y 8.0%"),
        ("Sube fuerte", "alza mayor a 8.0%"),
    ],
    "7d": [
        ("Baja fuerte", "caída mayor a 20.0%"),
        ("Baja leve", "caída entre 7.0% y 20.0%"),
        ("Estable", "variación menor a 7.0%"),
        ("Sube leve", "alza entre 7.0% y 20.0%"),
        ("Sube fuerte", "alza mayor a 20.0%"),
    ],
}

MAGNITUDE_CRITERIA = {
    horizon: [f"{label}: {range_text}" for label, range_text in bands]
    for horizon, bands in MAGNITUDE_BANDS.items()
}


def _magnitude_instructions(horizon: str) -> str:
    label = HORIZONS[horizon]["label"]
    return (
        "Dado el mismo estado de mercado, ¿qué tan grande esperas que sea el "
        f"cambio de precio dentro de {label}? Clasifica la magnitud esperada "
        "del movimiento (suba o baje), no solo la dirección. Un impulso de "
        "corto plazo que confirma la tendencia de largo plazo, o una fuerza "
        "relativa marcada contra BTC, suelen anticipar movimientos más "
        "grandes que un mercado mixto o sin impulso claro."
    )


async def _ask_jev(market_id: str, horizon: str, api_key: str) -> dict:
    """Llama a JEV y arma el registro de la predicción, SIN guardarlo."""
    if horizon not in HORIZONS:
        raise ValueError(f"Horizonte inválido: {horizon}")

    async with buda.get_http_client() as buda_client:
        market = await buda.fetch_ticker(buda_client, market_id)
        btc_market = market if market_id == "BTC-CLP" else await buda.fetch_ticker(buda_client, "BTC-CLP")

    state = {"market_id": market["market_id"], **_enrich_state(market, btc_market)}
    state['recent_trade_sample'] = await market_study.fetch(market_id)

    client = _client(api_key)
    try:
        response = await client.system_one(
            state=state,
            questions={
                "will_rise": ts.Noul(instructions=_instructions(horizon)),
                "magnitude": ts.Score(
                    instructions=_magnitude_instructions(horizon),
                    criteria=MAGNITUDE_CRITERIA[horizon],
                ),
            },
        )
    finally:
        await client.aclose()

    probability_up = response.answers["will_rise"].noul
    predicted_direction = "up" if probability_up >= 0.5 else "down"
    magnitude = response.answers["magnitude"]
    # El score es la posición continua entre niveles (ej. 3.2), pero la banda
    # que JEV realmente "eligió" es la más cercana — redondeamos solo para
    # mostrar cuál etiqueta/rango corresponde, no para inventar un % nuevo.
    band_index = round(min(max(magnitude.score, 0), 4))
    magnitude_label, magnitude_range = MAGNITUDE_BANDS[horizon][band_index]
    magnitude_score = magnitude.score
    magnitude_confidence = magnitude.confidence
    created_at = datetime.now(timezone.utc)
    resolves_at = created_at + timedelta(hours=HORIZONS[horizon]["hours"])

    return {
        "market_id": market_id,
        "horizon": horizon,
        "created_at": created_at.isoformat(),
        "resolves_at": resolves_at.isoformat(),
        "price_at_prediction": market["last_price"],
        "probability_up": probability_up,
        "predicted_direction": predicted_direction,
        "magnitude_label": magnitude_label,
        "magnitude_range": magnitude_range,
        "magnitude_score": magnitude_score,
        "magnitude_confidence": magnitude_confidence,
        "model": response.model,
        "context_version": reliability.VERSION,
        "market_context": state,
        "baseline_up": market['price_variation_1h'] > 0 if market['price_variation_1h'] is not None else None,
        "resolved": False,
        "resolved_at": None,
        "price_at_resolution": None,
        "actual_direction": None,
        "actual_change_pct": None,
        "correct": None,
    }


async def preview_direction(market_id: str, horizon: str = "1h") -> dict:
    """Pide la predicción a JEV pero NO la guarda — es solo para mostrarla."""
    api_key = _ensure_configured()
    return await _ask_jev(market_id, horizon, api_key)


REQUIRED_SAVE_FIELDS = {
    "market_id", "horizon", "created_at", "resolves_at",
    "price_at_prediction", "probability_up", "predicted_direction",
    "magnitude_label", "magnitude_range", "magnitude_score",
    "magnitude_confidence", "model",
}


def _build_record(preview: dict) -> dict:
    missing = REQUIRED_SAVE_FIELDS - preview.keys()
    if missing:
        raise ValueError(f"Faltan campos para guardar: {', '.join(sorted(missing))}")
    if preview["market_id"] not in buda.CLP_MARKETS:
        raise ValueError(f"Mercado no soportado: {preview['market_id']}")
    if preview["horizon"] not in HORIZONS:
        raise ValueError(f"Horizonte inválido: {preview['horizon']}")

    return {
        "market_id": preview["market_id"],
        "horizon": preview["horizon"],
        "created_at": preview["created_at"],
        "resolves_at": preview["resolves_at"],
        "price_at_prediction": preview["price_at_prediction"],
        "probability_up": preview["probability_up"],
        "predicted_direction": preview["predicted_direction"],
        "magnitude_label": preview["magnitude_label"],
        "magnitude_range": preview["magnitude_range"],
        "magnitude_score": preview["magnitude_score"],
        "magnitude_confidence": preview["magnitude_confidence"],
        "model": preview["model"],
        "context_version": preview.get('context_version'),
        "market_context": preview.get('market_context'),
        "baseline_up": preview.get('baseline_up'),
        "resolved": False,
        "resolved_at": None,
        "price_at_resolution": None,
        "actual_direction": None,
        "actual_change_pct": None,
        "correct": None,
    }


async def _upsert_prediction(record: dict) -> dict:
    """Escribe SOLO en predictions.json (informativo, tabla "Predicción
    JEV"): upsert por mercado+horizonte, como máximo una fila por
    combinación. No toca tracking.json — lo usa "Predecir los 7 mercados"."""
    async with _FILE_LOCK:
        records = _load()
        existing_index = next(
            (i for i, r in enumerate(records)
             if r["market_id"] == record["market_id"] and r["horizon"] == record["horizon"]),
            None,
        )
        active_record = dict(record)
        if existing_index is not None:
            active_record["id"] = records[existing_index]["id"]
            records[existing_index] = active_record
        else:
            active_record["id"] = (max((r["id"] for r in records), default=0)) + 1
            records.append(active_record)
        _save(records)
    return active_record


async def save_prediction(preview: dict) -> dict:
    """Botón "Guardar" explícito: actualiza predictions.json (upsert, igual
    que antes) Y agrega una fila nueva a tracking.json (Seguimiento) — es el
    ÚNICO camino que escribe en tracking.json. "Predecir los 7 mercados" NO
    pasa por acá, solo por _upsert_prediction."""
    record = _build_record(preview)
    if datetime.fromisoformat(record['resolves_at']) <= datetime.now(timezone.utc):
        raise ValueError('El plazo ya venció; genera una predicción nueva')
    record['saved_at'] = datetime.now(timezone.utc).isoformat()
    async with _TRACKING_LOCK:
        tracking = _load_tracking()
        if any(all(r.get(k)==record.get(k) for k in ('market_id','horizon','created_at')) for r in tracking):
            return record
        tracking_record = dict(record)
        tracking_record["id"] = (max((r["id"] for r in tracking), default=0)) + 1
        tracking.append(tracking_record)
        _save_tracking(tracking)

    return await _upsert_prediction(record)


async def predict_all_markets(horizons: list[str], markets: list[str] | None = None) -> dict:
    """Predice varios mercados en varios horizontes en UNA sola llamada a
    JEV: todas las preguntas (dirección + magnitud, por mercado Y por
    horizonte) van en el mismo system_one(). Así "Predecir los 7 mercados"
    para los 4 horizontes cuenta como 1 sola consulta a JEV, no 4 ni 28.

    Solo actualiza predictions.json (_upsert_prediction) — NO toca
    tracking.json. Seguimiento solo se llena con el botón "Guardar"
    explícito de una fila (save_prediction)."""
    invalid_horizons = [h for h in horizons if h not in HORIZONS]
    if invalid_horizons:
        raise ValueError(f"Horizonte inválido: {', '.join(invalid_horizons)}")
    if not horizons:
        raise ValueError("Debe indicarse al menos un horizonte")
    markets = markets if markets is not None else list(buda.CLP_MARKETS)
    invalid = [m for m in markets if m not in buda.CLP_MARKETS]
    if invalid:
        raise ValueError(f"Mercados no soportados: {', '.join(invalid)}")
    api_key = _ensure_configured()

    ticker_result = await buda.fetch_clp_tickers()
    tickers_by_id = {m["market_id"]: m for m in ticker_result["markets"]}
    errors = [
        {"market_id": e["market_id"], "message": e.get("message", "Error de Buda")}
        for e in ticker_result["errors"]
        if e["market_id"] in markets
    ]
    available = [m for m in markets if m in tickers_by_id]
    if not available:
        return {"predictions": [], "errors": errors}

    def _keys(market_id: str, horizon: str) -> tuple[str, str]:
        market_key = market_id.replace("-", "_")
        return f"will_rise_{market_key}_{horizon}", f"magnitude_{market_key}_{horizon}"

    btc_market = tickers_by_id.get("BTC-CLP")
    studies = dict(zip(available, await asyncio.gather(*(market_study.fetch(m) for m in available))))
    state: dict = {}
    questions: dict = {}
    for market_id in available:
        m = tickers_by_id[market_id]
        state[market_id] = _enrich_state(m, btc_market)
        state[market_id]['recent_trade_sample'] = studies[market_id]
        for horizon in horizons:
            will_rise_key, magnitude_key = _keys(market_id, horizon)
            questions[will_rise_key] = ts.Noul(
                instructions=f"Para el mercado {market_id}: {_instructions(horizon)}"
            )
            questions[magnitude_key] = ts.Score(
                instructions=f"Para el mercado {market_id}: {_magnitude_instructions(horizon)}",
                criteria=MAGNITUDE_CRITERIA[horizon],
            )

    client = _client(api_key)
    try:
        response = await client.system_one(state=state, questions=questions)
    finally:
        await client.aclose()

    created_at = datetime.now(timezone.utc)

    predictions: list[dict] = []
    for market_id in available:
        for horizon in horizons:
            will_rise_key, magnitude_key = _keys(market_id, horizon)
            try:
                will_rise = response.answers[will_rise_key]
                magnitude = response.answers[magnitude_key]
            except KeyError as exc:
                errors.append({"market_id": market_id, "message": f"JEV no respondió esta pregunta ({horizon}): {exc}"})
                continue
            probability_up = will_rise.noul
            predicted_direction = "up" if probability_up >= 0.5 else "down"
            band_index = round(min(max(magnitude.score, 0), 4))
            magnitude_label, magnitude_range = MAGNITUDE_BANDS[horizon][band_index]
            resolves_at = created_at + timedelta(hours=HORIZONS[horizon]["hours"])
            record = {
                "market_id": market_id,
                "horizon": horizon,
                "created_at": created_at.isoformat(),
                "resolves_at": resolves_at.isoformat(),
                "price_at_prediction": tickers_by_id[market_id]["last_price"],
                "probability_up": probability_up,
                "predicted_direction": predicted_direction,
                "magnitude_label": magnitude_label,
                "magnitude_range": magnitude_range,
                "magnitude_score": magnitude.score,
                "magnitude_confidence": magnitude.confidence,
                "model": response.model,
                "context_version": reliability.VERSION,
                "market_context": state[market_id],
                "baseline_up": tickers_by_id[market_id]['price_variation_1h'] > 0 if tickers_by_id[market_id]['price_variation_1h'] is not None else None,
                "resolved": False,
                "resolved_at": None,
                "price_at_resolution": None,
                "actual_direction": None,
                "actual_change_pct": None,
                "correct": None,
            }
            predictions.append(await _upsert_prediction(record))

    return {"predictions": predictions, "errors": errors}


async def resolve_due_tracking() -> int:
    """Revisa filas de Seguimiento vencidas (de cualquier horizonte) y las
    resuelve contra el precio actual. Opera exclusivamente sobre
    tracking.json — predictions.json (tabla "Predicción JEV") no se toca acá.

    Las llamadas a Buda de acá abajo pueden tardar (una por fila vencida) y
    corren SIN el lock — si mientras tanto alguien borra o guarda algo, no
    podemos simplemente reescribir el archivo con la lista que teníamos en
    memoria al principio (eso resucitaría lo borrado o pisaría guardados
    nuevos). Por eso se recalcula sobre una lectura fresca del archivo, recién
    al final, y solo se tocan los ids que sí se resolvieron."""
    now = datetime.now(timezone.utc)

    async with _TRACKING_LOCK:
        records = _load_tracking()
    due = [r for r in records if not r["resolved"] and datetime.fromisoformat(r["resolves_at"]) <= now]
    if not due:
        return 0

    updates: dict[int, dict] = {}
    async with buda.get_http_client() as client:
        for record in due:
            try:
                target_ms = int(datetime.fromisoformat(record['resolves_at']).timestamp() * 1000)
                response = await client.get(f"/markets/{record['market_id']}/trades",
                    params={'timestamp': target_ms, 'limit': 100})
                response.raise_for_status()
                entries = response.json()['trades']['entries']
                eligible = [entry for entry in entries if 0 <= target_ms-int(entry[0]) <= 900_000]
                if not eligible:
                    continue  # No sustituir un precio objetivo faltante por el ticker actual.
                latest_ms = max(int(entry[0]) for entry in eligible)
                last_entries = [entry for entry in eligible if int(entry[0]) == latest_ms]
                prices = {buda.number(entry[2], nonnegative=True) for entry in last_entries}
                if len(prices) != 1:
                    continue  # Orden ambiguo dentro del mismo milisegundo.
                price_now = prices.pop()
                if price_now <= 0 or record['price_at_prediction'] <= 0:
                    continue
            except Exception:
                continue
            price_before = record["price_at_prediction"]
            if price_now > price_before:
                actual_direction = "up"
            elif price_now < price_before:
                actual_direction = "down"
            else:
                actual_direction = "flat"
            updates[record["id"]] = {
                "resolved": True,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "evaluation_method": "last_trade_at_or_before_deadline_v1",
                "resolution_trade_timestamp_ms": latest_ms,
                "price_at_resolution": price_now,
                "actual_direction": actual_direction,
                "actual_change_pct": round(((price_now - price_before) / price_before) * 100, 2),
                "correct": actual_direction == record["predicted_direction"],
            }

    if not updates:
        return 0

    resolved_count = 0
    async with _TRACKING_LOCK:
        current = _load_tracking()
        for r in current:
            if r["id"] in updates:
                r.update(updates[r["id"]])
                resolved_count += 1
        if resolved_count:
            _save_tracking(current)
    return resolved_count


def list_predictions(limit: int = 100) -> list[dict]:
    records = sorted(_load(), key=lambda r: r["created_at"], reverse=True)
    tracking = _load_tracking()
    return [{**r, 'reliability': reliability.assess(r, tracking)} for r in records[:limit]]


def list_history(limit: int = 500) -> list[dict]:
    """Historial completo de Seguimiento — lee únicamente tracking.json, sin
    relación con predictions.json ni con la tabla "Predicción JEV"."""
    records = sorted(_load_tracking(), key=lambda r: r["created_at"], reverse=True)
    return records[:limit]


async def delete_record(record_id: int) -> bool:
    """Borra una fila del historial de Seguimiento. Solo toca tracking.json
    — no afecta a predictions.json ni a la tabla "Predicción JEV"."""
    async with _TRACKING_LOCK:
        records = _load_tracking()
        filtered = [r for r in records if r["id"] != record_id]
        if len(filtered) == len(records):
            return False
        _save_tracking(filtered)
    return True


def _accuracy(records: list[dict]) -> dict:
    resolved = [r for r in records if r["resolved"]]
    hits = sum(1 for r in resolved if r["correct"])
    total = len(resolved)
    return {
        "resolved": total,
        "correct": hits,
        "accuracy": (hits / total) if total else None,
    }


def accuracy_summary() -> dict:
    """Se calcula sobre tracking.json completo — como es append-only, la
    tasa de acierto es acumulativa de verdad, nunca se resetea."""
    records = _load_tracking()
    summary = _accuracy(records)
    summary["by_horizon"] = {
        horizon: _accuracy([r for r in records if r["horizon"] == horizon])
        for horizon in HORIZONS
    }
    return summary


CSV_COLUMNS = [
    "id", "market_id", "horizon", "created_at", "resolves_at",
    "price_at_prediction", "probability_up", "predicted_direction",
    "magnitude_label", "magnitude_range", "magnitude_score", "magnitude_confidence", "model",
    "resolved", "resolved_at", "price_at_resolution", "actual_direction",
    "actual_change_pct", "correct",
]


def export_csv() -> str:
    """Todo tracking.json, con el resultado de verificación (acertó/falló)
    cuando ya se resolvió."""
    records = sorted(_load_tracking(), key=lambda r: r["created_at"])
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow(record)
    return buffer.getvalue()
