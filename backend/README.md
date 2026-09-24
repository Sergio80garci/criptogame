# Backend activo: Crypto Quest

## Fiabilidad y almacenamiento limitado

El panel principal conserva el guardado manual. No registra todas las consultas:
`predictions.json` mantiene la última predicción por mercado/horizonte (28
combinaciones). `tracking.json` admite hasta 1000 filas o 5 MiB; al alcanzar el
límite rechaza nuevos guardados sin borrar ni archivar datos automáticamente.
Guardar dos veces la misma predicción no agrega duplicados; no se admiten
nuevos guardados de predicciones vencidas. El experimento opcional de velas
admite 500 intentos o 10 MiB antes de detener nuevos intentos (los eventos de
resolución de pendientes aún pueden incrementar ese tamaño).

Las nuevas predicciones guardan un contexto compacto versionado: variaciones,
volumen, spread, fuerza relativa a BTC y resumen de hasta 100 trades públicos
(retorno de la muestra, VWAP, dispersión de retornos y duración real). Los trades
crudos de este estudio no se guardan. No son velas ni una serie uniforme; el
estudio no demuestra poder predictivo. Se consulta solo al pedir predicciones.

La fiabilidad agrupa por mercado, horizonte, modelo y versión del contexto.
Excluye registros antiguos sin metodología/contexto verificables, guardados
tarde y ventanas solapadas. Muestra aciertos, intervalo Wilson descriptivo 95 %,
Brier binario (sube / no sube), calibración por rangos y referencia de tendencia
1h. 30 casos es un mínimo exploratorio, nunca un certificado de fiabilidad.
Los resultados se etiquetan siempre como muestra manual seleccionada. La
dependencia temporal restante y la selección limitan la interpretación del
intervalo. No se publican como tasa general ni como certeza de la próxima vela.
Faltantes, contradicciones e incertidumbre aparecen como motivos para abstenerse.
Los datos de contexto y las métricas están en «Magnitud y evidencia» del panel.

## Experimento actual: próxima vela BTC/CLP de 15 minutos

El inicio `/` mantiene el panel de siete mercados, predicciones por horizonte,
guardado, seguimiento y CSV. Una sección desplegable incorpora el experimento
de próxima vela, también accesible en `/experiment`. `/legacy` es un alias del
panel principal. Las estadísticas de velas y horizontes se mantienen separadas.

- `POST /api/experiment/predict`: una consulta explícita a Jev, con guardado
  automático; requiere `TYPESAFE_API_KEY` en el entorno o `.env` del backend.
  No hay llamadas automáticas de pago ni operaciones en una cuenta de Buda.
- Una oportunidad por vela UTC, sin repetir para elegir una respuesta favorable.
  En el último minuto antes de la apertura se rechazan nuevos intentos.
- Contexto: cuatro velas cerradas; la vela actual incompleta queda fuera.
- Se guardan instrucciones, versión, contexto y trades, distribución completa,
  modelo y horas de registro. Respuestas tardías quedan como intentos fallidos.
- SQLite en `data/experiment.sqlite3` guarda eventos de solo inserción con
  restricciones que impiden editar/borrar mediante SQL ordinario. Esto no es una
  prueba criptográfica contra alguien con control del archivo o del reloj local.
- Al cerrar la vela, el backend comprueba el intervalo histórico exacto cada
  30 segundos, incluso sin navegador. Al reiniciar recupera pendientes. No usa
  precios actuales para evaluar velas vencidas. Tras 24 h sin cobertura
  verificable, marca no evaluable; nunca lo cuenta como acierto ni fallo.
- Velas [inicio, fin): primera/última transacción como apertura/cierre. Empates
  temporales ambiguos, intervalos vacíos o paginación incompleta no se inventan.
- Métricas: aciertos, referencia de dirección de última vela cerrada, Brier
  multiclase (0–2, menor mejor), calibración por rangos y número de evaluaciones.
  Las probabilidades son experimentales: no se garantiza 100 % de aciertos.
- `GET /api/experiment` exporta toda la auditoría JSON.

Los apartados de tickers siguientes describen el panel principal. Su simulación
y juego siguen pendientes. Ejecutar las pruebas no llama a Buda ni a Jev.

FastAPI sirve el panel HTML/JavaScript y consulta exclusivamente tickers públicos
de Buda. No necesita claves ni ejecuta operaciones. La plantilla Next.js en la
raíz es un prototipo previo y no se requiere para este panel.

## Iniciar desde backend (PowerShell, Python 3.11+)

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Panel: http://127.0.0.1:8000 · API interactiva: http://127.0.0.1:8000/docs

`GET /health` verifica el proceso local, no la conexión a Buda.
`GET /api/markets` entrega markets, errors, status, source y fetched_at.
Responde 200 si al menos un mercado es válido; 502 si todos fallan.
Cada error se identifica por mercado. 404 significa no disponible en la consulta.

La hora UTC indica consulta, no última transacción. El panel actualiza cada 15
segundos tras finalizar la solicitud previa; advierte fallas y datos antiguos.
Las variaciones son fracciones, multiplicadas por 100 en pantalla.
Los floats son para visualización; la futura contabilidad debe usar Decimal.
No hay caché compartida: uso personal. Velas, simulación y Jev siguen pendientes.

## Pruebas sin conexión a Buda

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Cubren errores parciales, timeout, validación de moneda/números y rutas locales.
No demuestran disponibilidad real de mercados.

Si existe `certs/corporate-ca-bundle.pem`, se usa para validar TLS; en otras redes
se utiliza la validación predeterminada de httpx. No desactivar TLS.
Referencia de Buda: https://api.buda.com/
