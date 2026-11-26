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
START_DATE = date(2026, 11, 26)
INITIAL_CAPITAL = Decimal("100000")
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
]

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
    git("add", "README.md", "trades.csv", "daily.csv", "state.json", "update.py", ".gitignore")
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


def process(do_git: bool) -> int:
    run_at = now_sgt()
    today = run_at.date()
    state = load_state()
    processed = list(state.get("processed_dates") or [])

    if today.isoformat() in processed:
        print(f"already processed {today.isoformat()} SGT; will not trade again")
        try:
            git_commit_and_push(today, do_git)
        except SystemExit as e:
            return int(e.code or 2)
        return 0

    try:
        ch = fetch_clearinghouse()
        details = fetch_vault_details()
        mids = fetch_mids()
        sz_decimals = fetch_sz_decimals()
        v_eq = vault_equity(ch)
        v_pos = vault_positions(ch)
        fills_from = day_start_ms(START_DATE)
        fills = fetch_fills(fills_from, int(run_at.timestamp() * 1000) + 60_000)
    except ApiError as exc:
        print(f"API failure, abort without commit: {exc}", file=sys.stderr)
        return 1

    fills_today_by_coin: dict[str, list[dict]] = {}
    for f in fills:
        if date_sgt(int(f["time"])) != today:
            continue
        fills_today_by_coin.setdefault(str(f["coin"]), []).append(f)

    if today < START_DATE:
        print(f"start date is {START_DATE.isoformat()}; nothing to do")
        return 0

    if today == START_DATE and not state.get("initialized") and not fills_today_by_coin:
        print("start date has no vault fills yet; retry after the 09:45 SGT rebalance")
        return 0

    if today > START_DATE and not state.get("initialized"):
        print("state is not initialized; refusing to invent a mid-stream snapshot", file=sys.stderr)
        return 1

    if run_at.hour < WAIT_UNTIL_HOUR_SGT and not fills_today_by_coin:
        print(f"{run_at.strftime('%H:%M')} SGT is before {WAIT_UNTIL_HOUR_SGT:02d}:00 and vault has not traded yet; skip")
        return 0

    if v_eq <= 0:
        print("vault equity is not positive; abort", file=sys.stderr)
        return 1

    book = book_from_state(state)
    funding_today = Decimal("0")

    # Determine the day's rebalance timestamp (first fill, else 09:45 SGT)
    rebalance_ms = int(datetime(today.year, today.month, today.day, 9, 45, tzinfo=SGT).timestamp() * 1000)
    if fills_today_by_coin:
        rebalance_ms = min(int(f["time"]) for fs in fills_today_by_coin.values() for f in fs)

    coins_for_funding = set(book.live_positions()) | set(v_pos) | set(fills_today_by_coin)
    # Funding before today's rebalance, using the pre-trade book
    pre_start = book.last_funding_ms
    if book.last_funding_ms == 0 and not state.get("initialized"):
        # no position yet: do not accrue funding, but set watermark just before open
        book.last_funding_ms = rebalance_ms
    else:
        funding_today += apply_funding(book, coins_for_funding, pre_start, rebalance_ms, mids)

    traded = False
    trade_rows: list[dict] = []

    if not state.get("initialized"):
        # Snap to current vault leverage at today's VWAP, starting from INITIAL_CAPITAL cash.
        # Use pre-trade equity (initial capital; cash is still initial, no position).
        if book.live_positions():
            print("refusing to initialize because a position already exists", file=sys.stderr)
            return 1
        scale = INITIAL_CAPITAL / v_eq
        for coin, vault_szi in v_pos.items():
            if coin not in fills_today_by_coin:
                print(f"init skip {coin}: vault holds it but there is no same-day fill", file=sys.stderr)
                continue
            vwap, _net = vwap_and_net(fills_today_by_coin[coin])
            decimals = sz_decimals.get(coin, 5)
            target = q_size(vault_szi * scale, decimals)
            qty = target  # from flat
            if qty == 0:
                continue
            fee = q_money(abs(qty) * vwap * TAKER_FEE_RATE)
            book.apply_trade(coin, qty, vwap, fee)
            traded = True
            trade_rows.append(
                {
                    "date_sgt": today.isoformat(),
                    "coin": coin,
                    "side": side_label(qty),
                    "size": format(abs(qty), "f"),
                    "price": fmt_px(vwap),
                    "fee": format(fee, "f"),
                    "source": "init_vault_fill_vwap",
                    "vault_fill_vwap": fmt_px(vwap),
                }
            )
        if not traded:
            print("initialization produced no trade; abort", file=sys.stderr)
            return 1
        state["initialized"] = True
        state["init"] = {
            "date_sgt": today.isoformat(),
            "method": "open_at_same_day_vault_vwap_scaled_to_100000_over_current_vault_equity",
            "initial_capital": str(INITIAL_CAPITAL),
            "vault_equity_at_init": format(v_eq, "f"),
            "vault_positions_at_init": {c: format(s, "f") for c, s in v_pos.items()},
            "scale": format(scale, "f"),
            "note": "Virtual entry is today's vault VWAP, not the vault's historical average entry.",
        }
    else:
        # After funding-to-rebalance, mark and size off current equity, then trade coins the vault traded today.
        if fills_today_by_coin:
            eq_pre = book.equity(mids)
            if eq_pre <= 0:
                print("virtual equity is not positive; abort", file=sys.stderr)
                return 1
            scale = eq_pre / v_eq
            # Trade only coins the vault traded today (per spec). Also close a virtual coin
            # if the vault traded it down to flat — covered because v_pos would omit it and
            # target becomes 0 when we still see it in fills.
            coins_to_sync = set(fills_today_by_coin)
            for coin in sorted(coins_to_sync):
                vwap, _net = vwap_and_net(fills_today_by_coin[coin])
                decimals = sz_decimals.get(coin, 5)
                vault_szi = v_pos.get(coin, Decimal("0"))
                target = q_size(vault_szi * scale, decimals)
                current = book.szi(coin)
                qty = q_size(target - current, decimals)
                min_step = Decimal("1").scaleb(-decimals)
                if abs(qty) < min_step:
                    continue
                fee = q_money(abs(qty) * vwap * TAKER_FEE_RATE)
                book.apply_trade(coin, qty, vwap, fee)
                traded = True
                trade_rows.append(
                    {
                        "date_sgt": today.isoformat(),
                        "coin": coin,
                        "side": side_label(qty),
                        "size": format(abs(qty), "f"),
                        "price": fmt_px(vwap),
                        "fee": format(fee, "f"),
                        "source": "vault_fill_vwap",
                        "vault_fill_vwap": fmt_px(vwap),
                    }
                )

    # Funding after rebalance with the updated book, up to now
    coins_for_funding = set(book.live_positions()) | set(v_pos)
    funding_today += apply_funding(book, coins_for_funding, max(book.last_funding_ms, rebalance_ms), int(run_at.timestamp() * 1000), mids)

    eq = book.equity(mids)
    ntl = book.notional(mids)
    lev = (ntl / eq) if eq != 0 else Decimal("0")
    prev_eq = last_daily_equity()
    # If we are writing the first row, baseline is initial capital
    baseline = prev_eq if prev_eq is not None else INITIAL_CAPITAL
    daily_pnl = q_money(eq - baseline)
    cum_ret = (eq / INITIAL_CAPITAL) - 1
    btc_mid = mids["BTC"]

    daily_row = {
        "date_sgt": today.isoformat(),
        "virtual_equity": format(q_money(eq), "f"),
        "position": format_position_map(book.live_positions()),
        "leverage": format(q_lev(lev), "f"),
        "daily_pnl": format(daily_pnl, "f"),
        "cum_return": format(q_rate(cum_ret), "f"),
        "vault_equity": format(q_money(v_eq), "f"),
        "vault_position": format_position_map(v_pos),
        "btc_mark": fmt_px(btc_mid),
    }

    summary = build_summary(today, run_at, book, mids, v_eq, v_pos, daily_pnl, cum_ret, funding_today, traded)

    # Persist only after a complete successful computation
    for row in trade_rows:
        append_trade(row)
    upsert_csv_row(DAILY_PATH, DAILY_FIELDS, "date_sgt", daily_row)
    if today.isoformat() not in processed:
        processed.append(today.isoformat())
    new_state = state_from_book(state, book, today.isoformat())
    new_state["initialized"] = True
    new_state["processed_dates"] = processed
    new_state["last_run_sgt"] = run_at.isoformat()
    new_state["last_vault_equity"] = format(v_eq, "f")
    new_state["last_btc_mid"] = fmt_px(btc_mid)
    new_state["vault"] = VAULT
    new_state["vault_name"] = details.get("name")
    if "init" in state:
        new_state["init"] = state["init"]
    write_json(STATE_PATH, new_state)
    replace_readme_summary(summary)

    print(f"date: {today.isoformat()} SGT")
    print(f"virtual equity: {fmt_money(eq)} USD")
    print(f"position: {daily_row['position']}")
    print(f"leverage: {daily_row['leverage']}x")
    print(f"daily pnl: {daily_row['daily_pnl']}")
    print(f"cum return: {daily_row['cum_return']}")
    print(f"vault equity: {fmt_money(v_eq)} USD")
    print(f"vault position: {daily_row['vault_position']}")
    print(f"btc mid: {fmt_px(btc_mid)}")
    print(f"funding today: {fmt_money(funding_today)} USD")
    print(f"traded: {traded} rows={len(trade_rows)}")

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
