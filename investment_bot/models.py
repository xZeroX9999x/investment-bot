"""
models.py
=========
Modelos de dominio inmutables. Toda la comunicación entre fases del pipeline
viaja a través de estos contratos tipados. Las dataclasses son `frozen=True`
para evitar mutaciones laterales accidentales.

Diseño por capas:
    RawMarketData  -> datos crudos descargados
    FundamentalSnapshot -> instantánea fundamental procesada
    ValuationSnapshot -> instantánea de valoración
    TechnicalSnapshot -> instantánea técnica
    FilterResult -> resultado del paso por cada filtro
    GoldenOpportunity -> alerta final, lista para notificar
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import List, Optional

import pandas as pd


# =============================================================================
# Enumeraciones
# =============================================================================

class FilterStage(str, Enum):
    """Identifica en qué fase del pipeline ocurrió un evento."""
    FUNDAMENTAL = "FUNDAMENTAL"
    VALUATION = "VALUATION"
    TECHNICAL = "TECHNICAL"
    EVENT_DRIVEN = "EVENT_DRIVEN"


class FilterVerdict(str, Enum):
    """Veredicto binario explícito (evita usar bools sueltos en logs)."""
    PASS = "PASS"
    FAIL = "FAIL"


# =============================================================================
# Datos crudos
# =============================================================================

@dataclass(frozen=True)
class RawMarketData:
    """
    Contenedor crudo devuelto por la capa de adquisición.
    Mantiene los DataFrames originales sin transformar para que cada filtro
    decida cómo procesarlos. Pasarlo por valor evita acoplamiento.
    """
    ticker: str
    info: dict                              # yfinance .info (snapshot de fundamentales puntuales)
    history: pd.DataFrame                   # OHLCV diario (al menos `per_history_years` años)
    financials_annual: pd.DataFrame         # Income statement anual
    balance_sheet_annual: pd.DataFrame      # Balance anual
    cashflow_annual: pd.DataFrame           # Estado de flujos anual
    dividends: pd.Series                    # Serie de dividendos históricos
    fetched_at: datetime


# =============================================================================
# Snapshots por dimensión
# =============================================================================

@dataclass(frozen=True)
class FundamentalSnapshot:
    """Métricas fundamentales destiladas para decisión."""
    ticker: str
    eps_series: List[float]                 # EPS anual ordenado cronológicamente
    eps_dates: List[pd.Timestamp]           # Fechas paralelas (mismo orden e índice que eps_series)
    eps_trend_positive: bool                # Promedio últimos N años en pendiente +
    gross_margin: Optional[float]           # Margen bruto del último año
    gross_margin_passes: bool               # >= margen_bruto_minimo
    shares_outstanding_series: List[float]  # Acciones en circulación anuales
    shares_decreasing: bool                 # Buybacks activos en N años
    dividend_paid: bool
    dividend_no_recent_cuts: bool
    fcf_series: List[float]                 # Free cash flow anual
    fcf_consistently_positive: bool
    fcf_covers_short_term_debt: bool


@dataclass(frozen=True)
class ValuationSnapshot:
    """Métricas de valoración fundamental (PER vs histórico)."""
    ticker: str
    current_pe: Optional[float]
    historical_pe_mean: Optional[float]
    pe_discount: Optional[float]            # (mean - current) / mean  ->  positivo = descuento
    pe_discount_passes: bool                # >= per_discount_min


@dataclass(frozen=True)
class TechnicalSnapshot:
    """Indicadores técnicos para detectar pánico irracional."""
    ticker: str
    last_close: float
    ma200: Optional[float]
    below_ma200: bool
    rsi: Optional[float]
    rsi_oversold: bool                      # Considera ajuste por catalizador cercano


# =============================================================================
# Catalizadores temporales
# =============================================================================

@dataclass(frozen=True)
class Catalyst:
    """
    Catalizador temporal asociado a un ticker. Cuando la fecha está cerca,
    el sistema endurece los umbrales (exige sobreventa más profunda) para
    evitar falsos positivos asociados al ruido pre-evento.
    """
    ticker: str
    event_date: date
    description: str
    sensitivity_window_days: int = 30       # Días antes/después en que el catalizador modula


# =============================================================================
# Resultado por filtro
# =============================================================================

@dataclass(frozen=True)
class FilterResult:
    """Salida estructurada de cada filtro."""
    ticker: str
    stage: FilterStage
    verdict: FilterVerdict
    reasons: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.verdict == FilterVerdict.PASS


# =============================================================================
# Alerta final
# =============================================================================

@dataclass(frozen=True)
class GoldenOpportunity:
    """
    Estructura terminal: lo que se notifica al humano cuando una empresa
    atraviesa las tres fases. Pensado como contrato de cara al canal de alerta.
    """
    ticker: str
    detected_at: datetime
    gross_margin: float
    pe_discount: float
    rsi: float
    last_close: float
    ma200: float
    below_ma200: bool
    active_catalysts: List[Catalyst] = field(default_factory=list)

    def to_alert_text(self) -> str:
        """Renderiza un mensaje conciso de alerta multiplataforma."""
        catalyst_block = ""
        if self.active_catalysts:
            catalyst_lines = [
                f"   • {c.event_date.isoformat()} — {c.description}"
                for c in self.active_catalysts
            ]
            catalyst_block = "\n\nCatalizadores activos:\n" + "\n".join(catalyst_lines)

        return (
            f"🎯 OPORTUNIDAD DE ORO: {self.ticker}\n"
            f"Detectado: {self.detected_at.isoformat(timespec='seconds')}\n"
            f"\n"
            f"• Margen Bruto:     {self.gross_margin:.1%}\n"
            f"• Descuento PER:    {self.pe_discount:.1%}\n"
            f"• RSI actual:       {self.rsi:.2f}\n"
            f"• Precio último:    {self.last_close:.2f}\n"
            f"• MA200:            {self.ma200:.2f}\n"
            f"• ¿Bajo MA200?:     {'Sí' if self.below_ma200 else 'No'}"
            f"{catalyst_block}"
        )
