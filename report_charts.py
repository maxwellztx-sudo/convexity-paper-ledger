#!/usr/bin/env python3
"""Generate the daily Chinese report, X-post text, and PnL-vs-BTC charts."""

from __future__ import annotations

import csv
import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from update import (
    API,
    DAILY_PATH,
    INITIAL_CAPITAL,
    README_PATH,
    ROOT,
    SGT,
    START_DATE,
    TRADES_PATH,
    VAULT,
    ApiError,
    Book,
    Position,
    api_post,
    d,
    date_sgt,
    day_start_ms,
    fetch_candles,
    fetch_fills,
    fetch_funding,
    fmt_money,
    fmt_px,
    q_lev,
    q_money,
    q_rate,
    replace_readme_summary,
)

REPO_URL = "https://github.com/maxwellztx-sudo/convexity-paper-ledger"
REPORTS_DIR = ROOT / "reports"
CHARTS_DIR = ROOT / "charts"
EQUITY_HISTORY_PATH = REPORTS_DIR / "equity_history.csv"

CJK_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
CJK_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

POST_LIMIT = 270


def ensure_dirs() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def signed_pct(x: Decimal, digits: int = 2) -> str:
    pct = x * 100
    q = Decimal("1").scaleb(-digits)
    v = pct.quantize(q)
    return f"{v:+.{digits}f}%"


def signed_money(x: Decimal) -> str:
    v = q_money(x)
    if v > 0:
        return f"+{v:,}"
    return f"{v:,}"


def trade_qty(row: dict[str, str]) -> Decimal:
    sz = d(row["size"])
    return sz if row["side"].strip().lower() == "buy" else -sz


def action_cn(old: Decimal, qty: Decimal) -> str:
    new = old + qty
    if old == 0 and qty < 0:
        return "开空"
    if old == 0 and qty > 0:
        return "开多"
    if old < 0 and qty < 0:
        return "加空"
    if old < 0 and qty > 0 and new <= 0:
        return "减空"
    if old < 0 and new > 0:
        return "空翻多"
    if old > 0 and qty > 0:
        return "加多"
    if old > 0 and qty < 0 and new >= 0:
        return "减多"
    if old > 0 and new < 0:
        return "多翻空"
    return "调仓"


def default_rebalance_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 9, 45, tzinfo=SGT).timestamp() * 1000)


def first_fill_times(fills: list[dict]) -> dict[date, int]:
    out: dict[date, int] = {}
    for f in fills:
        day = date_sgt(int(f["time"]))
        ts = int(f["time"])
        out[day] = min(out.get(day, ts), ts)
    return out


def attach_trade_times(trades: list[dict[str, str]], fills: list[dict]) -> list[tuple[int, dict[str, str]]]:
    first = first_fill_times(fills)
    events: list[tuple[int, dict[str, str]]] = []
    for row in trades:
        day = date.fromisoformat(row["date_sgt"])
        ts = first.get(day, default_rebalance_ms(day))
        events.append((ts, row))
    events.sort(key=lambda x: x[0])
    return events


def candle_price_fn(candles: list[dict], fallback: Decimal):
    """Return a price lookup: hour-start → open, later in the hour → close if known."""
    by_hour: dict[int, dict] = {}
    for c in candles:
        t = int(c["t"])
        by_hour[t] = c

    def price_at(ts_ms: int) -> Decimal:
        hour = ts_ms - (ts_ms % 3_600_000)
        c = by_hour.get(hour)
        if c is None:
            earlier = [h for h in by_hour if h <= ts_ms]
            if not earlier:
                return fallback
            last = by_hour[max(earlier)]
            return d(last.get("c") or last.get("o"))
        o = d(c["o"])
        cl = d(c.get("c") or c["o"])
        frac = Decimal(ts_ms - hour) / Decimal(3_600_000)
        if frac <= 0:
            return o
        if frac >= 1:
            return cl
        return o + (cl - o) * frac

    return price_at


