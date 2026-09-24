"""Velas UTC [inicio, fin), exclusivamente desde trades públicos paginados."""
from decimal import Decimal
from . import buda

INTERVAL = 900

class IncompleteData(ValueError):
    pass

def direction(opening, closing):
    a, b = Decimal(str(opening)), Decimal(str(closing))
    return 'up' if b > a else 'down' if b < a else 'flat'

async def fetch_candles(start: int, end: int, max_pages=100):
    cursor = end * 1000 - 1
    trades = []
    complete = False
    async with buda.get_http_client() as client:
        for _ in range(max_pages):
            response = await client.get('/markets/BTC-CLP/trades',
                params={'timestamp': cursor, 'limit': 100})
            response.raise_for_status()
            entries = response.json()['trades']['entries']
            if not entries:
                raise IncompleteData('No se pudo demostrar cobertura del intervalo')
            parsed = []
            for row in entries:
                ts = int(row[0])
                volume, price = Decimal(str(row[1])), Decimal(str(row[2]))
                if ts > cursor or not price.is_finite() or price <= 0 or not volume.is_finite() or volume < 0:
                    raise IncompleteData('Trade inválido o fuera de la página solicitada')
                parsed.append((ts, str(volume), str(price)))
            oldest = min(r[0] for r in parsed)
            if oldest < start * 1000:
                trades.extend(r for r in parsed if r[0] >= start * 1000)
                complete = True
                break
            # Releer el milisegundo de frontera completo en la página siguiente.
            if oldest >= cursor:
                raise IncompleteData('Paginación ambigua: demasiados trades en la frontera')
            trades.extend(r for r in parsed if r[0] > oldest)
            cursor = oldest
        if not complete:
            raise IncompleteData('Límite de páginas: intervalo incompleto')
    result = []
    for bucket in range(start, end, INTERVAL):
        rows = sorted((r for r in trades if bucket*1000 <= r[0] < (bucket+INTERVAL)*1000), key=lambda r: r[0])
        if not rows:
            raise IncompleteData('Vela sin transacciones; no se inventa apertura ni cierre')
        # La API REST no garantiza orden entre trades con igual milisegundo.
        for boundary in (rows[0][0], rows[-1][0]):
            if len({r[2] for r in rows if r[0] == boundary}) > 1:
                raise IncompleteData('Apertura/cierre ambiguo en el mismo milisegundo')
        prices = [Decimal(r[2]) for r in rows]
        result.append(dict(start=bucket, end=bucket+INTERVAL, open=rows[0][2],
            close=rows[-1][2], high=str(max(prices)), low=str(min(prices)),
            volume=str(sum(Decimal(r[1]) for r in rows)), trades=len(rows), evidence=rows,
            direction=direction(rows[0][2], rows[-1][2])))
    return result
