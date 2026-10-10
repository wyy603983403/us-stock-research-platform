"""Rehearse the server's daily pipeline (scripts/server_daily.sh) end to end, offline.

Builds a throw-away copy of the app with a fake ``.venv/bin`` (every entry point runs this
checkout's code), points it at a local data store, and replaces only what needs the network:
``usr-update`` / ``usr-collect-macro`` (data are already local), the Alpaca paper account (an
in-process fake with the real ``usr-paper`` logic on top; orders fill at the next run's close)
and the notifier (prints instead of sending). Then it plays scenarios day by day and checks the
log and the notification text.

    python tools/rehearse_server.py --data /path/to/storage_root [--keep]

The data store must hold daily bars for SPY SSO BIL TLT IEF GLD and macro DTB3 up to the
scenario dates (default: the week ending 2026-10-07).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

FAKE_PAPER = r"""
import json, os, sys
from pathlib import Path
from us_stock_research.trading import alpaca_paper as ap
from us_stock_research.config import load_settings
from us_stock_research.storage import open_store

STATE = Path(os.environ["FAKE_ALPACA"])

def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"cash": 100000.0, "positions": {}, "orders": {}, "pending": []}

def save(s):
    STATE.write_text(json.dumps(s))

def close(sym):
    bars = [b for b in open_store(load_settings()).read_bars(sym) if b.day.isoformat() <= os.environ["USR_ASOF"]]
    return bars[-1].close

class Fake:
    def __init__(self, *a, **k):
        s = load()
        for o in s["pending"]:  # a close order fills at the close of the run's day
            px = close(o["symbol"]); q = o["qty"] if o["side"] == "buy" else -o["qty"]
            s["positions"][o["symbol"]] = s["positions"].get(o["symbol"], 0) + q
            s["cash"] -= q * px
            s["orders"][o["cid"]]["status"] = "filled"
        s["pending"] = []
        s["positions"] = {k: v for k, v in s["positions"].items() if v}
        save(s)
    def account(self):
        s = load()
        eq = s["cash"] + sum(q * close(k) for k, q in s["positions"].items())
        return {"status": "ACTIVE", "currency": "USD", "cash": str(s["cash"]), "equity": str(eq),
                "buying_power": str(eq * 4), "trading_blocked": False}
    def positions(self):
        return {k: float(v) for k, v in load()["positions"].items()}
    def order_by_client_id(self, cid):
        return load()["orders"].get(cid)
    def submit_close_order(self, symbol, side, qty, cid):
        if os.environ.get("FAKE_REJECT"):
            raise ap.OrderRejected(f"{symbol} {side} {qty}: HTTP 403 " + os.environ["FAKE_REJECT"])
        s = load()
        s["pending"].append({"symbol": symbol, "side": side.lower(), "qty": qty, "cid": cid})
        s["orders"][cid] = {"status": "accepted"}
        save(s)
        return {"status": "accepted"}

