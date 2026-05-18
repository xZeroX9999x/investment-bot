"""
event_calendar.py
=================
Módulo de catalizadores temporales (`event_driven_target`).

Permite asociar fechas relevantes a tickers (ej. presentaciones de producto,
reportes regulatorios, expiraciones de patentes, etc.). El filtro técnico
consulta este calendario y modula sus umbrales cuando hay un catalizador
activo en la ventana de sensibilidad.

El propio módulo NO decide políticas; expone una API limpia y persistencia
delegada en `CatalystRepository`. El catálogo viene "sembrado" con un caso
de ejemplo (19 de noviembre de 2026) pero todos los registros pueden
añadirse/eliminarse en runtime.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List

from database import CatalystRepository
from models import Catalyst

log = logging.getLogger(__name__)


class EventCalendar:
    """Fachada sobre el repositorio de catalizadores con caché en memoria."""

    def __init__(self, repository: CatalystRepository) -> None:
        self._repo = repository
        self._cache: Dict[str, List[Catalyst]] = {}
        self._cache_loaded = False

    # -------------------------------------------------------------------------
    # Carga inicial: siembra catalizadores por defecto si la tabla está vacía
    # -------------------------------------------------------------------------

    async def bootstrap_defaults(self, defaults: List[Catalyst]) -> None:
        """
        Inserta catalizadores por defecto (idempotente gracias a UNIQUE constraint).
        Pensado para llamarlo al arrancar con un set base de eventos conocidos.
        """
        for c in defaults:
            await self._repo.upsert(c)
            log.info(
                "Default catalyst seeded",
                extra={"ticker": c.ticker, "event_date": c.event_date.isoformat()},
            )
        # Invalidamos la caché para forzar recarga
        self._cache.clear()
        self._cache_loaded = False

    async def _ensure_loaded(self) -> None:
        if self._cache_loaded:
            return
        all_catalysts = await self._repo.list_all()
        self._cache = {}
        for c in all_catalysts:
            self._cache.setdefault(c.ticker, []).append(c)
        self._cache_loaded = True
        log.info(
            "Catalyst cache loaded",
            extra={"distinct_tickers": len(self._cache),
                   "total_catalysts": sum(len(v) for v in self._cache.values())},
        )

    # -------------------------------------------------------------------------
    # Consulta
    # -------------------------------------------------------------------------

    async def catalysts_for(self, ticker: str) -> List[Catalyst]:
        """Catalizadores asociados a un ticker. Lista vacía si no hay."""
        await self._ensure_loaded()
        return list(self._cache.get(ticker, []))

    async def active_catalysts(
        self,
        ticker: str,
        today: date,
    ) -> List[Catalyst]:
        """
        Catalizadores 'activos' = dentro de su ventana de sensibilidad
        respecto al día actual.
        """
        await self._ensure_loaded()
        actives: List[Catalyst] = []
        for c in self._cache.get(ticker, []):
            distance = abs((c.event_date - today).days)
            if distance <= c.sensitivity_window_days:
                actives.append(c)
        return actives

    async def all(self) -> List[Catalyst]:
        await self._ensure_loaded()
        flat: List[Catalyst] = []
        for catalysts in self._cache.values():
            flat.extend(catalysts)
        return flat


# =============================================================================
# Catalizadores por defecto
# =============================================================================
# Ejemplos concretos. La fecha 2026-11-19 viene mencionada explícitamente
# en la especificación; el resto son placeholders editables.
# =============================================================================

DEFAULT_CATALYSTS: List[Catalyst] = [
    Catalyst(
        ticker="NVDA",
        event_date=date(2026, 11, 19),
        description="Reporte regulatorio / lanzamiento programado (placeholder)",
        sensitivity_window_days=30,
    ),
]
