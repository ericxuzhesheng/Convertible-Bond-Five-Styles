"""Five weekly convertible-bond styles. One Python file; SQLite is the data store."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
STYLES = {"equity": "偏股型", "bond": "偏债型", "low_price": "低价",
          "double_low": "双低", "balanced": "平衡型"}
FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount,bond_value,bond_over_rate,cb_value,cb_over_rate"


def ymd(value):
    return pd.Timestamp(value).strftime("%Y%m%d")


def date_value(value):
    return None if pd.isna(value) or str(value) in ("", "None") else ymd(value)


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, source TEXT, fetched_at TEXT, payload TEXT)")
    return db


def cached(db, source, params, fetch, offline=False):
    key = source + ":" + json.dumps(params, sort_keys=True, ensure_ascii=True)
    row = db.execute("SELECT payload FROM cache WHERE key=?", (key,)).fetchone()
    if row:
        return pd.DataFrame(json.loads(row[0])["rows"], columns=json.loads(row[0])["columns"])
    if offline:
        raise ValueError(f"Offline cache missing: {key}")
    # API/auth/schema errors propagate. Only a complete response enters the cache.
    from requests.exceptions import ConnectionError, Timeout, ChunkedEncodingError
    for attempt in range(3):
        try:
            frame = fetch(**params)
            break
        except (ConnectionError, Timeout, ChunkedEncodingError):
            if attempt == 2:
                raise
            print(f"Transport interrupted: {source}; retry {attempt + 1}/2", flush=True)
            time.sleep(2 ** attempt)
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{source}: expected DataFrame")
    payload = {"columns": frame.columns.tolist(), "rows": json.loads(frame.to_json(orient="records", date_format="iso"))}
    with db:
        db.execute("INSERT INTO cache VALUES (?,?,?,?)", (key, source, datetime.now().astimezone().isoformat(), json.dumps(payload)))
    time.sleep(0.35)
    return frame


def paged(db, pro, api, params, offline=False):
    pages, offset, previous = [], 0, None
    while True:
        page = cached(db, "tushare." + api, {**params, "limit": 2000, "offset": offset},
                      lambda **kw: pro.query(api, **{k: v for k, v in kw.items() if not k.startswith("_")}), offline)
        if page.empty:
            break
        signature = page.to_json(orient="records")
        if signature == previous:
            raise ValueError(f"{api}: server repeated a page; pagination cannot be trusted")
        pages.append(page)
        if len(page) < 2000:
            break
        previous, offset = signature, offset + len(page)
    return pd.concat(pages, ignore_index=True).drop_duplicates() if pages else pd.DataFrame(columns=params["fields"].split(","))


def unique(frame, keys, name):
    if frame.duplicated(keys).any():
        raise ValueError(f"Conflicting duplicate keys in {name}: {keys}")


def sync(db, start, end, offline=False, reuse_repo=None):
    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN")
    if not offline and not token:
        raise ValueError("Set TUSHARE_TOKEN in your environment; never put it in source control")
    pro = None if offline else ts.pro_api(token, timeout=40)
    basic = paged(db, pro, "cb_basic", {"_snapshot": end, "fields": "ts_code,bond_short_name,cb_type,list_date,delist_date,issue_size"}, offline)
    unique(basic, ["ts_code"], "bonds")
    basic.to_sql("bonds", db, if_exists="replace", index=False)
    warmup = ymd(pd.Timestamp(start) - pd.Timedelta(days=20))
    calendar = paged(db, pro, "trade_cal", {"exchange": "SSE", "start_date": warmup,
                     "end_date": end, "fields": "cal_date,is_open"}, offline)
    calendar = calendar[calendar.is_open == 1].sort_values("cal_date")
    if calendar.empty or not (calendar.cal_date < start).any():
        raise ValueError("Calendar lacks the prior trading session")
    first = calendar.loc[calendar.cal_date < start, "cal_date"].iloc[-1]
    calendar = calendar[calendar.cal_date >= first]
    calendar.to_sql("calendar", db, if_exists="replace", index=False)
    bars = []
    for month in pd.period_range(pd.Timestamp(first), pd.Timestamp(end), freq="M"):
        left, right = max(first, ymd(month.start_time)), min(end, ymd(month.end_time))
        frame = paged(db, pro, "cb_daily", {"start_date": left, "end_date": right, "fields": FIELDS}, offline)
        expected = set(calendar.loc[calendar.cal_date.between(left, right), "cal_date"])
        counts = frame.groupby("trade_date").size()
        incomplete = expected - set(counts.index)
        if not counts.empty:
            incomplete |= set(counts[counts < counts.median() * .8].index)
        if incomplete:
            # An empty offset response is not proof that the requested period is complete.
            # Requery the entire suspect month by date, including any partially returned session.
            daily_parts = [paged(db, pro, "cb_daily", {"trade_date": d, "fields": FIELDS}, offline)
                           for d in sorted(expected)]
            frame = pd.concat(daily_parts, ignore_index=True)
            if set(frame.trade_date) != expected:
                raise ValueError(f"Incomplete daily repair: {left}..{right}")
        bars.append(frame)
        print(f"Daily {left}..{right}: {len(frame):,} rows", flush=True)
    bars = pd.concat(bars, ignore_index=True)
    if reuse_repo:
        daily_dir = Path(reuse_repo) / "backtest/strategy_inputs/daily"
        keys = bars.groupby("trade_date").ts_code.agg(set).to_dict()
        repaired = []
        for file in sorted(daily_dir.glob("*.csv")):
            date = file.stem
            if first <= date <= end:
                observed = set(pd.read_csv(file, usecols=["ts_code"]).ts_code)
                if observed - keys.get(date, set()):
                    fresh = paged(db, pro, "cb_daily", {"trade_date": date, "fields": FIELDS}, offline)
                    if fresh.empty:
                        raise ValueError(f"Empty daily cross-check repair: {date}")
                    repaired.append(fresh)
        if repaired:
            replacements = pd.concat(repaired, ignore_index=True)
            bars = pd.concat([bars[~bars.trade_date.isin(replacements.trade_date)], replacements], ignore_index=True)
            print(f"Cross-repository coverage check refreshed {len(repaired)} daily partitions", flush=True)
    unique(bars, ["ts_code", "trade_date"], "bars")
    numeric = FIELDS.split(",")[2:]
    bars[numeric] = bars[numeric].apply(pd.to_numeric, errors="raise")
    if np.isinf(bars[numeric].to_numpy(dtype=float)).any():
        raise ValueError("Infinite numeric market input")
    absent = sorted(set(calendar.cal_date) - set(bars.trade_date))
    if absent:
        raise ValueError(f"Missing entire trading sessions: {absent[:10]}")
    if not set(bars.ts_code).issubset(set(basic.ts_code)):
        raise ValueError("Market history contains bonds absent from cb_basic")
    bars.to_sql("bars", db, if_exists="replace", index=False)
    db.execute("CREATE UNIQUE INDEX bars_pk ON bars(trade_date,ts_code)")

    # Latest published balance strictly BEFORE a signal date. Never use today's remaining size.
    parts = []
    balance_start = pd.Timestamp(first) - pd.Timedelta(days=400)
    for year in range(balance_start.year, pd.Timestamp(end).year + 1):
        left, right = max(ymd(balance_start), f"{year}0101"), min(end, f"{year}1231")
        part = paged(db, pro, "cb_share", {"start_date": left, "end_date": right,
                      "fields": "ts_code,publish_date,end_date,remain_size"}, offline)
        parts.append(part)
        print(f"Balances {left}..{right}: {len(part):,} rows", flush=True)
    balances = pd.concat(parts, ignore_index=True).drop_duplicates()
    unique(balances, ["ts_code", "publish_date", "end_date"], "balances")
    balances.to_sql("balances", db, if_exists="replace", index=False)
    db.execute("CREATE INDEX balances_idx ON balances(ts_code,publish_date,end_date)")
    db.execute("CREATE TABLE IF NOT EXISTS redemptions (ts_code TEXT PRIMARY KEY, ann_date TEXT, record_date TEXT, payment_date TEXT, price REAL, source TEXT)")
    if reuse_repo:
        path = Path(reuse_repo) / "backtest/strategy_inputs/source_checks/eastmoney_all_basic.csv"
        records = pd.read_csv(path, dtype={"SECURITY_CODE": str})
        for row in records.to_dict("records"):
            event = parse_redemption(row)
            if event:
                put_redemption(db, event, "reused Eastmoney snapshot: " + str(path))
        print(f"Reused redemption snapshot: {path.name}", flush=True)
    pd.DataFrame([{"start": start, "end": end}]).to_sql("sync_state", db, if_exists="replace", index=False)
    db.commit()


def parse_redemption(row):
    # Eastmoney SH = redemption, HS = put-back. Do not confuse the two cash flows.
    required = ["NOTICE_DATE_SH", "RECORD_DATE_SH", "EXECUTE_START_DATESH", "EXECUTE_PRICE_SH"]
    if not all(pd.notna(row.get(k)) for k in required):
        return None
    ann, record, pay = [date_value(row[k]) for k in required[:3]]
    price = float(row[required[3]])
    # A final payment notice may be published after registration; availability still uses ann_date.
    if not (ann <= pay and record <= pay and np.isfinite(price) and price > 0):
        raise ValueError(f"Invalid redemption dates/price: {row.get('SECUCODE')}")
    return (row["SECUCODE"], ann, record, pay, price)


def put_redemption(db, event, source):
    with db:
        db.execute("INSERT OR REPLACE INTO redemptions VALUES (?,?,?,?,?,?)", (*event, source))


def ensure_redemption(db, code, offline):
    row = db.execute("SELECT ann_date,record_date,payment_date,price FROM redemptions WHERE ts_code=?", (code,)).fetchone()
    if row:
        return row
    import akshare as ak
    frame = cached(db, "akshare.bond_cb_info_em", {"symbol": code.split(".")[0], "indicator": "基本信息"},
                   ak.bond_cb_info_em, offline)
    if len(frame) != 1:
        raise ValueError(f"Unexpected redemption response for {code}")
    event = parse_redemption(frame.iloc[0].to_dict())
    if event:
        put_redemption(db, event, f"https://data.eastmoney.com/kzz/detail/{code[:6]}.html")
        return event[1:]
    return None


FACTOR_SQL = """
CREATE VIEW factors AS
SELECT d.*, b.bond_short_name,
       d.cb_over_rate AS equity, d.bond_over_rate AS bond,
       d.close AS low_price, d.close+d.cb_over_rate AS double_low,
       abs(d.cb_value / NULLIF(d.bond_value,0)-1) AS balanced