def reconstruct_hourly(
    trades: list[dict[str, str]],
    fills: list[dict],
    btc_candles: list[dict],
    funding_events: list[dict],
    now_ms: int,
    now_mid: Decimal,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Walk recorded trades + hourly funding/marks.

    The curve starts at the first virtual fill. Both paper and BTC returns
    are zero at that instant (paper is shifted by post-fee equity / 100,000).
    """
    trade_events = attach_trade_times(trades, fills)
    if not trade_events:
        raise ApiError("no recorded virtual trades to anchor the chart")
    origin_ts, origin_row = trade_events[0]
    origin_px = d(origin_row["price"])
    book = Book()
    price_at = candle_price_fn(btc_candles, now_mid)

    times: set[int] = {origin_ts, now_ms}
    for c in btc_candles:
        t = int(c["t"])
        if origin_ts <= t <= now_ms:
            times.add(t)
        end = t + 3_600_000
        if origin_ts <= end <= now_ms:
            times.add(end)
    for ts, _ in trade_events:
        if origin_ts <= ts <= now_ms:
            times.add(ts)
    for ev in funding_events:
        ts = int(ev["time"])
        if origin_ts <= ts <= now_ms:
            times.add(ts)

    pending_trades = list(trade_events)
    pending_fund = sorted(
        [ev for ev in funding_events if origin_ts < int(ev["time"]) <= now_ms],
        key=lambda ev: int(ev["time"]),
    )
    # running position before each trade, for labels
    pos_before: dict[int, Decimal] = {}
    sim_szi = Decimal("0")
    for ts, row in trade_events:
        if row["coin"] == "BTC":
            pos_before[id(row)] = sim_szi
            sim_szi += trade_qty(row)

    curve: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    applied_trade_ids: set[int] = set()
    applied_fund: set[int] = set()

    eq_origin: Decimal | None = None

    def snapshot(ts: int, mid_override: Decimal | None = None) -> dict[str, Any]:
        px = mid_override if mid_override is not None else (now_mid if ts >= now_ms else price_at(ts))
        eq = book.cash
        for coin, p in book.positions.items():
            if p.szi == 0:
                continue
            mark = px if coin == "BTC" else mid_override
            if mark is None:
                continue
            eq += p.szi * (mark - p.entry_px)
        ret = Decimal("0") if eq_origin is None else (eq - eq_origin) / INITIAL_CAPITAL
        btc_ret = (px / origin_px) - 1 if origin_px != 0 else Decimal("0")
        return {
            "ts": ts,
            "dt": datetime.fromtimestamp(ts / 1000, tz=SGT),
            "equity": eq,
            "ret": ret,
            "btc_px": px,
            "btc_ret": btc_ret,
        }

    for ts in sorted(times):
        if ts < origin_ts or ts > now_ms:
            continue
        # apply trades at this timestamp
        for ev_ts, row in pending_trades:
            rid = id(row)
            if rid in applied_trade_ids or ev_ts > ts:
                continue
            qty = trade_qty(row)
            px = d(row["price"])
            fee = d(row["fee"])
            old = book.szi(row["coin"])
            book.apply_trade(row["coin"], qty, px, fee)
            applied_trade_ids.add(rid)
            if eq_origin is None:
                # post-fee, marked at the fill price so unrealized is 0
                eq_origin = book.cash
                for coin, p in book.positions.items():
                    if p.szi == 0:
                        continue
                    mark = px if coin == row["coin"] else price_at(ts)
                    eq_origin += p.szi * (mark - p.entry_px)
            pt = snapshot(ts, px if row["coin"] == "BTC" else None)
            label = action_cn(old, qty)
            marks.append({**pt, "label": label, "qty": qty, "coin": row["coin"], "price": px})
            curve.append(pt)
        # apply funding at this timestamp
        for ev in pending_fund:
            ev_ts = int(ev["time"])
            if ev_ts in applied_fund or ev_ts > ts:
                continue
            if ev_ts <= origin_ts:
                applied_fund.add(ev_ts)
                continue
            coin = str(ev.get("coin") or "BTC")
            if book.szi(coin) == 0:
                applied_fund.add(ev_ts)
                continue
            rate = d(ev["fundingRate"])
            hour = ev_ts - (ev_ts % 3_600_000)
            mark = price_at(hour)
            book.cash += -book.szi(coin) * mark * rate
            applied_fund.add(ev_ts)
        if eq_origin is None:
            continue
        px_override = now_mid if ts == now_ms else None
        pt = snapshot(ts, px_override)
        if curve and curve[-1]["ts"] == ts:
            curve[-1] = pt
        else:
            curve.append(pt)

    origin = {
        "ts": origin_ts,
        "dt": datetime.fromtimestamp(origin_ts / 1000, tz=SGT),
        "px": origin_px,
        "equity": eq_origin,
    }
    return curve, marks, origin


def vault_twr_series(portfolio: list) -> tuple[list[tuple[int, Decimal, Decimal, Decimal]], str]:
    """Return [(ts, twr, cum_pnl, account_value), ...] and a method note."""
    alltime = None
    for item in portfolio or []:
        if isinstance(item, list) and item and item[0] == "allTime" and len(item) > 1:
            alltime = item[1]
            break
    if not isinstance(alltime, dict):
        raise ApiError("vaultDetails portfolio missing allTime")
    av = alltime.get("accountValueHistory") or []
    pnl = alltime.get("pnlHistory") or []
    if not av or not pnl:
        raise ApiError("allTime history is empty")
    pnl_map = {int(t): d(v) for t, v in pnl}
    points: list[tuple[int, Decimal, Decimal, Decimal]] = []
    twr = Decimal("1")
    prev_t = None
    prev_av = None
    prev_pnl = None
    note = (
        "时间加权收益率：相邻公开快照之间，用交易盈亏除以期初权益连乘；"
        "申购赎回视为现金流，不计入收益。快照大约每周一次，为近似值。"
    )
    for t_raw, v_raw in av:
        t = int(t_raw)
        aval = d(v_raw)
        if t not in pnl_map:
            continue
        pval = pnl_map[t]
        if prev_av is None:
            if aval > 0:
                points.append((t, Decimal("0"), pval, aval))
                prev_t, prev_av, prev_pnl = t, aval, pval
            continue
        dp = pval - prev_pnl
        if prev_av > 0:
            twr *= 1 + (dp / prev_av)
        points.append((t, twr - 1, pval, aval))
        prev_t, prev_av, prev_pnl = t, aval, pval
    if not points:
        raise ApiError("could not compute vault TWR")
    return points, note


def fetch_candles_chunked(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch candles in ~90-day chunks so the API does not silently truncate."""
    step = 90 * 86_400_000
    out: list[dict] = []
    seen: set[int] = set()
    t = start_ms
    while t <= end_ms:
        chunk_end = min(end_ms, t + step)
        data = api_post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": t,
                    "endTime": chunk_end,
                },
            }
        )
        if not isinstance(data, list):
            raise ApiError(f"invalid candleSnapshot for {coin} {interval}")
        for c in data:
            ts = int(c["t"])
            if ts in seen:
                continue
            seen.add(ts)
            out.append(c)
        if chunk_end == end_ms:
            break
        t = chunk_end
    out.sort(key=lambda c: int(c["t"]))
    return out


