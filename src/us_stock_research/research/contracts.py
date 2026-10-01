"""Study contracts (``research/<name>/study.yml``): the pre-registered question, data,
parameters, benchmark, validation and human-review state of one study.

Parameters are declared *before* looking at results. Changing them after a run means a new
study version, not an edit.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

StudyStatus = Literal["draft", "frozen", "rejected", "promoted"]
UniverseKind = Literal["etf", "macro", "stocks_current_constituents", "stocks_point_in_time"]
STRATEGIES = ("trend_sma_v1", "dual_momentum_v1", "buy_and_hold_v1", "vol_target_v1")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataSpec(_Strict):
    provider: str
    source_url: str
    universe: list[str] = Field(min_length=1)
    # Stocks picked from today's index members are survivorship-biased and can never promote.
    universe_kind: UniverseKind = "etf"
    start: date
    end: date
    snapshot_id: str | None = None
    quality_report: str | None = None

    @model_validator(mode="after")
    def _check(self) -> DataSpec:
        if self.end <= self.start:
            raise ValueError("data.end must be after data.start")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("data.universe contains duplicates")
        return self


class Parameters(_Strict):
    sma_days: int = Field(default=200, ge=20, le=400)
    lookback_days: int = Field(default=252, ge=20, le=504)
    top_k: int = Field(default=1, ge=1)
    vol_target: float = Field(default=0.10, gt=0, le=0.30)  # annualised, vol_target_v1 only
    vol_lookback_days: int = Field(default=60, ge=20, le=252)
    cash_symbol: str | None = None
    transaction_cost_bps: float = Field(default=5, ge=0, le=100)
    execution_lag_days: int = Field(default=1, ge=1, le=5)


class Benchmark(_Strict):
    symbol: str


class Inference(_Strict):
    method: Literal["moving_block_bootstrap_mean_excess_v1"]
    resamples: int = Field(ge=200)
    block_size_months: int = Field(ge=1)
    confidence_level: float = Field(gt=0.5, lt=1)


class Validation(_Strict):
    method: Literal["walk_forward"]
    warmup_days: int = Field(ge=0)
    test_months: int = Field(ge=1)


class HumanReview(_Strict):
    required: Literal[True]
    approved: bool
    reviewer: str | None = None
    reviewed_at: date | None = None


class StudyContract(_Strict):
    schema_version: Literal[1]
    name: str
    status: StudyStatus
    strategy: str
    question: str
    hypothesis: str
    data: DataSpec
    parameters: Parameters
    benchmark: Benchmark
    inference: Inference
    validation: Validation
    random_seed: int
    output: str
    limitations: list[str] = Field(min_length=1)
    human_review: HumanReview

    @model_validator(mode="after")
    def _check(self) -> StudyContract:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}; known: {STRATEGIES}")
        symbols = set(self.data.universe)
        if self.benchmark.symbol not in symbols:
            raise ValueError("benchmark.symbol must be part of data.universe")
        cash = self.parameters.cash_symbol
        if cash is not None and cash not in symbols:
            raise ValueError("parameters.cash_symbol must be part of data.universe")
        if self.status != "draft" and not (self.data.snapshot_id and self.data.quality_report):
            raise ValueError("non-draft studies need data.snapshot_id and data.quality_report")
        if self.status == "promoted" and not self.human_review.approved:
            raise ValueError("promoted studies require human_review.approved")
        return self

    def risk_universe(self) -> list[str]:
        """Symbols the strategy may hold (everything except a declared cash sleeve)."""
        cash = self.parameters.cash_symbol
        return [s for s in self.data.universe if s != cash]


def load_contract(path: Path) -> StudyContract:
    raw = yaml.safe_load(path.read_text())
    return StudyContract.model_validate(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate study contracts")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    ok = True
    for path in args.paths:
        try:
            if (yaml.safe_load(path.read_text()) or {}).get("kind") == "cross_section":
                from us_stock_research.research.cross_section import load_xs_contract

                raw = load_xs_contract(path)
                print(f"OK   {path} ({raw['name']}, {raw.get('status')}, cross_section)")
                continue
            contract = load_contract(path)
            print(f"OK   {path} ({contract.name}, {contract.status})")
        except Exception as exc:  # noqa: BLE001 - report every failing contract
            ok = False
            print(f"FAIL {path}: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
