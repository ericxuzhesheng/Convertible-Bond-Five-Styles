"""Five weekly convertible-bond styles. One Python file; SQLite is the data store."""
from __future__ import annotations

import argparse
import json
import os
import re
import io
import unicodedata
from pathlib import Path
import sqlite3
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
STYLES = {"equity": "偏股型", "bond": "偏债型", "low_price": "低价",
          "double_low": "双低", "balanced": "平衡型"}
FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount,bond_value,bond_over_rate,cb_value,cb_over_rate"
ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"


def fetch_notices(query, start, end, stock="", category="category_kzzq_szsh"):
    import requests
    records, expected = [], None
    for page in range(1, 2001):
        response = requests.post(ANNOUNCEMENT_URL, data={"pageNum": page, "pageSize": 30,
            "column": "szse", "tabName": "fulltext", "plate": "", "stock": stock, "searchkey": query,
            "secid": "", "category": category, "trade": "",
            "seDate": f"{pd.Timestamp(start):%Y-%m-%d}~{pd.Timestamp(end):%Y-%m-%d}",
            "sortName": "", "sortType": "", "isHLtitle": "false"}, timeout=40)
        response.raise_for_status()
        data = response.json()
        if "totalAnnouncement" not in data:
            raise ValueError("CNInfo did not return an announcement count")
        if expected is None:
            expected = int(data["totalAnnouncement"])
        if expected != int(data["totalAnnouncement"]):
            raise ValueError("CNInfo result changed during pagination; retry the interval")
        part = data.get("announcements") or []
        if not part and len(records) < expected:
            raise ValueError("CNInfo pagination ended before the advertised count")
        records.extend(part)
        if len(records) >= expected:
            break
        time.sleep(.15)
    else:
        raise ValueError("CNInfo page limit exceeded")
    columns = ["announcementId", "secCode", "announcementTitle", "announcementTime", "adjunctUrl"]
    frame = pd.DataFrame(records, columns=columns)
    unique(frame, ["announcementId"], "announcement pages")
    if len(frame) != expected:
        raise ValueError("CNInfo announcement count mismatch")
    return frame


def fetch_issuers(snapshot):
    import requests
    response = requests.get("https://www.cninfo.com.cn/new/data/szse_stock.json", timeout=40)
    response.raise_for_status()
    frame = pd.DataFrame(response.json()["stockList"])[["code", "orgId"]]
    unique(frame, ["code"], "CNInfo issuer directory")
    return frame


def missing_coupon_issuers(basic, coupons, start, end, market_codes):
    groups = {code: pd.to_datetime(group.ex_date) for code, group in coupons.groupby("ts_code")}
    missing = set()
    for b in basic[basic.ts_code.isin(market_codes)].to_dict("records"):
        if not b["value_date"] or not b["maturity_date"]:
            raise ValueError("Missing coupon contract dates")
        for year in range(1, 20):
            date = pd.Timestamp(b["value_date"]) + pd.DateOffset(years=year)
            if date >= pd.Timestamp(b["maturity_date"]) or ymd(date) > end:
                break
            if ymd(date) < start or (b["delist_date"] and ymd(date) >= b["delist_date"]) or b["list_date"] > ymd(date):
                continue
            observed = groups.get(b["ts_code"], pd.Series([], dtype="datetime64[ns]"))
            if not (observed-date).dt.days.abs().le(15).any():
                missing.add(b["stk_code"][:6])
    return sorted(missing)


def fetch_notice_text(url):
    import requests
    from pypdf import PdfReader
    response = requests.get(url, timeout=40)
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise ValueError(f"Announcement is not a PDF: {url}")
    pages = []
    for number, page in enumerate(PdfReader(io.BytesIO(response.content)).pages, 1):
        lines = (page.extract_text() or "").strip().splitlines()
        if lines and lines[0].strip() == str(number):
            lines.pop(0)
        if lines and lines[-1].strip() == str(number):
            lines.pop()
        pages.append("\n".join(lines))
    text = "\n\f\n".join(pages)
    if len(text.strip()) < 100:
        raise ValueError(f"Announcement has no usable text: {url}")
    return pd.DataFrame([{"text": text}])


def prefetch_coupon_texts(db, index, offline):
    urls = sorted(set(index.loc[index.announcementTitle.str.contains("付息|兑付利息") &
                               index.announcementTitle.str.contains("转债|可转换|转0?[2-9]"), "source"]))
    existing = {json.loads(key.split(":", 1)[1])["url"] for key, in db.execute("SELECT key FROM cache WHERE source='cninfo.pdf_text'")}
    missing = [url for url in urls if url not in existing]
    if offline and missing:
        raise ValueError(f"Offline announcement text cache missing {len(missing)} PDFs")
    def download(url):
        from requests.exceptions import ConnectionError, Timeout, ChunkedEncodingError
        for attempt in range(3):
            try:
                return fetch_notice_text(url)
            except (ConnectionError, Timeout, ChunkedEncodingError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(download, url): url for url in missing}
        for n, future in enumerate(as_completed(futures), 1):
            frame = future.result()
            cached(db, "cninfo.pdf_text", {"url": futures[future]}, lambda **kw: frame)
            if n % 100 == 0:
                print(f"Coupon PDF cache: {n}/{len(missing)}", flush=True)


def notice_codes(text, codes):
    found = re.findall(r"(?<!\d)\d{6}(?!\d)", text)
    found += re.findall(r"代码[:：]?(\d{6})", text)
    return sorted(set(found) & set(codes))


