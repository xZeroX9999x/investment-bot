"""
config.py
=========
Configuración centralizada del sistema. Toda la configuración se carga desde
variables de entorno (cargadas opcionalmente desde un archivo `.env`) y se
valida estrictamente mediante Pydantic. Esto garantiza que cualquier desviación
de los rangos esperados aborte el arranque del proceso antes de tocar capital.

Filosofía: fallar rápido y de manera ruidosa en el arranque; ser determinista
y silencioso en ejecución.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated


class Settings(BaseSettings):
    """
    Modelo de configuración inmutable. Pydantic se encarga de:
      * Leer variables de entorno (o `.env`).
      * Coercionar tipos (str -> float, str -> List[str], etc.).
      * Validar rangos y restricciones de dominio.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,  # Inmutable tras carga (defensa contra mutaciones en runtime)
    )

    # ---------------------------------------------------------------------
    # Universo de activos
    # ---------------------------------------------------------------------
    # NoDecode evita que pydantic-settings intente parsear el valor como JSON
    # antes de pasar por nuestro validador (el formato real es CSV plano).
    ticker_universe: Annotated[List[str], NoDecode] = Field(default_factory=list)

    @field_validator("ticker_universe", mode="before")
    @classmethod
    def _split_universe(cls, value):
        """Permite definir tickers como CSV en una sola env var."""
        if isinstance(value, str):
            return [t.strip().upper() for t in value.split(",") if t.strip()]
        return value

    # ---------------------------------------------------------------------
    # Filtro Fundamental (Fase 1)
    # ---------------------------------------------------------------------
    margen_bruto_minimo: float = Field(default=0.50, ge=0.0, le=1.0)
    eps_years_lookback: int = Field(default=5, ge=2, le=20)
    shares_years_lookback: int = Field(default=3, ge=2, le=10)

    # ---------------------------------------------------------------------
    # Valoración y Técnico (Fase 2)
    # ---------------------------------------------------------------------
    per_discount_min: float = Field(default=0.20, ge=0.0, le=1.0)
    per_history_years: int = Field(default=5, ge=2, le=20)
    rsi_oversold_threshold: float = Field(default=30.0, ge=0.0, le=100.0)
    rsi_extreme_oversold: float = Field(default=20.0, ge=0.0, le=100.0)
    ma_period: int = Field(default=200, ge=20, le=500)
    rsi_period: int = Field(default=14, ge=2, le=50)

    # ---------------------------------------------------------------------
    # Ejecución
    # ---------------------------------------------------------------------
    concurrency_limit: int = Field(default=5, ge=1, le=50)
    retry_max_attempts: int = Field(default=5, ge=1, le=20)
    retry_base_delay: float = Field(default=2.0, ge=0.1, le=60.0)
    scan_interval_seconds: int = Field(default=3600, ge=60)

    # ---------------------------------------------------------------------
    # Persistencia
    # ---------------------------------------------------------------------
    database_path: Path = Field(default=Path("./data/arquitecto.db"))

    # ---------------------------------------------------------------------
    # Alertas
    # ---------------------------------------------------------------------
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_to: Optional[str] = None

    # ---------------------------------------------------------------------
    # Logging
    # ---------------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_path: Path = Field(default=Path("./logs/arquitecto.jsonl"))

    # ---------------------------------------------------------------------
    # Validaciones cruzadas
    # ---------------------------------------------------------------------
    @field_validator("rsi_extreme_oversold")
    @classmethod
    def _extreme_must_be_stricter(cls, v, info):
        normal = info.data.get("rsi_oversold_threshold", 30.0)
        if v > normal:
            raise ValueError(
                f"rsi_extreme_oversold ({v}) debe ser <= rsi_oversold_threshold ({normal})"
            )
        return v

    # ---------------------------------------------------------------------
    # Conveniencias
    # ---------------------------------------------------------------------
    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def smtp_enabled(self) -> bool:
        return bool(
            self.smtp_host
            and self.smtp_user
            and self.smtp_password
            and self.smtp_from
            and self.smtp_to
        )


# Singleton de configuración. Importar desde otros módulos como:
#     from config import get_settings
# Se memoiza para evitar relecturas del entorno.
_settings_singleton: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings_singleton
    if _settings_singleton is None:
        _settings_singleton = Settings()
    return _settings_singleton
