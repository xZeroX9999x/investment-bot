"""
smoke_test.py
=============
Test de humo del sistema completo SIN red. Valida que:
  1. La configuración se carga y valida con Pydantic.
  2. El schema SQLite se crea correctamente y es idempotente.
  3. Los repositorios persisten y leen sin error.
  4. Los filtros procesan datos sintéticos sin lanzar excepciones.
  5. El pipeline completo aplica las 3 fases sobre un RawMarketData fake.
  6. Las notificaciones llegan al ConsoleNotifier.

No se conecta a yfinance ni a Telegram/SMTP.
Ejecutar:  python smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Preparamos un .env mínimo en memoria ANTES de importar config
os.environ.setdefault("TICKER_UNIVERSE", "TEST")
os.environ.setdefault("DATABASE_PATH", str(Path(tempfile.gettempdir()) / "smoke.db"))
os.environ.setdefault("LOG_PATH", str(Path(tempfile.gettempdir()) / "smoke.jsonl"))
os.environ.setdefault("LOG_LEVEL", "WARNING")

from alerts import AlertManager, ConsoleNotifier
from config import get_settings
from database import (
    AlertRepository,
    CatalystRepository,
    Database,
    HistoryRepository,
)
from event_calendar import EventCalendar
from fundamental_filter import FundamentalFilter
from logger_setup import setup_logging
from main import Pipeline
from models import Catalyst, RawMarketData
from technical_filter import TechnicalFilter
from valuation_filter import ValuationFilter


# =============================================================================
# Generador de datos sintéticos
# =============================================================================

def make_fake_raw(ticker: str = "TEST", oversold: bool = True) -> RawMarketData:
    """
    Construye un RawMarketData que DEBE pasar las tres fases si oversold=True.
    Crecimiento EPS positivo, margen alto, buybacks, dividendos crecientes,
    FCF positivo, PER barato, precio bajo MA200 y RSI < 30.
    """
    # --- Precios: ~5.5 años de histórico (cubre el lookback fiscal de 5 años) ---
    n = 1900
    dates_idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="D")
    base = np.linspace(80, 200, n - 30)
    if oversold:
        # Caída brusca al final para forzar RSI bajo y precio < MA200
        crash = np.linspace(200, 95, 30)
        prices = np.concatenate([base, crash])
    else:
        # Mantener subida saludable
        cont = np.linspace(200, 220, 30)
        prices = np.concatenate([base, cont])

    history = pd.DataFrame(
        {
            "Open":   prices,
            "High":   prices * 1.01,
            "Low":    prices * 0.99,
            "Close":  prices,
            "Volume": np.full(n, 1_000_000),
        },
        index=dates_idx,
    )

    # --- Financials anuales (5 años) ordenados reciente -> viejo (estilo yfinance) ---
    # Las fechas fiscales se generan relativas a HOY para que caigan SIEMPRE
    # dentro del histórico de precios (independiente de cuándo se ejecute).
    today_ts = pd.Timestamp.today().normalize()
    years = pd.DatetimeIndex([today_ts - pd.DateOffset(years=k) for k in range(5)])
    financials = pd.DataFrame(
        {
            years[0]: [120e9, 50e9, 70e9, 25e9, 9.5e9],  # más reciente
            years[1]: [110e9, 47e9, 63e9, 23e9, 9.7e9],
            years[2]: [100e9, 45e9, 55e9, 20e9, 9.9e9],
            years[3]: [ 90e9, 42e9, 48e9, 18e9, 10.1e9],
            years[4]: [ 80e9, 40e9, 40e9, 15e9, 10.3e9],
        },
        index=[
            "Total Revenue",
            "Cost Of Revenue",
            "Gross Profit",
            "Net Income",
            "Diluted Average Shares",   # decreciendo en el tiempo (buybacks)
        ],
    )

    cashflow = pd.DataFrame(
        {
            years[0]: [ 30e9, -5e9, 25e9],
            years[1]: [ 28e9, -5e9, 23e9],
            years[2]: [ 25e9, -5e9, 20e9],
            years[3]: [ 22e9, -4e9, 18e9],
            years[4]: [ 20e9, -4e9, 16e9],
        },
        index=["Operating Cash Flow", "Capital Expenditure", "Free Cash Flow"],
    )

    balance = pd.DataFrame(
        {
            years[0]: [3e9],
            years[1]: [3e9],
            years[2]: [3e9],
            years[3]: [3e9],
            years[4]: [3e9],
        },
        index=["Current Debt"],
    )

    # --- Dividendos crecientes ---
    div_dates = pd.date_range("2020-01-01", periods=24, freq="QS")
    div_values = np.linspace(0.20, 0.80, len(div_dates))
    dividends = pd.Series(div_values, index=div_dates)

    # --- info: trailingPE bajo para forzar descuento ---
    info = {
        "trailingPE": 12.0,        # PER actual bajo
        "sharesOutstanding": 9.5e9,
        "grossMargins": 0.58,
    }

    return RawMarketData(
        ticker=ticker,
        info=info,
        history=history,
        financials_annual=financials,
        balance_sheet_annual=balance,
        cashflow_annual=cashflow,
        dividends=dividends,
        fetched_at=datetime.now(tz=timezone.utc),
    )


# =============================================================================
# Test
# =============================================================================

async def run_smoke() -> None:
    # 1. Config carga sin error
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_path)
    print(f"[OK] Settings loaded; universe={settings.ticker_universe}")

    # 2. Base de datos: limpia y crea schema
    db_path = settings.database_path
    if db_path.exists():
        db_path.unlink()
    db = Database(db_path)
    await db.initialize()
    # Reaplicar es idempotente
    await db.initialize()
    print(f"[OK] DB schema created at {db_path}")

    history_repo = HistoryRepository(db)
    alert_repo = AlertRepository(db)
    catalyst_repo = CatalystRepository(db)

    # 3. Catalizadores
    calendar = EventCalendar(catalyst_repo)
    await catalyst_repo.upsert(
        Catalyst(
            ticker="TEST",
            event_date=date(2026, 11, 19),
            description="Smoke test catalyst",
            sensitivity_window_days=30,
        )
    )
    cats = await calendar.catalysts_for("TEST")
    assert len(cats) == 1, f"Expected 1 catalyst, got {len(cats)}"
    print(f"[OK] Catalyst stored and retrieved: {cats[0].description}")

    # 4. Filtros sobre datos sintéticos
    fundamental = FundamentalFilter(
        gross_margin_min=settings.margen_bruto_minimo,
        eps_years_lookback=settings.eps_years_lookback,
        shares_years_lookback=settings.shares_years_lookback,
    )
    valuation = ValuationFilter(
        discount_min=settings.per_discount_min,
        history_years=settings.per_history_years,
    )
    technical = TechnicalFilter(
        ma_period=settings.ma_period,
        rsi_period=settings.rsi_period,
        rsi_oversold_threshold=settings.rsi_oversold_threshold,
        rsi_extreme_oversold=settings.rsi_extreme_oversold,
    )

    raw_good = make_fake_raw("TEST", oversold=True)

    # Fase 1
    fund_result, fund_snap = fundamental.evaluate(raw_good)
    print(f"[OK] Fundamental verdict={fund_result.verdict.value} "
          f"margin={fund_snap.gross_margin:.2%} "
          f"eps_dates_aligned={len(fund_snap.eps_dates) == len(fund_snap.eps_series)}")
    assert fund_result.passed, f"Expected PASS, got reasons={fund_result.reasons}"
    assert len(fund_snap.eps_dates) == len(fund_snap.eps_series), \
        "INVARIANT BROKEN: eps_dates and eps_series misaligned"

    # Fase 2a
    val_result, val_snap = valuation.evaluate(
        raw_good, fund_snap.eps_dates, fund_snap.eps_series
    )
    print(f"[OK] Valuation verdict={val_result.verdict.value} "
          f"current_pe={val_snap.current_pe:.2f} "
          f"historical_pe={val_snap.historical_pe_mean:.2f} "
          f"discount={val_snap.pe_discount:.2%}")
    assert val_result.passed, f"Expected PASS, got reasons={val_result.reasons}"

    # Fase 2b
    tech_result, tech_snap = technical.evaluate(
        raw_good, await calendar.catalysts_for("TEST"), date.today()
    )
    print(f"[OK] Technical verdict={tech_result.verdict.value} "
          f"close={tech_snap.last_close:.2f} ma200={tech_snap.ma200:.2f} "
          f"rsi={tech_snap.rsi:.2f}")
    assert tech_result.passed, f"Expected PASS, got reasons={tech_result.reasons}"

    # 5. Pipeline completo + dedup + alertas
    pipeline = Pipeline(fundamental, valuation, technical, history_repo, calendar)
    manager = AlertManager([ConsoleNotifier()])

    opp = await pipeline.process(raw_good)
    assert opp is not None, "Pipeline should have produced an opportunity"
    print(f"[OK] Pipeline produced opportunity for {opp.ticker}")

    # No alertado todavía hoy
    assert await alert_repo.already_alerted_today("TEST") is False
    await manager.dispatch(opp)
    await alert_repo.record(opp)
    # Ahora sí
    assert await alert_repo.already_alerted_today("TEST") is True
    print("[OK] Dedup logic works (alerted -> blocks re-alert same day)")

    # 6. Caso negativo: precio en máximos no debe alertar
    raw_bad = make_fake_raw("TEST", oversold=False)
    opp_bad = await pipeline.process(raw_bad)
    assert opp_bad is None, "Bullish scenario should NOT produce opportunity"
    print("[OK] Bullish scenario correctly rejected")

    # Cleanup
    if db_path.exists():
        db_path.unlink()

    print("\n" + "=" * 50)
    print("ALL SMOKE TESTS PASSED ✓")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(run_smoke())