def parse_coupon(notice, text, codes, bond_names=None):
    text = re.sub(r"(?m)^[ \t]+\d{1,2}[ \t]+$", "", text)
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    found = notice_codes(text, codes)
    specific = set(re.findall(r"(?:可转债|可转换(?:公司)?债券)(?:的)?代码[:：]?(\d{6})", text)) & set(codes)
    section_codes = set(re.findall(r"本期债券(?:的)?基本情况.{0,200}?代码[:：]?(\d{6})", text)) & set(codes)
    if len(section_codes) == 1:
        specific = section_codes
    if len(specific) == 1:
        found = sorted(specific)
    if len(found) > 1 and bond_names:
        named = [c for c in found if bond_names.get(c, "\0") in notice["announcementTitle"]]
        if not named:
            named = [c for c in found if re.search(re.escape(bond_names.get(c,"\0")) + r"[”\"]?(?:\([^)]*\))?(?:将于|本次付息)", text)]
        if len(named) == 1:
            found = named
    if len(found) != 1:
        raise ValueError(f"Expected one bond code, found {found}")
    amendment = bool(re.search("更正|调整|补充", notice["announcementTitle"]))
    if amendment:
        text = re.split("更正后[:：]|更新后[:：]|调整后[:：]", text)[-1]
    date_pattern = r"(20\d{2})年(\d{1,2})月(\d{1,2})日"
    ann = notice["ann_date"]
    def dates_for(label):
        cleaned = re.sub(r"原(?:定)?(?:付息日|除息日)20\d{2}年\d{1,2}月\d{1,2}日", "", text)
        changes = re.findall(r"(?:" + label + r")[为是:：、,]{0,4}由20\d{2}年\d{1,2}月\d{1,2}日(?:调整|变更)为" + date_pattern, text)
        matches = changes or re.findall(r"(?:" + label + r")[为是:：、,]{0,4}" + date_pattern, cleaned)
        dates = {f"{int(y):04}{int(m):02}{int(d):02}" for y,m,d in matches}
        return sorted(d for d in dates if ymd(pd.Timestamp(ann)-pd.Timedelta(days=40)) <= d <= ymd(pd.Timestamp(ann)+pd.Timedelta(days=60)))
    registration = dates_for("债权登记日|债券登记日|利息登记日|权益登记日|付息登记日|可转债登记日")
    payment = dates_for(r"发放日\)?|兑息日|利息支付日|兑息") or dates_for(r"付息日|除权除息\(付息\)日")
    ex = dates_for(r"除息日|除权除息\(付息\)日")
    if len(ex) == 1 and len(payment) > 1:
        payment = [d for d in payment if d >= ex[0]]
    if len(registration) > 1 or len(payment) > 1 or len(ex) > 1 or (not amendment and (not registration or not payment)):
        raise ValueError(f"Ambiguous coupon dates: record={registration}, payment={payment}, ex={ex}")
    amounts = []
    # Tax examples may contain copied prior-year amounts; use the current gross-payment section.
    cash_text = re.split("对于持有", text, maxsplit=1)[0]
    for match in re.finditer(r"([\d,]+(?:\.\d+)?)元(?:人民币)?[()、,，]*(?:含税|税前)", cash_text):
        before = cash_text[max(0,match.start()-160):match.start()]
        before = re.sub(r"[（(]每张面值为?100(?:\.0+)?元[,，、]?", "(", before)
        units = list(re.finditer(r"每(?:(10|十|1|一))?(张|手)|每(?:1000|1,000|千)元|每(?:100|一百)元", before))
        if not units:
            continue
        unit = units[-1]
        # The surrounding phrase must describe coupon interest, not face value or redemption principal.
        if not re.search("利息|付息|派发|兑息", before[unit.end():]):
            continue
        count = 10 if unit.group(2) == "手" or unit.group(1) in ("10","十") or re.search("1000|1,000|千",unit.group()) else 1
        amounts.append(float(match.group(1).replace(",", "")) / count)
    amounts.extend(float(v)/10 for v in re.findall(r"1000元面值债券利息\(含税\)[:：]([\d.]+)元", cash_text))
    amounts = sorted(set(round(a, 8) for a in amounts))
    if not amounts:
        amounts = sorted({float(v) for v in re.findall(
            r"(?:本期|本次|当期)(?:债券|可转债)?(?:的)?(?:票面)?利率[为:：]*([\d.]+)%", text)})
    if not amounts and amendment:
        amounts = [None]  # Resolved only against the earlier original announcement.
    if len(amounts) != 1 or (amounts[0] is not None and not 0 < amounts[0] < 20):
        raise ValueError(f"Ambiguous gross interest per CNY100: {amounts}")
    record, pay = registration[0] if registration else None, payment[0] if payment else None
    ex_date = ex[0] if ex else pay
    if not amendment and (not record < ex_date <= pay or ann >= ex_date):
        raise ValueError("Coupon announcement/record/ex/payment dates are inconsistent")
    return {"ts_code": codes[found[0]], "ann_date": ann, "record_date": record,
            "ex_date": ex_date, "payment_date": pay, "gross_per_bond": amounts[0],
            "announcement_id": str(notice["announcementId"]), "source": notice["source"]}


def fetch_redemption_snapshot(snapshot):
    import requests
    rows, expected = [], None
    for page in range(1, 101):
        response = requests.get("https://datacenter-web.eastmoney.com/api/data/v1/get", params={
            "reportName": "RPT_BOND_CB_LIST", "columns": "SECURITY_CODE,SECUCODE,NOTICE_DATE_SH,RECORD_DATE_SH,EXECUTE_PRICE_SH,EXECUTE_START_DATESH,REDEEM_TYPE",
            "pageSize": 500, "pageNumber": page, "source": "WEB", "client": "WEB"}, timeout=40)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success") or not payload.get("result"):
            raise ValueError("Eastmoney corporate-action snapshot failed")
        result = payload["result"]
        if expected is None:
            expected = int(result["count"])
        if int(result["count"]) != expected:
            raise ValueError("Eastmoney snapshot changed during pagination")
        rows.extend(result["data"])
        if page >= int(result["pages"]):
            break
    frame = pd.DataFrame(rows)
    unique(frame, ["SECUCODE"], "redemption snapshot")
    if len(frame) != expected:
        raise ValueError("Incomplete redemption snapshot")
    return frame


def normalize_coupons(rows):
    # A corrected notice replaces the earlier notice for the same annual payment.
    # The original notice is retained in announcement_index and the request cache.
    normalized = []
    for row in sorted(rows, key=lambda r: (r["ts_code"], r["ann_date"], r["announcement_id"])):
        anchor = row["ex_date"] or row["payment_date"] or row["record_date"] or row["ann_date"]
        previous = [r for r in normalized if r["ts_code"] == row["ts_code"] and
                    abs((pd.Timestamp(r["ex_date"]) - pd.Timestamp(anchor)).days) < 60]
        if previous:
            if len(previous) != 1:
                raise ValueError("Multiple original coupon notices for one correction")
            old = previous[0]
            fields = ["record_date", "ex_date", "payment_date", "gross_per_bond"]
            row = {**row, **{field: old[field] for field in fields if row[field] is None},
                   "source": old["source"] + " | " + row["source"]}
            if any(row[f] != old[f] for f in fields):
                if row["ann_date"] >= min(row["ex_date"], old["ex_date"]):
                    raise ValueError("Coupon revision discovered after ex-date requires explicit reconciliation")
                normalized.remove(old)
            else:
                continue
        if any(row[f] is None for f in ["record_date", "ex_date", "payment_date", "gross_per_bond"]):
            raise ValueError("Coupon correction lacks an original amount or date")
        if not row["record_date"] < row["ex_date"] <= row["payment_date"]:
            raise ValueError("Corrected coupon dates are inconsistent")
        normalized.append(row)
    frame = pd.DataFrame(normalized)
    unique(frame, ["ts_code", "ex_date"], "coupon cash flows")
    return frame


