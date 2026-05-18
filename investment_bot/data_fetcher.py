"""
data_fetcher.py
===============
Capa de adquisición de datos. yfinance es una librería síncrona basada en
peticiones HTTP bloqueantes; la convertimos en asíncrona delegando cada
llamada a un thread (`asyncio.to_thread`). Esto evita bloquear el event loop.

Controles de robustez:
    * Semáforo asíncrono limita la concurrencia (evita rate limits de Yahoo).
    * `tenacity` aplica reintentos con backoff exponencial + jitter.
    * Validación post-descarga: si los DataFrames críticos vienen vacíos,
      lanzamos una excepción de dominio para que el filtro descarte el ticker.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd
import yfinance as yf
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from models import RawMarketData

log = logging.getLogger(__name__)


# =============================================================================
# Excepciones de dominio
# =============================================================================

class DataFetchError(Exception):
    """Error genérico de adquisición de datos para un ticker."""


class InsufficientDataError(DataFetchError):
    """Los datos descargados son insuficientes para evaluar el ticker."""


# =============================================================================
# Fetcher asíncrono
# =============================================================================

class AsyncDataFetcher:
    """
    Adquiere los datos crudos necesarios para evaluar un ticker.
    Internamente paraleliza llamadas con `asyncio.gather` controlado por un
    semáforo, garantizando que nunca haya más de N descargas simultáneas.
    """

    def __init__(
        self,
        concurrency_limit: int,
        retry_max_attempts: int,
        retry_base_delay: float,
        history_years: int,
    ) -> None:
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self._retry_max_attempts = retry_max_attempts
        self._retry_base_delay = retry_base_delay
        self._history_years = history_years

    # -------------------------------------------------------------------------
    # API pública
    # -------------------------------------------------------------------------

    async def fetch_batch(self, tickers: List[str]) -> List[RawMarketData]:
        """
        Descarga datos para múltiples tickers en paralelo. Los tickers que
        fallen son omitidos (con log warn), no abortan el batch completo.
        """
        coros = [self._fetch_single_safe(t) for t in tickers]
        results = await asyncio.gather(*coros, return_exceptions=False)
        # `_fetch_single_safe` jamás lanza; devuelve Optional[RawMarketData]
        return [r for r in results if r is not None]

    # -------------------------------------------------------------------------
    # Internos
    # -------------------------------------------------------------------------

    async def _fetch_single_safe(self, ticker: str) -> Optional[RawMarketData]:
        """Envuelve `_fetch_single` para que NUNCA propague excepciones."""
        try:
            return await self._fetch_single(ticker)
        except InsufficientDataError as exc:
            log.warning(
                "Insufficient data; skipping",
                extra={"ticker": ticker, "reason": str(exc)},
            )
        except DataFetchError as exc:
            log.warning(
                "Fetch failed after retries; skipping",
                extra={"ticker": ticker, "reason": str(exc)},
            )
        except Exception as exc:
            log.exception(
                "Unexpected error during fetch; skipping",
                extra={"ticker": ticker, "error": str(exc)},
            )
        return None

    async def _fetch_single(self, ticker: str) -> RawMarketData:
        """Descarga un único ticker con retries y semáforo."""
        async with self._semaphore:
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(self._retry_max_attempts),
                    wait=(
                        wait_exponential(multiplier=self._retry_base_delay, max=60)
                        + wait_random(0, 1)
                    ),
                    retry=retry_if_exception_type(DataFetchError),
                    reraise=True,
                ):
                    with attempt:
                        return await self._do_fetch(ticker)
            except RetryError as exc:
                raise DataFetchError(
                    f"Max retries exceeded for {ticker}: {exc}"
                ) from exc

        # Inalcanzable, pero el linter lo agradece
        raise DataFetchError(f"Unreachable state for {ticker}")

    async def _do_fetch(self, ticker: str) -> RawMarketData:
        """
        Ejecuta la descarga real. Se delega al thread pool por defecto
        porque yfinance es síncrono. Valida que los datos críticos estén
        presentes antes de devolver el contenedor.
        """
        try:
            data = await asyncio.to_thread(self._sync_fetch, ticker)
        except (ConnectionError, TimeoutError) as exc:
            # Errores transitorios -> reintentables
            raise DataFetchError(f"Transient network error: {exc}") from exc
        except Exception as exc:
            # Otros errores (parsing, etc.) también son reintentables
            # porque yfinance a veces falla intermitentemente
            raise DataFetchError(f"yfinance error: {exc}") from exc

        # Validación post-descarga
        if data.history is None or data.history.empty:
            raise InsufficientDataError("Empty price history")

        # Necesitamos al menos `ma_period` puntos para calcular MA200
        # (200 es el default, validamos 220 como margen de seguridad)
        if len(data.history) < 220:
            raise InsufficientDataError(
                f"Only {len(data.history)} price points; need >= 220"
            )

        return data

    @staticmethod
    def _sync_fetch(ticker: str) -> RawMarketData:
        """
        Función puramente síncrona que ejecuta yfinance. Aislada para
        poder ser delegada a `asyncio.to_thread` sin estado compartido.
        """
        yf_ticker = yf.Ticker(ticker)

        # info: snapshot puntual (PER actual, shares outstanding, etc.)
        # yfinance puede devolver dict vacío silenciosamente
        info = yf_ticker.info or {}

        # Historia de precios: necesitamos cobertura del PER histórico (~5y)
        # + margen para MA200. Pedimos 'max' por defecto y luego truncamos.
        history = yf_ticker.history(
            period="10y",
            interval="1d",
            auto_adjust=True,
            actions=False,
        )

        # Estados financieros anuales
        financials = yf_ticker.financials
        balance_sheet = yf_ticker.balance_sheet
        cashflow = yf_ticker.cashflow

        # Si no hay financials, el ticker no es analizable fundamentalmente
        if financials is None or financials.empty:
            raise InsufficientDataError("No financials data available")

        # Dividendos (puede estar vacío si la empresa no paga; no es fatal)
        dividends = yf_ticker.dividends

        return RawMarketData(
            ticker=ticker,
            info=info,
            history=history,
            financials_annual=financials,
            balance_sheet_annual=balance_sheet if balance_sheet is not None else pd.DataFrame(),
            cashflow_annual=cashflow if cashflow is not None else pd.DataFrame(),
            dividends=dividends if dividends is not None else pd.Series(dtype=float),
            fetched_at=datetime.now(tz=timezone.utc),
        )