FROM bars d JOIN bonds b USING(ts_code)
WHERE b.cb_type='CB' AND d.close>0 AND d.vol>0
  AND b.list_date <= d.trade_date
  AND (b.delist_date IS NULL OR b.delist_date='' OR b.delist_date>d.trade_date);
"""


def risk_available(announcement, effective):
    return max(ymd(pd.Timestamp(announcement) + pd.Timedelta(days=1)), effective)


def prepare_risk(db, end, offline):
    import tushare as ts
    pro = None if offline else ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=30)
    mapping = paged(db, pro, "cb_basic", {"_snapshot": end, "fields": "ts_code,stk_code"}, offline)
    mapping.to_sql("bond_stock_map", db, if_exists="replace", index=False)
    names = paged(db, pro, "namechange", {"start_date": "19900101", "end_date": end,
                  "fields": "ts_code,name,start_date,end_date,ann_date,change_reason"}, offline)
    names = names.rename(columns={"ts_code": "stk_code"}).merge(mapping, on="stk_code")
    names = names.dropna(subset=["ann_date", "start_date"])
    names["effective_date"] = names.start_date
    names["risk_type"] = "stock_name"
    names["blocked"] = names.name.str.contains("ST|退", case=False, regex=True).astype(int)
    events = pd.read_csv(ROOT / "risk_events.csv", dtype={"ann_date": str, "effective_date": str})
    events = pd.concat([events[events.risk_type != "stock_name"], names], ignore_index=True)
    events["available"] = events.apply(lambda r: risk_available(r.ann_date, r.effective_date), axis=1)
    events = events.sort_values(["available", "ann_date", "effective_date"], kind="stable")
    dates = pd.read_sql_query("SELECT cal_date FROM calendar ORDER BY cal_date", db).cal_date
    state, rows, cursor = {}, [], 0
    records = events.to_dict("records")
    for date in dates:
        while cursor < len(records) and records[cursor]["available"] <= date:
            event = records[cursor]
            state[(event["ts_code"], event["risk_type"])] = int(event["blocked"])
            cursor += 1
        unknown = set(mapping.ts_code) - {code for code, kind in state if kind == "stock_name"}
        rows.extend((date, code) for code in sorted(unknown | {k[0] for k, v in state.items() if v}))
    frame = pd.DataFrame(rows, columns=["trade_date", "ts_code"])
    frame.to_sql("risk_blocked", db, if_exists="replace", index=False)
    db.execute("CREATE UNIQUE INDEX risk_blocked_pk ON risk_blocked(trade_date,ts_code)")
    return {date: set(group.ts_code) for date, group in frame.groupby("trade_date")}


def apply_quote_overrides(db):
    path = ROOT / "quote_overrides.csv"
    if not path.exists():
        return
    quotes = pd.read_csv(path, dtype={"trade_date": str})
    unique(quotes, ["ts_code", "trade_date"], "quote overrides")
    quotes.to_sql("quote_overrides", db, if_exists="replace", index=False)
    for row in quotes.to_dict("records"):
        if not (0 < row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"] and row["vol"] > 0):
            raise ValueError("Invalid supplemental quote")
        existing = db.execute("SELECT close FROM bars WHERE ts_code=? AND trade_date=?", (row["ts_code"],row["trade_date"])).fetchone()
        if existing:
            if abs(existing[0]-row["close"]) > .01:
                raise ValueError("Conflicting supplemental quote; review provider revision")
            continue
        db.execute("INSERT INTO bars(ts_code,trade_date,open,high,low,close,vol) VALUES (?,?,?,?,?,?,?)",
                   tuple(row[k] for k in ["ts_code","trade_date","open","high","low","close","vol"]))
    db.commit()


def confirmed_halt(db, code, date, offline):
    # Since the 2022 CB trading rules, an underlying full-day suspension also suspends its CB.
    # This explains a close mark only; it never supplies an execution price.
    if date < "20220801":
        return False
    stock = db.execute("SELECT stk_code FROM bond_stock_map WHERE ts_code=?", (code,)).fetchone()
    if not stock:
        raise ValueError(f"Missing underlying mapping: {code}")
    import tushare as ts
    pro = None if offline else ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=30)
    rows = cached(db, "tushare.suspend_d", {"ts_code": stock[0], "trade_date": date},
                  lambda **kw: pro.suspend_d(**kw), offline)
    return not rows.empty and bool(((rows.suspend_type == "S") & rows.suspend_timing.isna()).any())


def build_signals(db, start, end, fraction, risk=False):
    db.execute("DROP VIEW IF EXISTS factors")
    db.executescript(FACTOR_SQL)
    dates = pd.read_sql_query("SELECT cal_date FROM calendar ORDER BY cal_date", db).cal_date.tolist()
    schedule = []
    for i, date in enumerate(dates):
        if i and start <= date <= end:
            previous = dates[i - 1]
            # Initial entry uses the immediately prior session; later entries are weekly only.
            if not schedule or pd.Timestamp(date).to_period("W-FRI") != pd.Timestamp(previous).to_period("W-FRI"):
                schedule.append((previous, date))
    selections = []
    for signal, execution in schedule:
        for style in STYLES:
            risk_clause = "AND close>=90 AND ts_code NOT IN (SELECT ts_code FROM risk_blocked WHERE trade_date=?)" if risk else ""
            frame = pd.read_sql_query(f"""
                SELECT ts_code,bond_short_name,{style} AS factor,
                       ROW_NUMBER() OVER(ORDER BY {style},ts_code) AS rank,
                       COUNT(*) OVER() AS universe_count
                FROM factors WHERE trade_date=? AND {style} IS NOT NULL
                  AND (? != 'balanced' OR (bond_value>0 AND cb_value>0))
                  {risk_clause}
                ORDER BY rank""", db, params=(signal, style, execution) if risk else (signal, style))
            if frame.empty:
                raise ValueError(f"Empty factor universe: {signal} {style}")
            n = max(1, round(len(frame) * fraction))
            chosen = frame.head(n).copy()
            chosen["signal_date"], chosen["execution_date"], chosen["style"] = signal, execution, style
            chosen["target_weight"] = 1 / n
            selections.append(chosen)
    if not selections:
        raise ValueError("No executable trading sessions in the requested interval")
    result = pd.concat(selections, ignore_index=True)
    result.to_sql("signals", db, if_exists="replace", index=False)
    return result


def fit_curve(db):
    import statsmodels.api as sm
    data = pd.read_sql_query("""
        SELECT f.trade_date,f.ts_code,f.cb_value,f.cb_over_rate,
          (SELECT s.remain_size FROM balances s WHERE s.ts_code=f.ts_code
             AND s.publish_date<f.trade_date AND s.end_date<=f.trade_date
             ORDER BY s.publish_date DESC,s.end_date DESC LIMIT 1) AS remain_size
        FROM factors f WHERE cb_value>50 AND cb_value<130 AND cb_over_rate IS NOT NULL
        ORDER BY trade_date,ts_code""", db)
    rows = []
    for date, frame in data.groupby("trade_date", sort=True):
        sample = frame[frame.remain_size > 1e8]
        row = {"trade_date": date, "candidate_n": len(frame), "balance_missing_n": int(frame.remain_size.isna().sum()),
               "sample_n": len(sample), "a": None, "b": None, "premium_at_100": None, "status": "insufficient_sample"}
        if len(sample) >= 10 and sample.cb_value.nunique() >= 2:
            design = sm.add_constant(1 / sample.cb_value, has_constant="add")
            fit = sm.RLM(sample.cb_over_rate, design, M=sm.robust.norms.HuberT()).fit(maxiter=200, tol=1e-8)
            if fit.fit_history["iteration"] >= 200 or not np.isfinite(fit.params).all():
                raise ValueError(f"Curve fit failed to converge: {date}")
            a, b = fit.params.iloc[0], fit.params.iloc[1]
            row.update(a=float(a), b=float(b), premium_at_100=float(a + b / 100), status="ok")
        rows.append(row)
    curve = pd.DataFrame(rows)
    curve.to_sql("curve", db, if_exists="replace", index=False)
    return curve


def rebalance(positions, cash, opens, targets, cost):
    """Equal notional among available target slots, frozen untradeable holdings untouched."""
    frozen = {c: q for c, q in positions.items() if c not in opens}
    liquid_value = cash + sum(q * opens[c] for c, q in positions.items() if c in opens)
    active_targets = [c for c in targets if c in opens and c not in frozen]
    # Retain failed target slots in cash: no substitution based on next-session outcomes.
    slots = len(targets)
    def desired(budget):
        return {c: budget / slots / opens[c] for c in active_targets}
    def turnover(new):
        return sum(abs(new.get(c, 0) - positions.get(c, 0)) * opens[c]
                   for c in set(new) | (set(positions) & set(opens)))
    lo, hi = 0., liquid_value
    for _ in range(60):
        mid = (lo + hi) / 2
        if mid + cost * turnover(desired(mid)) > liquid_value:
            hi = mid
        else:
            lo = mid
    target = desired(lo)
    fees = cost * turnover(target)
    cash_after = liquid_value - sum(q * opens[c] for c, q in target.items()) - fees
    if cash_after < -1e-9 or not np.isfinite(cash_after):
        raise ValueError("Invalid cash accounting")
    trades = [(c, target.get(c, 0) - positions.get(c, 0), opens[c])
              for c in sorted(set(target) | (set(positions) & set(opens)))
              if abs(target.get(c, 0) - positions.get(c, 0)) > 1e-14]
    return {**frozen, **target}, max(0., cash_after), fees, trades


def backtest(db, signals, start, end, cost, offline, risk_blocks=None):
    verified_file = ROOT / "verified_redemptions.csv"
    if verified_file.exists():
        for event in pd.read_csv(verified_file, dtype=str).to_dict("records"):
            ann, record, pay = [ymd(event[k]) for k in ["ann_date", "record_date", "payment_date"]]
            price = float(event["price"])
            if not (ann <= pay and record <= pay and np.isfinite(price) and price > 0):
                raise ValueError(f"Invalid verified event: {event['ts_code']}")
            put_redemption(db, (event["ts_code"], ann, record, pay, price), event["source"])
    bars = pd.read_sql_query("SELECT * FROM bars ORDER BY trade_date,ts_code", db)
    by_date = {d: f.set_index("ts_code") for d, f in bars.groupby("trade_date")}
    previous_closes = bars.sort_values(["ts_code", "trade_date"]).groupby("ts_code").close.shift()
    price_risk = bars.assign(previous_close=previous_closes)
    price_risk = {d: set(f.ts_code) for d, f in price_risk[price_risk.previous_close < 90].groupby("trade_date")}
    dates = pd.read_sql_query("SELECT cal_date FROM calendar WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date", db,
                              params=(start, end)).cal_date
    nav_rows, trade_rows, issue_rows, holding_rows = [], [], [], []
    for style in STYLES:
        positions, marks, cash, receivables = {}, {}, 1., []
        tainted = False
        for date in dates:
            day = by_date[date]
            # Cash becomes available only on the provider's payment date (next market session if closed).
            due = [r for r in receivables if r[0] <= date]
            cash += sum(r[1] for r in due)
            receivables = [r for r in receivables if r[0] > date]
            fees = 0.
            pending_codes = set()
            for code in list(positions):
                traded = code in day.index and pd.notna(day.at[code, "open"]) and day.at[code, "open"] > 0 and day.at[code, "vol"] > 0
                if not traded:
                    event = ensure_redemption(db, code, offline)
                    if event and event[0] < date and event[1] < date:
                        _, _, payment, price = event
                        amount = positions.pop(code) * price
                        if payment <= date:
                            cash += amount
                        else:
                            receivables.append((payment, amount))
                        trade_rows.append((date, style, code, 0., price, 0., "redemption"))
                        marks.pop(code, None)
                        continue
                    mark_present = code in day.index and pd.notna(day.at[code, "close"]) and day.at[code, "close"] > 0
                    if not mark_present:
                        # Announced redemption creates a known non-trading interval before registration.
                        pending = event and event[0] < date <= event[1] and (pd.Timestamp(event[1])-pd.Timestamp(date)).days <= 10
                        halted = risk_blocks is not None and confirmed_halt(db, code, date, offline)
                        if pending or halted:
                            pending_codes.add(code)
                            issue_rows.append((date, style, code, "confirmed_halt_stale_mark" if halted else "redemption_pending_stale_mark"))
                        else:
                            tainted = True
                            issue_rows.append((date, style, code, "missing_quote_unresolved"))
                    else:
                        issue_rows.append((date, style, code, "nontrading_provider_mark"))
            tradable = day[(day.open > 0) & (day.vol > 0) & day.open.notna()]
            opens = tradable.open.to_dict()
            blocked = set() if risk_blocks is None else risk_blocks.get(date, set()) | price_risk.get(date, set())
            for code in list(positions):
                if code in blocked:
                    if code in opens:
                        quantity = positions.pop(code)
                        fee = quantity * opens[code] * cost
                        cash += quantity * opens[code] - fee
                        fees += fee
                        trade_rows.append((date, style, code, -quantity, opens[code], fee, "risk_exit"))
                        marks.pop(code, None)
                    else:
                        issue_rows.append((date, style, code, "risk_exit_frozen"))
            selected = signals[(signals["style"] == style) & (signals.execution_date == date)]
            if not selected.empty:
                targets = selected.ts_code.tolist()
                opens = {c: price for c, price in opens.items() if c not in blocked}
                for code in targets:
                    if code not in opens:
                        issue_rows.append((date, style, code, "entry_unfilled_cash_slot"))
                positions, cash, rebalance_fees, trades = rebalance(positions, cash, opens, targets, cost)
                fees += rebalance_fees
                for code, quantity, price in trades:
                    trade_rows.append((date, style, code, quantity, price, abs(quantity) * price * cost, "weekly_open"))
                    marks.setdefault(code, price)
            for code, quantity in positions.items():
                if code in day.index and pd.notna(day.at[code, "close"]) and day.at[code, "close"] > 0:
                    marks[code] = day.at[code, "close"]
                else:
                    if code not in pending_codes:
                        tainted = True
                        issue_rows.append((date, style, code, "missing_close_unresolved"))
                holding_rows.append((date, style, code, quantity, marks[code], quantity * marks[code]))
            value = cash + sum(receipt[1] for receipt in receivables) + sum(q * marks[c] for c, q in positions.items())
            if value <= 0 or not np.isfinite(value):
                raise ValueError(f"Invalid NAV: {style} {date}")
            nav_rows.append((date, style, value, cash, fees, len(positions), int(not tainted)))
        print(f"Backtest {style}: {len(dates)} sessions; valid={not tainted}", flush=True)
    tables = {
        "nav": pd.DataFrame(nav_rows, columns=["trade_date", "style", "nav", "cash", "fees", "holdings", "valid"]),
        "trades": pd.DataFrame(trade_rows, columns=["trade_date", "style", "ts_code", "quantity_change", "price", "fee", "reason"]),
        "issues": pd.DataFrame(issue_rows, columns=["trade_date", "style", "ts_code", "issue"]).drop_duplicates(),
        "holdings": pd.DataFrame(holding_rows, columns=["trade_date", "style", "ts_code", "quantity", "mark", "value"])}
    for table, frame in tables.items():
        frame.to_sql(table, db, if_exists="replace", index=False)
    return tables


def crosscheck(db, code, end, offline=False):
    import akshare as ak
    frame = cached(db, "akshare.bond_cb_value_analysis_em", {"symbol": code.split(".")[0], "through": end},
                   lambda symbol, through: ak.bond_cb_value_analysis_em(symbol=symbol), offline)
    frame = frame.rename(columns={"日期": "trade_date", "收盘价": "close", "纯债价值": "bond_value",
                                  "转股价值": "cb_value", "转股溢价率": "cb_over_rate", "纯债溢价率": "bond_over_rate"})
    frame["trade_date"] = frame.trade_date.map(ymd)
    other = pd.read_sql_query("SELECT trade_date,close,bond_value,cb_value,cb_over_rate,bond_over_rate FROM bars WHERE ts_code=?", db, params=(code,))
    paired = other.merge(frame, on="trade_date", suffixes=("_ts", "_ak"), validate="one_to_one")
    if paired.empty:
        raise ValueError(f"No overlapping AKShare dates: {code}")
    rows = []
    for field in ["close", "bond_value", "cb_value", "cb_over_rate", "bond_over_rate"]:
        error = (paired[field + "_ts"] - paired[field + "_ak"]).abs()
        rows.append({"ts_code": code, "field": field, "paired_n": int(error.notna().sum()),
                     "median_abs_diff": float(error.median()), "max_abs_diff": float(error.max())})
    result = pd.DataFrame(rows)
    result.to_sql("crosscheck", db, if_exists="replace", index=False)
    return result


def export(db, out, args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out.mkdir(parents=True, exist_ok=True)
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in ["signals", "curve", "nav", "trades", "issues", "crosscheck"]:
        if table in names:
            pd.read_sql_query(f"SELECT * FROM {table}", db).to_csv(out / (table + ".csv"), index=False, encoding="utf-8-sig")
    nav = pd.read_sql_query("SELECT * FROM nav ORDER BY trade_date,style", db)
    metrics, annual = [], []
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    for style, frame in nav.groupby("style"):
        values = frame.nav.to_numpy()
        returns = np.diff(np.r_[1., values]) / np.r_[1., values[:-1]]
        drawdown = values / np.maximum.accumulate(np.r_[1., values])[1:] - 1
        valid = bool(frame.valid.all())
        known = frame[frame.valid == 1]
        row = {"style": style, "start": frame.trade_date.iloc[0], "end": frame.trade_date.iloc[-1], "sessions": len(frame), "valid": valid,
               "valid_through": known.trade_date.iloc[-1] if not known.empty else None,
               "first_unresolved_date": frame.loc[frame.valid == 0, "trade_date"].iloc[0] if not valid else None}
        row.update(total_return=values[-1] - 1 if valid else None,
                   annual_return=values[-1] ** (252 / len(frame)) - 1 if valid else None,
                   annual_volatility=np.std(returns, ddof=1) * np.sqrt(252) if valid and len(frame) > 1 else None,
                   max_drawdown=float(drawdown.min()) if valid else None)
        metrics.append(row)
        frame = frame.assign(daily_return=returns, year=frame.trade_date.str[:4])
        for year, group in frame.groupby("year"):
            annual.append({"year": year, "style": style, "price_return": float((1 + group.daily_return).prod() - 1) if group.valid.all() else None})
        dates = pd.to_datetime(frame.trade_date)
        axes[0].plot(dates, np.where(frame.valid, values, np.nan), label=style)
        axes[1].plot(dates, np.where(frame.valid, drawdown, np.nan))
    axes[0].set_title("Weekly CB styles | price return + identified redemptions | no regular coupons")
    axes[0].set_ylabel("NAV"); axes[0].legend(ncol=3)
    axes[1].set_ylabel("Drawdown")
    for axis in axes:
        axis.grid(alpha=.25)
    fig.tight_layout(); fig.savefig(out / "weekly_styles.png", dpi=160); plt.close(fig)
    curve = pd.read_sql_query("SELECT * FROM curve ORDER BY trade_date", db)
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(pd.to_datetime(curve.trade_date), curve.premium_at_100)
    ax.set(title="Market conversion premium at parity 100 | Huber fit", ylabel="Premium (percentage points)")
    ax.grid(alpha=.25)
    fig.tight_layout(); fig.savefig(out / "premium_at_100.png", dpi=160); plt.close(fig)
    pd.DataFrame(metrics).to_csv(out / "metrics.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(annual).to_csv(out / "annual_returns.csv", index=False, encoding="utf-8-sig")
    coverage = pd.read_sql_query("""SELECT substr(trade_date,1,4) AS year,COUNT(DISTINCT trade_date) AS sessions,
        COUNT(DISTINCT ts_code) AS bonds,COUNT(*) AS rows,
        SUM(bond_value IS NOT NULL AND bond_value>0)*1.0/COUNT(*) AS bond_value_coverage,
        SUM(cb_value IS NOT NULL AND cb_value>0)*1.0/COUNT(*) AS parity_coverage
        FROM bars GROUP BY year""", db)
    coverage.to_csv(out / "coverage.csv", index=False, encoding="utf-8-sig")
    manifest = {"generated_at": datetime.now().astimezone().isoformat(), "start": args.start, "end": args.end,
                "frequency": "weekly", "signal": "prior session close", "execution": "next week first session open",
                "one_way_cost_bps": args.cost_bps, "fraction": args.fraction,
                "risk_policy": {"price_floor": 90, "stock_ST_or_delisting": "exclude and exit", "unknown_name_history": "exclude",
                                "redemption_and_trading_termination": "exclude and exit after announcement", "announcement_lag": "next calendar day, executed on next trading session",
                                "risk_exit_frequency": "daily", "normal_rebalance": "weekly"},
                "limitations": ["Regular coupons excluded; not total return", "Daily data cannot guarantee open auction fills or capacity",
                                "Provider history can be revised; no original-release vintage guarantee",
                                "Unresolved held marks invalidate subsequent performance; inspect issues.csv"],
                "sources": [{"source": r[0], "cache_entries": r[1], "last_fetched_at": r[2]} for r in
                            db.execute("SELECT source,count(*),max(fetched_at) FROM cache GROUP BY source")]}
    (out / "run.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False), flush=True)


def self_test():
    import unittest
    class Contracts(unittest.TestCase):
        def test_risk_information_delay(self):
            self.assertEqual(risk_available('20251013','20251013'),'20251014')
            self.assertEqual(risk_available('20251010','20251020'),'20251020')
        def test_fee_and_fixed_quantity(self):
            pos, cash, fee, _ = rebalance({}, 1., {"A": 100.}, ["A"], .001)
            self.assertAlmostEqual(pos["A"], 1 / 1.001 / 100)
            self.assertAlmostEqual(cash + pos["A"] * 100 + fee, 1.)
            self.assertAlmostEqual(cash + pos["A"] * 110, 1.1 / 1.001)
        def test_failed_entry_cash(self):
            pos, cash, _, _ = rebalance({}, 1., {"A": 100.}, ["A", "B"], 0.)
            self.assertAlmostEqual(cash, .5)
            self.assertAlmostEqual(pos["A"] * 100, .5)
        def test_locked_sale(self):
            pos, cash, _, _ = rebalance({"A": .005}, .5, {"B": 100.}, ["B"], 0.)
            self.assertAlmostEqual(pos["A"], .005)
            self.assertAlmostEqual(pos["B"], .005)
        def test_formula_and_lag(self):
            db = sqlite3.connect(":memory:")
            dates = ["20251009", "20251010", "20251013", "20251014", "20251017", "20251020"]
            pd.DataFrame({"cal_date": dates}).to_sql("calendar", db, index=False)
            pd.DataFrame([{"ts_code": c, "bond_short_name": c, "cb_type": "CB", "list_date": "20200101", "delist_date": None}
                          for c in ["A", "B"]]).to_sql("bonds", db, index=False)
            rows = [{"trade_date": d, "ts_code": c, "close": 120., "vol": 1., "cb_value": 100., "bond_value": 80.,
                     "cb_over_rate": 20. if c == "A" else 30., "bond_over_rate": 50.} for d in dates for c in ["A", "B"]]
            pd.DataFrame(rows).to_sql("bars", db, index=False)
            sig = build_signals(db, "20251013", "20251020", .1)
            self.assertEqual(set(sig.execution_date), {"20251013", "20251020"})
            self.assertTrue((sig.signal_date < sig.execution_date).all())
            self.assertEqual(db.execute("SELECT double_low,balanced FROM factors WHERE ts_code='A' LIMIT 1").fetchone(), (140., .25))
            # Future factor perturbation cannot affect an earlier selection.
            db.execute("UPDATE bars SET cb_over_rate=-999 WHERE trade_date>'20251010' AND ts_code='B'")
            changed = build_signals(db, "20251013", "20251020", .1)
            pd.testing.assert_frame_equal(sig[sig.execution_date == "20251013"], changed[changed.execution_date == "20251013"])
        def test_redemption_not_putback(self):
            self.assertIsNone(parse_redemption({"NOTICE_DATE_HS": "20250101", "EXECUTE_PRICE_HS": 100}))
        def test_duplicate_is_error(self):
            with self.assertRaises(ValueError):
                unique(pd.DataFrame({"code": ["A", "A"]}), ["code"], "test")
        def test_weekly_accounting_and_redemption(self):
            db = sqlite3.connect(":memory:")
            dates = ["20251013", "20251014", "20251015"]
            pd.DataFrame({"cal_date": dates}).to_sql("calendar", db, index=False)
            # Two names rise differently; Tuesday must retain Monday's quantities.
            rows = [{"trade_date": d, "ts_code": c, "open": o, "close": p, "vol": v}
                    for d, c, o, p, v in [
                        (dates[0], "A", 100., 100., 1), (dates[0], "B", 100., 100., 1),
                        (dates[1], "A", 100., 120., 1), (dates[1], "B", 100., 100., 1),
                        (dates[2], "A", 0., 0., 0), (dates[2], "B", 100., 100., 1)]]
            pd.DataFrame(rows).to_sql("bars", db, index=False)
            db.execute("CREATE TABLE redemptions (ts_code TEXT,ann_date TEXT,record_date TEXT,payment_date TEXT,price REAL,source TEXT)")
            db.execute("INSERT INTO redemptions VALUES ('A','20251010','20251014','20251015',105,'test')")
            sig = pd.DataFrame([{"style": style, "execution_date": dates[0], "ts_code": c}
                                for style in STYLES for c in ["A", "B"]])
            result = backtest(db, sig, dates[0], dates[-1], 0., True)
            nav = result["nav"].query("style == 'equity'")
            np.testing.assert_allclose(nav.nav, [1., 1.1, 1.025])
            self.assertTrue(nav.valid.all())
            self.assertEqual(set(result["trades"].query("reason == 'weekly_open'").trade_date), {dates[0]})
        def test_price_risk_exits_next_open(self):
            db = sqlite3.connect(":memory:")
            dates = ["20251013", "20251014"]
            pd.DataFrame({"cal_date": dates}).to_sql("calendar", db, index=False)
            pd.DataFrame([{"trade_date": dates[0], "ts_code": "A", "open": 100., "close": 80., "vol": 1},
                          {"trade_date": dates[1], "ts_code": "A", "open": 70., "close": 90., "vol": 1}]).to_sql("bars", db, index=False)
            db.execute("CREATE TABLE redemptions (ts_code TEXT PRIMARY KEY,ann_date TEXT,record_date TEXT,payment_date TEXT,price REAL,source TEXT)")
            sig = pd.DataFrame([{"style": style, "execution_date": dates[0], "ts_code": "A"} for style in STYLES])
            result = backtest(db, sig, dates[0], dates[-1], 0., True, {})
            frame = result["nav"].query("style == 'equity'")
            np.testing.assert_allclose(frame.nav, [.8, .7])
            exits = result["trades"].query("reason == 'risk_exit'")
            self.assertEqual(set(exits.trade_date), {dates[1]})
            self.assertTrue((exits.price == 70.).all())
        def test_risk_exit_waits_for_trading(self):
            db = sqlite3.connect(":memory:")
            dates = ['20251013','20251014','20251015']
            pd.DataFrame({'cal_date':dates}).to_sql('calendar',db,index=False)
            pd.DataFrame([{'trade_date':d,'ts_code':'A','open':o,'close':p,'vol':v}
                          for d,o,p,v in [(dates[0],100.,100.,1),(dates[1],0.,100.,0),(dates[2],80.,85.,1)]]).to_sql('bars',db,index=False)
            db.execute('CREATE TABLE redemptions (ts_code TEXT PRIMARY KEY,ann_date TEXT,record_date TEXT,payment_date TEXT,price REAL,source TEXT)')
            db.execute("INSERT INTO redemptions VALUES ('A','20300101','20300102','20300103',100,'future')")
            sig = pd.DataFrame([{'style':style,'execution_date':dates[0],'ts_code':'A'} for style in STYLES])
            result = backtest(db,sig,dates[0],dates[-1],0.,True,{dates[1]:{'A'},dates[2]:{'A'}})
            exits = result['trades'].query("reason == 'risk_exit'")
            self.assertEqual(set(exits.trade_date),{dates[2]})
            self.assertTrue((exits.price == 80.).all())
            self.assertEqual(len(result['issues'].query("issue == 'risk_exit_frozen'")),5)
        def test_risk_filter_precedes_ranking(self):
            db = sqlite3.connect(":memory:")
            pd.DataFrame({"cal_date": ["20251010", "20251013"]}).to_sql("calendar", db, index=False)
            pd.DataFrame([{"ts_code": c,"bond_short_name": c,"cb_type":"CB","list_date":"20200101","delist_date":None}
                          for c in ["A","B"]]).to_sql("bonds", db, index=False)
            pd.DataFrame([{"trade_date":"20251010","ts_code":c,"close":100.,"vol":1.,"cb_value":100.,"bond_value":90.,
                           "cb_over_rate":1. if c=='A' else 2.,"bond_over_rate":10.} for c in ['A','B']]).to_sql('bars',db,index=False)
            pd.DataFrame([{"trade_date":"20251013","ts_code":"A"}]).to_sql('risk_blocked',db,index=False)
            sig = build_signals(db,'20251013','20251013',.1,True)
            self.assertEqual(set(sig.ts_code), {'B'})
            self.assertTrue((sig.universe_count == 1).all())
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Contracts))
    if not result.wasSuccessful():
        raise SystemExit(1)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "sync", "research", "self-test"])
    parser.add_argument("--start", default="20190101")
    parser.add_argument("--end", default=ymd(pd.Timestamp(datetime.now(ZoneInfo("Asia/Shanghai")).date()) - pd.Timedelta(days=1)))
    parser.add_argument("--db", type=Path, default=ROOT / "data/research.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "results")
    parser.add_argument("--fraction", type=float, default=.1)
    parser.add_argument("--cost-bps", type=float, default=5.)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--reuse-repo", type=Path)
    parser.add_argument("--check-code", default="113052.SH")
    args = parser.parse_args()
    if args.command == "self-test":
        self_test(); return
    args.start, args.end = ymd(args.start), ymd(args.end)
    if args.start > args.end or not 0 < args.fraction <= 1 or not 0 <= args.cost_bps < 1000:
        parser.error("Invalid date interval, fraction, or cost")
    with connect(args.db) as db:
        if args.command in ("run", "sync"):
            sync(db, args.start, args.end, args.offline, args.reuse_repo)
        if args.command in ("run", "research"):
            bounds = db.execute("SELECT start,end FROM sync_state").fetchone()
            if not bounds or args.start < bounds[0] or args.end > bounds[1]:
                raise ValueError("Requested research dates exceed synchronized history; run sync first")
            apply_quote_overrides(db)
            risk_blocks = prepare_risk(db, args.end, args.offline)
            signals = build_signals(db, args.start, args.end, args.fraction, risk=True)
            fit_curve(db)
            backtest(db, signals, args.start, args.end, args.cost_bps / 10000, args.offline, risk_blocks)
            crosscheck(db, args.check_code, args.end, args.offline)
            export(db, args.out, args)


if __name__ == "__main__":
    main()
