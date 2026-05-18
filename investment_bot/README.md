# Arquitecto Financiero Supremo

Sistema asíncrono de filtrado institucional para identificar oportunidades de
inversión basadas en **calidad fundamental innegociable** + **pánico técnico
verificable**. Local, auditable, sin servicios en la nube.

> *"Be fearful when others are greedy, and greedy when others are fearful."*
> — Buffett. El sistema solo se vuelve codicioso cuando los fundamentos son
> impecables y el mercado entra en pánico irracional al mismo tiempo.

---

## Filosofía de diseño

| Principio | Cómo se materializa en el código |
|---|---|
| **Cero falsos positivos > sensibilidad** | Las tres fases son ANDs estrictas. Un solo criterio incumplido descarta. |
| **Soberanía de datos** | SQLite local. Cero dependencia de servicios externos para histórico. |
| **Determinismo** | Configuración inmutable (`frozen=True`), funciones puras donde es posible, sin estado global compartido. |
| **Robustez ante red** | Backoff exponencial + jitter en cada llamada a yfinance. Un ticker que falla NO aborta el batch. |
| **Auditoría sin compromisos** | Cada decisión queda en `logs/arquitecto.jsonl` y en las tablas `*_history`. |

---

## Arquitectura

```
┌────────────────┐
│   main.py      │  Orquestador. Loop infinito con shutdown limpio (SIGINT/SIGTERM).
└────────┬───────┘
         │
    ┌────▼────────────────────────────────────────────┐
    │ data_fetcher.py                                 │
    │   AsyncDataFetcher (semáforo + tenacity backoff)│
    └────────────────────────┬────────────────────────┘
                             │  RawMarketData
                             ▼
    ┌────────────────────────────────────────────────────────────────┐
    │ PIPELINE  (3 fases en serie por ticker, en paralelo entre ellos)│
    │                                                                 │
    │  FASE 1: fundamental_filter.py                                  │
    │    • EPS trend  • Gross Margin >= 50%  • Buybacks               │
    │    • Dividendos sin recortes  • FCF > deuda CP                  │
    │                                                                 │
    │  FASE 2a: valuation_filter.py                                   │
    │    • PER actual vs mediana 5y  • descuento >= 20%               │
    │                                                                 │
    │  FASE 2b: technical_filter.py                                   │
    │    • Precio < MA200  • RSI < umbral                             │
    │    • Umbral endurecido si event_calendar.py indica catalizador  │
    │      activo (modula sensibilidad)                               │
    └────────────────────────┬────────────────────────────────────────┘
                             │  GoldenOpportunity
                             ▼
    ┌─────────────────────────────────────────┐
    │ alerts.py                               │
    │   AlertManager + Notifier (strategy)    │
    │   ▶ ConsoleNotifier (siempre activo)    │
    │   ▶ TelegramNotifier (aiohttp Bot API)  │
    │   ▶ EmailNotifier (smtplib en thread)   │
    └─────────────────────────────────────────┘

Capa transversal:
    • config.py       (pydantic-settings: validación estricta al arranque)
    • models.py       (dataclasses frozen: contratos tipados entre fases)
    • database.py     (aiosqlite: schema + repositorios + dedup)
    • logger_setup.py (JSON Lines estructurado)
```

### Mapeo prompt → módulo

| Requisito de la especificación | Implementación |
|---|---|
| Tendencia EPS positiva en 5y | `FundamentalFilter._trend_is_positive` (slope OLS + valor final > inicial) |
| Margen bruto >= 50% | `FundamentalFilter._compute_gross_margin` con 3 rutas de fallback |
| Recompras activas en 3y | `_extract_shares_series` + `_is_strictly_decreasing_overall` |
| Dividendos sin recortes | `_evaluate_dividends` con tolerancia 5% al ruido de calendario |
| FCF positivo que cubre deuda CP | `_extract_fcf_series` + `_fcf_covers_short_term_debt` |
| PER 20% bajo media histórica | `ValuationFilter._compute_historical_pe_mean` (mediana, robusta) |
| MA200 + RSI sobreventa | `TechnicalFilter` con RSI de Wilder verificado |
| Catalizadores temporales | `event_calendar.py` con ventana de sensibilidad por catalizador |
| Asyncio + aiohttp paralelo | `AsyncDataFetcher` con `Semaphore` + `asyncio.gather` |
| Backoff exponencial | `tenacity.AsyncRetrying` con `wait_exponential + wait_random` |
| SQLite local | `database.py` con WAL mode |
| Alerta Telegram/Email | `alerts.py` (Strategy pattern) |
| OOP + logging estructurado | Toda la base de código |