def risk_notice_type(title):
    # The exchange category also contains exchangeable bonds, offshore bonds and cash management notices.
    if re.search("可交换|优先股|现金管理|理财|美元|港币|法律意见|核查意见|受托管理|担保|持有人会议", title):
        return None
    if not re.search("转债|可转换|转0?[2-9]|股票.*终止上市", title):
        return None
    if re.search("不提前|不行使|不赎回|暂不|未提前|取消.*赎回|撤销.*赎回", title):
        return ("redemption", 0)
    if re.search("预计|可能|满足|触发", title):
        return None
    if re.search("停止交易|终止上市|摘牌", title):
        return ("trading_termination", 1)
    if "赎回" in title:
        return ("redemption", 1)
    return None


def sync_corporate_actions(db, start, end, offline=False):
    import tushare as ts
    pro = None if offline else ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=40)
    basic = paged(db, pro, "cb_basic", {"_snapshot": end,
        "fields": "ts_code,stk_code,bond_short_name,value_date,maturity_date,rate_clause,pay_per_year,delist_date,list_date"}, offline).fillna("")
    unique(basic, ["ts_code"], "coupon bond master")
    basic.to_sql("coupon_bonds", db, if_exists="replace", index=False)
    market_codes = set(pd.read_sql_query("SELECT DISTINCT ts_code FROM bars", db).ts_code)
    codes = {c[:6]: c for c in basic.ts_code}
    names = dict(zip(basic.ts_code.str[:6], basic.bond_short_name))
    parts, coverage = [], []
    for query in ["付息", "兑付利息", "赎回", "停止交易", "终止上市", "摘牌"]:
        for year in range(int(start[:4])-1, int(end[:4])+1):
            left, right = f"{year}0101", min(f"{year}1231", end)
            params = {"query": query, "start": left, "end": right}
            if query == "兑付利息":
                params["category"] = ""  # These historical notices are often outside the CB category.
            frame = cached(db, "cninfo.announcements", params, fetch_notices, offline)
            parts.append(frame)
            coverage.append({"source": "cninfo", "query": query, "start": left, "end": right, "rows": len(frame)})
    index = pd.concat(parts, ignore_index=True).drop_duplicates("announcementId")
    index["ann_date"] = pd.to_datetime(index.announcementTime, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y%m%d")
    index["source"] = "https://static.cninfo.com.cn/" + index.adjunctUrl
    index.to_sql("announcement_index", db, if_exists="replace", index=False)
    coverage = pd.DataFrame(coverage)
    coverage.to_sql("announcement_coverage", db, if_exists="replace", index=False)
    prefetch_coupon_texts(db, index, offline)
    coupons, events, reviews = [], [], []
    def text_of(row):
        return cached(db, "cninfo.pdf_text", {"url": row["source"]}, fetch_notice_text, offline).text.iloc[0]
    def add_coupon(n):
        title = n["announcementTitle"]
        if not re.search("转债|可转换|转0?[2-9]", title):
            reviews.append((n["announcementId"], "not_a_convertible_coupon", title, n["source"]))
            return
        text = text_of(n)
        observed = notice_codes(re.sub(r"\s+", "", text), codes)
        if "资金到位" in title:
            reviews.append((n["announcementId"], "funding_update_not_payment_schedule", title, n["source"]))
            return
        if not any(codes[c] in market_codes for c in observed):
            reviews.append((n["announcementId"], "outside_traded_universe", title, n["source"]))
            return
        try:
            coupons.append(parse_coupon(n, text, codes, names))
        except ValueError as exc:
            raise ValueError(f"Coupon notice {n['announcementId']} ({title}): {exc}") from exc
    for n in index.sort_values(["ann_date", "announcementId"]).to_dict("records"):
        title = n["announcementTitle"]
        issuer = basic[basic.stk_code.str[:6].eq(str(n["secCode"]).zfill(6))]
        issuer = issuer[issuer.ts_code.isin(market_codes)]
        if re.search("付息|兑付利息", title):
            add_coupon(n)
        kind = risk_notice_type(title)
        if kind is None:
            continue
        named = issuer[issuer.bond_short_name.map(lambda name: bool(name) and name in title)]
        choices = named if len(named) == 1 else issuer[(issuer.value_date < n["ann_date"]) &
            ((issuer.delist_date == "") | (issuer.delist_date >= ymd(pd.Timestamp(n["ann_date"])-pd.Timedelta(days=60))))]
        choices = choices[choices.ts_code.isin(market_codes)]
        if len(choices) != 1:
            observed = notice_codes(re.sub(r"\s+", "", text_of(n)), codes)
            actual = [codes[c] for c in observed if codes[c] in market_codes]
            if not actual:
                reviews.append((n["announcementId"], "outside_traded_universe", title, n["source"]))
                continue
            if len(actual) != 1:
                raise ValueError(f"Ambiguous risk-announcement bond: {n['announcementId']} {actual}")
            code = actual[0]
        else:
            code = choices.ts_code.iloc[0]
        events.append({"ts_code": code, "ann_date": n["ann_date"], "effective_date": n["ann_date"],
                       "risk_type": kind[0], "blocked": kind[1], "source": n["source"]})
    coupon_frame = normalize_coupons(coupons)
    missing_issuers = missing_coupon_issuers(basic, coupon_frame, start, end, market_codes)
    if missing_issuers:
        directory = cached(db, "cninfo.issuers", {"snapshot": end}, fetch_issuers, offline).set_index("code").orgId
        supplements = []
        existing_ids = set(index.announcementId.astype(str))
        for issuer_code in missing_issuers:
            if issuer_code not in directory.index:
                raise ValueError(f"Cannot resolve CNInfo organization for coupon search: {issuer_code}")
            left = f"{int(start[:4])-1}0101"
            extra = cached(db, "cninfo.issuer_coupons", {"query": "付息", "start": left, "end": end,
                "stock": issuer_code + "," + directory.loc[issuer_code], "category": ""}, fetch_notices, offline)
            if not extra.empty and not extra.secCode.astype(str).eq(issuer_code).all():
                raise ValueError("CNInfo issuer filter did not restrict the announcement results")
            extra["ann_date"] = pd.to_datetime(extra.announcementTime, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y%m%d")
            extra["source"] = "https://static.cninfo.com.cn/" + extra.adjunctUrl
            supplements.append(extra)
            coverage.loc[len(coverage)] = ["cninfo_issuer:" + issuer_code, "付息 (all categories)", left, end, len(extra)]
            for n in extra[~extra.announcementId.astype(str).isin(existing_ids)].to_dict("records"):
                add_coupon(n)
                existing_ids.add(str(n["announcementId"]))
            print(f"Coupon issuer coverage repair {issuer_code}: {len(extra)} notices", flush=True)
        index = pd.concat([index, *supplements], ignore_index=True).drop_duplicates("announcementId")
        index.to_sql("announcement_index", db, if_exists="replace", index=False)
        coupon_frame = normalize_coupons(coupons)
    coupon_frame.to_sql("coupons", db, if_exists="replace", index=False)
    coverage.to_sql("announcement_coverage", db, if_exists="replace", index=False)
    snapshot = cached(db, "eastmoney.redemptions", {"snapshot": end}, fetch_redemption_snapshot, offline)
    for row in snapshot.to_dict("records"):
        if str(row["REDEEM_TYPE"]).removesuffix(".0") != "2":
            continue
        event = parse_redemption(row)
        if event and event[1] <= end:
            put_redemption(db, event, "https://data.eastmoney.com/kzz/detail/" + row["SECUCODE"][:6] + ".html")
    # Query the full paginated snapshot: date-filtered cb_call requests can return empty
    # even when the unfiltered endpoint contains the requested announcement dates.
    calls = paged(db, pro, "cb_call", {"_snapshot": end,
        "fields": "ts_code,call_type,is_call,ann_date,call_date,call_price,payment_date,call_reg_date"}, offline)
    if calls.empty:
        raise ValueError("The complete cb_call snapshot is empty")
    calls = calls[calls.ann_date.notna() & (calls.ann_date <= end)].sort_values("ann_date", kind="stable")
    calls.to_sql("call_announcements", db, if_exists="replace", index=False)
    statuses = {"公告实施强赎": 1, "公告强赎": 1, "公告到期赎回": 1, "公告不强赎": 0}
    for call in calls.to_dict("records"):
        if call["ts_code"] not in market_codes or call["is_call"] not in statuses:
            continue
        events.append({"ts_code": call["ts_code"], "ann_date": call["ann_date"], "effective_date": call["ann_date"],
                       "risk_type": "redemption", "blocked": statuses[call["is_call"]], "source": "https://tushare.pro/document/2?doc_id=269"})
        needed = ["call_reg_date", "payment_date", "call_price"]
        if statuses[call["is_call"]] and all(pd.notna(call[k]) for k in needed):
            record, pay, price = str(call["call_reg_date"]), str(call["payment_date"]), float(call["call_price"])
            if call["ann_date"] <= pay and record <= pay and np.isfinite(price) and price > 0:
                put_redemption(db, (call["ts_code"], call["ann_date"], record, pay, price), "Tushare cb_call")
    pd.DataFrame(events).drop_duplicates().to_sql("announcement_risks", db, if_exists="replace", index=False)
    pd.DataFrame(reviews, columns=["announcement_id", "status", "title", "source"]).to_sql("announcement_reviews", db, if_exists="replace", index=False)
    pd.DataFrame([{"start": start, "end": end, "notices": len(index), "coupons": len(coupon_frame),
                   "risk_events": len(events), "status": "complete"}]).to_sql("corporate_action_state", db, if_exists="replace", index=False)
    print(f"Corporate actions: {len(index)} notices, {len(coupon_frame)} coupons, {len(events)} risk events", flush=True)


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
    if source.startswith("tushare."):
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


def week_decision(calendar, date):
    day = pd.Timestamp(date)
    monday = day - pd.Timedelta(days=day.weekday())
    expected = {ymd(d) for d in pd.date_range(monday, periods=7)}
    unique(calendar, ["cal_date"], "weekly calendar")
    if set(calendar.cal_date) != expected or not calendar.is_open.isin([0, 1]).all():
        raise ValueError("Weekly calendar is incomplete or invalid; cannot decide the last session")
    sessions = sorted(calendar.loc[calendar.is_open == 1, "cal_date"])
    last = sessions[-1] if sessions else None
    return {"date": ymd(day), "timezone": "Asia/Shanghai", "last_trading_day": last,
            "should_update": ymd(day) == last}


def check_week(db, date, offline=False):
    import tushare as ts
    day = pd.Timestamp(date)
    monday = day - pd.Timedelta(days=day.weekday())
    pro = None if offline else ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=40)
    calendar = paged(db, pro, "trade_cal", {"exchange": "SSE", "_snapshot": date,
                     "start_date": ymd(monday), "end_date": ymd(monday + pd.Timedelta(days=6)),
                     "fields": "cal_date,is_open"}, offline)
    return week_decision(calendar, date)


def benchmark_nav(frame, dates):
    unique(frame, ["trade_date"], "benchmark")
    if set(frame.trade_date) != set(dates):
        raise ValueError("Benchmark dates do not exactly cover the portfolio sessions")
    frame = frame.set_index("trade_date").loc[list(dates)].reset_index()
    prices = frame[["open", "close"]].apply(pd.to_numeric, errors="raise").to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("Benchmark has missing, nonpositive or nonfinite prices")
    # Portfolios start at the first session OPEN: retain that session's return.
    return pd.DataFrame({"trade_date": frame.trade_date, "style": "csi_cb",
                         "nav": prices[:, 1] / prices[0, 0], "valid": 1})


def sync_benchmark(db, start, end, offline=False):
    import tushare as ts
    pro = None if offline else ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=40)
    dates = pd.read_sql_query("SELECT cal_date FROM calendar WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date",
                             db, params=(start, end)).cal_date.tolist()
    if not dates:
        raise ValueError("No portfolio sessions for benchmark comparison")
    parts = []
    for year in range(int(dates[0][:4]), int(dates[-1][:4]) + 1):
        wanted = [d for d in dates if d.startswith(str(year))]
        if not wanted:
            continue
        def fetch(**params):
            result = pro.index_daily(**params)
            benchmark_nav(result, wanted)  # Never cache a delayed/incomplete response.
            if not result.ts_code.eq("000832.CSI").all():
                raise ValueError("Unexpected benchmark code")
            return result
        parts.append(cached(db, "tushare.index_daily", {"ts_code": "000832.CSI", "start_date": wanted[0],
                            "end_date": wanted[-1], "fields": "ts_code,trade_date,open,close"}, fetch, offline))
    frame = pd.concat(parts, ignore_index=True).sort_values("trade_date")
    benchmark_nav(frame, dates)
    frame.to_sql("benchmark", db, if_exists="replace", index=False)


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
    # Keep the two sourced manual exceptions; live announcement history replaces stale vendor snapshots.
    manual = events[(events.risk_type != "stock_name") & ~events.source.str.contains("data.eastmoney.com", regex=False)]
    notices = pd.read_sql_query("SELECT * FROM announcement_risks", db)
    events = pd.concat([manual, names, notices], ignore_index=True)
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