ap.PaperClient = Fake
sys.exit(ap.main(sys.argv[1:]))
"""

NOTIFY = r"""#!/usr/bin/env bash
printf '%s\n%s\n' "NOTIFY<<$1" "$2" >> "${NOTIFY_LOG:-/dev/null}"
echo "（通知）$1"
"""


def entry_points() -> dict[str, str]:
    text = (REPO / "pyproject.toml").read_text()
    block = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    return dict(re.findall(r'^(usr-[\w-]+)\s*=\s*"([\w.]+):main"', block, re.M))


def build(work: Path, data: Path) -> Path:
    app = work / "app"
    app.mkdir(parents=True)
    for d in ("src", "scripts", "configs", "research"):
        shutil.copytree(REPO / d, app / d)
    for d in ("orders", "portfolio", "artifacts", "logs"):
        (app / d).mkdir()
    (app / "scripts/notify.sh").write_text(NOTIFY)
    (app / "scripts/server_backup.sh").write_text("#!/usr/bin/env bash\necho 备份：演练跳过\n")
    (app / ".env").write_text(
        f"USR_STORAGE_ROOT={data}\nALPACA_PAPER_KEY_ID=fake\nALPACA_PAPER_SECRET_KEY=fake\n"
    )
    bin_ = app / ".venv/bin"
    bin_.mkdir(parents=True)
    env_line = f'export PYTHONPATH="{app}/src${{PYTHONPATH:+:$PYTHONPATH}}"'
    (bin_ / "python").write_text(f'#!/usr/bin/env bash\n{env_line}\nexec {sys.executable} "$@"\n')
    stubs = {"usr-update": "update", "usr-collect-macro": "macro"}
    for name, module in entry_points().items():
        if name == "usr-update":
            body = (
                f'{env_line}\nexec {sys.executable} -c "import json,sys,os;'
                "a=sys.argv;r=a[a.index('--report')+1];"
                "json.dump({'date':os.environ['USR_ASOF'],'failed':{},'behind':[],"
                "'quality_errors':{},'fallback':{},'crosscheck':{},'crosscheck_mismatch':[]},"
                'open(r,\'w\'))" "$@"'
            )
        elif name in stubs:
            body = "exit 0"
        elif name == "usr-paper":
            (bin_ / "fake_paper.py").write_text(FAKE_PAPER)
            body = f'{env_line}\nexec {sys.executable} {bin_ / "fake_paper.py"} "$@"'
        else:
            body = f'{env_line}\nexec {sys.executable} -m {module} "$@"'
        (bin_ / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    for f in (app / "scripts").glob("*.sh"):
        f.chmod(0o755)
    return app


def run_day(
    app: Path, day: str, stamp: str, extra: dict[str, str] | None = None
) -> tuple[str, str]:
    notify_log = app / f"notify_{stamp}.txt"
    env = {**os.environ, "USR_ASOF": day, "USR_STAMP": stamp, "NOTIFY_LOG": str(notify_log),
           "FAKE_ALPACA": str(app / "fake_alpaca.json"), **(extra or {})}  # fmt: skip
    env.pop("FAKE_REJECT", None) if not (extra or {}).get("FAKE_REJECT") else None
    subprocess.run(["bash", "scripts/server_daily.sh"], cwd=app, env=env, check=False,
                   capture_output=True, timeout=900)  # fmt: skip
    log = (app / f"logs/daily_{stamp}.log").read_text()
    note = notify_log.read_text() if notify_log.exists() else ""
    return log, note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--days",
        nargs=3,
        default=["2026-10-06", "2026-10-07", "2026-10-09"],
        help="first day, next day, a day after the data end",
    )
    args = parser.parse_args(argv)
    D1, D2, D3 = args.days  # noqa: N806
    work = Path(tempfile.mkdtemp(prefix="rehearse_"))
    app = build(work, args.data.resolve())
    ops = app / "configs/operating.yml"
    ops.write_text(
        re.sub(r"^model_start: [0-9-]+", "model_start: " + D1, ops.read_text(), flags=re.M)
    )
    checks: list[tuple[str, bool, str]] = []

    def reset() -> None:
        shutil.rmtree(app / "orders", ignore_errors=True)
        (app / "orders").mkdir()
        (app / "fake_alpaca.json").unlink(missing_ok=True)
        for f in (app / "portfolio/paper").glob("*"):
            f.unlink()

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    def traceback_free(log: str) -> bool:
        return "Traceback" not in log

    # 1. first day: start event -> list, independent check, paper submission
    # (dates within 4 days of today's wall clock or usr-paper treats the list as expired;
    #  the default store ends 2026-10-07, so d1/d2 are the last two sessions in it)
    log, note = run_day(app, D1, "d1")
    check("d1 order list", "有新订单" in note and "买入" in note, note[:300])
    check("d1 verified", "独立复核：一致" in log)
    check("d1 paper submitted", "模拟单已提交" in note, note[:300])
    check("d1 no traceback", traceback_free(log), log[-800:])
    # 2. next day: fills reconciled, no new orders unless the model trades
    log, note = run_day(app, D2, "d2")
    check("d2 holdings synced", "模拟盘持仓已写入" in log and "对账不一致" not in log, log[-600:])
    check("d2 no traceback", traceback_free(log), log[-800:])
    # 3. data end before the signal day: reduce-only / waiting, no crash
    log, note = run_day(app, D3, "d3")
    check("d3 weekly report", "周报" in note and "【模拟盘】" in note, note[-500:])
    check(
        "d3 stale handled",
        traceback_free(log) and ("只减仓" in note or "不出单" in log),
        note[:300],
    )
    # 4. Alpaca rejects: reason in the notification
    reset()
    log, note = run_day(app, D2, "d4", {"FAKE_REJECT": "insufficient buying power"})
    check("d4 rejection reported", "⚠️" in note and "insufficient buying power" in note, note[:300])
    # 5. emergency stop: list generated, nothing sent
    reset()
    (app / "portfolio/STOP_TRADING").write_text("test\n")
    log, note = run_day(app, D2, "d5")
    check("d5 stop respected", "⛔" in note and "模拟单已提交" not in note, note[:300])
    (app / "portfolio/STOP_TRADING").unlink()
    # 6. live ledger started: live list with cash buffer and its own independent check
    live = app / "configs/live.yml"
    live.write_text(
        re.sub(
            r"^start_signal_day: [0-9-]+", "start_signal_day: " + D2, live.read_text(), flags=re.M
        )
    )
    subprocess.run([str(app / ".venv/bin/usr-live"), "init", "--cash", "10000"], cwd=app,
                   check=False, capture_output=True)  # fmt: skip
    log, note = run_day(app, D2, "d6")
    check("d6 live list", "【实盘·嘉信手动】" in note, note[:400])
    check("d6 live verified", "实盘独立复核：一致" in log, log[-800:])
    check("d6 no traceback", traceback_free(log), log[-800:])
    width = max(len(n) for n, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}" + ("" if ok else f"\n      {detail}"))
        failed += not ok
    print(f"{len(checks) - failed}/{len(checks)} passed; work dir {work}")
    if not args.keep and not failed:
        shutil.rmtree(work)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
