"""
fundamental_filter.py
=====================
FASE 1 — El Filtro Fundamental.

Implementa los cinco contratos fundamentales (Buffett + Read):
    1. Crecimiento Sostenido: tendencia EPS positiva en N años.
    2. Calidad: margen bruto >= umbral configurado (default 50%).
    3. Trato al accionista: recompras activas + dividendos sin recortes.
    4. Deuda controlada: FCF positivo que cubre obligaciones a corto plazo.

Cada condición se evalúa de manera independiente. El veredicto final es PASS
si y solo si TODAS las condiciones son verdaderas. Las razones de fallo se
acumulan para auditoría (no se hace short-circuit).
"""

from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np
import pandas as pd

from models import FilterResult, FilterStage, FilterVerdict, FundamentalSnapshot, RawMarketData

log = logging.getLogger(__name__)


# =============================================================================
# Helpers numéricos
# =============================================================================

def _safe_series_from_row(
    df: pd.DataFrame,
    row_candidates: List[str],
) -> Optional[pd.Series]:
    """
    Extrae una fila de un DataFrame de yfinance probando varios nombres
    posibles (yfinance ha cambiado etiquetas entre versiones).
    Devuelve la serie ordenada CRONOLÓGICAMENTE (vieja -> reciente).
    """
    if df is None or df.empty:
        return None
    for name in row_candidates:
        if name in df.index:
            # yfinance entrega columnas en orden reciente -> viejo; invertimos.
            series = df.loc[name].dropna().iloc[::-1]
            if series.empty:
                continue
            return series
    return None


def _trend_is_positive(values: List[float]) -> bool:
    """
    Determina si una secuencia tiene tendencia ascendente.
    Criterio robusto: pendiente positiva de la regresión lineal **y**
    el valor final supera al inicial. Evita falsos positivos por ruido.
    """
    if len(values) < 2:
        return False
    arr = np.asarray(values, dtype=float)
    x = np.arange(len(arr), dtype=float)
    # Mínimos cuadrados manual (evitamos scipy)
    slope = np.polyfit(x, arr, 1)[0]
    return bool(slope > 0 and arr[-1] > arr[0])


def _is_strictly_decreasing_overall(values: List[float]) -> bool:
    """
    Comprueba si el valor final es estrictamente menor que el inicial,
    admitiendo fluctuaciones intermedias (toleramos un año con recompra
    nula). Más realista que exigir monotonía estricta.
    """
    if len(values) < 2:
        return False
    return values[-1] < values[0]


# =============================================================================
# Filtro
# =============================================================================

