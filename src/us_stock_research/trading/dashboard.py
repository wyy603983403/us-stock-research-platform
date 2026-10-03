"""Self-contained HTML monitoring page (``artifacts/dashboard.html``), written by ``usr-status``.

One file, no network: the day's status is embedded as JSON and drawn with inline SVG. Sections:
status banner, stat tiles, three charts (portfolio vs model vs SPY indexed to 100; exposure;
SPY and its 200-day average), recent orders, holdings, items needing attention. Light and dark
themes; every chart has a crosshair tooltip and a data table.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TEMPLATE = Path(__file__).with_name("dashboard_template.html")


def render_html(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", text)
