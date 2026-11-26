#!/usr/bin/env python3
"""公开模拟账本：按相同杠杆跟踪 Hyperliquid 上的 +convexity 金库。

只读公开接口，不下单、不碰钱包、不使用密钥。
同一天重复运行不会重复成交；接口失败时以非零状态退出且不提交。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

getcontext().prec = 40

SGT = ZoneInfo("Asia/Singapore")
UTC = timezone.utc

VAULT = "0x5661a070eb13c7c55ac3210b2447d4bea426cbf5"
API = "https://api.hyperliquid.xyz/info"
TAKER_FEE_RATE = Decimal("0.00045")
from ledger_core import (  # noqa: E402
    START_DATE,
    LIVE_FROM,
    LEDGER_VERSION,
    NOTE_REPLAY,
    NOTE_LIVE,
    AUDIT_FIELDS,
    INITIAL_CAPITAL,
    run_ledger,
    apply_live_day,
    book_from_state as core_book_from_state,
    write_csv as core_write_csv,
)
ROOT = Path(__file__).resolve().parent

STATE_PATH = ROOT / "state.json"
TRADES_PATH = ROOT / "trades.csv"
DAILY_PATH = ROOT / "daily.csv"
README_PATH = ROOT / "README.md"

TRADES_FIELDS = [
    "date_sgt",
    "coin",
    "side",
    "size",
    "price",
    "fee",
    "source",
    "vault_fill_vwap",
    "note",
]
DAILY_FIELDS = [
    "date_sgt",
    "virtual_equity",
    "position",
    "leverage",
    "daily_pnl",
    "cum_return",
    "vault_equity",
    "vault_position",
    "btc_mark",
    "note",
]
AUDIT_PATH = ROOT / "replay_audit.csv"

SUMMARY_START = "<!-- LEDGER-SUMMARY:START -->"
SUMMARY_END = "<!-- LEDGER-SUMMARY:END -->"

WAIT_UNTIL_HOUR_SGT = 10  # 金库约 09:45 调仓；10:00 前且尚无成交则等待


class ApiError(RuntimeError):
    pass


def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None:
        raise ApiError("unexpected null numeric value")
    return Decimal(str(x))


def q_money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def q_rate(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP)


def q_lev(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def q_size(x: Decimal, sz_decimals: int) -> Decimal:
    step = Decimal("1").scaleb(-sz_decimals)
    return x.quantize(step, rounding=ROUND_HALF_UP)


def fmt_money(x: Decimal) -> str:
    return f"{q_money(x):,}"


def fmt_px(x: Decimal) -> str:
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def now_sgt() -> datetime:
    return datetime.now(SGT)


def date_sgt(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000, tz=SGT).date()


def day_start_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=SGT).timestamp() * 1000)


def api_post(payload: dict, retries: int = 4, timeout: int = 30) -> Any:
    body = json.dumps(payload).encode()
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "convexity-paper-ledger/1"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise ApiError(f"HTTP {resp.status} for {payload.get('type')}")
                raw = resp.read().decode()
            data = json.loads(raw)
            if data is None:
                raise ApiError(f"null response for {payload.get('type')}")
            return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ApiError) as exc:
            last_err = exc
            time.sleep(min(8, 1.5 * (attempt + 1)))
    raise ApiError(f"API failed for {payload.get('type')}: {last_err}")


def fetch_clearinghouse() -> dict:
    data = api_post({"type": "clearinghouseState", "user": VAULT})
    if not isinstance(data, dict) or "marginSummary" not in data or "assetPositions" not in data:
        raise ApiError("invalid clearinghouseState")
    return data


def fetch_vault_details() -> dict:
    data = api_post({"type": "vaultDetails", "vaultAddress": VAULT})
    if not isinstance(data, dict) or "vaultAddress" not in data:
        raise ApiError("invalid vaultDetails")
    return data


def fetch_mids() -> dict[str, Decimal]:
    data = api_post({"type": "allMids"})
    if not isinstance(data, dict) or "BTC" not in data:
        raise ApiError("invalid allMids")
    return {str(k): d(v) for k, v in data.items()}


def fetch_fills(start_ms: int, end_ms: int) -> list[dict]:
    data = api_post(
        {
            "type": "userFillsByTime",
            "user": VAULT,
            "startTime": start_ms,
            "endTime": end_ms,
            "aggregateByTime": True,
        }
    )
    if not isinstance(data, list):
        raise ApiError("invalid userFillsByTime")
    return data


def fetch_ledger_updates(start_ms: int) -> list[dict]:
    data = api_post({"type": "userNonFundingLedgerUpdates", "user": VAULT, "startTime": start_ms})
    if not isinstance(data, list):
        raise ApiError("invalid userNonFundingLedgerUpdates")
    return data


def fetch_funding(coin: str, start_ms: int) -> list[dict]:
    data = api_post({"type": "fundingHistory", "coin": coin, "startTime": start_ms})
    if not isinstance(data, list):
        raise ApiError(f"invalid fundingHistory for {coin}")
    return data


def fetch_candles(coin: str, start_ms: int, end_ms: int) -> list[dict]:
    data = api_post(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": "1h",
                "startTime": start_ms,
                "endTime": end_ms,
            },
        }
    )
    if not isinstance(data, list):
        raise ApiError(f"invalid candleSnapshot for {coin}")
    return data


def fetch_sz_decimals() -> dict[str, int]:
    data = api_post({"type": "meta"})
    universe = data.get("universe") if isinstance(data, dict) else None
    if not isinstance(universe, list):
        raise ApiError("invalid meta")
    out: dict[str, int] = {}
    for asset in universe:
        if isinstance(asset, dict) and "name" in asset and "szDecimals" in asset:
            out[str(asset["name"])] = int(asset["szDecimals"])
    if "BTC" not in out:
        raise ApiError("meta missing BTC")
    return out


def vault_positions(state: dict) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for item in state.get("assetPositions") or []:
        pos = item.get("position") if isinstance(item, dict) else None
        if not pos:
            continue
        coin = str(pos["coin"])
        szi = d(pos["szi"])
        if szi != 0:
            out[coin] = szi
    return out


def vault_equity(state: dict) -> Decimal:
    return d(state["marginSummary"]["accountValue"])


def fill_signed_size(fill: dict) -> Decimal:
    sz = d(fill["sz"])
    side = str(fill["side"]).upper()
    # A = sell/ask, B = buy/bid
    if side in ("A", "SELL"):
        return -sz
    if side in ("B", "BUY"):
        return sz
    raise ApiError(f"unknown fill side: {fill.get('side')}")


def vwap_and_net(fills: list[dict]) -> tuple[Decimal, Decimal]:
    """Return (vwap, net signed size) for a list of fills of one coin."""
    notional = Decimal("0")
    volume = Decimal("0")
    net = Decimal("0")
    for f in fills:
        sz = d(f["sz"])
        px = d(f["px"])
        notional += sz * px
        volume += sz
        net += fill_signed_size(f)
    if volume == 0:
        raise ApiError("zero fill volume")
    return notional / volume, net


def format_position_map(pos: dict[str, Decimal]) -> str:
    if not pos:
        return "FLAT"
    parts = []
    for coin in sorted(pos):
        sz = pos[coin]
        if sz == 0:
            continue
        parts.append(f"{coin}:{format(sz, 'f')}")
    return " ".join(parts) if parts else "FLAT"


@dataclass
class Position:
    szi: Decimal = Decimal("0")
    entry_px: Decimal = Decimal("0")


@dataclass
class Book:
    cash: Decimal = INITIAL_CAPITAL
    positions: dict[str, Position] = field(default_factory=dict)
    last_funding_ms: int = 0

    def szi(self, coin: str) -> Decimal:
        p = self.positions.get(coin)
        return p.szi if p else Decimal("0")

    def live_positions(self) -> dict[str, Decimal]:
        return {c: p.szi for c, p in self.positions.items() if p.szi != 0}

    def unrealized(self, mids: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for coin, p in self.positions.items():
            if p.szi == 0:
                continue
            if coin not in mids:
                raise ApiError(f"missing mid for {coin}")
            total += p.szi * (mids[coin] - p.entry_px)
        return total

    def equity(self, mids: dict[str, Decimal]) -> Decimal:
        return self.cash + self.unrealized(mids)

    def notional(self, mids: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for coin, p in self.positions.items():
            if p.szi == 0:
                continue
            if coin not in mids:
                raise ApiError(f"missing mid for {coin}")
            total += abs(p.szi) * mids[coin]
        return total

    def apply_trade(self, coin: str, qty: Decimal, px: Decimal, fee: Decimal) -> Decimal:
        """Apply a signed qty trade (+ buy, - sell). Return realized pnl (before fee)."""
        self.cash -= fee
        pos = self.positions.get(coin) or Position()
        old = pos.szi
        new = old + qty
        realized = Decimal("0")
        if old == 0 or (qty != 0 and (old > 0) == (qty > 0)):
            # opening or increasing
            if new == 0:
                pos.szi = Decimal("0")
                pos.entry_px = Decimal("0")
            else:
                pos.entry_px = (abs(old) * pos.entry_px + abs(qty) * px) / abs(new)
                pos.szi = new
        else:
            closed = min(abs(old), abs(qty))
            closed_szi = closed if old > 0 else -closed
            realized = closed_szi * (px - pos.entry_px)
            self.cash += realized
            if abs(qty) < abs(old):
                pos.szi = new
            elif abs(qty) == abs(old):
                pos.szi = Decimal("0")
                pos.entry_px = Decimal("0")
            else:
                pos.szi = new
                pos.entry_px = px
        if pos.szi == 0:
            self.positions.pop(coin, None)
        else:
            self.positions[coin] = pos
        return realized


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "initialized": False,
            "start_date": START_DATE.isoformat(),
            "initial_capital": str(INITIAL_CAPITAL),
            "last_date": None,
            "last_funding_ms": 0,
            "cash": str(INITIAL_CAPITAL),
            "positions": {},
            "processed_dates": [],
        }
    with STATE_PATH.open() as f:
        return json.load(f)


def book_from_state(state: dict) -> Book:
    book = Book(
        cash=d(state.get("cash", INITIAL_CAPITAL)),
        last_funding_ms=int(state.get("last_funding_ms") or 0),
    )
    for coin, raw in (state.get("positions") or {}).items():
        book.positions[coin] = Position(szi=d(raw["szi"]), entry_px=d(raw["entry_px"]))
    return book


def state_from_book(state: dict, book: Book, last_date: str | None) -> dict:
    state = dict(state)
    state["cash"] = format(book.cash, "f")
    state["last_funding_ms"] = book.last_funding_ms
    state["last_date"] = last_date
    state["positions"] = {
        coin: {"szi": format(p.szi, "f"), "entry_px": format(p.entry_px, "f")}
        for coin, p in book.positions.items()
        if p.szi != 0
    }
    return state


def write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def ensure_csv(path: Path, fields: list[str]) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()


def upsert_csv_row(path: Path, fields: list[str], key: str, row: dict) -> None:
    ensure_csv(path, fields)
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    # tolerate older header subsets
    replaced = False
    out_rows = []
    for existing in rows:
        if existing.get(key) == row[key]:
            out_rows.append(row)
            replaced = True
        else:
            out_rows.append(existing)
    if not replaced:
        out_rows.append(row)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in out_rows:
            writer.writerow({k: r.get(k, "") for k in fields})
    tmp.replace(path)


def append_trade(row: dict) -> None:
    ensure_csv(TRADES_PATH, TRADES_FIELDS)
    # idempotent: skip if an identical date+coin+source+size+price row exists
    with TRADES_PATH.open(newline="") as f:
        existing = list(csv.DictReader(f))
    sig = (row["date_sgt"], row["coin"], row["side"], row["size"], row["price"], row["source"])
    for r in existing:
        if (r.get("date_sgt"), r.get("coin"), r.get("side"), r.get("size"), r.get("price"), r.get("source")) == sig:
            return
    with TRADES_PATH.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADES_FIELDS)
        writer.writerow({k: row.get(k, "") for k in TRADES_FIELDS})


def last_daily_equity() -> Decimal | None:
    if not DAILY_PATH.exists():
        return None
    with DAILY_PATH.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    return d(rows[-1]["virtual_equity"].replace(",", ""))


def replace_readme_summary(text: str) -> None:
    raw = README_PATH.read_text()
    if SUMMARY_START not in raw or SUMMARY_END not in raw:
        raise RuntimeError("README.md missing LEDGER-SUMMARY markers")
    before, rest = raw.split(SUMMARY_START, 1)
    _, after = rest.split(SUMMARY_END, 1)
    README_PATH.write_text(before + SUMMARY_START + "\n" + text.rstrip() + "\n" + SUMMARY_END + after)


def side_label(qty: Decimal) -> str:
    return "buy" if qty > 0 else "sell"


def apply_funding(
    book: Book,
    coins: set[str],
    start_exclusive_ms: int,
    end_inclusive_ms: int,
    fallback_mids: dict[str, Decimal],
) -> Decimal:
    """Apply hourly funding for (start_exclusive_ms, end_inclusive_ms]. Returns net payment to the book."""
    if end_inclusive_ms <= start_exclusive_ms:
        return Decimal("0")
    total = Decimal("0")
    lookback_start = max(0, start_exclusive_ms - 2 * 3600 * 1000)
    for coin in sorted(coins):
        if book.szi(coin) == 0:
            continue
        events = fetch_funding(coin, lookback_start)
        candles = fetch_candles(coin, lookback_start, end_inclusive_ms + 3600_000)
        opens: dict[int, Decimal] = {}
        for c in candles:
            t = int(c["t"])
            hour_ms = t - (t % 3_600_000)
            opens[hour_ms] = d(c["o"])
        for ev in events:
            ts = int(ev["time"])
            if ts <= start_exclusive_ms or ts > end_inclusive_ms:
                continue
            rate = d(ev["fundingRate"])
            hour_ms = ts - (ts % 3_600_000)
            mark = opens.get(hour_ms, fallback_mids.get(coin))
            if mark is None:
                raise ApiError(f"no mark for {coin} funding at {ts}")
            szi = book.szi(coin)
            # positive rate: longs pay shorts; payment to account = -szi * mark * rate
            payment = -szi * mark * rate
            book.cash += payment
            total += payment
            book.last_funding_ms = max(book.last_funding_ms, ts)
    if book.last_funding_ms < start_exclusive_ms:
        # no events, still advance watermark so we do not re-scan forever
        book.last_funding_ms = max(book.last_funding_ms, start_exclusive_ms)
    return total


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def git_commit_and_push(day: date, do_git: bool) -> None:
    if not do_git:
        print("skip git (--no-git)")
        return
    git(
        "add",
        "README.md",
        "trades.csv",
        "daily.csv",
        "state.json",
        "update.py",
        "report_charts.py",
        "ledger_core.py",
        "replay_audit.csv",
        ".gitignore",
        "reports",
        "charts",
        "since-2026-11-26",
    )
    status = git("status", "--porcelain", check=True)
    if not status.stdout.strip():
        # maybe an unpushed commit remains
        unpushed = git("log", "--oneline", "origin/main..HEAD", check=False)
        if unpushed.returncode == 0 and unpushed.stdout.strip():
            push = git("push", "origin", "HEAD:main")
            if push.returncode != 0:
                print(push.stderr, file=sys.stderr)
                raise SystemExit(2)
            print("pushed previously unpushed commit(s)")
        else:
            print("no changes to commit")
        return
    msg = f"ledger {day.isoformat()} SGT"
    commit = git("commit", "-m", msg)
    if commit.returncode != 0:
        print(commit.stdout, commit.stderr, file=sys.stderr)
        raise SystemExit(2)
    print(commit.stdout.strip())
    push = git("push", "origin", "HEAD:main", check=False)
    if push.returncode != 0:
        err = (push.stderr or push.stdout or "git push failed").strip()
        print(err, file=sys.stderr)
        raise SystemExit(2)
    print("pushed", msg)


def build_summary(
    day: date,
    run_at: datetime,
    book: Book,
    mids: dict[str, Decimal],
    v_eq: Decimal,
    v_pos: dict[str, Decimal],
    daily_pnl: Decimal,
    cum_ret: Decimal,
    funding_today: Decimal,
    traded: bool,
) -> str:
    eq = book.equity(mids)
    ntl = book.notional(mids)
    lev = (ntl / eq) if eq != 0 else Decimal("0")
    btc_mid = mids.get("BTC", Decimal("0"))
    lines = [
        f"- 更新时间（新加坡时间）：{run_at.strftime('%Y-%m-%d %H:%M:%S')} SGT",
        f"- 模拟权益：{fmt_money(eq)} USD",
        f"- 累计收益：{q_rate(cum_ret) * 100:.4f}%",
        f"- 当日盈亏：{fmt_money(daily_pnl)} USD",
        f"- 模拟仓位：{format_position_map(book.live_positions())}",
        f"- 模拟杠杆：{fmt_px(q_lev(lev))}x（名义价值 / 模拟权益）",
        f"- 金库权益：{fmt_money(v_eq)} USD",
        f"- 金库仓位：{format_position_map(v_pos)}",
        f"- BTC 中间价：{fmt_px(btc_mid)}",
        f"- 当日是否跟随调仓：{'是' if traded else '否（金库当日无成交，只做盯市和资金费）'}",
        f"- 当日计提资金费（计入现金）：{fmt_money(funding_today)} USD",
    ]
    return "\n".join(lines) + "\n"



def _persist_ledger(result: dict, details: dict, run_at: datetime, last_date: date, mid: Decimal, v_eq, v_pos) -> dict:
    from ledger_core import AUDIT_FIELDS as _AF
    core_write_csv(TRADES_PATH, TRADES_FIELDS, result["trades"])
    core_write_csv(DAILY_PATH, DAILY_FIELDS, result["daily"])
    core_write_csv(AUDIT_PATH, _AF, result["audit"])
    book = result["book"]
    processed = [r["date_sgt"] for r in result["daily"]]
    state = {
        "ledger_version": LEDGER_VERSION,
        "initialized": True,
        "start_date": START_DATE.isoformat(),
        "live_from": LIVE_FROM.isoformat(),
        "initial_capital": str(INITIAL_CAPITAL),
        "last_date": last_date.isoformat(),
        "last_funding_ms": book.last_funding_ms,
        "cash": format(book.cash, "f"),
        "positions": {
            coin: {"szi": format(p.szi, "f"), "entry_px": format(p.entry_px, "f")}
            for coin, p in book.positions.items()
            if p.szi != 0
        },
        "processed_dates": processed,
        "init": result.get("origin") or {},
        "last_run_sgt": run_at.isoformat(),
        "last_vault_equity": format(v_eq, "f") if v_eq is not None else None,
        "last_btc_mid": fmt_px(mid),
        "vault": VAULT,
        "vault_name": (details or {}).get("name"),
    }
    write_json(STATE_PATH, state)
    return state


def process(do_git: bool) -> int:
    run_at = now_sgt()
    today = run_at.date()
    state = load_state()
    needs_rebuild = (
        state.get("ledger_version") != LEDGER_VERSION
        or state.get("start_date") != START_DATE.isoformat()
        or not state.get("initialized")
    )
    processed = list(state.get("processed_dates") or [])

    if today < START_DATE:
        print(f"start date is {START_DATE.isoformat()}; nothing to do")
        return 0

    try:
        ch = fetch_clearinghouse()
        details = fetch_vault_details()
        mids = fetch_mids()
        v_eq = vault_equity(ch)
        v_pos = vault_positions(ch)
        fetch_from = day_start_ms(START_DATE) - 36 * 3600 * 1000
        now_ms = int(run_at.timestamp() * 1000)
        fills = fetch_fills(fetch_from, now_ms + 60_000)
        ledger = fetch_ledger_updates(fetch_from)
        funding = fetch_funding("BTC", fetch_from)
        candles = fetch_candles("BTC", fetch_from, now_ms + 3_600_000)
    except ApiError as exc:
        print(f"API failure, abort without commit: {exc}", file=sys.stderr)
        return 1

    fills_today = [f for f in fills if date_sgt(int(f["time"])) == today]
    if (
        not needs_rebuild
        and today.isoformat() in processed
    ):
        print(f"already processed {today.isoformat()} SGT; will not trade again")
        try:
            from ledger_core import apply_funding
            mid = mids["BTC"]
            book = core_book_from_state(state)
            apply_funding(
                book,
                funding,
                candles,
                int(state.get("last_funding_ms") or 0),
                now_ms,
                mid,
            )
            eq = book.equity({"BTC": mid})
            import csv as _csv
            prev_eq = INITIAL_CAPITAL
            existing_today = None
            if DAILY_PATH.exists():
                with DAILY_PATH.open(newline="", encoding="utf-8") as f:
                    all_daily = list(_csv.DictReader(f))
                prior = [r for r in all_daily if r.get("date_sgt", "") < today.isoformat()]
                todays = [r for r in all_daily if r.get("date_sgt") == today.isoformat()]
                if prior:
                    prev_eq = d(prior[-1]["virtual_equity"].replace(",", ""))
                if todays:
                    existing_today = todays[-1]
            ntl = abs(book.szi("BTC")) * mid
            lev = (ntl / eq) if eq else Decimal("0")
            pos_txt = (
                existing_today["position"]
                if existing_today and existing_today.get("position")
                else (f"BTC:{format(book.szi('BTC'), 'f')}" if book.szi("BTC") != 0 else "FLAT")
            )
            daily_row = {
                "date_sgt": today.isoformat(),
                "virtual_equity": format(q_money(eq), "f"),
                "position": pos_txt,
                "leverage": format(q_lev(lev), "f"),
                "daily_pnl": format(q_money(eq - prev_eq), "f"),
                "cum_return": format(((eq / INITIAL_CAPITAL) - 1).quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP), "f"),
                "vault_equity": format(q_money(v_eq), "f"),
                "vault_position": format_position_map(v_pos),
                "btc_mark": fmt_px(mid),
                "note": NOTE_LIVE,
            }
            upsert_csv_row(DAILY_PATH, DAILY_FIELDS, "date_sgt", daily_row)
            state = dict(state)
            state.update(
                {
                    "last_funding_ms": book.last_funding_ms,
                    "cash": format(book.cash, "f"),
                    "positions": {
                        coin: {"szi": format(p.szi, "f"), "entry_px": format(p.entry_px, "f")}
                        for coin, p in book.positions.items()
                        if p.szi != 0
                    },
                    "last_run_sgt": run_at.isoformat(),
                    "last_vault_equity": format(v_eq, "f"),
                    "last_btc_mid": fmt_px(mid),
                }
            )
            write_json(STATE_PATH, state)
            from report_charts import generate_artifacts
            info = generate_artifacts(today, run_at, state, mids, details)
            print(f"reports refreshed, post_len={info['post_len']}")
        except ApiError as exc:
            print(f"API failure, abort without commit: {exc}", file=sys.stderr)
            return 1
        try:
            git_commit_and_push(today, do_git)
        except SystemExit as e:
            return int(e.code or 2)
        return 0

    if run_at.hour < WAIT_UNTIL_HOUR_SGT and not fills_today and today >= LIVE_FROM and not needs_rebuild:
        print(f"{run_at.strftime('%H:%M')} SGT is before {WAIT_UNTIL_HOUR_SGT:02d}:00 and vault has not traded yet; skip")
        return 0

    mid = mids["BTC"]
    try:
        if needs_rebuild:
            print(f"rebuilding ledger from {START_DATE.isoformat()} (version {LEDGER_VERSION})")
            result = run_ledger(
                fills=fills,
                ledger=ledger,
                funding=funding,
                candles=candles,
                portfolio=details.get("portfolio") or [],
                now_mid=mid,
                now_ms=now_ms,
                through=today,
            )
            # stamp today's vault equity from live clearinghouse if today is live
            if today >= LIVE_FROM and result["daily"]:
                result["daily"][-1]["vault_equity"] = format(q_money(v_eq), "f")
                result["daily"][-1]["vault_position"] = format_position_map(v_pos)
            state = _persist_ledger(result, details, run_at, today, mid, v_eq, v_pos)
            print(f"rebuilt {len(result['trades'])} trades, {len(result['daily'])} daily rows")
        else:
            book = core_book_from_state(state)
            prev_eq = INITIAL_CAPITAL
            if DAILY_PATH.exists():
                import csv as _csv
                with DAILY_PATH.open(newline="", encoding="utf-8") as f:
                    rows = list(_csv.DictReader(f))
                prior = [r for r in rows if r.get("date_sgt", "") < today.isoformat()]
                if prior:
                    prev_eq = d(prior[-1]["virtual_equity"].replace(",", ""))
            one = apply_live_day(
                book,
                today,
                fills,
                ledger,
                funding,
                candles,
                details.get("portfolio") or [],
                mid,
                now_ms,
                prev_eq,
            )
            if one["daily"]["vault_equity"] in ("", "0.00", "0"):
                one["daily"]["vault_equity"] = format(q_money(v_eq), "f")
                one["daily"]["vault_position"] = format_position_map(v_pos)
            for row in one["trades"]:
                append_trade(row)
            upsert_csv_row(DAILY_PATH, DAILY_FIELDS, "date_sgt", one["daily"])
            if one["audit"]:
                # append audit row
                from ledger_core import AUDIT_FIELDS as _AF
                existing = []
                if AUDIT_PATH.exists():
                    import csv as _csv
                    with AUDIT_PATH.open(newline="", encoding="utf-8") as f:
                        existing = [r for r in _csv.DictReader(f) if r.get("date_sgt") != today.isoformat()]
                existing.append(one["audit"])
                core_write_csv(AUDIT_PATH, _AF, existing)
            if today.isoformat() not in processed:
                processed.append(today.isoformat())
            book = one["book"]
            state = dict(state)
            state.update(
                {
                    "ledger_version": LEDGER_VERSION,
                    "initialized": True,
                    "last_date": today.isoformat(),
                    "last_funding_ms": book.last_funding_ms,
                    "cash": format(book.cash, "f"),
                    "positions": {
                        coin: {"szi": format(p.szi, "f"), "entry_px": format(p.entry_px, "f")}
                        for coin, p in book.positions.items()
                        if p.szi != 0
                    },
                    "processed_dates": processed,
                    "last_run_sgt": run_at.isoformat(),
                    "last_vault_equity": format(v_eq, "f"),
                    "last_btc_mid": fmt_px(mid),
                }
            )
            write_json(STATE_PATH, state)
            print(f"live day {today.isoformat()} trades={len(one['trades'])} equity={one['daily']['virtual_equity']}")
    except Exception as exc:
        # do not commit a half-written ledger
        if isinstance(exc, ApiError):
            print(f"API failure, abort without commit: {exc}", file=sys.stderr)
            return 1
        print(f"ledger build failed, abort without commit: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        from report_charts import generate_artifacts
        info = generate_artifacts(today, run_at, state, mids, details)
        print(f"reports written, post_len={info['post_len']}")
    except ApiError as exc:
        print(f"API failure after ledger write, abort without commit: {exc}", file=sys.stderr)
        return 1

    try:
        git_commit_and_push(today, do_git)
    except SystemExit as e:
        return int(e.code or 2)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the +convexity paper ledger")
    parser.add_argument("--no-git", action="store_true", help="update files but do not commit or push")
    args = parser.parse_args()
    try:
        return process(do_git=not args.no_git)
    except ApiError as exc:
        print(f"API failure, abort without commit: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