---

## Instalación

Requisitos: Python ≥ 3.10.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# editar .env con tu universo, umbrales y credenciales
```

---

## Configuración

Todo se configura por variables de entorno (o `.env`). Valores por defecto
seguros; revisar `.env.example` para la lista completa.

Parámetros clave:

| Variable | Por defecto | Significado |
|---|---|---|
| `TICKER_UNIVERSE` | — (vacío) | CSV de tickers, ej. `AAPL,MSFT,JNJ` |
| `MARGEN_BRUTO_MINIMO` | `0.50` | Umbral de margen bruto |
| `PER_DISCOUNT_MIN` | `0.20` | Descuento mínimo del PER vs histórico |
| `RSI_OVERSOLD_THRESHOLD` | `30.0` | RSI sobreventa estándar |
| `RSI_EXTREME_OVERSOLD` | `20.0` | RSI sobreventa cuando hay catalizador activo |
| `CONCURRENCY_LIMIT` | `5` | Llamadas paralelas a yfinance |
| `SCAN_INTERVAL_SECONDS` | `3600` | Período entre escaneos |

Pydantic valida cada campo al arrancar. Una config inválida ABORTA el proceso
antes de tocar la red. Esa es la intención.

---

## Ejecución

### Modo headless (solo scanner + alertas)

```bash
python main.py
```

Lo que verás:
* Logs JSON en stdout y en `logs/arquitecto.jsonl` (rotación 10 MB × 5).
* En cada ciclo: descarga del batch, evaluación por fases, persistencia de
  snapshots en SQLite, alertas si aplica.
* `Ctrl+C` o `SIGTERM` → shutdown limpio.

### Modo web (dashboard + API REST + scanner periódico)

```bash
python web_runner.py
```

Sirve en `http://127.0.0.1:8765/` por defecto:

| Ruta | Función |
|---|---|
| `/` | Dashboard editorial (Fraunces + JetBrains Mono, paleta tinta+oro) |
| `/docs` | Documentación interactiva OpenAPI (Swagger UI) |
| `/api/status` | Estado del scanner + configuración |
| `/api/opportunities` | Últimas alertas registradas |
| `/api/tickers` | Universo actual |
| `/api/tickers/{ticker}/history` | Historial de las 3 fases para un ticker |
| `/api/catalysts` (GET/POST) | Listar y añadir catalizadores |
| `/api/catalysts/{id}` (DELETE) | Eliminar catalizador |
| `/api/scan/trigger` (POST) | Disparar un escaneo manual |
| `/api/healthz` | Sonda de salud |

El loop periódico corre en background con el mismo intervalo que en modo
headless. La UI dispone de un botón "Disparar escaneo" para ejecutar bajo
demanda sin esperar al siguiente tick.

**Seguridad:** binding por defecto a `127.0.0.1` (solo localhost). Para
exponer a una red de confianza, sobreescribir `WEB_HOST=0.0.0.0` y `WEB_PORT`
en el entorno, y **montar autenticación delante** (nginx + basic auth,
Cloudflare Access, etc.). La API no incluye auth porque asume entorno
de un solo operador local.

### Verificación del entorno

Antes de la primera ejecución real:

```bash
python technical_indicators.py    # self-test del RSI Wilder
python smoke_test.py              # pipeline completo con datos sintéticos
```

