from pathlib import Path
from typing import Any
import asyncio
from contextlib import asynccontextmanager, suppress

import typesafe_sdk as ts
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import buda, predictions, experiment

load_dotenv()

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(experiment.worker())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="Crypto Quest — backend de pruebas", lifespan=lifespan)


@app.get('/api/experiment')
async def experiment_history():
    rows = experiment.records()
    return {'records': rows, 'summary': experiment.summary(rows)}


@app.post('/api/experiment/predict')
async def experiment_predict():
    try:
        return await experiment.predict()
    except predictions.JevNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post('/api/experiment/resolve')
async def experiment_resolve():
    await experiment.resolve()
    return await experiment_history()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/api/markets")
async def get_markets():
    result = await buda.fetch_clp_tickers()
    return JSONResponse(result, status_code=200 if result["markets"] else 502,
                        headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "public-read-only"}


@app.post("/api/predictions/preview/{market_id}")
async def preview_prediction(market_id: str, horizon: str = "1h"):
    """Pide la predicción a JEV pero NO la guarda. Usar /save para guardarla."""
    market_id = market_id.upper()
    if market_id not in buda.CLP_MARKETS:
        raise HTTPException(status_code=404, detail=f"Mercado no soportado: {market_id}")
    if horizon not in predictions.HORIZONS:
        raise HTTPException(status_code=400, detail=f"Horizonte inválido: {horizon}")
    try:
        return await predictions.preview_direction(market_id, horizon)
    except predictions.JevNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ts.TypeSafeAuthenticationError as exc:
        raise HTTPException(status_code=502, detail="TYPESAFE_API_KEY inválida") from exc
    except ts.TypeSafeRateLimitError as exc:
        raise HTTPException(status_code=429, detail="Límite de tasa de JEV alcanzado") from exc
    except ts.TypeSafeError as exc:
        raise HTTPException(status_code=502, detail=f"Error de JEV: {exc}") from exc


@app.post("/api/predictions/save")
async def save_prediction(payload: dict[str, Any] = Body(...)):
    """Guarda una predicción que ya se le mostró al usuario (resultado de
    /preview). A partir de acá queda en el historial y se verifica sola."""
    try:
        return await predictions.save_prediction(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/predictions/bulk/{horizon}")
async def create_bulk_predictions(horizon: str, markets: str | None = None):
    """horizon: "1h"/"4h"/"24h"/"7d" para un solo horizonte, o "all" para los
    4 juntos (sigue siendo 1 sola llamada a JEV, con más preguntas adentro).
    markets: opcional, lista separada por comas (ej. BTC-CLP,ETH-CLP) para
    predecir solo esos mercados en vez de los 7."""
    if horizon == "all":
        horizon_list = list(predictions.HORIZONS.keys())
    elif horizon in predictions.HORIZONS:
        horizon_list = [horizon]
    else:
        raise HTTPException(status_code=400, detail=f"Horizonte inválido: {horizon}")
    market_list = [m.strip().upper() for m in markets.split(",")] if markets else None
    if market_list:
        invalid = [m for m in market_list if m not in buda.CLP_MARKETS]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Mercados no soportados: {', '.join(invalid)}")
    try:
        return await predictions.predict_all_markets(horizon_list, market_list)
    except predictions.JevNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ts.TypeSafeAuthenticationError as exc:
        raise HTTPException(status_code=502, detail="TYPESAFE_API_KEY inválida") from exc
    except ts.TypeSafeRateLimitError as exc:
        raise HTTPException(status_code=429, detail="Límite de tasa de JEV alcanzado") from exc
    except ts.TypeSafeError as exc:
        raise HTTPException(status_code=502, detail=f"Error de JEV: {exc}") from exc


@app.get("/api/predictions")
async def get_predictions():
    """Alimenta SOLO la tabla "Predicción JEV" — lee predictions.json, que es
    puramente informativo. No toca tracking.json ni resuelve nada (eso es
    trabajo exclusivo de Seguimiento, en /api/predictions/history)."""
    return {
        "predictions": predictions.list_predictions(),
    }


@app.get("/api/predictions/history")
async def get_predictions_history():
    """Alimenta SOLO la tabla de Seguimiento (igual al CSV) y el resumen de
    tasa de acierto — lee tracking.json exclusivamente, sin relación con
    predictions.json ni con la tabla "Predicción JEV". No llama a JEV, solo
    verifica contra el precio real lo que ya venció."""
    resolved_now = await predictions.resolve_due_tracking()
    return {
        "history": predictions.list_history(),
        "summary": predictions.accuracy_summary(),
        "resolved_this_call": resolved_now,
    }


@app.delete("/api/predictions/tracking/{record_id}")
async def delete_tracking_record(record_id: int):
    """Borra una fila del historial de Seguimiento (tracking.json). No
    afecta a predictions.json ni a la tabla "Predicción JEV"."""
    deleted = await predictions.delete_record(record_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    return {"deleted": True}


@app.get("/api/predictions/export.csv")
async def export_predictions_csv():
    await predictions.resolve_due_tracking()
    csv_text = predictions.export_csv()
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=predictions.csv",
            "Cache-Control": "no-store",
        },
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get('/experiment')
async def experiment_panel():
    return FileResponse(STATIC_DIR / 'experiment.html')


@app.get('/legacy')
async def legacy():
    return FileResponse(STATIC_DIR / 'index.html')