def daily_btc_lookup(candles: list[dict]):
    series = sorted((int(c["t"]), d(c["c"])) for c in candles)

    def price_at(ts_ms: int) -> Decimal | None:
        last = None
        for t, px in series:
            if t <= ts_ms:
                last = px
            else:
                break
        return last

    return price_at, series


def fmt_pos_lines(positions: dict[str, dict], mid: Decimal, equity: Decimal) -> list[str]:
    if not positions:
        return ["当前空仓。"]
    lines = []
    for coin, raw in sorted(positions.items()):
        szi = d(raw["szi"])
        entry = d(raw["entry_px"])
        if szi == 0:
            continue
        direction = "空" if szi < 0 else "多"
        px = mid if coin == "BTC" else mid
        notional = abs(szi) * px
        lev = (notional / equity) if equity != 0 else Decimal("0")
        lines.append(
            f"{coin} {direction} {fmt_px(abs(szi))}，开仓均价 {fmt_px(entry)}，"
            f"名义 {fmt_money(notional)} USD，杠杆 {fmt_px(q_lev(lev))}x"
        )
    return lines or ["当前空仓。"]


def fmt_today_trades(trades: list[dict[str, str]], day: date) -> str:
    today_rows = [r for r in trades if r.get("date_sgt") == day.isoformat()]
    if not today_rows:
        return "今日未调仓"
    # rebuild running size from all trades up to but not including each today trade
    running: dict[str, Decimal] = {}
    earlier = [r for r in trades if r.get("date_sgt", "") < day.isoformat()]
    for r in earlier:
        running[r["coin"]] = running.get(r["coin"], Decimal("0")) + trade_qty(r)
    parts = []
    for r in today_rows:
        coin = r["coin"]
        old = running.get(coin, Decimal("0"))
        qty = trade_qty(r)
        label = action_cn(old, qty)
        running[coin] = old + qty
        parts.append(f"{label} {fmt_px(abs(qty))} {coin} @ {r['price']}")
    return "；".join(parts)