def backtest(db, signals, start, end, cost, offline, risk_blocks=None, coupon_events=None):
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
    coupons_by_record = {} if coupon_events is None else {d: f.to_dict("records") for d,f in coupon_events.groupby("record_date")}
    nav_rows, trade_rows, issue_rows, holding_rows = [], [], [], []
    coupon_rows = []
    for style in STYLES:
        positions, marks, cash, receivables = {}, {}, 1., []
        coupon_claims = []
        tainted = False
        for date in dates:
            day = by_date[date]
            for claim in coupon_claims:
                if claim["ex_date"] <= date and not claim["recognized"]:
                    claim["recognized"] = True
                    coupon_rows.append((date, style, claim["ts_code"], "coupon_receivable", claim["amount"],
                        claim["quantity"], claim["gross_per_bond"], claim["record_date"], claim["ex_date"], claim["payment_date"], claim["source"]))
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
            # Full-price marks already contain accrued interest at registration close.
            # Lock entitlement now, recognize it on ex-date, credit cash at payment close.
            for coupon in coupons_by_record.get(date, []):
                quantity = positions.get(coupon["ts_code"], 0.)
                if quantity > 0:
                    event = db.execute("SELECT record_date FROM redemptions WHERE ts_code=?", (coupon["ts_code"],)).fetchone()
                    if event and event[0] == date:
                        raise ValueError("Coupon overlaps a principal-and-interest redemption; reconcile before accounting")
                    claim = {**coupon, "quantity": quantity, "amount": quantity * coupon["gross_per_bond"], "recognized": False}
                    coupon_claims.append(claim)
                    coupon_rows.append((date, style, claim["ts_code"], "entitlement_locked", claim["amount"],
                        quantity, claim["gross_per_bond"], date, claim["ex_date"], claim["payment_date"], claim["source"]))
            due_coupons = [c for c in coupon_claims if c["payment_date"] <= date]
            for claim in due_coupons:
                if not claim["recognized"]:
                    raise ValueError("Coupon payment precedes recognition")
                cash += claim["amount"]
                coupon_rows.append((date, style, claim["ts_code"], "coupon_payment", claim["amount"],
                    claim["quantity"], claim["gross_per_bond"], claim["record_date"], claim["ex_date"], claim["payment_date"], claim["source"]))
            coupon_claims = [c for c in coupon_claims if c["payment_date"] > date]
            coupon_receivable = sum(c["amount"] for c in coupon_claims if c["recognized"])
            value = cash + sum(receipt[1] for receipt in receivables) + coupon_receivable + sum(q * marks[c] for c, q in positions.items())
            if value <= 0 or not np.isfinite(value):
                raise ValueError(f"Invalid NAV: {style} {date}")
            nav_rows.append((date, style, value, cash, fees, len(positions), int(not tainted), coupon_receivable))
        print(f"Backtest {style}: {len(dates)} sessions; valid={not tainted}", flush=True)
    tables = {
        "nav": pd.DataFrame(nav_rows, columns=["trade_date", "style", "nav", "cash", "fees", "holdings", "valid", "coupon_receivable"]),
        "trades": pd.DataFrame(trade_rows, columns=["trade_date", "style", "ts_code", "quantity_change", "price", "fee", "reason"]),
        "issues": pd.DataFrame(issue_rows, columns=["trade_date", "style", "ts_code", "issue"]).drop_duplicates(),
        "holdings": pd.DataFrame(holding_rows, columns=["trade_date", "style", "ts_code", "quantity", "mark", "value"]),
        "coupon_ledger": pd.DataFrame(coupon_rows, columns=["trade_date", "style", "ts_code", "event", "amount", "quantity", "gross_per_bond", "record_date", "ex_date", "payment_date", "source"])}
    for table, frame in tables.items():
        frame.to_sql(table, db, if_exists="replace", index=False)
    return tables


