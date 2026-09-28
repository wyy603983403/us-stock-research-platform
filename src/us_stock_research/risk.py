"""Explicit risk limits. Research only: trading can never be enabled from this repository."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trading_enabled: Literal[False]
    # Principal first: worst trailing-12-month loss must stay within this fraction of capital.
    max_worst_12m_loss: float = Field(gt=0, le=0.25)
    # Reported, not enforced (user accepts any drawdown if the 12-month loss cap holds).
    max_drawdown: float | None = Field(default=None, gt=0, le=1)
    max_single_asset_weight: float = Field(gt=0, le=1)
    min_oos_months: int = Field(ge=36)
    require_human_approval: Literal[True]


def load_risk(path: Path) -> RiskConfig:
    return RiskConfig.model_validate(yaml.safe_load(path.read_text()))
