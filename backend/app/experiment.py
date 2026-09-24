"""Experimento prospectivo. Eventos SQLite sin edición ni borrado por la app."""
import asyncio
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
import typesafe_sdk as ts
from . import candles, predictions

DB_PATH = Path(__file__).resolve().parent.parent / 'data' / 'experiment.sqlite3'
VERSION = 'btc-clp-next-15m-v1'
INSTRUCTIONS = ('Estima la dirección de la vela objetivo BTC-CLP de 15 minutos. '
    'Alcista significa cierre > apertura de ESA vela, bajista cierre < apertura, '
    'sin cambio cierre = apertura. Usa solo las velas cerradas del contexto. '
    'Devuelve probabilidades honestas, sin asumir certeza ni rentabilidad.')
CRITERIA = {'up': 'Cierre mayor que apertura', 'down': 'Cierre menor que apertura', 'flat': 'Cierre igual a apertura'}

@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS events (target INTEGER, kind TEXT, recorded REAL, payload TEXT, PRIMARY KEY(target, kind))')
        for operation in ('UPDATE', 'DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS no_{operation.lower()} BEFORE {operation} ON events BEGIN SELECT RAISE(ABORT, 'immutable event'); END")
        with conn:
            yield conn
    finally:
        conn.close()

def append(target, kind, payload, before=None):
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        recorded = time.time()
        if before is not None and recorded >= before:
            raise ValueError('La vela ya comenzó; predicción descartada')
        conn.execute('INSERT INTO events VALUES (?,?,?,?)',
            (target, kind, recorded, json.dumps(payload, allow_nan=False)))

def records():
    with connect() as conn:
        rows = conn.execute('SELECT target,kind,recorded,payload FROM events ORDER BY target DESC, recorded').fetchall()
    grouped = {}
    for target, kind, recorded, payload in rows:
        row = grouped.setdefault(target, {'target_start': target, 'target_end': target+900})
        row[kind] = json.loads(payload)
        row[kind+'_at'] = recorded
    return list(grouped.values())

async def ask(state):
    client = predictions._client(predictions._ensure_configured())
    try:
        response = await client.system_one(state=state, questions={
            'direction': ts.Choice(instructions=INSTRUCTIONS, criteria=CRITERIA)})
        answer = response.answers['direction']
        return {'model': response.model, 'probabilities': dict(answer.probabilities),
            'choice': answer.choice, 'confidence': answer.confidence}
    finally:
        await client.aclose()

def validate(answer):
    probabilities = answer['probabilities']
    if set(probabilities) != set(CRITERIA) or any(
        isinstance(p, bool) or not isinstance(p, (float, int)) or not math.isfinite(p) or not 0 <= p <= 1
        for p in probabilities.values()) or abs(sum(probabilities.values())-1) > .01:
        raise ValueError('Distribución de probabilidades inválida')
    if answer['choice'] not in CRITERIA or probabilities[answer['choice']] < max(probabilities.values()):
        raise ValueError('Elección inconsistente')
    if not math.isfinite(answer['confidence']) or not 0 <= answer['confidence'] <= 1:
        raise ValueError('Confianza inválida')

async def predict():
    predictions._ensure_configured()
    if len(records()) >= 500 or (DB_PATH.exists() and DB_PATH.stat().st_size >= 10*1024*1024):
        raise ValueError('Límite del experimento: 500 intentos o 10 MiB. Exporta y revisa el almacenamiento antes de continuar.')
    now = time.time()
    target = (int(now)//900 + 1)*900
    if target-now < 60:
        raise ValueError('Falta menos de un minuto: espera la siguiente ventana')
    try:
        append(target, 'attempt', {'version': VERSION, 'instructions': INSTRUCTIONS,
            'criteria': CRITERIA, 'market': 'BTC-CLP'}, before=target)
    except sqlite3.IntegrityError:
        return next(r for r in records() if r['target_start'] == target)
    try:
        # Cuatro velas completamente cerradas. Nunca usar la vela en curso.
        cutoff = target-900
        history = await asyncio.wait_for(candles.fetch_candles(cutoff-4*900, cutoff), timeout=min(90, target-time.time()))
        state = {'market': 'BTC-CLP', 'target_start_utc_epoch': target,
            'target_end_utc_epoch': target+900, 'closed_candles': history}
        append(target, 'context', state, before=target)
        if target-time.time() <= 0:
            raise ValueError('La vela ya comenzó')
        answer = await asyncio.wait_for(ask(state), timeout=min(45, target-time.time()))
        validate(answer)
        answer['baseline'] = history[-1]['direction']
        append(target, 'prediction', answer, before=target)
    except Exception as exc:
        append(target, 'error', {'reason': type(exc).__name__,
            'message': str(exc) if isinstance(exc, candles.IncompleteData) else
                'No se emitió predicción válida a tiempo; revisar datos/configuración'})
    return next(r for r in records() if r['target_start'] == target)

async def resolve():
    for row in records():
        target = row['target_start']
        if 'prediction' not in row or 'result' in row or time.time() < row['target_end']+10:
            continue
        try:
            candle = (await candles.fetch_candles(target, target+900))[0]
            answer = row['prediction']
            payload = {'status': 'resolved', 'candle': candle, 'actual': candle['direction'],
                'correct': answer['choice'] == candle['direction'],
                'baseline_correct': answer['baseline'] == candle['direction'],
                'brier': sum((p-int(k == candle['direction']))**2 for k,p in answer['probabilities'].items())}
        except Exception:
            # Reintentar hasta 24h. No evaluar usando el ticker actual.
            if time.time() < row['target_end']+86400:
                continue
            payload = {'status': 'not_evaluable', 'reason': 'Sin cobertura verificable tras 24h'}
        try:
            append(target, 'result', payload)
        except sqlite3.IntegrityError:
            pass  # Otro resolver ya insertó el resultado; nunca sobrescribir.

def summary(rows):
    resolved = [r for r in rows if r.get('result', {}).get('status') == 'resolved']
    total = len(resolved)
    bins = []
    for low, high in [(0,.5),(.5,.6),(.6,.7),(.7,.8),(.8,.9),(.9,1.01)]:
        selected = [r for r in resolved if low <= r['prediction']['probabilities'][r['prediction']['choice']] < high]
        if selected:
            bins.append({'from':low, 'to':min(high,1), 'count':len(selected),
                'mean_probability':sum(r['prediction']['probabilities'][r['prediction']['choice']] for r in selected)/len(selected),
                'observed_accuracy':sum(r['result']['correct'] for r in selected)/len(selected)})
    return {'attempts':len(rows), 'predictions':sum('prediction' in r for r in rows),
        'resolved':total, 'not_evaluable':sum(r.get('result',{}).get('status')=='not_evaluable' for r in rows),
        'accuracy':sum(r['result']['correct'] for r in resolved)/total if total else None,
        'baseline_accuracy':sum(r['result']['baseline_correct'] for r in resolved)/total if total else None,
        'brier':sum(r['result']['brier'] for r in resolved)/total if total else None,
        'calibration':bins}

async def worker():
    while True:
        try:
            await resolve()
        except Exception:
            import logging
            logging.getLogger(__name__).exception('No se pudo resolver el experimento')
        await asyncio.sleep(30)
