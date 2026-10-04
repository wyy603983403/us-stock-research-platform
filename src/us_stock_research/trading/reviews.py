"""Stage 1 review log and progress (``usr-review``).

The user signs off each order list after checking it (the automatic independent re-check must say
"一致" first). Sign-offs are appended to ``portfolio/reviews/<study>.jsonl`` and ticked in the
list's ``.md``. ``progress`` tells how far the stage 1 gate is: every list since the first one
reviewed and consistent, for ``consecutive_months`` calendar months.

    usr-review --study vt_plus_defensive --day 2026-10-02 --reviewer 用户
    usr-review --study vt_plus_defensive --progress
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

VERIFIED = "**结论：一致**"


def add_months(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, d)
        except ValueError:
            continue
    raise ValueError(day)


def order_lists(folder: Path) -> list[date]:
    return sorted(date.fromisoformat(p.stem) for p in folder.glob("????-??-??.json"))


def load_reviews(path: Path) -> dict[date, dict[str, Any]]:
    out: dict[date, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                out[date.fromisoformat(r["signal_day"])] = r
    return out


def record(
    folder: Path, log: Path, day: date, reviewer: str, note: str = "", now: datetime | None = None
) -> dict[str, Any]:
    md = folder / f"{day.isoformat()}.md"
    if not (folder / f"{day.isoformat()}.json").exists():
        raise ValueError(f"没有 {day} 的订单清单（或已被复核程序搁置）")
    text = md.read_text() if md.exists() else ""
    if VERIFIED not in text:
        raise ValueError(f"{day} 的清单还没有“自动独立复核：一致”的结论，先运行 usr-verify-intent")
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    entry = {"signal_day": day.isoformat(), "reviewer": reviewer, "result": "ok", "note": note,
             "reviewed_at": stamp}  # fmt: skip
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    sign = f"- [x] 复核人 / 日期：{reviewer} / {stamp[:10]}"
    signed = text.replace("- [ ] 复核人 / 日期：", sign)
    for box in ("- [ ] 数据截至日正确", "- [ ] 目标权重与手工", "- [ ] 订单股数与现持仓"):
        signed = signed.replace(box, box.replace("[ ]", "[x]"))
    md.write_text(signed)
    return entry


def progress(folder: Path, log: Path, months: int, today: date) -> dict[str, Any]:
    lists = order_lists(folder)
    reviews = load_reviews(log)
    if not lists:
        return {"lists": 0, "reviewed": 0, "pending": [], "gate_date": None, "met": False,
                "rejected": []}  # fmt: skip
    first = lists[0]
    gate = add_months(first, months)
    pending = [d.isoformat() for d in lists if d not in reviews]
    rejected = sorted(p.name for p in folder.glob("*.json.rejected"))
    return {
        "first_list": first.isoformat(),
        "lists": len(lists),
        "reviewed": len(lists) - len(pending),
        "pending": pending,
        "rejected": rejected,
        "gate_date": gate.isoformat(),
        "days_left": max(0, (gate - today).days),
        "met": today >= gate and not pending and not rejected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True)
    parser.add_argument("--day", type=date.fromisoformat, help="signal day of the list to sign")
    parser.add_argument("--reviewer", default="用户")
    parser.add_argument("--note", default="")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--orders-dir", type=Path, default=Path("orders"))
    parser.add_argument("--reviews-dir", type=Path, default=Path("portfolio/reviews"))
    parser.add_argument("--gates", type=Path, default=Path("configs/stage_gates.yml"))
    args = parser.parse_args(argv)
    folder = args.orders_dir / args.study
    log = args.reviews_dir / f"{args.study}.jsonl"
    if args.day:
        try:
            entry = record(folder, log, args.day, args.reviewer, args.note)
        except ValueError as exc:
            print(f"未记录：{exc}")
            return 2
        print(f"已记录复核：{entry['signal_day']}（{entry['reviewer']}，{entry['reviewed_at']}）")
    gates = yaml.safe_load(args.gates.read_text()) if args.gates.exists() else {}
    months = int(gates.get("stage1", {}).get("consecutive_months", 3))
    p = progress(folder, log, months, datetime.now(UTC).date())
    if not p["lists"]:
        print("还没有订单清单")
        return 0
    print(
        f"阶段 1：清单 {p['lists']} 份，已复核 {p['reviewed']} 份"
        + (f"，待复核 {'、'.join(p['pending'])}" if p["pending"] else "")
        + (f"，被搁置 {len(p['rejected'])} 份" if p["rejected"] else "")
        + f"；自 {p['first_list']} 起满 {months} 个月为 {p['gate_date']}"
        + ("，门槛已满足" if p["met"] else f"（还有 {p['days_left']} 天）")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