def check_coupon_coverage(db, start, end):
    basic = pd.read_sql_query("SELECT * FROM coupon_bonds", db)
    coupons = pd.read_sql_query("SELECT * FROM coupons", db)
    holdings = pd.read_sql_query("SELECT DISTINCT trade_date,ts_code FROM holdings", db)
    held = set(map(tuple, holdings.itertuples(index=False, name=None)))
    held_codes = set(holdings.ts_code)
    coupons["ex_timestamp"] = pd.to_datetime(coupons.ex_date)
    coupon_groups = {code: group for code, group in coupons.groupby("ts_code")}
    calendar = pd.read_sql_query("SELECT cal_date FROM calendar ORDER BY cal_date", db).cal_date.tolist()
    contracts = basic.set_index("ts_code")
    for coupon in coupons.to_dict("records"):
        contract = contracts.loc[coupon["ts_code"]]
        year = round((pd.Timestamp(coupon["ex_date"])-pd.Timestamp(contract.value_date)).days/365.25)
        rates = re.findall(r"(\d+(?:\.\d+)?)\s*[%％]", contract.rate_clause)
        if not 1 <= year <= len(rates) or not np.isclose(float(rates[year-1]), coupon["gross_per_bond"]):
            raise ValueError(f"Coupon amount disagrees with the bond contract: {coupon['ts_code']} {coupon['ex_date']}")
        if start <= coupon["record_date"] <= end and coupon["record_date"] not in calendar:
            raise ValueError(f"Coupon record date is not a trading session: {coupon['ts_code']} {coupon['record_date']}")
    rows = []
    for b in basic.to_dict("records"):
        if b["ts_code"] not in held_codes:
            continue
        if not b["value_date"] or not b["maturity_date"] or int(b["pay_per_year"]) != 1:
            raise ValueError(f"Unsupported coupon contract for held bond {b['ts_code']}")
        issue, maturity = pd.Timestamp(b["value_date"]), pd.Timestamp(b["maturity_date"])
        for year in range(1, 20):
            anniversary = issue + pd.DateOffset(years=year)
            if anniversary >= maturity or ymd(anniversary) > end:
                break  # The maturity amount already includes the final coupon.
            if ymd(anniversary) < start or (b["delist_date"] and ymd(anniversary) >= b["delist_date"]):
                continue
            before = [d for d in calendar if d < ymd(anniversary)]
            if not before:
                raise ValueError("Calendar cannot locate the expected coupon record session")
            expected_record = before[-1]
            candidates = coupon_groups.get(b["ts_code"], coupons.iloc[:0])
            notices = candidates[(candidates.ex_timestamp-anniversary).dt.days.abs().le(15)]
            exposed = (expected_record, b["ts_code"]) in held
            status = "verified_notice" if len(notices) == 1 else "not_held_on_expected_record"
            if len(notices) > 1 or (notices.empty and exposed):
                status = "unresolved"
            rows.append({"ts_code": b["ts_code"], "anniversary": ymd(anniversary),
                         "expected_record": expected_record, "held": exposed, "status": status})
    result = pd.DataFrame(rows)
    result.to_sql("coupon_coverage", db, if_exists="replace", index=False)
    if (result.status == "unresolved").any():
        missing = result[result.status == "unresolved"]
        raise ValueError("Held coupon periods without verified cash flows: " + missing.to_json(orient="records"))
    return result


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
    for table in ["signals", "curve", "nav", "trades", "issues", "crosscheck", "benchmark", "coupons", "coupon_ledger", "coupon_coverage", "announcement_risks", "announcement_coverage", "announcement_reviews", "corporate_action_state"]:
        if table in names:
            pd.read_sql_query(f"SELECT * FROM {table}", db).to_csv(out / (table + ".csv"), index=False, encoding="utf-8-sig")
    nav = pd.read_sql_query("SELECT * FROM nav ORDER BY trade_date,style", db)
    benchmark = benchmark_nav(pd.read_sql_query("SELECT * FROM benchmark", db), sorted(nav.trade_date.unique()))
    nav = pd.concat([nav[["trade_date", "style", "nav", "valid"]], benchmark], ignore_index=True)
    nav.pivot(index="trade_date", columns="style", values="nav").to_csv(out / "comparison_nav.csv", encoding="utf-8-sig")
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
            annual.append({"year": year, "style": style, "return": float((1 + group.daily_return).prod() - 1) if group.valid.all() else None})
        dates = pd.to_datetime(frame.trade_date)
        line = {"color": "black", "linestyle": "--", "linewidth": 2.2, "zorder": 5} if style == "csi_cb" else {}
        label = "CSI Convertible Bond (000832.CSI)" if style == "csi_cb" else style
        axes[0].plot(dates, np.where(frame.valid, values, np.nan), label=label, **line)
        axes[1].plot(dates, np.where(frame.valid, drawdown, np.nan), **line)
    axes[0].set_title("Five weekly CB styles vs CSI Convertible Bond Index\nInitial capital = 1 at first session open | gross coupons included, net of trading costs")
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
    issues = pd.read_sql_query("SELECT * FROM issues", db)
    issues.groupby("issue").size().reset_index(name="observations").to_csv(out / "issue_summary.csv", index=False, encoding="utf-8-sig")
    issues[issues.issue.str.contains("unresolved")].groupby(["ts_code", "issue"]).size().reset_index(name="observations").to_csv(out / "unresolved_summary.csv", index=False, encoding="utf-8-sig")
    ledger = pd.read_sql_query("SELECT * FROM coupon_ledger", db)
    summary = ledger.groupby(["style", "event"]).agg(events=("amount", "size"), amount=("amount", "sum")).reset_index()
    summary.to_csv(out / "coupon_summary.csv", index=False, encoding="utf-8-sig")
    manifest = {"generated_at": datetime.now().astimezone().isoformat(), "start": args.start, "end": args.end,
                "frequency": "weekly", "signal": "prior session close", "execution": "next week first session open",
                "one_way_cost_bps": args.cost_bps, "fraction": args.fraction,
                "benchmark": {"code": "000832.CSI", "source": "Tushare index_daily", "normalization": "close / first session open",
                              "fees_bps": 0, "missing_dates": "fail; never fill or shorten the interval",
                              "methodology": "Published official index levels; no synthetic coupon adjustment"},
                "coupons": {"basis": "gross, before investor-specific income tax", "entitlement": "record-date closing quantity",
                            "recognition": "ex-date receivable; no double counting in full-price record-date mark",
                            "payment": "cash at payment-date close; available for subsequent trades",
                            "source": "CNInfo issuer payment PDFs; annual held-period coverage checked"},
                "risk_policy": {"price_floor": 90, "stock_ST_or_delisting": "exclude and exit", "unknown_name_history": "exclude",
                                "redemption_and_trading_termination": "exclude and exit after announcement", "announcement_lag": "next calendar day, executed on next trading session",
                                "risk_exit_frequency": "daily", "normal_rebalance": "weekly"},
                "limitations": ["Investor-specific taxes excluded; benchmark uses published official levels", "Daily data cannot guarantee open auction fills or capacity",
                                "Provider history can be revised; no original-release vintage guarantee",
                                "Unresolved held marks invalidate subsequent performance; inspect issues.csv"],
                "sources": [{"source": r[0], "cache_entries": r[1], "last_fetched_at": r[2]} for r in
                            db.execute("SELECT source,count(*),max(fetched_at) FROM cache GROUP BY source")]}
    manifest["corporate_actions"] = pd.read_sql_query("SELECT * FROM corporate_action_state", db).iloc[0].to_dict()
    manifest["checks"] = {"portfolio_nav_rows": int((nav['style'] != 'csi_cb').sum()),
        "rebalance_dates": int(db.execute("SELECT count(DISTINCT execution_date) FROM signals").fetchone()[0]),
        "unresolved_valuations": int(issues.issue.str.contains("unresolved").sum()),
        "unresolved_coupon_periods": int(db.execute("SELECT count(*) FROM coupon_coverage WHERE status='unresolved'").fetchone()[0]),
        "coupon_payments": int((ledger.event == "coupon_payment").sum()),
        "coupon_receivable_entries": int((ledger.event == "coupon_receivable").sum())}
    (out / "run.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False), flush=True)


def publish_results(out):
    manifest = json.loads((out / "run.json").read_text(encoding="utf-8"))
    metrics = pd.read_csv(out / "metrics.csv").set_index("style")
    comparison = pd.read_csv(out / "comparison_nav.csv", index_col="trade_date")
    expected = set(STYLES) | {"csi_cb"}
    if set(metrics.index) != expected or not metrics.valid.all() or set(comparison.columns) != expected:
        raise ValueError("Publication requires five valid portfolios and the benchmark")
    if not np.isfinite(comparison.to_numpy()).all() or (comparison <= 0).any().any() or comparison.index.duplicated().any():
        raise ValueError("Invalid comparison NAVs; public files were not changed")
    if not metrics.sessions.eq(len(comparison)).all() or metrics.end.nunique() != 1:
        raise ValueError("Benchmark and portfolio intervals differ")
    if manifest["corporate_actions"]["end"] != manifest["end"] or manifest["corporate_actions"]["status"] != "complete":
        raise ValueError("Corporate actions have not been refreshed through the research cutoff")
    checks = manifest["checks"]
    if checks["unresolved_valuations"] or checks["unresolved_coupon_periods"]:
        raise ValueError("Unresolved valuation or coupon periods block publication")
    files = ["metrics.csv", "annual_returns.csv", "coverage.csv", "crosscheck.csv", "run.json", "issue_summary.csv",
             "unresolved_summary.csv", "weekly_styles.png", "premium_at_100.png", "comparison_nav.csv", "coupons.csv",
             "coupon_summary.csv", "coupon_coverage.csv", "announcement_risks.csv", "announcement_coverage.csv",
             "announcement_reviews.csv", "corporate_action_state.csv"]
    for name in files:
        if not (out / name).is_file():
            raise ValueError(f"Missing publication artifact: {name}")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    first = pd.Timestamp(str(metrics.start.iloc[0])).strftime("%Y-%m-%d")
    last = pd.Timestamp(str(metrics.end.iloc[0])).strftime("%Y-%m-%d")
    english = {"equity":"Equity-like", "bond":"Bond-like", "low_price":"Low-price", "double_low":"Double-low", "balanced":"Balanced", "csi_cb":"CSI Convertible Bond (000832.CSI)"}
    chinese = {**STYLES, "csi_cb":"中证转债指数（000832.CSI）"}
    for lang, labels in [("ZH", chinese), ("EN", english)]:
        lines = ([f"共同区间为 **{first}—{last}**，共 **{len(comparison):,} 个交易日、{checks['rebalance_dates']} 个正常建仓/调仓日**。组合计入税前票息及已识别赎回，扣除单边 {manifest['one_way_cost_bps']:g}bp 交易摩擦；年化按 252 个交易日计算。", "", "| 组合 | 累计收益 | 年化收益 | 年化波动率 | 最大回撤 |"] if lang == "ZH" else
                 [f"Common interval: **{first} to {last}**, **{len(comparison):,} sessions and {checks['rebalance_dates']} initial-entry/rebalance dates**. Portfolios include gross coupons and identified redemptions, net of {manifest['one_way_cost_bps']:g}bp one-way friction; annualization uses 252 sessions.", "", "| Portfolio | Cumulative return | Annualized return | Annualized volatility | Max drawdown |"])
        lines.append("|---|---:|---:|---:|---:|")
        for style, label in labels.items():
            values = " | ".join(f"{metrics.loc[style,field]:.2%}" for field in ["total_return","annual_return","annual_volatility","max_drawdown"])
            lines.append(f"| {label} | {values} |")
        if lang == "ZH":
            lines += ["", f"已核验 {checks['portfolio_nav_rows']:,} 条组合日净值、{checks['coupon_payments']:,} 笔票息到账。持仓票息期间与估值未解决记录均为 0。"]
        else:
            lines += ["", f"Validated {checks['portfolio_nav_rows']:,} portfolio-day NAVs and {checks['coupon_payments']:,} coupon payments. No unresolved held coupon periods or valuation entries remain."]
        pattern = rf"<!-- RESULTS_{lang}:START -->.*?<!-- RESULTS_{lang}:END -->"
        replacement = f"<!-- RESULTS_{lang}:START -->\n" + "\n".join(lines) + f"\n<!-- RESULTS_{lang}:END -->"
        readme, count = re.subn(pattern, lambda match: replacement, readme, flags=re.S)
        if count != 1:
            raise ValueError(f"README requires one {lang} results block")
    readme = re.sub(r"数据截止-\d{4}--\d{2}--\d{2}", "数据截止-" + last.replace("-","--"), readme)
    readme = re.sub(r"Data through \d{4}-\d{2}-\d{2}", "Data through " + last, readme)
    readme = re.sub(r"--end \d{8}", "--end " + last.replace("-",""), readme)
    examples = ROOT / "examples"
    examples.mkdir(exist_ok=True)
    for name in files:
        shutil.copyfile(out / name, examples / name)
    (ROOT / "README.md").write_text(readme, encoding="utf-8")
    print(f"Published complete local examples and bilingual README through {last}", flush=True)


def self_test():
    import unittest
    class Contracts(unittest.TestCase):
        def coupon_fixture(self):
            db = sqlite3.connect(":memory:")
            dates = ["20251013", "20251014", "20251020"]
            pd.DataFrame({"cal_date": dates}).to_sql("calendar", db, index=False)
            pd.DataFrame([{"trade_date": d, "ts_code": c, "open": p, "close": p, "vol": 1.}
                for d,p in zip(dates,[100.,99.,99.]) for c in ["A","B"]]).to_sql("bars",db,index=False)
            db.execute("CREATE TABLE redemptions (ts_code TEXT PRIMARY KEY,ann_date TEXT,record_date TEXT,payment_date TEXT,price REAL,source TEXT)")
            sig = pd.DataFrame([{"style": st, "execution_date": dates[0], "ts_code": "A"} for st in STYLES])
            coupons = pd.DataFrame([{"ts_code": "A", "record_date": dates[0], "ex_date": dates[1],
                "payment_date": dates[2], "gross_per_bond": 1., "ann_date": "20251010", "source": "fixture"}])
            return db, dates, sig, coupons
        def test_coupon_registration_ex_and_payment(self):
            db, dates, sig, coupons = self.coupon_fixture()
            result = backtest(db,sig,dates[0],dates[-1],0.,True,{},coupons)
            nav = result['nav'].query("style == 'equity'")
            np.testing.assert_allclose(nav.nav,[1.,1.,1.])
            np.testing.assert_allclose(nav.cash,[0.,0.,.01])
            np.testing.assert_allclose(nav.coupon_receivable,[0.,.01,0.])
            self.assertEqual(len(result['coupon_ledger'].query("event == 'coupon_payment'")),5)
        def test_coupon_entitlement_survives_ex_date_sale(self):
            db, dates, sig, coupons = self.coupon_fixture()
            result = backtest(db,sig,dates[0],dates[-1],0.,True,{dates[1]:{'A'}},coupons)
            nav = result['nav'].query("style == 'equity'")
            np.testing.assert_allclose(nav.nav,[1.,1.,1.])
            np.testing.assert_allclose(nav.cash,[0.,.99,1.])
        def test_ex_date_buyer_has_no_coupon(self):
            db, dates, sig, coupons = self.coupon_fixture()
            sig['execution_date'] = dates[1]
            result = backtest(db,sig,dates[0],dates[-1],0.,True,{},coupons)
            self.assertTrue(result['coupon_ledger'].empty)
            np.testing.assert_allclose(result['nav'].nav,1.)
        def test_payment_not_available_before_close(self):
            db, dates, sig, coupons = self.coupon_fixture()
            new = pd.DataFrame([{"style": st,"execution_date":dates[-1],"ts_code":"B"} for st in STYLES])
            result = backtest(db,pd.concat([sig,new]),dates[0],dates[-1],0.,True,{},coupons)
            purchase = result['trades'].query("style == 'equity' and ts_code == 'B'")
            self.assertAlmostEqual(purchase.quantity_change.iloc[0],.01)
            self.assertAlmostEqual(result['nav'].query("style == 'equity'").cash.iloc[-1],.01)
        def test_coupon_redemption_overlap_is_error(self):
            db, dates, sig, coupons = self.coupon_fixture()
            db.execute("INSERT INTO redemptions VALUES ('A','20251010','20251013','20251020',101,'test')")
            with self.assertRaisesRegex(ValueError,'overlaps'):
                backtest(db,sig,dates[0],dates[-1],0.,True,{},coupons)
        def test_coupon_parser_lot_units_and_late_cash(self):
            n={'ann_date':'20251010','announcementId':'test','announcementTitle':'转债付息公告','source':'test'}
            text='债券代码：123001；债权登记日：2025年10月13日；除息日：2025年10月14日；兑息发放日：2025年10月20日；每10张转债（每张面值为100.00元，10张合计1000元）派发利息10元（含税）。'
            c=parse_coupon(n,text,{'123001':'123001.SZ'})
            self.assertEqual(c['gross_per_bond'],1.)
            self.assertEqual(c['payment_date'],'20251020')
            self.assertEqual(c['ex_date'],'20251014')
        def test_risk_notice_classification(self):
            self.assertEqual(risk_notice_type('关于不提前赎回某转债的公告'),('redemption',0))
            self.assertEqual(risk_notice_type('关于某转债赎回实施的公告'),('redemption',1))
            self.assertEqual(risk_notice_type('关于某转债停止交易的公告'),('trading_termination',1))
            self.assertEqual(risk_notice_type('关于希望转2赎回实施的公告'),('redemption',1))
            self.assertEqual(risk_notice_type('关于新乳转债不提前赎回的公告'),('redemption',0))
            self.assertIsNone(risk_notice_type('关于某转债预计触发强赎条件的公告'))
            self.assertIsNone(risk_notice_type('关于可转债募集资金现金管理赎回的公告'))
        def test_held_coupon_coverage_and_amount_guards(self):
            db, dates, sig, coupons = self.coupon_fixture()
            backtest(db,sig,dates[0],dates[-1],0.,True,{},coupons)
            pd.DataFrame([{'ts_code':'A','value_date':'20241014','maturity_date':'20301014',
                'pay_per_year':1,'delist_date':'','rate_clause':'第一年1%，第二年2%'}]).to_sql('coupon_bonds',db,index=False)
            coupons.to_sql('coupons',db,index=False)
            self.assertTrue(check_coupon_coverage(db,dates[0],dates[-1]).status.eq('verified_notice').all())
            db.execute('UPDATE coupons SET gross_per_bond=10')
            with self.assertRaisesRegex(ValueError,'disagrees'):
                check_coupon_coverage(db,dates[0],dates[-1])
            db.execute('DELETE FROM coupons')
            with self.assertRaisesRegex(ValueError,'without verified'):
                check_coupon_coverage(db,dates[0],dates[-1])
        def test_coupon_parenthesized_payment_date(self):
            n={'ann_date':'20201102','announcementId':'test','announcementTitle':'转债付息公告','source':'test'}
            text='债券代码：113520；债权登记日：2020年11月6日；除息日：2020年11月9日；付息日（兑息发放日）：2020年11月9日；每张转债兑息0.70元（含税）。'
            self.assertEqual(parse_coupon(n,text,{'113520':'113520.SH'})['payment_date'],'20201109')
        def test_coupon_correction_keeps_original_amount(self):
            old={'ts_code':'A','ann_date':'20200121','record_date':'20200131','ex_date':'20200203','payment_date':'20200203','gross_per_bond':1.,'announcement_id':'1','source':'original'}
            new={**old,'ann_date':'20200130','record_date':'20200203','ex_date':'20200204','payment_date':'20200204','gross_per_bond':None,'announcement_id':'2','source':'correction'}
            rows=normalize_coupons([old,new])
            self.assertEqual(len(rows),1)
            self.assertEqual(rows.iloc[0].gross_per_bond,1.)
            self.assertEqual(rows.iloc[0].record_date,'20200203')
        def test_benchmark_open_anchor_and_missing_data(self):
            frame = pd.DataFrame({"trade_date": ["20251013", "20251014"], "open": [100., 110.], "close": [110., 99.]})
            dates = frame.trade_date.tolist()
            np.testing.assert_allclose(benchmark_nav(frame.iloc[::-1], dates).nav, [1.1, .99])
            with self.assertRaises(ValueError):
                benchmark_nav(frame.iloc[:1], dates)
            with self.assertRaises(ValueError):
                benchmark_nav(pd.concat([frame, frame.iloc[:1]]), dates)
            for price in [0., np.nan, np.inf]:
                broken = frame.copy()
                broken.loc[0, "open"] = price
                with self.assertRaises(ValueError):
                    benchmark_nav(broken, dates)
        def test_last_trading_day_gate(self):
            calendar = pd.DataFrame({"cal_date": [ymd(d) for d in pd.date_range('20251006', periods=7)],
                                     "is_open": [0, 0, 0, 1, 1, 0, 0]})
            self.assertFalse(week_decision(calendar, '20251009')['should_update'])
            self.assertTrue(week_decision(calendar, '20251010')['should_update'])
            calendar.loc[4, 'is_open'] = 0
            self.assertTrue(week_decision(calendar, '20251009')['should_update'])
            self.assertFalse(week_decision(calendar, '20251010')['should_update'])
            calendar['is_open'] = 0
            self.assertIsNone(week_decision(calendar, '20251010')['last_trading_day'])
            self.assertFalse(week_decision(calendar, '20251010')['should_update'])
            with self.assertRaises(ValueError):
                week_decision(calendar.iloc[:5], '20251010')
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
    parser.add_argument("command", choices=["run", "sync", "research", "self-test", "week-check", "publish"])
    parser.add_argument("--start", default="20190101")
    parser.add_argument("--end", help="YYYYMMDD; yesterday for research, today for week-check (Asia/Shanghai)")
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
    if args.command == "publish":
        publish_results(args.out); return
    if args.end is None:
        today = pd.Timestamp(datetime.now(ZoneInfo("Asia/Shanghai")).date())
        args.end = ymd(today if args.command == "week-check" else today - pd.Timedelta(days=1))
    args.start, args.end = ymd(args.start), ymd(args.end)
    if args.start > args.end or not 0 < args.fraction <= 1 or not 0 <= args.cost_bps < 1000:
        parser.error("Invalid date interval, fraction, or cost")
    with connect(args.db) as db:
        if args.command == "week-check":
            decision = check_week(db, args.end, args.offline)
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                    output.write(f"date={decision['date']}\nshould_update={str(decision['should_update']).lower()}\n")
            print(json.dumps(decision)); return
        if args.command in ("run", "sync"):
            sync(db, args.start, args.end, args.offline, args.reuse_repo)
        if args.command in ("run", "research"):
            bounds = db.execute("SELECT start,end FROM sync_state").fetchone()
            if not bounds or args.start < bounds[0] or args.end > bounds[1]:
                raise ValueError("Requested research dates exceed synchronized history; run sync first")
            sync_benchmark(db, args.start, args.end, args.offline)
            sync_corporate_actions(db, args.start, args.end, args.offline)
            apply_quote_overrides(db)
            risk_blocks = prepare_risk(db, args.end, args.offline)
            signals = build_signals(db, args.start, args.end, args.fraction, risk=True)
            fit_curve(db)
            coupons = pd.read_sql_query("SELECT * FROM coupons", db)
            backtest(db, signals, args.start, args.end, args.cost_bps / 10000, args.offline, risk_blocks, coupons)
            check_coupon_coverage(db, args.start, args.end)
            crosscheck(db, args.check_code, args.end, args.offline)
            export(db, args.out, args)


if __name__ == "__main__":
    main()
