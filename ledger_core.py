#!/usr/bin/env python3
"""Week-replay ledger core: start 2026-11-19, strip flow-driven sizing, then go live.

Pure computation. Callers fetch public API data and pass it in.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")

START_DATE = date(2026, 11, 19)
LIVE_FROM = date(2026, 11, 26)
LEDGER_VERSION = "week_replay_2026-11-19"
INITIAL_CAPITAL = Decimal("100000")
TAKER_FEE_RATE = Decimal("0.00045")
NOTE_REPLAY = "回溯重放，非实时"
NOTE_LIVE = "实时"

# Documented init price: all 11-19 vault fills were at this price (09:45–09:46 SGT).
INIT_PRICE_HINT = Decimal("62752")


def d(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def q_money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def q_size(x: Decimal, sz_decimals: int = 5) -> Decimal:
    return x.quantize(Decimal("1").scaleb(-sz_decimals), rounding=ROUND_HALF_UP)


def q_lev(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def fmt_px(x: Decimal) -> str:
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_money(x: Decimal) -> str:
    return f"{q_money(x):,}"


def regime_note(day: date) -> str:
    return NOTE_LIVE if day >= LIVE_FROM else NOTE_REPLAY


def signed_fill(fill: dict) -> Decimal:
    sz = d(fill["sz"])
    side = str(fill["side"]).upper()
    if side in ("A", "SELL"):
        return -sz
    if side in ("B", "BUY"):
        return sz
    raise ValueError(f"unknown fill side {fill.get('side')}")


def flow_amount(entry: dict) -> Decimal:
    delta = entry.get("delta") or {}
    typ = delta.get("type")
    if typ == "vaultDeposit":
        return d(delta["usdc"])
    if typ == "vaultWithdraw":
        return -d(delta["netWithdrawnUsd"])
    return Decimal("0")


def flows_between(ledger: list[dict], t0_ms: int, t1_ms: int) -> Decimal:
    total = Decimal("0")
    for item in ledger:
        ts = int(item["time"])
        if t0_ms < ts <= t1_ms:
            total += flow_amount(item)
    return total


def px_at(candles: list[dict], ts_ms: int, fallback: Decimal | None = None) -> Decimal:
    if not candles:
        if fallback is None:
            raise ValueError("no candles")
        return fallback
    times = [int(c["t"]) for c in candles]
    # linear search is fine for ~200 bars; keep deterministic
    chosen = None
    for c in candles:
        if int(c["t"]) <= ts_ms:
            chosen = c
        else:
            break
    if chosen is None:
        chosen = candles[0]
    hour = int(chosen["t"])
    o = d(chosen["o"])
    cl = d(chosen.get("c") or chosen["o"])
    frac = (Decimal(ts_ms - hour) / Decimal(3_600_000)) if ts_ms >= hour else Decimal("0")
    if frac <= 0:
        return o
    if frac >= 1:
        return cl
    return o + (cl - o) * frac


def aggregate_vault_days(fills: list[dict]) -> list[dict]:
    by_day: dict[date, list[dict]] = {}
    for fill in fills:
        day = datetime.fromtimestamp(int(fill["time"]) / 1000, tz=SGT).date()
        by_day.setdefault(day, []).append(fill)
    out = []
    for day in sorted(by_day):
        fs = sorted(by_day[day], key=lambda x: int(x["time"]))
        start_pos = d(fs[0]["startPosition"])
        net = sum((signed_fill(f) for f in fs), Decimal("0"))
        vol = sum((d(f["sz"]) for f in fs), Decimal("0"))
        notional = sum((d(f["sz"]) * d(f["px"]) for f in fs), Decimal("0"))
        vwap = notional / vol if vol else Decimal("0")
        out.append(
            {
                "day": day,
                "start_pos": start_pos,
                "end_pos": start_pos + net,
                "net": net,
                "vwap": vwap,
                "fee": sum((d(f.get("fee") or 0) for f in fs), Decimal("0")),
                "t0": int(fs[0]["time"]),
                "t1": int(fs[-1]["time"]),
                "fills": fs,
            }
        )
    return out


def av_series_from_portfolio(portfolio: list) -> list[tuple[int, Decimal]]:
    by_t: dict[int, Decimal] = {}
    for item in portfolio or []:
        if not (isinstance(item, list) and len(item) > 1 and isinstance(item[1], dict)):
            continue
        for t, v in item[1].get("accountValueHistory") or []:
            by_t[int(t)] = d(v)
    return sorted(by_t.items())


def estimate_e_pre(
    day_row: dict,
    av_series: list[tuple[int, Decimal]],
    candles: list[dict],
    ledger: list[dict],
    funding: list[dict],
) -> tuple[Decimal, int, Decimal]:
    """Walk the last public AV snapshot before the rebalance to t0 with mid MTM, flows and funding."""
    t0 = day_row["t0"]
    prior = [(t, v) for t, v in av_series if t < t0]
    if not prior:
        raise ValueError(f"no equity snapshot before {day_row['day']}")
    t_a, e_a = prior[-1]
    szi = day_row["start_pos"]
    mid_a = px_at(candles, t_a)
    mid_t = px_at(candles, t0, fallback=day_row["vwap"])
    fl = flows_between(ledger, t_a, t0)
    fu = funding_payments_vault(funding, t_a, t0)
    e_pre = e_a + szi * (mid_t - mid_a) + fl + fu
    return e_pre, t_a, e_a


def funding_payments_vault(funding: list[dict], t0_ms: int, t1_ms: int) -> Decimal:
    total = Decimal("0")
    for ev in funding:
        ts = int(ev["time"])
        if t0_ms < ts <= t1_ms:
            delta = ev.get("delta") or ev
            if "usdc" in delta:
                total += d(delta["usdc"])
    return total


def window_start_ms(day: date) -> int:
    t = datetime(day.year, day.month, day.day, 9, 45, tzinfo=SGT) - timedelta(days=1)
    return int(t.timestamp() * 1000)


def split_strategy(day_row: dict, e_pre: Decimal, net_flow: Decimal) -> dict:
    """Strip the deposit/withdrawal scale component from the day's vault trade.

    Rule (written down for the public README):
    - F is net vault inflow from the previous 09:45 SGT to this rebalance
      (deposits +usdc, withdrawals −netWithdrawnUsd).
    - E_organic = E_pre − F, i.e. estimated pre-trade equity without those flows.
    - scale_delta = S_before × F / E_organic  (the size change that only restores
      leverage after AUM change). If F=0, scale_delta=0.
    - If E_organic ≤ 0, skip the split and treat the whole trade as strategy.
    - S_strategy = S_after − scale_delta
    - Virtual target = S_strategy × (virtual_equity / E_organic)
    """
    s_minus = day_row["start_pos"]
    s_plus = day_row["end_pos"]
    fallback = False
    e_org = e_pre - net_flow
    if e_org <= 0:
        e_org = e_pre
        scale_delta = Decimal("0")
        fallback = True
    else:
        scale_delta = s_minus * net_flow / e_org
    s_strategy = s_plus - scale_delta
    return {
        "net_flow": net_flow,
        "e_pre": e_pre,
        "e_organic": e_org,
        "scale_delta": scale_delta,
        "s_strategy": s_strategy,
        "fallback": fallback,
    }


@dataclass
class Position:
    szi: Decimal = Decimal("0")
    entry_px: Decimal = Decimal("0")


@dataclass
class Book:
    cash: Decimal = INITIAL_CAPITAL
    positions: dict[str, Position] = field(default_factory=dict)
    last_funding_ms: int = 0

    def szi(self, coin: str = "BTC") -> Decimal:
        p = self.positions.get(coin)
        return p.szi if p else Decimal("0")

    def entry(self, coin: str = "BTC") -> Decimal:
        p = self.positions.get(coin)
        return p.entry_px if p else Decimal("0")

    def live_positions(self) -> dict[str, Decimal]:
        return {c: p.szi for c, p in self.positions.items() if p.szi != 0}

    def equity(self, mids: dict[str, Decimal]) -> Decimal:
        total = self.cash
        for coin, p in self.positions.items():
            if p.szi == 0:
                continue
            if coin not in mids:
                raise ValueError(f"missing mid for {coin}")
            total += p.szi * (mids[coin] - p.entry_px)
        return total

    def apply_trade(self, coin: str, qty: Decimal, px: Decimal, fee: Decimal) -> Decimal:
        self.cash -= fee
        pos = self.positions.get(coin) or Position()
        old = pos.szi
        new = old + qty
        realized = Decimal("0")
        if old == 0 or (qty != 0 and (old > 0) == (qty > 0)):
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


def apply_funding(
    book: Book,
    funding: list[dict],
    candles: list[dict],
    start_exclusive_ms: int,
    end_inclusive_ms: int,
    fallback_mid: Decimal,
) -> Decimal:
    if end_inclusive_ms <= start_exclusive_ms or book.szi("BTC") == 0:
        book.last_funding_ms = max(book.last_funding_ms, start_exclusive_ms)
        return Decimal("0")
    total = Decimal("0")
    last_ts = book.last_funding_ms
    for ev in funding:
        ts = int(ev["time"])
        if ts <= start_exclusive_ms or ts > end_inclusive_ms:
            continue
        delta = ev.get("delta") or ev
        if str(delta.get("coin") or ev.get("coin") or "BTC") != "BTC":
            continue
        rate = d(delta["fundingRate"])
        hour = ts - (ts % 3_600_000)
        mark = px_at(candles, hour, fallback=fallback_mid)
        pay = -book.szi("BTC") * mark * rate
        book.cash += pay
        total += pay
        last_ts = max(last_ts, ts)
    book.last_funding_ms = max(book.last_funding_ms, last_ts, start_exclusive_ms)
    return total


def _trade_row(day: date, qty: Decimal, px: Decimal, fee: Decimal, source: str, vwap: Decimal) -> dict:
    return {
        "date_sgt": day.isoformat(),
        "coin": "BTC",
        "side": "buy" if qty > 0 else "sell",
        "size": format(abs(qty), "f"),
        "price": fmt_px(px),
        "fee": format(fee, "f"),
        "source": source,
        "vault_fill_vwap": fmt_px(vwap),
        "note": regime_note(day),
    }


def run_ledger(
    fills: list[dict],
    ledger: list[dict],
    funding: list[dict],
    candles: list[dict],
    portfolio: list,
    now_mid: Decimal,
    now_ms: int,
    through: date | None = None,
) -> dict:
    """Build the virtual ledger from START_DATE through `through` (inclusive)."""
    through = through or date.today()
    days = [row for row in aggregate_vault_days(fills) if START_DATE <= row["day"] <= through]
    if not days or days[0]["day"] != START_DATE:
        raise ValueError("missing 2026-11-19 vault fills; cannot initialize")
    av_series = av_series_from_portfolio(portfolio)
    book = Book()
    trades: list[dict] = []
    daily_rows: list[dict] = []
    audit: list[dict] = []
    prev_eq = INITIAL_CAPITAL
    last_event_ms = 0
    origin = {}

    for i, row in enumerate(days):
        day = row["day"]
        px = row["vwap"]
        e_pre, t_a, e_a = estimate_e_pre(row, av_series, candles, ledger, funding)
        net_flow = flows_between(ledger, window_start_ms(day), row["t0"])
        split = split_strategy(row, e_pre, net_flow)
        note = regime_note(day)

        if last_event_ms:
            apply_funding(book, funding, candles, last_event_ms, row["t0"], px)

        if not origin:
            # Snapshot the then vault position onto 100k at the 11-19 fill price.
            qty0 = q_size(row["start_pos"] * INITIAL_CAPITAL / e_pre)
            fee0 = q_money(abs(qty0) * px * TAKER_FEE_RATE)
            book.apply_trade("BTC", qty0, px, fee0)
            trades.append(_trade_row(day, qty0, px, fee0, "replay_init", px))
            origin = {
                "date_sgt": day.isoformat(),
                "time_sgt": datetime.fromtimestamp(row["t0"] / 1000, tz=SGT).isoformat(),
                "vault_szi_before": format(row["start_pos"], "f"),
                "vault_equity_pre": format(e_pre, "f"),
                "equity_anchor_ms": t_a,
                "equity_anchor": format(e_a, "f"),
                "price": fmt_px(px),
                "virtual_szi": format(qty0, "f"),
                "fee": format(fee0, "f"),
                "method": "pre_rebalance_vault_position_scaled_to_100000_over_E_pre_at_11-19_vwap_62752",
                "note": NOTE_REPLAY,
            }

        eq_pre = book.equity({"BTC": px})
        e_org = split["e_organic"]
        target = q_size(split["s_strategy"] * eq_pre / e_org)
        qty = q_size(target - book.szi("BTC"))
        fee = q_money(abs(qty) * px * TAKER_FEE_RATE) if qty != 0 else Decimal("0")
        if qty != 0:
            src = "live_strategy" if day >= LIVE_FROM else "replay_strategy"
            book.apply_trade("BTC", qty, px, fee)
            trades.append(_trade_row(day, qty, px, fee, src, px))

        mark_px = now_mid if (day == through and day >= LIVE_FROM) else px
        if day == through and day >= LIVE_FROM:
            apply_funding(book, funding, candles, row["t1"], now_ms, now_mid)
        eq = book.equity({"BTC": mark_px})
        ntl = abs(book.szi("BTC")) * mark_px
        lev = (ntl / eq) if eq else Decimal("0")
        daily_rows.append(
            {
                "date_sgt": day.isoformat(),
                "virtual_equity": format(q_money(eq), "f"),
                "position": f"BTC:{format(book.szi('BTC'), 'f')}",
                "leverage": format(q_lev(lev), "f"),
                "daily_pnl": format(q_money(eq - prev_eq), "f"),
                "cum_return": format(((eq / INITIAL_CAPITAL) - 1).quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP), "f"),
                "vault_equity": format(q_money(e_pre if day < LIVE_FROM else e_pre), "f"),
                "vault_position": f"BTC:{format(row['end_pos'], 'f')}",
                "btc_mark": fmt_px(mark_px),
                "note": note,
            }
        )
        audit.append(
            {
                "date_sgt": day.isoformat(),
                "note": note,
                "vault_szi_before": format(row["start_pos"], "f"),
                "vault_szi_after": format(row["end_pos"], "f"),
                "vault_net": format(row["net"], "f"),
                "vwap": fmt_px(px),
                "net_flow": format(q_money(net_flow), "f"),
                "e_pre": format(q_money(e_pre), "f"),
                "e_organic": format(q_money(e_org), "f"),
                "scale_delta": format(split["scale_delta"], "f"),
                "s_strategy": format(split["s_strategy"], "f"),
                "virtual_szi": format(book.szi("BTC"), "f"),
                "virtual_equity": format(q_money(eq), "f"),
                "split_fallback": "1" if split["fallback"] else "0",
            }
        )
        prev_eq = eq
        last_event_ms = row["t1"]
        book.last_funding_ms = max(book.last_funding_ms, row["t1"])

    return {
        "book": book,
        "trades": trades,
        "daily": daily_rows,
        "audit": audit,
        "origin": origin,
        "last_event_ms": last_event_ms,
    }


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})
    tmp.replace(path)


AUDIT_FIELDS = [
    "date_sgt",
    "note",
    "vault_szi_before",
    "vault_szi_after",
    "vault_net",
    "vwap",
    "net_flow",
    "e_pre",
    "e_organic",
    "scale_delta",
    "s_strategy",
    "virtual_szi",
    "virtual_equity",
    "split_fallback",
]


def book_from_state(state: dict) -> Book:
    book = Book(cash=d(state.get("cash", INITIAL_CAPITAL)), last_funding_ms=int(state.get("last_funding_ms") or 0))
    for coin, raw in (state.get("positions") or {}).items():
        book.positions[coin] = Position(szi=d(raw["szi"]), entry_px=d(raw["entry_px"]))
    return book


def apply_live_day(
    book: Book,
    day: date,
    fills: list[dict],
    ledger: list[dict],
    funding: list[dict],
    candles: list[dict],
    portfolio: list,
    now_mid: Decimal,
    now_ms: int,
    prev_equity: Decimal,
) -> dict:
    """Apply one live day onto an existing book. No trade if the vault did not trade."""
    days = [r for r in aggregate_vault_days(fills) if r["day"] == day]
    av_series = av_series_from_portfolio(portfolio)
    new_trades: list[dict] = []
    audit = None
    if days:
        row = days[0]
        apply_funding(book, funding, candles, book.last_funding_ms, row["t0"], row["vwap"])
        e_pre, t_a, e_a = estimate_e_pre(row, av_series, candles, ledger, funding)
        net_flow = flows_between(ledger, window_start_ms(day), row["t0"])
        split = split_strategy(row, e_pre, net_flow)
        px = row["vwap"]
        eq_pre = book.equity({"BTC": px})
        target = q_size(split["s_strategy"] * eq_pre / split["e_organic"])
        qty = q_size(target - book.szi("BTC"))
        fee = q_money(abs(qty) * px * TAKER_FEE_RATE) if qty != 0 else Decimal("0")
        if qty != 0:
            book.apply_trade("BTC", qty, px, fee)
            new_trades.append(_trade_row(day, qty, px, fee, "live_strategy", px))
        apply_funding(book, funding, candles, row["t1"], now_ms, now_mid)
        vault_pos = f"BTC:{format(row['end_pos'], 'f')}"
        vault_eq = q_money(e_pre)
        audit = {
            "date_sgt": day.isoformat(),
            "note": NOTE_LIVE,
            "vault_szi_before": format(row["start_pos"], "f"),
            "vault_szi_after": format(row["end_pos"], "f"),
            "vault_net": format(row["net"], "f"),
            "vwap": fmt_px(px),
            "net_flow": format(q_money(net_flow), "f"),
            "e_pre": format(q_money(e_pre), "f"),
            "e_organic": format(q_money(split["e_organic"]), "f"),
            "scale_delta": format(split["scale_delta"], "f"),
            "s_strategy": format(split["s_strategy"], "f"),
            "virtual_szi": format(book.szi("BTC"), "f"),
            "virtual_equity": format(q_money(book.equity({"BTC": now_mid})), "f"),
            "split_fallback": "1" if split["fallback"] else "0",
        }
    else:
        apply_funding(book, funding, candles, book.last_funding_ms, now_ms, now_mid)
        vault_pos = "FLAT" if book.szi("BTC") == 0 else ""
        vault_eq = Decimal("0")
        # leave vault columns to the caller if it has clearinghouse
    eq = book.equity({"BTC": now_mid})
    ntl = abs(book.szi("BTC")) * now_mid
    lev = (ntl / eq) if eq else Decimal("0")
    daily_row = {
        "date_sgt": day.isoformat(),
        "virtual_equity": format(q_money(eq), "f"),
        "position": f"BTC:{format(book.szi('BTC'), 'f')}" if book.szi("BTC") != 0 else "FLAT",
        "leverage": format(q_lev(lev), "f"),
        "daily_pnl": format(q_money(eq - prev_equity), "f"),
        "cum_return": format(((eq / INITIAL_CAPITAL) - 1).quantize(Decimal("0.0000001"), rounding=ROUND_HALF_UP), "f"),
        "vault_equity": format(vault_eq, "f") if vault_eq else "",
        "vault_position": vault_pos,
        "btc_mark": fmt_px(now_mid),
        "note": NOTE_LIVE,
    }
    book.last_funding_ms = max(book.last_funding_ms, now_ms)
    return {"trades": new_trades, "daily": daily_row, "audit": audit, "book": book}