def build_report_md(
    day: date,
    run_at: datetime,
    positions: dict,
    equity: Decimal,
    daily_pnl: Decimal,
    cum_pnl: Decimal,
    cum_ret: Decimal,
    mid: Decimal,
    today_trades: str,
    btc_24h: Decimal,
    btc_since_start: Decimal,
    px_24h: Decimal,
    dt_24h: datetime,
    fill_px: Decimal,
    fill_dt: datetime,
) -> str:
    pos_lines = fmt_pos_lines(positions, mid, equity)
    pos_block = "\n".join(f"- {x}" for x in pos_lines)
    lines = [
        f"# +convexity 模拟账本 {day.isoformat()}",
        "",
        f"日期：{day.isoformat()}（新加坡时间，SGT）",
        f"更新：{run_at.strftime('%Y-%m-%d %H:%M')} SGT（按当时中间价盯市）",
        "",
        "## 持仓",
        pos_block,
        "",
        "## 今日调仓",
        today_trades,
        "",
        "## 盈亏",
        f"- 今日 PnL：{signed_money(daily_pnl)} USD",
        f"- 累计 PnL：{signed_money(cum_pnl)} USD",
        f"- 累计收益率：{signed_pct(cum_ret, 4)}",
        f"- 模拟权益：{fmt_money(equity)} USD",
        "",
        "## BTC",
        f"- 近24h：{fmt_px(mid)}，较 {dt_24h.strftime('%m-%d %H:%M')} SGT 的 {fmt_px(px_24h)} 为 {signed_pct(btc_24h, 2)}",
        f"- 自起始日（首次模拟成交 {fill_dt.strftime('%Y-%m-%d %H:%M')} SGT @ {fmt_px(fill_px)}）：{signed_pct(btc_since_start, 2)}",
        "",
        f"账本：{REPO_URL}",
        f"金库地址：`{VAULT}`（可公开核对）",
        "",
        "免责：此为模拟账户记录，不是投资建议。",
        "",
    ]
    return "\n".join(lines)


def build_post(
    day: date,
    positions: dict,
    equity: Decimal,
    daily_pnl: Decimal,
    cum_pnl: Decimal,
    cum_ret: Decimal,
    mid: Decimal,
    today_trades: str,
    btc_24h: Decimal,
    btc_since_start: Decimal,
) -> str:
    if positions:
        bits = []
        for coin, raw in sorted(positions.items()):
            szi = d(raw["szi"])
            entry = d(raw["entry_px"])
            if szi == 0:
                continue
            direction = "空" if szi < 0 else "多"
            notional = abs(szi) * mid
            lev = (notional / equity) if equity != 0 else Decimal("0")
            bits.append(
                f"{coin}{direction} {fmt_px(abs(szi))}，均价 {fmt_px(entry)}，杠杆 {fmt_px(q_lev(lev))}x"
            )
        pos_txt = "；".join(bits) if bits else "空仓"
    else:
        pos_txt = "空仓"

    btc_line = f"BTC近24h {signed_pct(btc_24h, 2)}（自起始日 {signed_pct(btc_since_start, 2)}）"

    candidates = [
        "\n".join(
            [
                f"+convexity 模拟账本 {day.isoformat()}（SGT）",
                f"持仓：{pos_txt}",
                f"今日调仓：{today_trades}",
                f"今日PnL：{signed_money(daily_pnl)} USD｜累计 {signed_money(cum_pnl)} USD（{signed_pct(cum_ret, 4)}）",
                f"权益：{fmt_money(equity)} USD",
                btc_line,
                f"账本：{REPO_URL}",
                "免责：模拟账户，非投资建议。",
            ]
        ),
        "\n".join(
            [
                f"+convexity 模拟账本 {day.isoformat()}",
                f"持仓：{pos_txt}",
                f"今日调仓：{today_trades}",
                f"今日PnL：{signed_money(daily_pnl)} USD，累计 {signed_pct(cum_ret, 2)}",
                f"权益：{fmt_money(equity)} USD",
                btc_line,
                f"{REPO_URL}",
                "模拟账户，非投资建议。",
            ]
        ),
    ]
    for text in candidates:
        if len(text) <= POST_LIMIT:
            return text
    # last resort: trim the URL line is already short; cut position details
    short = candidates[-1]
    return short[: POST_LIMIT - 1] + "…"