class FundamentalFilter:
    """
    Aplica los criterios de Fase 1 sobre un `RawMarketData`.
    El método público `evaluate` devuelve un par (FilterResult, FundamentalSnapshot).
    """

    def __init__(
        self,
        gross_margin_min: float,
        eps_years_lookback: int,
        shares_years_lookback: int,
    ) -> None:
        self._gross_margin_min = gross_margin_min
        self._eps_years_lookback = eps_years_lookback
        self._shares_years_lookback = shares_years_lookback

    # -------------------------------------------------------------------------
    # Entrada principal
    # -------------------------------------------------------------------------

    def evaluate(self, raw: RawMarketData) -> tuple[FilterResult, FundamentalSnapshot]:
        reasons: List[str] = []

        # 1. EPS Series + trend
        eps_dates, eps_series = self._extract_eps_series(raw)
        eps_trend_positive = (
            len(eps_series) >= 2 and _trend_is_positive(eps_series[-self._eps_years_lookback:])
        )
        if not eps_trend_positive:
            reasons.append(
                f"EPS no muestra tendencia positiva en {self._eps_years_lookback}y "
                f"(serie={eps_series})"
            )

        # 2. Gross margin
        gross_margin = self._compute_gross_margin(raw)
        gross_margin_passes = (
            gross_margin is not None and gross_margin >= self._gross_margin_min
        )
        if not gross_margin_passes:
            reasons.append(
                f"Margen bruto {gross_margin!r} < umbral {self._gross_margin_min:.0%}"
            )

        # 3. Shares outstanding decreasing
        shares_series = self._extract_shares_series(raw)
        shares_decreasing = (
            len(shares_series) >= 2
            and _is_strictly_decreasing_overall(shares_series[-self._shares_years_lookback:])
        )
        if not shares_decreasing:
            reasons.append(
                f"Acciones en circulación no decrecen en {self._shares_years_lookback}y "
                f"(serie={shares_series})"
            )

        # 4. Dividendos pagados + sin recortes recientes
        dividend_paid, dividend_no_cuts = self._evaluate_dividends(raw)
        if not dividend_paid:
            reasons.append("Empresa sin historial de dividendos")
        elif not dividend_no_cuts:
            reasons.append("Recorte de dividendos detectado en últimos 3 años")

        # 5. Free Cash Flow
        fcf_series = self._extract_fcf_series(raw)
        fcf_consistently_positive = (
            len(fcf_series) >= 2 and all(v > 0 for v in fcf_series[-self._eps_years_lookback:])
        )
        if not fcf_consistently_positive:
            reasons.append(f"FCF no consistentemente positivo (serie={fcf_series})")

        fcf_covers_debt = self._fcf_covers_short_term_debt(raw, fcf_series)
        if not fcf_covers_debt:
            reasons.append("FCF no cubre obligaciones a corto plazo")

        snapshot = FundamentalSnapshot(
            ticker=raw.ticker,
            eps_series=eps_series,
            eps_dates=eps_dates,
            eps_trend_positive=eps_trend_positive,
            gross_margin=gross_margin,
            gross_margin_passes=gross_margin_passes,
            shares_outstanding_series=shares_series,
            shares_decreasing=shares_decreasing,
            dividend_paid=dividend_paid,
            dividend_no_recent_cuts=dividend_no_cuts,
            fcf_series=fcf_series,
            fcf_consistently_positive=fcf_consistently_positive,
            fcf_covers_short_term_debt=fcf_covers_debt,
        )

        verdict = (
            FilterVerdict.PASS if not reasons else FilterVerdict.FAIL
        )
        result = FilterResult(
            ticker=raw.ticker,
            stage=FilterStage.FUNDAMENTAL,
            verdict=verdict,
            reasons=reasons,
        )

        log.info(
            "Fundamental evaluation completed",
            extra={
                "ticker": raw.ticker,
                "verdict": verdict.value,
                "reason_count": len(reasons),
            },
        )
        return result, snapshot

    # -------------------------------------------------------------------------
    # Extractores
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_eps_series(raw: RawMarketData) -> tuple[List[pd.Timestamp], List[float]]:
        """
        EPS no aparece directamente como fila en `financials`. Lo derivamos:
            EPS = Net Income / Diluted Average Shares.
        Si no hay shares en financials, caemos al `info["sharesOutstanding"]`
        como aproximación (pierde calidad, pero permite continuar).

        Devuelve (dates, values) ESTRICTAMENTE alineadas: misma longitud,
        mismo índice. Esto es crítico para que `valuation_filter` pueda
        cruzar EPS históricos con precios sin riesgo de desfase.
        """
        empty: tuple[List[pd.Timestamp], List[float]] = ([], [])

        net_income = _safe_series_from_row(
            raw.financials_annual,
            ["Net Income", "NetIncome", "Net Income Common Stockholders"],
        )
        if net_income is None:
            return empty

        diluted_shares = _safe_series_from_row(
            raw.financials_annual,
            ["Diluted Average Shares", "Basic Average Shares"],
        )

        dates: List[pd.Timestamp] = []
        values: List[float] = []

        if diluted_shares is None:
            # Fallback: snapshot puntual (peor calidad histórica)
            shares_now = raw.info.get("sharesOutstanding")
            if not shares_now or shares_now <= 0:
                return empty
            for ts, ni in net_income.items():
                dates.append(pd.Timestamp(ts))
                values.append(float(ni) / float(shares_now))
            return dates, values

        # Alineamos por índice (fechas comunes). El orden cronológico se
        # preserva porque `_safe_series_from_row` ya devuelve la serie
        # invertida (oldest -> newest), y trabajamos sobre `net_income`.
        for ts in net_income.index:
            if ts not in diluted_shares.index:
                continue
            ni = float(net_income.loc[ts])
            sh = float(diluted_shares.loc[ts])
            if sh <= 0:
                continue
            dates.append(pd.Timestamp(ts))
            values.append(ni / sh)
        return dates, values

    @staticmethod
    def _compute_gross_margin(raw: RawMarketData) -> Optional[float]:
        """
        Gross Margin = (Revenue - COGS) / Revenue, último año disponible.
        yfinance puede no exponer COGS directamente; intentamos varias rutas:
          1. (Total Revenue - Cost Of Revenue) / Total Revenue
          2. Gross Profit / Total Revenue
          3. info["grossMargins"] como último recurso (snapshot puntual)
        """
        revenue = _safe_series_from_row(
            raw.financials_annual,
            ["Total Revenue", "TotalRevenue", "Revenue"],
        )
        if revenue is None or revenue.empty:
            gm_info = raw.info.get("grossMargins")
            return float(gm_info) if gm_info is not None else None

        last_revenue = float(revenue.iloc[-1])
        if last_revenue <= 0:
            return None

        cogs = _safe_series_from_row(
            raw.financials_annual,
            ["Cost Of Revenue", "CostOfRevenue", "Cost of Revenue"],
        )
        if cogs is not None and not cogs.empty:
            last_cogs = float(cogs.iloc[-1])
            return (last_revenue - last_cogs) / last_revenue

        gross_profit = _safe_series_from_row(
            raw.financials_annual,
            ["Gross Profit", "GrossProfit"],
        )
        if gross_profit is not None and not gross_profit.empty:
            return float(gross_profit.iloc[-1]) / last_revenue

        gm_info = raw.info.get("grossMargins")
        return float(gm_info) if gm_info is not None else None

    @staticmethod
    def _extract_shares_series(raw: RawMarketData) -> List[float]:
        """Histórico anual de acciones diluidas en circulación."""
        series = _safe_series_from_row(
            raw.financials_annual,
            ["Diluted Average Shares", "Basic Average Shares"],
        )
        if series is None:
            # Algunos tickers exponen shares en balance_sheet
            series = _safe_series_from_row(
                raw.balance_sheet_annual,
                ["Share Issued", "Ordinary Shares Number"],
            )
        if series is None:
            return []
        return [float(v) for v in series.values]

    @staticmethod
    def _evaluate_dividends(raw: RawMarketData) -> tuple[bool, bool]:
        """
        Devuelve (pagó_dividendos, sin_recortes_recientes).
        Política conservadora: si la empresa NUNCA pagó dividendos,
        `dividend_paid=False` y descartamos.
        Si pagó pero recortó en últimos 3 años, `no_cuts=False`.
        """
        dividends = raw.dividends
        if dividends is None or dividends.empty:
            return (False, False)

        # Sumamos por año fiscal
        try:
            annual = dividends.groupby(dividends.index.year).sum()
        except Exception:
            log.warning(
                "Could not group dividends by year",
                extra={"ticker": raw.ticker},
            )
            return (True, False)

        if len(annual) < 2:
            # Empresa muy joven en dividendos; aceptamos pero sin garantía
            return (True, True)

        # Verificamos los últimos 4 años (3 comparaciones) para detectar recortes.
        # `diff[i]` representa el cambio desde `recent[i-1]` (año previo) a
        # `recent[i]` (año actual). El % de recorte debe medirse contra el
        # año PREVIO (denominador correcto), no contra el año actual.
        recent = annual.iloc[-4:]
        has_significant_cut = False
        # Tolerancia: un recorte < 5% se considera ruido (ajuste de calendario)
        threshold_pct = 0.05
        for i in range(1, len(recent)):
            previous = float(recent.iloc[i - 1])
            current = float(recent.iloc[i])
            if previous <= 0:
                continue  # base no comparable
            delta = current - previous
            if delta < 0 and abs(delta) / previous > threshold_pct:
                has_significant_cut = True
                break
        return (True, not has_significant_cut)

    @staticmethod
    def _extract_fcf_series(raw: RawMarketData) -> List[float]:
        """
        Free Cash Flow = Operating Cash Flow - Capital Expenditure.
        yfinance ofrece directamente "Free Cash Flow" en versiones recientes.
        """
        fcf = _safe_series_from_row(
            raw.cashflow_annual,
            ["Free Cash Flow", "FreeCashFlow"],
        )
        if fcf is not None and not fcf.empty:
            return [float(v) for v in fcf.values]

        # Cálculo manual
        ocf = _safe_series_from_row(
            raw.cashflow_annual,
            ["Operating Cash Flow", "Total Cash From Operating Activities"],
        )
        capex = _safe_series_from_row(
            raw.cashflow_annual,
            ["Capital Expenditure", "Capital Expenditures"],
        )
        if ocf is None or capex is None:
            return []

        common = ocf.index.intersection(capex.index)
        if len(common) == 0:
            return []
        # CapEx en yfinance suele venir como NEGATIVO; sumamos.
        return [float(ocf.loc[ts]) + float(capex.loc[ts]) for ts in common]

    @staticmethod
    def _fcf_covers_short_term_debt(
        raw: RawMarketData,
        fcf_series: List[float],
    ) -> bool:
        """
        El último FCF anual debe ser >= deuda a corto plazo (Current Debt
        o Short Long Term Debt). Si no hay deuda a corto, la condición se
        cumple trivialmente.
        """
        if not fcf_series:
            return False
        last_fcf = fcf_series[-1]
        if last_fcf <= 0:
            return False

        short_term_debt = _safe_series_from_row(
            raw.balance_sheet_annual,
            [
                "Current Debt",
                "Short Long Term Debt",
                "Short Term Debt",
                "CurrentDebt",
            ],
        )
        if short_term_debt is None or short_term_debt.empty:
            # Sin deuda a corto identificable -> condición se cumple
            return True
        last_debt = float(short_term_debt.iloc[-1])
        if last_debt <= 0:
            return True
        return last_fcf >= last_debt
