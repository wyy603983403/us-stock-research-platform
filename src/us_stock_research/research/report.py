"""HTML tear sheet (quantstats) from a backtest artifact: returns, drawdowns, monthly heat map.

Needs the optional extra: ``pip install -e '.[report]'``. Reads the equity curves that
``usr-backtest`` stores in the artifact, so the report shows exactly what the engine computed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def returns_from_artifact(artifact: dict[str, Any]) -> tuple[Any, Any]:
    """Daily return series (strategy, benchmark) indexed by date."""
    import pandas as pd

    curve = artifact.get("equity_curve")
    if not curve:
        raise ValueError("artifact has no equity_curve; re-run usr-backtest")
    index = pd.to_datetime(curve["dates"])
    strategy = pd.Series(curve["strategy"], index=index).pct_change().dropna()
    benchmark = pd.Series(curve["benchmark"], index=index).pct_change().dropna()
    return strategy, benchmark


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="HTML file to write")
    args = parser.parse_args(argv)
    artifact = json.loads(args.artifact.read_text())
    strategy, benchmark = returns_from_artifact(artifact)
    try:
        import quantstats as qs
    except ImportError as exc:
        raise SystemExit("needs quantstats: pip install -e '.[report]'") from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    qs.reports.html(  # type: ignore[no-untyped-call]  # quantstats.reports.html is untyped
        strategy,
        benchmark=benchmark,
        title=f"{artifact['study']} vs {artifact['benchmark']['symbol']}",
        output=str(args.output),
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