def _font(bold: bool = False):
    from matplotlib import font_manager

    path = CJK_BOLD if bold else CJK_REGULAR
    return font_manager.FontProperties(fname=path)


def _apply_tick_font(ax, fp) -> None:
    for lab in ax.get_xticklabels() + ax.get_yticklabels():
        lab.set_fontproperties(fp)


def plot_paper_vs_btc(
    curve: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    dests: list[Path],
    origin: dict[str, Any],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fp, fpb = _font(False), _font(True)
    fig = plt.figure(figsize=(10.8, 13.5), dpi=100, facecolor="#FAF7F2")
    ax = fig.add_axes([0.14, 0.16, 0.80, 0.68])
    ax.set_facecolor("#FFFdf8")

    xs = [p["dt"] for p in curve]
    y_paper = [float(p["ret"] * 100) for p in curve]
    y_btc = [float(p["btc_ret"] * 100) for p in curve]
    ax.plot(xs, y_paper, color="#1F4E79", lw=2.8, label="模拟账本累计收益")
    ax.plot(xs, y_btc, color="#C47B17", lw=2.8, label="BTC 累计涨跌")

    if marks:
        mx = [m["dt"] for m in marks]
        my = [float(m["ret"] * 100) for m in marks]
        ax.scatter(mx, my, s=90, c="#B3261E", zorder=5, label="调仓时点")
        # annotate last / all if few
        for m in marks:
            ax.annotate(
                m["label"],
                (m["dt"], float(m["ret"] * 100)),
                textcoords="offset points",
                xytext=(8, 10),
                fontproperties=fp,
                fontsize=12,
                color="#B3261E",
            )

    ax.axhline(0, color="#888888", lw=0.8, ls="--")
    ax.set_ylabel("累计涨跌（%）", fontproperties=fp, fontsize=15)
    ax.set_xlabel("新加坡时间", fontproperties=fp, fontsize=15)
    ax.grid(True, color="#E2D9CC", lw=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=SGT))
    if len(xs) <= 2 or (xs[-1] - xs[0]) < timedelta(days=2):
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=SGT))
    fig.autofmt_xdate(rotation=30, ha="right")
    _apply_tick_font(ax, fp)
    leg = ax.legend(loc="upper left", frameon=True, prop=fp, fontsize=13, facecolor="white", edgecolor="#E2D9CC")
    for t in leg.get_texts():
        t.set_fontproperties(fp)

    last = curve[-1]
    fig.text(
        0.50,
        0.945,
        "+convexity 模拟账本 vs BTC",
        ha="center",
        fontproperties=fpb,
        fontsize=26,
        color="#1A1A1A",
    )
    fig.text(
        0.50,
        0.905,
        f"自首次模拟成交 {origin['dt'].strftime('%Y-%m-%d %H:%M')} SGT @ {fmt_px(origin['px'])} 起"
        f"（该时点两条曲线均为 0）· 截至 {last['dt'].strftime('%Y-%m-%d %H:%M')} SGT",
        ha="center",
        fontproperties=fp,
        fontsize=13,
        color="#444444",
    )
    fig.text(
        0.50,
        0.08,
        f"模拟账本 {signed_pct(last['ret'], 3)}    BTC {signed_pct(last['btc_ret'], 2)}",
        ha="center",
        fontproperties=fpb,
        fontsize=16,
        color="#1A1A1A",
    )
    fig.text(
        0.50,
        0.045,
        "模拟账户，非投资建议。收益按小时价格与已记录成交、手续费、资金费重建。",
        ha="center",
        fontproperties=fp,
        fontsize=11,
        color="#666666",
    )
    for path in dests:
        fig.savefig(path, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_vault_history(
    twr_points: list[tuple[int, Decimal, Decimal, Decimal]],
    btc_series: list[tuple[int, Decimal]],
    method_note: str,
    dest: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fp, fpb = _font(False), _font(True)
    fig = plt.figure(figsize=(10.8, 13.5), dpi=100, facecolor="#FAF7F2")
    ax = fig.add_axes([0.14, 0.18, 0.80, 0.61])
    ax.set_facecolor("#FFFdf8")

    t0 = twr_points[0][0]
    # BTC baseline: last daily close at or before first vault point
    btc0 = None
    for t, px in btc_series:
        if t <= t0:
            btc0 = px
        else:
            break
    if btc0 is None and btc_series:
        btc0 = btc_series[0][1]
    if btc0 is None or btc0 == 0:
        raise ApiError("no BTC baseline for vault history chart")

    v_x = [datetime.fromtimestamp(t / 1000, tz=SGT) for t, *_ in twr_points]
    v_y = [float(twr * 100) for _, twr, *_ in twr_points]
    b_x = [datetime.fromtimestamp(t / 1000, tz=SGT) for t, px in btc_series if t >= t0 - 14 * 86_400_000]
    b_y = [float((px / btc0 - 1) * 100) for t, px in btc_series if t >= t0 - 14 * 86_400_000]

    ax.plot(b_x, b_y, color="#C47B17", lw=2.4, label="BTC 累计涨跌")
    ax.plot(v_x, v_y, color="#1F4E79", lw=2.8, marker="o", ms=3.5, label="金库时间加权收益")

    ax.axhline(0, color="#888888", lw=0.8, ls="--")
    ax.set_ylabel("累计涨跌（%）", fontproperties=fp, fontsize=15)
    ax.set_xlabel("新加坡时间", fontproperties=fp, fontsize=15)
    ax.grid(True, color="#E2D9CC", lw=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m", tz=SGT))
    fig.autofmt_xdate(rotation=30, ha="right")
    _apply_tick_font(ax, fp)
    leg = ax.legend(loc="upper left", frameon=True, prop=fp, fontsize=13, facecolor="white", edgecolor="#E2D9CC")
    for t in leg.get_texts():
        t.set_fontproperties(fp)

    last_t, last_twr, last_pnl, last_av = twr_points[-1]
    last_btc = None
    for t, px in btc_series:
        if t <= last_t:
            last_btc = px
    btc_ret = (last_btc / btc0 - 1) if last_btc else Decimal("0")
    start_dt = datetime.fromtimestamp(t0 / 1000, tz=SGT).strftime("%Y-%m-%d")

    fig.text(
        0.50,
        0.955,
        "+convexity 金库自身历史 vs BTC",
        ha="center",
        fontproperties=fpb,
        fontsize=24,
        color="#1A1A1A",
    )
    fig.text(
        0.50,
        0.918,
        f"区间自 {start_dt} 起（首次有权益的公开快照）",
        ha="center",
        fontproperties=fp,
        fontsize=13,
        color="#444444",
    )
    fig.text(
        0.50,
        0.882,
        "此图是金库自身的公开历史，不是本仓库的模拟账本",
        ha="center",
        fontproperties=fpb,
        fontsize=14,
        color="#8B1E1E",
    )
    fig.text(
        0.50,
        0.095,
        f"金库时间加权收益 {signed_pct(last_twr, 1)}    累计交易盈亏 {signed_money(last_pnl)} USD    BTC {signed_pct(btc_ret, 1)}",
        ha="center",
        fontproperties=fpb,
        fontsize=13,
        color="#1A1A1A",
    )
    fig.text(
        0.50,
        0.04,
        method_note + " 非投资建议。",
        ha="center",
        fontproperties=fp,
        fontsize=10,
        color="#666666",
        wrap=True,
    )
    fig.savefig(dest, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)


def previous_equity(daily_rows: list[dict[str, str]], day: date) -> Decimal:
    prior = [r for r in daily_rows if r.get("date_sgt", "") < day.isoformat()]
    if not prior:
        return INITIAL_CAPITAL
    return d(prior[-1]["virtual_equity"].replace(",", ""))


def load_equity_history() -> list[dict[str, str]]:
    return read_csv(EQUITY_HISTORY_PATH)


def previous_report_equity(day: date) -> Decimal:
    """Yesterday's last published report equity; first day baselines at 100,000."""
    prior = [r for r in load_equity_history() if r.get("date_sgt", "") < day.isoformat()]
    if not prior:
        return INITIAL_CAPITAL
    return d(prior[-1]["equity"].replace(",", ""))


def upsert_equity_history(day: date, equity: Decimal, run_at: datetime) -> None:
    ensure_dirs()
    rows = [r for r in load_equity_history() if r.get("date_sgt") != day.isoformat()]
    rows.append(
        {
            "date_sgt": day.isoformat(),
            "equity": format(q_money(equity), "f"),
            "run_at_sgt": run_at.isoformat(),
        }
    )
    rows.sort(key=lambda r: r["date_sgt"])
    tmp = EQUITY_HISTORY_PATH.with_suffix(".csv.tmp")
    import csv as _csv
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=["date_sgt", "equity", "run_at_sgt"])
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(EQUITY_HISTORY_PATH)


def generate_artifacts(
    day: date,
    run_at: datetime,
    state: dict,
    mids: dict[str, Decimal],
    vault_details: dict,
) -> dict[str, Any]:
    """Build reports and charts from the already-recorded ledger. Does not place trades."""
    ensure_dirs()
    trades = read_csv(TRADES_PATH)
    daily_rows = read_csv(DAILY_PATH)
    mid = mids["BTC"]
    now_ms = int(run_at.timestamp() * 1000)
    start_ms = day_start_ms(START_DATE)

    fills = fetch_fills(start_ms, now_ms + 60_000)
    lookback_ms = now_ms - 26 * 3_600_000
    btc_hourly = fetch_candles("BTC", min(start_ms, lookback_ms), now_ms + 3_600_000)
    if not btc_hourly:
        raise ApiError("no BTC hourly candles for the ledger window")
    funding = fetch_funding("BTC", start_ms - 3_600_000)

    trade_events = attach_trade_times(trades, fills)
    if not trade_events:
        raise ApiError("no recorded virtual trades to anchor BTC since-start")
    fill_ts, fill_row = trade_events[0]
    fill_px = d(fill_row["price"])
    fill_dt = datetime.fromtimestamp(fill_ts / 1000, tz=SGT)

    price_at = candle_price_fn(btc_hourly, mid)
    ts_24h = now_ms - 86_400_000
    dt_24h = datetime.fromtimestamp(ts_24h / 1000, tz=SGT)
    px_24h = price_at(ts_24h).quantize(Decimal("0.1"))
    btc_24h = (mid / px_24h) - 1 if px_24h else Decimal("0")
    btc_since = (mid / fill_px) - 1 if fill_px else Decimal("0")

    book = Book()
    book.cash = d(state.get("cash", INITIAL_CAPITAL))
    for coin, raw in (state.get("positions") or {}).items():
        book.positions[coin] = Position(szi=d(raw["szi"]), entry_px=d(raw["entry_px"]))
    equity = book.equity({"BTC": mid, **{c: mid for c in book.positions if c == "BTC"}})
    # use per-coin mids when available
    mark_map = dict(mids)
    equity = book.equity({c: mark_map[c] for c in book.live_positions() if c in mark_map} or {"BTC": mid})

    cum_pnl = equity - INITIAL_CAPITAL
    cum_ret = (equity / INITIAL_CAPITAL) - 1
    daily_pnl = equity - previous_report_equity(day)
    today_trades = fmt_today_trades(trades, day)
    positions = state.get("positions") or {}

    md = build_report_md(
        day,
        run_at,
        positions,
        equity,
        daily_pnl,
        cum_pnl,
        cum_ret,
        mid,
        today_trades,
        btc_24h,
        btc_since,
        px_24h,
        dt_24h,
        fill_px,
        fill_dt,
    )
    post = build_post(
        day, positions, equity, daily_pnl, cum_pnl, cum_ret, mid, today_trades, btc_24h, btc_since
    )
    dated_md = REPORTS_DIR / f"{day.isoformat()}.md"
    latest_md = REPORTS_DIR / "latest.md"
    post_path = REPORTS_DIR / "latest_post.txt"
    dated_md.write_text(md, encoding="utf-8")
    latest_md.write_text(md, encoding="utf-8")
    post_path.write_text(post if post.endswith("\n") else post + "\n", encoding="utf-8")
    upsert_equity_history(day, equity, run_at)

    curve, marks, origin = reconstruct_hourly(trades, fills, btc_hourly, funding, now_ms, mid)
    if not curve:
        raise ApiError("empty paper equity curve")
    paper_png = CHARTS_DIR / "pnl_vs_btc.png"
    dated_png = CHARTS_DIR / f"{day.isoformat()}.png"
    plot_paper_vs_btc(curve, marks, [paper_png, dated_png], origin)

    twr_points, method_note = vault_twr_series(vault_details.get("portfolio") or [])
    first_ts = twr_points[0][0]
    day_ms = 86_400_000
    btc_daily = fetch_candles_chunked("BTC", "1d", first_ts - 14 * day_ms, now_ms + day_ms)
    if not btc_daily:
        raise ApiError("no BTC daily candles for the vault history window")
    _lookup, btc_series = daily_btc_lookup(btc_daily)
    vault_png = CHARTS_DIR / "vault_history_vs_btc.png"
    plot_vault_history(twr_points, btc_series, method_note, vault_png)

    summary = "\n".join(
        [
            f"- 更新时间（新加坡时间）：{run_at.strftime('%Y-%m-%d %H:%M:%S')} SGT",
            f"- 模拟权益：{fmt_money(equity)} USD",
            f"- 累计收益：{signed_pct(cum_ret, 4)}",
            f"- 当日盈亏：{signed_money(daily_pnl)} USD",
            f"- 模拟仓位：{today_trades if False else _pos_short(positions)}",
            f"- BTC 中间价：{fmt_px(mid)}（近24h {signed_pct(btc_24h, 2)}，自起始日 {signed_pct(btc_since, 2)}）",
            f"- 今日调仓：{today_trades}",
            "",
            f"日报全文：[{dated_md.as_posix().split('/')[-1]}](reports/{day.isoformat()}.md) · 可复制文案：[latest_post.txt](reports/latest_post.txt)",
            "",
            f"**模拟账本 vs BTC**（自首次模拟成交 {fill_dt.strftime('%Y-%m-%d %H:%M')} SGT @ {fmt_px(fill_px)} 起，该时点两条曲线为 0；账本刚开始，曲线较短）",
            "",
            "![+convexity 模拟账本 vs BTC](charts/pnl_vs_btc.png)",
            "",
            "**金库自身历史 vs BTC**（不是本模拟账本）",
            "",
            "![+convexity 金库自身历史 vs BTC](charts/vault_history_vs_btc.png)",
            "",
        ]
    )
    replace_readme_summary(summary)

    return {
        "equity": equity,
        "daily_pnl": daily_pnl,
        "cum_ret": cum_ret,
        "post": post,
        "post_len": len(post),
        "md_path": str(latest_md),
        "paper_png": str(paper_png),
        "dated_png": str(dated_png),
        "vault_png": str(vault_png),
        "marks": len(marks),
        "curve_points": len(curve),
    }


def _pos_short(positions: dict) -> str:
    if not positions:
        return "FLAT"
    parts = []
    for coin, raw in sorted(positions.items()):
        szi = d(raw["szi"])
        if szi == 0:
            continue
        parts.append(f"{coin}:{format(szi, 'f')}")
    return " ".join(parts) if parts else "FLAT"