---

## Catalizadores temporales (`event_driven_target`)

Cada catalizador asocia un ticker a una fecha. Cuando estamos dentro de la
ventana de sensibilidad (default ±30 días) el umbral de RSI se endurece
de `RSI_OVERSOLD_THRESHOLD` a `RSI_EXTREME_OVERSOLD` para ese ticker.

Por defecto se siembra el ejemplo de la especificación (19/11/2026). Para
añadir más, edita `event_calendar.DEFAULT_CATALYSTS` o inserta directo:

```python
from datetime import date
from database import Database, CatalystRepository
from models import Catalyst
import asyncio

async def add():
    db = Database(Path("./data/arquitecto.db"))
    await db.initialize()
    repo = CatalystRepository(db)
    await repo.upsert(Catalyst(
        ticker="AAPL",
        event_date=date(2026, 9, 10),
        description="Apple event esperado",
        sensitivity_window_days=21,
    ))

asyncio.run(add())
```

---

## Auditoría e investigación post-mortem

Toda decisión queda persistida. Para investigar por qué un ticker fue (o no)
una oportunidad:

```bash
sqlite3 data/arquitecto.db
.headers on
.mode column
SELECT * FROM fundamental_history WHERE ticker = 'AAPL' ORDER BY captured_at DESC LIMIT 5;
SELECT * FROM valuation_history   WHERE ticker = 'AAPL' ORDER BY captured_at DESC LIMIT 5;
SELECT * FROM technical_history   WHERE ticker = 'AAPL' ORDER BY captured_at DESC LIMIT 5;
SELECT * FROM alerts_log ORDER BY detected_at DESC LIMIT 10;
```

Para investigar logs (JSON Lines):

```bash
jq 'select(.ticker=="AAPL")' logs/arquitecto.jsonl
jq 'select(.level=="ERROR")' logs/arquitecto.jsonl
```

---

## Limitaciones conocidas y mitigaciones

| Limitación | Mitigación |
|---|---|
| `yfinance` puede cambiar nombres de filas entre versiones | `_safe_series_from_row` prueba múltiples candidatos por métrica |
| PER histórico es una aproximación (precio_eoy / EPS_anual) | Usamos mediana en lugar de media para resistir outliers |
| Dividendos pueden tener ajustes calendario (split, frecuencia) | Tolerancia del 5% en la detección de recortes |
| yfinance no es asíncrono nativo | Delegamos a `asyncio.to_thread`; el event loop NO se bloquea |
| Rate limits de Yahoo Finance | `Semaphore` + `tenacity` backoff exponencial con jitter |

---

## Estructura del proyecto

```
investment_bot/
├── main.py                  # orquestador headless
├── web_runner.py            # orquestador web (uvicorn + scanner + dashboard)
├── web_app.py               # FastAPI + endpoints + lifespan
├── scanner.py               # Scanner con lock para escaneos manuales/periódicos
├── config.py                # pydantic-settings, inmutable
├── logger_setup.py          # JSON Lines + rotación
├── models.py                # dataclasses frozen (contratos)
├── database.py              # aiosqlite + repositorios
├── data_fetcher.py          # AsyncDataFetcher
├── technical_indicators.py  # RSI Wilder + SMA puros
├── fundamental_filter.py    # FASE 1
├── valuation_filter.py      # FASE 2a (PER)
├── technical_filter.py      # FASE 2b (MA200 + RSI)
├── event_calendar.py        # catalizadores
├── alerts.py                # notifiers (Strategy)
├── smoke_test.py            # validación sin red
├── static/
│   └── index.html           # dashboard (Fraunces + JetBrains Mono)
├── requirements.txt
├── .env.example
└── data/   logs/            # creados en runtime
```

---

## Licencia y uso

Software experimental con fines educativos y de investigación cuantitativa.
**No constituye asesoramiento financiero.** Las decisiones de inversión son
responsabilidad exclusiva del operador. Verifica cada señal manualmente
antes de comprometer capital.
