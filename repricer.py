#!/usr/bin/env python3
"""Tito Repricer - local repricing cockpit for DelCampoXT (Amazon.com, FBA).
Scan inventory -> see Buy Box, fees, floor, margin per SKU -> edit/apply prices yourself.
Pure Python 3 standard library. Runs at http://localhost:8766
"""
import datetime
import glob
import gzip
import hashlib
import json
import py_compile
import shutil
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

VERSION = "1.10"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("TR_DATA_DIR") or os.path.expanduser("~/.tito-repricer")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
RULES_PATH = os.path.join(DATA_DIR, "rules.json")
COGS_PATH = os.path.join(DATA_DIR, "cogs.json")
SCAN_PATH = os.path.join(DATA_DIR, "scan.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
FEECACHE_PATH = os.path.join(DATA_DIR, "feecache.json")
REPORT_PATH = os.path.join(DATA_DIR, "report.json")
PORT = int(os.environ.get("TR_PORT", "8766"))
LOCK = threading.Lock()
HTTPD = None

DEFAULT_CONFIG = {
    "seller_id": "A2RZ3W8F9LN5TS",
    "marketplace_id": "ATVPDKIKX0DER",
    "endpoint": "https://sellingpartnerapi-na.amazon.com",
    "client_id": "",
    "client_secret": "",
    "refresh_token": "",
    "dry_run": True,
    "update_url": "",
    "sched_enabled": True,
    "scan_times": ["07:30", "16:30"],
}

DEFAULT_RULES = {
    "offset": -0.05,                 # price vs Buy Box when a competitor holds it
    "q4_match_buybox": True,         # match the Buy Box exactly instead of offset
    "q4_until": "2027-01-31",        # Q4 override expires after this date
    "skip_amazon_buybox": True,      # never chase a Buy Box that Amazon itself holds
    "floor_profit": 1.00,            # minimum profit per unit after all Amazon fees
    "default_cogs": 1.85,            # cost assumed when no cost is known (cosmetics default)
    "use_sku_cost": True,            # leading number in the SKU name is the unit cost
    "ten_break": True,               # keep items near $10 at $9.99 (low-price FBA fee tier)
    "ten_band_top": 10.75,           # targets between $10.00 and this propose $9.99 instead
}


# ----------------------------------------------------------------- storage
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _read_config_file(path):
    """Reads config.json; repairs curly quotes that TextEdit may add."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return None
    for attempt in (raw, raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")):
        try:
            data = json.loads(attempt)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return None


def load_config_file():
    data = _read_config_file(CONFIG_PATH)
    if data is None:
        data = _read_config_file(CONFIG_PATH + ".bak")   # last good copy
    return data or {}


def save_config_file(cfg):
    if os.path.exists(CONFIG_PATH) and _read_config_file(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "rb") as a, open(CONFIG_PATH + ".bak", "wb") as b:
                b.write(a.read())
            os.chmod(CONFIG_PATH + ".bak", 0o600)
        except OSError:
            pass
    save_json(CONFIG_PATH, cfg)


def get_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_config_file())
    return cfg


# ----------------------------------------------------------------- helpers
def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


_owner_cache = {"t": 0, "logins": []}


# ----------------------------------------------------------------- Amazon SP-API
_token = {"value": "", "exp": 0}


def has_creds(cfg=None):
    cfg = cfg or get_config()
    return all(cfg.get(k) for k in ("client_id", "client_secret", "refresh_token"))


def lwa_token():
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    cfg = get_config()
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"].strip(),
        "client_id": cfg["client_id"].strip(),
        "client_secret": cfg["client_secret"].strip(),
    }).encode()
    req = urllib.request.Request(os.environ.get("TR_LWA_URL", "https://api.amazon.com/auth/o2/token"), data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    _token["value"] = data["access_token"]
    _token["exp"] = time.time() + int(data.get("expires_in", 3600))
    return _token["value"]


def _sp_request(method, path, params=None, body=None):
    cfg = get_config()
    url = cfg["endpoint"] + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-amz-access-token": lwa_token(),
        "user-agent": "TitoRepricer/%s (Language=Python)" % VERSION,
        "accept": "application/json", "content-type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 4:
                time.sleep(1.6 * (attempt + 1))
                continue
            _raise_http(e)


def _raise_http(e):
    try:
        detail = json.loads(e.read().decode())
        msg = "; ".join(x.get("message", "") for x in detail.get("errors", [])) or str(detail)
    except Exception:
        msg = str(e)
    err = RuntimeError("Amazon %s: %s" % (e.code, msg))
    err.status = e.code
    raise err


def _money(d, key):
    v = (d or {}).get(key) or {}
    try:
        return float(v.get("Amount", 0) or 0)
    except Exception:
        return 0.0


# ----------------------------------------------------------------- rules / costs
def get_rules():
    r = dict(DEFAULT_RULES)
    r.update(load_json(RULES_PATH, {}))
    return r


def q4_active(rules):
    try:
        return bool(rules.get("q4_match_buybox")) and datetime.date.today().isoformat() <= str(rules.get("q4_until", ""))
    except Exception:
        return False


_SKU_COST = re.compile(r"^(\d{1,3}(?:\.\d{1,2})?)[-_]")


def cogs_for(sku, rules, cogs_map):
    """Returns (cost, source). Source: manual | sku | default"""
    e = cogs_map.get(sku)
    if e and e.get("cost") is not None:
        return float(e["cost"]), e.get("source", "manual")
    if rules.get("use_sku_cost"):
        m = _SKU_COST.match(sku.strip())
        if m:
            v = float(m.group(1))
            if 0.05 <= v <= 500:
                return v, "sku"
    return float(rules.get("default_cogs", 1.85)), "default"


# ----------------------------------------------------------------- Amazon data
def merchant_listings():
    """GET_MERCHANT_LISTINGS_ALL_DATA -> {sku: {price, asin, status, fulfillment}}"""
    rep = _sp_request("POST", "/reports/2021-06-30/reports", None,
                      {"reportType": "GET_MERCHANT_LISTINGS_ALL_DATA",
                       "marketplaceIds": [get_config()["marketplace_id"]]})
    rid = rep.get("reportId")
    doc_id = None
    for _ in range(90):
        r = _sp_request("GET", "/reports/2021-06-30/reports/%s" % rid)
        s = r.get("processingStatus")
        if s == "DONE":
            doc_id = r.get("reportDocumentId")
            break
        if s in ("CANCELLED", "FATAL"):
            raise RuntimeError("Amazon could not build the listings report (%s)" % s)
        time.sleep(4)
    if not doc_id:
        raise RuntimeError("Listings report timed out")
    d = _sp_request("GET", "/reports/2021-06-30/documents/%s" % doc_id)
    req = urllib.request.Request(d["url"], headers={"user-agent": "TitoRepricer/%s" % VERSION})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    if (d.get("compressionAlgorithm") or "").upper() == "GZIP":
        raw = gzip.decompress(raw)
    lines = raw.decode("utf-8", "replace").splitlines()
    head = [h.strip().lower().replace("_", "-") for h in lines[0].split("\t")] if lines else []

    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return -1
    ci_sku, ci_price, ci_asin = col("seller-sku", "sku"), col("price"), col("asin1", "asin", "product-id")
    ci_stat, ci_name, ci_ful = col("status"), col("item-name"), col("fulfillment-channel", "fulfilment-channel")
    out = {}
    for ln in lines[1:]:
        f = ln.split("\t")
        if ci_sku < 0 or len(f) <= ci_sku or not f[ci_sku].strip():
            continue
        def g(ci):
            return f[ci].strip() if 0 <= ci < len(f) else ""
        try:
            price = round(float(g(ci_price)), 2) if g(ci_price) else None
        except ValueError:
            price = None
        out[g(ci_sku)] = {"price": price, "asin": g(ci_asin).upper(), "status": g(ci_stat),
                          "title": g(ci_name), "fulfillment": g(ci_ful)}
    return out


def fba_inventory():
    """FBA inventory summaries -> {sku: {fnsku, asin, fulfillable, title}}"""
    cfg = get_config()
    out = {}
    params = {"granularityType": "Marketplace", "granularityId": cfg["marketplace_id"],
              "marketplaceIds": cfg["marketplace_id"], "details": "true"}
    token = None
    for _ in range(60):
        p = dict(params)
        if token:
            p["nextToken"] = token
        data = _sp_request("GET", "/fba/inventory/v1/summaries", p)
        pl = data.get("payload") or data
        for s in pl.get("inventorySummaries", []) or []:
            det = s.get("inventoryDetails") or {}
            out[s.get("sellerSku", "")] = {
                "fnsku": s.get("fnSku", ""), "asin": (s.get("asin") or "").upper(),
                "title": s.get("productName", ""),
                "fulfillable": int(det.get("fulfillableQuantity") or s.get("totalQuantity") or 0)}
        token = ((data.get("pagination") or {}).get("nextToken")) or ((pl.get("pagination") or {}).get("nextToken"))
        if not token:
            break
        time.sleep(0.6)
    return out


def parse_offer_payload(pl, cfg):
    summ = pl.get("Summary", {})
    offers = pl.get("Offers", [])
    bb = None
    amazon_selling = False
    for o in offers:
        if o.get("SellerId") == "ATVPDKIKX0DER":
            amazon_selling = True
        if o.get("IsBuyBoxWinner"):
            who = "Amazon" if o.get("SellerId") == "ATVPDKIKX0DER" else ("You" if o.get("SellerId") == cfg["seller_id"] else ("FBA" if o.get("IsFulfilledByAmazon") else "FBM"))
            bb = {"price": round(_money(o, "ListingPrice") + _money(o, "Shipping"), 2), "who": who}
    if not bb:
        for b in summ.get("BuyBoxPrices", []) or []:
            if str(b.get("condition", "")).lower() == "new":
                bb = {"price": round(_money(b, "LandedPrice"), 2), "who": ""}
    n_off = 0
    for n in summ.get("NumberOfOffers", []) or []:
        if str(n.get("condition", "")).lower() == "new":
            n_off += int(n.get("OfferCount", 0))
    fba = [round(_money(o, "ListingPrice") + _money(o, "Shipping"), 2) for o in offers if o.get("IsFulfilledByAmazon")]
    if not fba:
        fba = [round(_money(lp, "LandedPrice"), 2) for lp in summ.get("LowestPrices", []) or []
               if lp.get("fulfillmentChannel") == "Amazon" and str(lp.get("condition", "")).lower() == "new"]
    return {"buybox": bb, "amazon_selling": amazon_selling, "offers": n_off,
            "lowest_fba": (min(fba) if fba else None)}


def batch_offers(asins, progress=None):
    """getItemOffersBatch, 20 ASINs per call -> {asin: parsed}"""
    cfg = get_config()
    out = {}
    asins = [a for a in dict.fromkeys(asins) if a]
    for i in range(0, len(asins), 20):
        chunk = asins[i:i + 20]
        body = {"requests": [{"uri": "/products/pricing/v0/items/%s/offers" % a, "method": "GET",
                              "MarketplaceId": cfg["marketplace_id"], "ItemCondition": "New",
                              "CustomerType": "Consumer"} for a in chunk]}
        for attempt in range(5):
            try:
                data = _sp_request("POST", "/batches/products/pricing/v0/itemOffers", None, body)
                break
            except RuntimeError as e:
                if getattr(e, "status", 0) == 429 and attempt < 4:
                    time.sleep(4 * (attempt + 1))
                    continue
                raise
        for a, resp in zip(chunk, data.get("responses", []) or []):
            pl = ((resp.get("body") or {}).get("payload")) or {}
            if pl:
                out[a] = parse_offer_payload(pl, cfg)
        if progress:
            progress(min(i + 20, len(asins)), len(asins))
        time.sleep(2.2)
    return out


def batch_fees(items, progress=None):
    """items: [(sku, asin, price)] -> {sku: {total, referral_pct, fixed}} with a 24h cache.
    Uses the per-ASIN fee estimate endpoint (the batch endpoint is not allowed for this app)."""
    cfg = get_config()
    cache = load_json(FEECACHE_PATH, {})
    now = time.time()
    out, todo = {}, []
    for sku, asin, price in items:
        if not price or price <= 0 or not asin:
            continue
        key = "%s:%.1f" % (asin, price)   # dime-band cache
        c = cache.get(key)
        if c and now - c.get("t", 0) < 24 * 3600:
            out[sku] = c["v"]
        else:
            todo.append((sku, asin, round(price, 2), key))
    for i, (sku, asin, price, key) in enumerate(todo):
        body = {"FeesEstimateRequest": {"MarketplaceId": cfg["marketplace_id"], "IsAmazonFulfilled": True,
                                        "Identifier": "tr-%s" % sku,
                                        "PriceToEstimateFees": {"ListingPrice": {"CurrencyCode": "USD", "Amount": price}}}}
        data = None
        for attempt in range(5):
            try:
                data = _sp_request("POST", "/products/fees/v0/items/%s/feesEstimate" % asin, None, body)
                break
            except RuntimeError as e:
                if getattr(e, "status", 0) == 429 and attempt < 4:
                    time.sleep(2.5 * (attempt + 1))
                    continue
                if getattr(e, "status", 0) in (400, 404):
                    data = None
                    break
                raise
        if data:
            fr = ((data.get("payload") or {}).get("FeesEstimateResult")) or {}
            est = fr.get("FeesEstimate") or {}
            total = _money(est, "TotalFeesEstimate")
            referral = 0.0
            fixed = 0.0
            for fd in est.get("FeeDetailList", []) or []:
                amt = _money(fd, "FinalFee") or _money(fd, "FeeAmount")
                if fd.get("FeeType") == "ReferralFee":
                    referral += amt
                else:
                    fixed += amt
            if total > 0:
                ref_pct = max(0.05, min(0.25, referral / price)) if price else 0.15
                v = {"total": round(total, 2), "referral_pct": round(ref_pct, 4), "fixed": round(fixed, 2), "at": price}
                out[sku] = v
                cache[key] = {"t": now, "v": v}
        if progress:
            progress(i + 1, len(todo))
        if (i + 1) % 50 == 0:
            with LOCK:
                save_json(FEECACHE_PATH, cache)
        time.sleep(0.55)
    with LOCK:
        save_json(FEECACHE_PATH, cache)
    return out


# ----------------------------------------------------------------- rules engine
def floor_price(cogs, fee, profit):
    """Lowest price where price - fees(price) - cogs >= profit."""
    if not fee:
        return None
    return round((cogs + fee["fixed"] + profit) / max(0.5, 1.0 - fee["referral_pct"]) + 0.004, 2)


def propose(item, rules):
    """item has: price, buybox {price, who}, fee, cogs. Adds proposal fields in place."""
    cur = item.get("price")
    bb = item.get("buybox") or {}
    fee = item.get("fee")
    fl = floor_price(item["cogs"], fee, float(rules.get("floor_profit", 1.0)))
    item["floor"] = fl
    flags = []
    target = None
    why = ""
    if not bb.get("price"):
        why = "no competing offer - hold"
        flags.append("no-bb")
    elif bb.get("who") == "You":
        why = "you hold the Buy Box - hold"
        flags.append("bb-yours")
    elif bb.get("who") == "Amazon" and rules.get("skip_amazon_buybox", True):
        why = "Amazon holds the Buy Box - skipped"
        flags.append("bb-amazon")
    else:
        if q4_active(rules):
            target = round(bb["price"], 2)
            why = "Q4: match Buy Box"
        else:
            target = round(bb["price"] + float(rules.get("offset", -0.05)), 2)
            why = "Buy Box %+0.2f" % float(rules.get("offset", -0.05))
    if target is not None:
        if rules.get("ten_break") and 10.00 <= target <= float(rules.get("ten_band_top", 10.75)):
            target = 9.99
            why += " -> $9.99 (fee break)"
            flags.append("ten-break")
        if fl and target < fl:
            target = fl
            why = "floor stop (was %s)" % why
            flags.append("at-floor")
    if fl and cur and cur < fl:
        flags.append("below-floor-now")
    if target is not None and cur is not None and abs(target - cur) < 0.005:
        target = None
        why = "already at target"
    item["proposed"] = target
    item["why"] = why
    item["flags"] = flags
    if fee and target:
        item["margin_at_proposed"] = round(target * (1 - fee["referral_pct"]) - fee["fixed"] - item["cogs"], 2)
    elif fee and cur:
        item["margin_at_proposed"] = round(cur * (1 - fee["referral_pct"]) - fee["fixed"] - item["cogs"], 2)
    else:
        item["margin_at_proposed"] = None
    item["margin_now"] = round(cur * (1 - fee["referral_pct"]) - fee["fixed"] - item["cogs"], 2) if (fee and cur) else None
    if item["margin_now"] is not None and item["margin_now"] < -0.004:
        flags.append("losing")
        item["flags"] = flags
    return item


# ----------------------------------------------------------------- scan
SCAN = {"state": "idle", "step": "", "done": 0, "total": 0, "error": "", "ts": 0}


def run_scan(scheduled=False):
    rules = get_rules()
    cogs_map = load_json(COGS_PATH, {})
    try:
        SCAN.update(state="running", step="Loading your listings report", done=0, total=0, error="")
        listings = merchant_listings()
        SCAN.update(step="Loading FBA inventory")
        inv = fba_inventory()
        items = []
        for sku, s in inv.items():
            if s["fulfillable"] <= 0:
                continue
            l = listings.get(sku, {})
            cost, csrc = cogs_for(sku, rules, cogs_map)
            items.append({"sku": sku, "asin": s["asin"] or l.get("asin", ""), "fnsku": s["fnsku"],
                          "title": s["title"] or l.get("title", ""), "qty": s["fulfillable"],
                          "price": l.get("price"), "cogs": cost, "cogs_source": csrc})
        SCAN.update(step="Loading Buy Box offers", done=0, total=len(items))
        offers = batch_offers([x["asin"] for x in items], progress=lambda d, t: SCAN.update(done=d, total=t))
        for x in items:
            o = offers.get(x["asin"]) or {}
            x["buybox"] = o.get("buybox")
            x["amazon_selling"] = o.get("amazon_selling", False)
            x["offers"] = o.get("offers", 0)
            x["lowest_fba"] = o.get("lowest_fba")
        SCAN.update(step="Loading fee estimates", done=0, total=len(items))
        fee_in = [(x["sku"], x["asin"], (x.get("buybox") or {}).get("price") or x.get("price")) for x in items]
        fees = batch_fees(fee_in, progress=lambda d, t: SCAN.update(done=d, total=t))
        for x in items:
            x["fee"] = fees.get(x["sku"])
            propose(x, rules)
        items.sort(key=lambda x: (x["proposed"] is None, x["sku"].lower()))
        snap = {"ts": time.time(), "items": items, "rules": rules, "q4": q4_active(rules)}
        prev = load_json(SCAN_PATH, {})
        with LOCK:
            save_json(SCAN_PATH, snap)
        if prev.get("items"):
            rpt = make_report(prev, snap)
            with LOCK:
                save_json(REPORT_PATH, rpt)
            if scheduled:
                s = rpt["summary"]
                notify_mac("Tito Repricer", "%d need change - %d losing - lost Buy Box on %d, won %d" %
                           (s["proposals"], s["losing"], s["lost_bb"], s["won_bb"]))
        elif scheduled:
            notify_mac("Tito Repricer", "Scan done - %d items loaded" % len(items))
        mark_outcomes(snap)
        SCAN.update(state="done", step="", ts=time.time())
    except Exception as e:
        SCAN.update(state="error", error=str(e))
        if scheduled:
            notify_mac("Tito Repricer", "Scheduled scan failed: %s" % str(e)[:120])


_sched = {"last": ""}


def _sched_loop():
    time.sleep(20)
    while True:
        try:
            cfg = get_config()
            if cfg.get("sched_enabled", True) and has_creds(cfg):
                now = datetime.datetime.now()
                hm = now.strftime("%H:%M")
                slot = now.strftime("%Y-%m-%d ") + hm
                if hm in [str(t).strip() for t in (cfg.get("scan_times") or [])] and _sched["last"] != slot and SCAN["state"] != "running":
                    _sched["last"] = slot
                    run_scan(scheduled=True)
        except Exception:
            pass
        time.sleep(25)


def make_report(prev, snap):
    """Compares two scans -> notable changes + summary counts."""
    rows = []
    old = {x["sku"]: x for x in (prev.get("items") or [])}
    for x in snap.get("items", []):
        o = old.get(x["sku"])
        bb_n = (x.get("buybox") or {})
        if not o:
            continue
        bb_o = (o.get("buybox") or {})
        t = (x.get("title") or "")[:60]
        if bb_o.get("price") and bb_n.get("price") and abs(bb_n["price"] - bb_o["price"]) > 0.004:
            rows.append({"kind": "bb_up" if bb_n["price"] > bb_o["price"] else "bb_down", "sku": x["sku"], "title": t,
                         "detail": "Buy Box %.2f -> %.2f" % (bb_o["price"], bb_n["price"])})
        if bb_o.get("who") == "You" and bb_n.get("who") not in ("You", "", None):
            rows.append({"kind": "lost_bb", "sku": x["sku"], "title": t,
                         "detail": "lost the Buy Box to %s at %s" % (bb_n.get("who"), bb_n.get("price"))})
        if bb_n.get("who") == "You" and bb_o.get("who") not in ("You", "", None):
            rows.append({"kind": "won_bb", "sku": x["sku"], "title": t, "detail": "you WON the Buy Box"})
        was_losing = o.get("margin_now") is not None and o["margin_now"] < -0.004
        is_losing = x.get("margin_now") is not None and x["margin_now"] < -0.004
        if is_losing and not was_losing:
            rows.append({"kind": "new_loser", "sku": x["sku"], "title": t,
                         "detail": "now losing %.2f per unit" % abs(x["margin_now"])})
        if "below-floor-now" in (x.get("flags") or []) and "below-floor-now" not in (o.get("flags") or []):
            rows.append({"kind": "below_floor", "sku": x["sku"], "title": t,
                         "detail": "price %.2f fell below floor %.2f" % (x.get("price") or 0, x.get("floor") or 0)})
    summ = {"proposals": sum(1 for x in snap.get("items", []) if x.get("proposed") is not None),
            "losing": sum(1 for x in snap.get("items", []) if x.get("margin_now") is not None and x["margin_now"] < -0.004),
            "lost_bb": sum(1 for r in rows if r["kind"] == "lost_bb"),
            "won_bb": sum(1 for r in rows if r["kind"] == "won_bb"),
            "bb_moves": sum(1 for r in rows if r["kind"] in ("bb_up", "bb_down")),
            "new_losers": sum(1 for r in rows if r["kind"] == "new_loser")}
    return {"ts": snap.get("ts"), "prev_ts": prev.get("ts"), "summary": summ, "rows": rows[:200]}


def notify_mac(title, text):
    try:
        run(["osascript", "-e", 'display notification "%s" with title "%s"' %
             (text.replace("\\", "").replace('"', "'"), title.replace('"', "'"))], timeout=5)
    except Exception:
        pass


def mark_outcomes(snap):
    """For recent applied changes, record whether we now hold the Buy Box."""
    try:
        with LOCK:
            hist = load_json(HISTORY_PATH, [])
            by_sku = {x["sku"]: x for x in snap["items"]}
            changed = False
            for h in hist:
                if h.get("outcome") or h.get("dry_run") or time.time() - h.get("ts", 0) > 7 * 86400:
                    continue
                it = by_sku.get(h.get("sku"))
                if not it:
                    continue
                who = (it.get("buybox") or {}).get("who", "")
                h["outcome"] = "won" if who == "You" else ("lost" if who else "unknown")
                h["outcome_ts"] = time.time()
                changed = True
            if changed:
                save_json(HISTORY_PATH, hist)
    except Exception:
        pass


# ----------------------------------------------------------------- apply
def patch_price(sku, price):
    cfg = get_config()
    m = cfg["marketplace_id"]
    body = {"productType": "PRODUCT",
            "patches": [{"op": "replace", "path": "/attributes/purchasable_offer",
                         "value": [{"currency": "USD", "marketplace_id": m,
                                    "our_price": [{"schedule": [{"value_with_tax": round(price, 2)}]}]}]}]}
    data = _sp_request("PATCH", "/listings/2021-08-01/items/%s/%s" % (
        urllib.parse.quote(cfg["seller_id"]), urllib.parse.quote(sku, safe="")),
        {"marketplaceIds": m, "includedData": "issues"}, body)
    issues = [i.get("message", "") for i in (data.get("issues") or []) if i.get("severity") == "ERROR"]
    return data.get("status", ""), issues


def apply_changes(changes):
    """changes: [{sku, price}] -> results; logs history. Honors dry_run."""
    cfg = get_config()
    dry = bool(cfg.get("dry_run", True))
    snap = load_json(SCAN_PATH, {})
    by_sku = {x["sku"]: x for x in snap.get("items", [])}
    results = []
    hist_add = []
    for ch in changes:
        sku = ch["sku"]
        price = round(float(ch["price"]), 2)
        it = by_sku.get(sku, {})
        old = it.get("price")
        entry = {"ts": time.time(), "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "sku": sku, "asin": it.get("asin", ""), "title": (it.get("title") or "")[:90],
                 "old": old, "new": price, "bb_at_apply": (it.get("buybox") or {}).get("price"),
                 "dry_run": dry}
        if dry:
            entry["status"] = "DRY RUN"
            results.append({"sku": sku, "ok": True, "status": "DRY RUN (no change sent)"})
        else:
            try:
                status, issues = patch_price(sku, price)
                ok = status == "ACCEPTED" and not issues
                entry["status"] = status if ok else ("%s: %s" % (status, "; ".join(issues)))
                results.append({"sku": sku, "ok": ok, "status": entry["status"]})
                if ok:
                    it["price"] = price
                    it["proposed"] = None
                    it["why"] = "changed just now"
            except Exception as e:
                entry["status"] = "ERROR: %s" % e
                results.append({"sku": sku, "ok": False, "status": str(e)})
            time.sleep(0.35)
        hist_add.append(entry)
    with LOCK:
        hist = load_json(HISTORY_PATH, [])
        hist = hist_add + hist
        save_json(HISTORY_PATH, hist[:2000])
        if not dry:
            save_json(SCAN_PATH, snap)
    return results
# ----------------------------------------------------------------- self-update (GitHub)
_upd_cache = {"t": 0.0, "data": None}


def _ver_tuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)
    except Exception:
        return (0,)


def _update_url():
    u = (get_config().get("update_url") or "").strip().rstrip("/")
    if u.startswith("http://127.0.0.1") or u.startswith("http://localhost"):
        return u
    return u if u.startswith("https://") else ""


def _fetch_update(path, limit=20 * 1024 * 1024):
    req = urllib.request.Request(_update_url() + "/" + path,
                                 headers={"User-Agent": "TitoRepricer/%s" % VERSION, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large: %s" % path)
    return data


def check_update(force=False):
    if not _update_url():
        return {"configured": False, "current": VERSION}
    now = time.time()
    if not force and _upd_cache["data"] and now - _upd_cache["t"] < 6 * 3600:
        return _upd_cache["data"]
    man = json.loads(_fetch_update("version.json", 256 * 1024).decode("utf-8"))
    latest = str(man.get("version") or "")
    out = {"configured": True, "current": VERSION, "latest": latest,
           "notes": str(man.get("notes") or "")[:300],
           "update_available": _ver_tuple(latest) > _ver_tuple(VERSION)}
    _upd_cache.update(t=now, data=out)
    return out


def _mirror_dirs():
    out = []
    for d in glob.glob(os.path.expanduser("~/Library/CloudStorage/GoogleDrive-*/My Drive/Tito-Repricer")):
        if os.path.isdir(d) and os.path.realpath(d) != os.path.realpath(APP_DIR):
            out.append(d)
    return out


def apply_update():
    if not _update_url():
        return {"error": "No update source is set. Paste the GitHub address in Settings first."}
    man = json.loads(_fetch_update("version.json", 256 * 1024).decode("utf-8"))
    latest = str(man.get("version") or "")
    if _ver_tuple(latest) <= _ver_tuple(VERSION):
        return {"ok": True, "updated": False, "current": VERSION, "latest": latest}
    info = (man.get("files") or {}).get("repricer.py") or {}
    data = _fetch_update("repricer.py")
    want = str(info.get("sha256") or "").lower()
    if not want or hashlib.sha256(data).hexdigest() != want:
        return {"error": "The download did not verify (checksum). Nothing was changed - try again."}
    tmp = tempfile.mkdtemp(prefix="tito-update-")
    try:
        new_path = os.path.join(tmp, "repricer.py")
        with open(new_path, "wb") as f:
            f.write(data)
        py_compile.compile(new_path, doraise=True)
        with open(new_path, encoding="utf-8") as f:
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        if not m or m.group(1) != latest:
            return {"error": "Version inside the download (%s) does not match version.json (%s)." % (m and m.group(1), latest)}
        bdir = os.path.join(DATA_DIR, "backup-%s" % VERSION)
        shutil.rmtree(bdir, ignore_errors=True)
        os.makedirs(bdir, exist_ok=True)
        cur = os.path.join(APP_DIR, "repricer.py")
        if os.path.exists(cur):
            shutil.copy2(cur, os.path.join(bdir, "repricer.py"))
        shutil.copy2(new_path, cur + ".new")
        os.replace(cur + ".new", cur)
        for d in _mirror_dirs():
            try:
                shutil.copy2(new_path, os.path.join(d, "repricer.py"))
            except Exception:
                pass
        return {"ok": True, "updated": True, "from": VERSION, "to": latest, "backup": bdir}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _restart_self():
    time.sleep(0.8)
    try:
        if HTTPD:
            HTTPD.socket.close()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable, os.path.join(APP_DIR, "repricer.py")])


# ----------------------------------------------------------------- server
class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode() or "{}") if n else {}

    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower().rsplit(":", 1)[0]
        ok = host in ("localhost", "127.0.0.1", "[::1]")
        origin = (self.headers.get("Origin") or "").lower()
        if origin and origin != "null":
            oh = urllib.parse.urlparse(origin).hostname or ""
            ok = ok and oh in ("localhost", "127.0.0.1", "::1")
        return ok

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html")
            if u.path == "/api/state":
                cfg = get_config()
                snap = load_json(SCAN_PATH, {})
                return self._send(200, {
                    "version": VERSION, "dry_run": bool(cfg.get("dry_run", True)), "live": has_creds(cfg),
                    "rules": get_rules(), "q4": q4_active(get_rules()),
                    "scan_ts": snap.get("ts", 0), "items": snap.get("items", []), "scan": SCAN})
            if u.path == "/api/scan/status":
                return self._send(200, SCAN)
            if u.path == "/api/report":
                return self._send(200, load_json(REPORT_PATH, {}))
            if u.path == "/api/update/check":
                try:
                    return self._send(200, check_update(force=q.get("force") == "1"))
                except Exception as e:
                    return self._send(200, {"configured": True, "current": VERSION, "error": str(e)})
            if u.path == "/api/history":
                return self._send(200, {"rows": load_json(HISTORY_PATH, [])[:300]})
            if u.path == "/api/cogs":
                snap = load_json(SCAN_PATH, {})
                cmap = load_json(COGS_PATH, {})
                rows = [{"sku": x["sku"], "title": (x.get("title") or "")[:70], "cogs": x["cogs"],
                         "source": x["cogs_source"]} for x in snap.get("items", [])]
                return self._send(200, {"rows": rows, "manual": cmap})
            if u.path == "/api/settings":
                cfg = get_config()
                return self._send(200, {"client_id": cfg.get("client_id", ""),
                                        "has_secret": bool(cfg.get("client_secret")),
                                        "has_token": bool(cfg.get("refresh_token")),
                                        "seller_id": cfg.get("seller_id", ""), "dry_run": bool(cfg.get("dry_run", True)),
                                        "live": has_creds(cfg), "version": VERSION,
                                        "sched_enabled": bool(cfg.get("sched_enabled", True)),
                                        "scan_times": cfg.get("scan_times") or ["07:30", "16:30"],
                                        "update_url": cfg.get("update_url", ""),
                                        "fnsku_keys_found": os.path.exists(os.path.expanduser("~/.fnsku-label-printer/config.json"))})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not self._host_ok() or (u.path != "/api/quit" and self.headers.get("X-App") != "1"):
            return self._send(403, {"error": "forbidden"})
        try:
            b = self._body()
            if u.path == "/api/scan":
                if SCAN["state"] == "running":
                    return self._send(200, {"ok": True, "already": True})
                if not has_creds():
                    return self._send(400, {"error": "Connect Amazon first (Settings)."})
                threading.Thread(target=run_scan, daemon=True).start()
                return self._send(200, {"ok": True})
            if u.path == "/api/apply":
                changes = b.get("changes") or []
                if not changes:
                    return self._send(400, {"error": "Nothing selected"})
                for ch in changes:
                    p = float(ch.get("price") or 0)
                    if p <= 0 or p > 5000:
                        return self._send(400, {"error": "Bad price for %s" % ch.get("sku")})
                return self._send(200, {"results": apply_changes(changes),
                                        "dry_run": bool(get_config().get("dry_run", True))})
            if u.path == "/api/update/apply":
                out = apply_update()
                if out.get("updated"):
                    threading.Thread(target=_restart_self, daemon=True).start()
                return self._send(200, out)
            if u.path == "/api/cogs":
                with LOCK:
                    cmap = load_json(COGS_PATH, {})
                    if "csv" in b:
                        n = 0
                        for ln in str(b["csv"]).splitlines():
                            parts = re.split(r"[\t,;]", ln)
                            if len(parts) >= 2:
                                sku = parts[0].strip().strip('"')
                                try:
                                    cost = float(parts[-1].strip().strip('"').replace("$", ""))
                                except ValueError:
                                    continue
                                if sku and sku.lower() not in ("sku", "msku", "seller-sku") and 0 < cost < 10000:
                                    cmap[sku] = {"cost": round(cost, 2), "source": "manual"}
                                    n += 1
                        save_json(COGS_PATH, cmap)
                        return self._send(200, {"ok": True, "imported": n})
                    sku = (b.get("sku") or "").strip()
                    if not sku:
                        return self._send(400, {"error": "no sku"})
                    if b.get("cost") in (None, ""):
                        cmap.pop(sku, None)
                    else:
                        cmap[sku] = {"cost": round(float(b["cost"]), 2), "source": "manual"}
                    save_json(COGS_PATH, cmap)
                return self._send(200, {"ok": True})
            if u.path == "/api/rules":
                with LOCK:
                    r = load_json(RULES_PATH, {})
                    for k in DEFAULT_RULES:
                        if k in b:
                            r[k] = b[k]
                    save_json(RULES_PATH, r)
                return self._send(200, {"ok": True, "rules": get_rules()})
            if u.path == "/api/settings":
                with LOCK:
                    cfg = load_config_file()
                    if b.get("import_fnsku"):
                        src = _read_config_file(os.path.expanduser("~/.fnsku-label-printer/config.json")) or {}
                        for k in ("client_id", "client_secret", "refresh_token", "seller_id", "marketplace_id"):
                            if src.get(k):
                                cfg[k] = src[k]
                    for k in ("client_id", "client_secret", "refresh_token"):
                        if b.get(k):
                            cfg[k] = str(b[k]).strip()
                    if "seller_id" in b and b["seller_id"]:
                        cfg["seller_id"] = str(b["seller_id"]).strip()
                    if "dry_run" in b:
                        cfg["dry_run"] = bool(b["dry_run"])
                    if "sched_enabled" in b:
                        cfg["sched_enabled"] = bool(b["sched_enabled"])
                    if "scan_times" in b:
                        ts = [t.strip() for t in b["scan_times"] if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(t).strip())]
                        cfg["scan_times"] = ts[:4] or ["07:30", "16:30"]
                    if "update_url" in b:
                        cfg["update_url"] = str(b["update_url"]).strip()
                    save_config_file(cfg)
                    try:
                        os.chmod(CONFIG_PATH, 0o600)
                    except OSError:
                        pass
                _token.update(value="", exp=0)
                msg = "Saved."
                if has_creds():
                    try:
                        lwa_token()
                        msg = "Saved. Amazon connection OK."
                    except Exception as e:
                        msg = "Saved, but Amazon login failed: %s" % e
                return self._send(200, {"ok": True, "message": msg})
            if u.path == "/api/quit":
                self._send(200, {"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tito Repricer</title>
<style>
:root{--ink:#1e2227;--mute:#6b7078;--line:#e4e6e2;--bg:#f4f5f2;--card:#fff;--green:#0c6b48;--red:#a01f12;--amber:#8a4b00;--amberbg:#fff4e0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px -apple-system,Helvetica,Arial,sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:16px 20px 60px}
header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
h1{font-size:21px;margin:0 10px 0 0}
.pill{font:600 12px -apple-system;background:#e9ebe7;border-radius:20px;padding:5px 11px;color:var(--mute)}
.pill.on{background:#e2efe7;color:var(--green)}
.pill.dry{background:var(--amberbg);color:var(--amber);border:1px solid #f3d29a}
.pill.livemode{background:#fde8e5;color:var(--red);border:1px solid #f0b5ad}
.sp{flex:1}
button{font:600 13.5px -apple-system;border:1px solid #cfd2cc;background:#fff;border-radius:9px;padding:8px 14px;cursor:pointer}
button:hover{background:#f6f7f4}button.primary{background:var(--green);border-color:var(--green);color:#fff}
button.primary:hover{background:#0a5a3d}button:disabled{opacity:.45;cursor:default}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
.chip{font:600 12.5px -apple-system;border:1px solid #cfd2cc;background:#fff;border-radius:18px;padding:6px 12px;cursor:pointer;color:var(--mute)}
.chip.on{background:var(--ink);color:#fff;border-color:var(--ink)}
input[type=text],input[type=number]{font:14px -apple-system;border:1px solid #cfd2cc;border-radius:8px;padding:7px 10px;background:#fff}
.card{background:var(--card);border-radius:14px;box-shadow:0 1px 4px rgba(0,0,0,.06);padding:4px 14px;margin-bottom:18px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{font:700 11px -apple-system;text-transform:uppercase;letter-spacing:.4px;color:var(--mute);text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid #f0f1ee;vertical-align:middle}
tr:hover td{background:#fafbf8}
.t{max-width:330px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.sub{font-size:11px;color:var(--mute)}
.f{display:inline-block;font:700 10.5px -apple-system;border-radius:6px;padding:2px 7px;margin:1px 2px 1px 0;white-space:nowrap}
.f.red{background:#fde8e5;color:var(--red)}.f.amber{background:var(--amberbg);color:var(--amber)}
.f.grey{background:#eef0ec;color:var(--mute)}.f.green{background:#e2efe7;color:var(--green)}
td input.pp{width:84px;text-align:right;font-variant-numeric:tabular-nums;padding:5px 7px}
td input.pp.chg{border-color:var(--green);background:#f1f8f4;font-weight:700}
a.mlink{display:inline-block;font:700 11px -apple-system;color:var(--green);cursor:pointer;border-bottom:1px dotted var(--green);margin-top:2px}
a.mlink:hover{color:#0a5a3d}
button.up1{font:700 12px -apple-system;padding:6px 10px;border-radius:8px;border:1px solid var(--green);color:var(--green);background:#fff}
button.up1:hover{background:#e2efe7}button.up1:disabled{border-color:#cfd2cc;color:#aab0a8;background:#fff}
button.up1.done{background:var(--green);color:#fff}
.msg{font:600 13px -apple-system;padding:9px 13px;border-radius:9px;display:none;margin:8px 0}
.msg.ok{display:block;background:#e2efe7;color:var(--green)}.msg.err{display:block;background:#fde8e5;color:var(--red)}
.prog{display:none;align-items:center;gap:10px;font:600 13px -apple-system;color:var(--mute)}
.prog .barbg{width:220px;height:8px;background:#e4e6e2;border-radius:6px;overflow:hidden}
.prog .barfill{height:100%;background:var(--green);width:0%}
h2{font-size:15.5px;margin:26px 0 8px}
dialog{border:none;border-radius:16px;box-shadow:0 12px 50px rgba(0,0,0,.25);padding:0;width:560px;max-width:95vw}
dialog::backdrop{background:rgba(20,22,20,.35)}
.in{padding:20px 22px}
.fld{margin-bottom:12px}.fld label{display:block;font:700 12px -apple-system;color:var(--mute);margin-bottom:4px}
.fld input[type=text],.fld input[type=number]{width:100%}
.row{display:flex;gap:10px;align-items:center;margin-top:14px}
.switch{display:flex;gap:8px;align-items:center;font:600 13px -apple-system}
.small{font-size:12px;color:var(--mute)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}.t{max-width:160px}}
</style></head><body>
<div class="wrap">
<header><h1>Tito Repricer</h1>
 <span id="mode" class="pill dry">DRY RUN</span>
 <span id="live" class="pill">…</span>
 <span id="q4" class="pill" style="display:none">Q4: matching Buy Box</span>
 <span id="age" class="pill">no scan yet</span>
 <span id="upd" class="pill" style="display:none;background:#fff4e0;color:#8a4b00;border:1px solid #f3d29a;cursor:pointer"></span>
 <span class="sp"></span>
 <button id="gear">Settings</button><button id="quit" title="Stop Tito Repricer">Quit</button></header>

<div class="bar">
 <button class="primary" id="scanBtn">Scan now</button>
 <div class="prog" id="prog"><span id="progTxt"></span><div class="barbg"><div class="barfill" id="progFill"></div></div></div>
 <span class="sp"></span>
 <span style="position:relative;display:inline-flex;align-items:center"><input type="text" id="search" placeholder="Search SKU / title / ASIN" style="width:230px;padding-right:30px"><button id="searchClr" type="button" title="Clear search (Esc)" style="position:absolute;right:4px;border:none;background:#e9ebe7;border-radius:50%;width:22px;height:22px;padding:0;font:700 12px -apple-system;color:#6b7078;display:none">✕</button></span>
</div>
<div class="bar" id="chips">
 <span class="chip on" data-k="changes">Needs change</span>
 <span class="chip" data-k="floor">At / below floor</span>
 <span class="chip" data-k="losing">Losing</span>
 <span class="chip" data-k="amazon">Amazon has Buy Box</span>
 <span class="chip" data-k="yours">You have Buy Box</span>
 <span class="chip" data-k="nocost">Default cost</span>
 <span class="chip" data-k="all">All</span>
 <span class="sp"></span>
 <button id="mBB" title="Fill the Proposed box of every ticked row with its Buy Box price">Match Buy Box</button>
 <button id="mFBA" title="Fill the Proposed box of every ticked row with the lowest FBA offer price">Match lowest FBA</button>
 <button id="mFLR" title="Fill the Proposed box of every ticked row with its floor (break-even + profit) price">Raise to floor</button>
 <button id="applyBtn" disabled>Apply selected (0)</button>
</div>
<div id="msg" class="msg"></div>
<div class="card" id="rptCard" style="display:none;padding:12px 14px">
 <div style="display:flex;align-items:center;gap:10px"><b style="font-size:14px">Since last scan</b><span class="small" id="rptWhen"></span><span class="sp"></span><button id="rptToggle" style="padding:4px 10px;font-size:12px">Show</button></div>
 <div class="small" id="rptSum" style="margin-top:6px"></div>
 <div id="rptRows" style="display:none;margin-top:8px"></div>
</div>
<div class="card"><table id="tbl"><thead><tr>
 <th><input type="checkbox" id="selAll"></th><th>SKU</th><th>Title</th><th class="num">Qty</th>
 <th class="num">Cost</th><th class="num">Now</th><th class="num">Buy Box</th><th class="num">Floor</th>
 <th class="num">Proposed</th><th></th><th class="num">Margin</th><th>Why</th></tr></thead>
<tbody id="rows"><tr><td colspan="12" class="small" style="padding:22px">Press <b>Scan now</b> to load your inventory, Buy Box prices, fees and proposals.</td></tr></tbody></table></div>

<h2>History</h2>
<div class="card"><table><thead><tr><th>When</th><th>SKU</th><th>Title</th><th class="num">Old</th><th class="num">New</th><th>Status</th><th>Buy Box after</th><th></th></tr></thead>
<tbody id="hist"><tr><td colspan="8" class="small" style="padding:16px">No changes yet.</td></tr></tbody></table></div>

<h2>Costs</h2>
<div class="card" style="padding:12px 14px">
 <div class="small" style="margin-bottom:8px">Cost per unit comes from: your manual entry here → the leading number in the SKU (<span class="sub">6-dl67-26 → $6.00</span>) → the default. Paste an InventoryLab export (SKU,cost per line) to import.</div>
 <div class="bar"><input type="text" id="cSku" placeholder="SKU" style="width:220px"><input type="number" id="cCost" placeholder="cost $" step="0.01" style="width:110px"><button id="cSet">Set cost</button>
 <span class="sp"></span><button id="cImp">Import pasted CSV…</button></div>
 <div id="cMsg" class="msg"></div>
 <div class="small">Rows using the <b>default</b> cost appear under the "Default cost" filter above — set their real cost to sharpen floors.</div>
</div>
</div>

<dialog id="dlg"><div class="in">
<h3 style="margin-top:0">Settings</h3>
<div class="fld"><label>Scheduled scans</label>
 <div class="switch"><input type="checkbox" id="s_sched"><span>Scan automatically and notify me</span></div>
 <div class="row" style="margin-top:6px"><input type="text" id="s_t1" style="width:90px" placeholder="07:30"><input type="text" id="s_t2" style="width:90px" placeholder="16:30"><span class="small">24-hour times. The app must be open (minimized is fine).</span></div></div>
<div class="fld"><label>Updates — address of the code on GitHub</label><input type="text" id="s_upd" autocomplete="off" spellcheck="false" placeholder="https://raw.githubusercontent.com/delcampoxt-cmd/tito-repricer/main"></div>
<div class="fld"><button id="s_chk" type="button">Check for updates</button><span class="small" id="s_updnote" style="margin-left:8px"></span></div>
<hr style="border:none;border-top:1px solid #eee;margin:14px 0">
<div class="fld"><label>Price changes</label>
 <div class="switch"><input type="checkbox" id="s_dry"><span><b>Dry run</b> — proposals only, never send price changes to Amazon</span></div></div>
<hr style="border:none;border-top:1px solid #eee;margin:14px 0">
<div class="small" style="margin-bottom:8px"><b>Rules</b> — how proposals are computed.</div>
<div class="grid2">
<div class="fld"><label>Offset vs Buy Box ($)</label><input type="number" id="r_offset" step="0.01"></div>
<div class="fld"><label>Floor profit per unit ($)</label><input type="number" id="r_profit" step="0.05"></div>
<div class="fld"><label>Default cost when unknown ($)</label><input type="number" id="r_defcogs" step="0.01"></div>
<div class="fld"><label>Q4 override ends (YYYY-MM-DD)</label><input type="text" id="r_q4until"></div>
</div>
<div class="switch" style="margin-bottom:6px"><input type="checkbox" id="r_q4"><span>Q4 override: match the Buy Box exactly</span></div>
<div class="switch" style="margin-bottom:6px"><input type="checkbox" id="r_skipamz"><span>Skip items where Amazon holds the Buy Box</span></div>
<div class="switch" style="margin-bottom:6px"><input type="checkbox" id="r_ten"><span>Keep near-$10 items at $9.99 (FBA fee break)</span></div>
<div class="switch" style="margin-bottom:6px"><input type="checkbox" id="r_skucost"><span>Read unit cost from the SKU's leading number</span></div>
<hr style="border:none;border-top:1px solid #eee;margin:14px 0">
<div class="small" style="margin-bottom:8px"><b>Amazon connection</b> — blank fields keep saved values.</div>
<button id="s_import" style="margin-bottom:10px">Import keys from FNSKU Labels</button><span class="small" id="s_impnote"></span>
<div class="fld"><label>LWA Client ID</label><input type="text" id="s_cid" autocomplete="off" spellcheck="false" data-lpignore="true"></div>
<div class="fld"><label>LWA Client Secret</label><input type="text" id="s_sec" autocomplete="off" spellcheck="false" data-lpignore="true"></div>
<div class="fld"><label>Refresh Token</label><input type="text" id="s_tok" autocomplete="off" spellcheck="false" data-lpignore="true"></div>
<div class="small" id="s_note"></div>
<div class="row"><span class="small" id="s_ver"></span><span class="sp"></span><button id="s_close">Close</button><button class="primary" id="s_save">Save</button></div>
</div></dialog>

<dialog id="csvDlg"><div class="in">
<h3 style="margin-top:0">Import costs</h3>
<div class="small" style="margin-bottom:8px">Paste lines of <b>SKU,cost</b> (commas, tabs or semicolons — e.g. an InventoryLab export).</div>
<textarea id="csvTxt" style="width:100%;height:180px;font:12px ui-monospace,Menlo"></textarea>
<div class="row"><span class="sp"></span><button id="csvClose">Cancel</button><button class="primary" id="csvGo">Import</button></div>
</div></dialog>

<script>
const _fetch=window.fetch.bind(window);window.fetch=(u,o)=>{o=o||{};if((o.method||"GET").toUpperCase()!=="GET")o.headers=Object.assign({},o.headers||{},{"X-App":"1","Content-Type":"application/json"});return _fetch(u,o);};
const $=id=>document.getElementById(id);
let items=[],filter="changes",search="",dry=true,sel=new Set();
const money=v=>v==null?"—":"$"+Number(v).toFixed(2);
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function msg(t,ok){const m=$("msg");m.textContent=t;m.className="msg "+(ok?"ok":"err");setTimeout(()=>{if(m.textContent===t)m.className="msg";},12000);}
function ageTxt(ts){if(!ts)return "no scan yet";const m=(Date.now()/1000-ts)/60;return m<2?"scanned just now":m<90?`scanned ${Math.round(m)} min ago`:`scanned ${(m/60).toFixed(1)} h ago`;}
function visible(){const q=search.toLowerCase();
 if(q)return items.filter(x=>(x.sku||"").toLowerCase().includes(q)||(x.title||"").toLowerCase().includes(q)||(x.asin||"").toLowerCase().includes(q));
 let r = items.filter(x=>{
  const fl=x.flags||[];
  if(filter==="changes")return x.proposed!=null;
  if(filter==="floor")return fl.includes("at-floor")||fl.includes("below-floor-now");
  if(filter==="losing")return x.margin_now!=null&&x.margin_now<-0.004;
  if(filter==="amazon")return fl.includes("bb-amazon")||x.amazon_selling;
  if(filter==="yours")return fl.includes("bb-yours");
  if(filter==="nocost")return x.cogs_source==="default";
  return true;});
 if(filter==="losing")r=r.slice().sort((a,b)=>(a.margin_now??0)-(b.margin_now??0));
 return r;}
const CHIP_LBL={changes:"Needs change",floor:"At / below floor",losing:"Losing",amazon:"Amazon has Buy Box",yours:"You have Buy Box",nocost:"Default cost",all:"All"};
function chipCounts(){
 const fl=x=>x.flags||[];
 const c={changes:items.filter(x=>x.proposed!=null).length,
  floor:items.filter(x=>fl(x).includes("at-floor")||fl(x).includes("below-floor-now")).length,
  losing:items.filter(x=>x.margin_now!=null&&x.margin_now<-0.004).length,
  amazon:items.filter(x=>fl(x).includes("bb-amazon")||x.amazon_selling).length,
  yours:items.filter(x=>fl(x).includes("bb-yours")).length,
  nocost:items.filter(x=>x.cogs_source==="default").length,
  all:items.length};
 document.querySelectorAll("#chips .chip").forEach(ch=>{const k=ch.dataset.k;
  ch.textContent=CHIP_LBL[k]+(items.length?` (${c[k]})`:"");});}
function flagHtml(x){const out=[];const fl=x.flags||[];
 if(fl.includes("at-floor"))out.push('<span class="f red">AT FLOOR</span>');
 if(fl.includes("below-floor-now"))out.push('<span class="f red">BELOW FLOOR NOW</span>');
 if(fl.includes("losing"))out.push('<span class="f red">LOSING</span>');
 if(fl.includes("bb-amazon"))out.push('<span class="f amber">AMAZON BB</span>');
 if(fl.includes("bb-yours"))out.push('<span class="f green">YOUR BB</span>');
 if(fl.includes("ten-break"))out.push('<span class="f grey">$9.99</span>');
 if(x.cogs_source==="default")out.push('<span class="f amber">DEFAULT COST</span>');
 return out.join("");}
function render(){
 chipCounts();
 const rows=visible();const tb=$("rows");tb.innerHTML="";
 if(!rows.length){tb.innerHTML='<tr><td colspan="12" class="small" style="padding:18px">'+(search?("No in-stock item matches \u201c"+esc(search)+"\u201d. The scan covers in-hand FBA inventory only \u2014 items with zero units (or added after the last scan) are not here. Searching ignores the filter chips."):"Nothing matches this filter.")+'</td></tr>';}
 rows.forEach(x=>{const tr=document.createElement("tr");const bb=x.buybox||{};
  const pv=x.edited!=null?Number(x.edited).toFixed(2):(x.proposed!=null?x.proposed.toFixed(2):(x.price!=null?x.price.toFixed(2):""));
  tr.innerHTML=`<td><input type="checkbox" class="rc" data-sku="${esc(x.sku)}" ${sel.has(x.sku)?"checked":""} ${x.proposed==null&&!sel.has(x.sku)?"":""}></td>
   <td style="font:600 12px ui-monospace,Menlo">${esc(x.sku)}<div class="sub">${esc(x.asin)}</div></td>
   <td class="t" title="${esc(x.title)}">${esc(x.title)}</td>
   <td class="num">${x.qty}</td>
   <td class="num">${money(x.cogs)}<div class="sub">${esc(x.cogs_source)}</div></td>
   <td class="num">${money(x.price)}</td>
   <td class="num">${money(bb.price)}${bb.who?`<div class="sub">${esc(bb.who)}</div>`:""}${bb.price&&bb.who!=="You"?`<div><a class="mlink" data-k="bb" data-sku="${esc(x.sku)}">match</a></div>`:""}${x.lowest_fba&&(!bb.price||Math.abs(x.lowest_fba-bb.price)>0.004)?`<div class="sub">FBA low ${money(x.lowest_fba)}${x.lowest_fba>0?` <a class="mlink" data-k="fba" data-sku="${esc(x.sku)}">match</a>`:""}</div>`:""}</td>
   <td class="num">${money(x.floor)}${x.floor?`<div><a class="mlink" data-k="floor" data-sku="${esc(x.sku)}">match</a></div>`:""}</td>
   <td class="num"><input class="pp ${(x.edited!=null||x.proposed!=null)?"chg":""}" data-sku="${esc(x.sku)}" type="number" step="0.01" value="${pv}"></td>
   <td><button class="up1" data-sku="${esc(x.sku)}" ${(x.edited!=null||x.proposed!=null)?"":"disabled"}>Update</button></td>
   <td class="num"${(x.proposed==null&&x.edited==null?x.margin_now:x.margin_at_proposed)<0?' style="color:#a01f12;font-weight:700"':""}>${(()=>{const m=(x.proposed==null&&x.edited==null)?x.margin_now:x.margin_at_proposed;return m==null?"—":money(m);})()}</td>
   <td><div class="sub">${esc(x.why||"")}</div>${flagHtml(x)}</td>`;
  tb.appendChild(tr);});
 if(filter==="losing"){const unk=items.filter(x=>x.price==null||!x.fee).length;
  if(unk){const tr=document.createElement("tr");
   tr.innerHTML=`<td colspan="12" class="small" style="padding:12px">⚠ ${unk} item(s) have no price or fee data yet, so they can't be judged — run Scan now again later to fill them in.</td>`;
   tb.appendChild(tr);}}
 tb.querySelectorAll(".rc").forEach(c=>c.onchange=()=>{c.checked?sel.add(c.dataset.sku):sel.delete(c.dataset.sku);updApply();});
 tb.querySelectorAll(".pp").forEach(p=>{
  const commit=()=>{const x=items.find(i=>i.sku===p.dataset.sku);if(!x)return;
   const v=parseFloat(p.value);
   if(v>0&&(x.price==null||Math.abs(v-x.price)>0.004||x.proposed!=null)){x.edited=v;sel.add(x.sku);p.classList.add("chg");
    const row=p.closest("tr");const cb=row&&row.querySelector(".rc");if(cb)cb.checked=true;
    const ub=row&&row.querySelector(".up1");if(ub)ub.disabled=false;updApply();}};
  p.addEventListener("change",commit);
  p.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();commit();
   const row=p.closest("tr");const b=row&&row.querySelector(".up1");if(b){b.disabled=false;b.focus();}}});});
 tb.querySelectorAll(".up1").forEach(b=>b.onclick=()=>applyOne(b.dataset.sku,b));
 tb.querySelectorAll(".mlink").forEach(a=>a.onclick=()=>{
  const x=items.find(i=>i.sku===a.dataset.sku);if(!x)return;
  const v=a.dataset.k==="bb"?((x.buybox||{}).price):(a.dataset.k==="fba"?x.lowest_fba:x.floor);
  if(!(v>0))return;
  if(x.price!=null&&Math.abs(v-x.price)<0.005){msg(x.sku+" is already at that price.",true);return;}
  x.edited=Math.round(v*100)/100;
  const row=a.closest("tr");const btn=row&&row.querySelector(".up1");
  const inp=row&&row.querySelector(".pp");if(inp){inp.value=x.edited.toFixed(2);inp.classList.add("chg");}
  if(btn){btn.disabled=false;applyOne(x.sku,btn);}});
 updApply();}
function updApply(){const n=sel.size;$("applyBtn").textContent=(dry?"Preview apply":"Apply selected")+` (${n})`;$("applyBtn").disabled=!n;
 const vis=visible();$("selAll").checked=n>0&&vis.every(x=>sel.has(x.sku)||x.proposed==null&&x.edited==null);}
$("selAll").onchange=()=>{const on=$("selAll").checked;visible().forEach(x=>{if(x.proposed!=null||x.edited!=null){on?sel.add(x.sku):sel.delete(x.sku);}});render();};
document.querySelectorAll("#chips .chip").forEach(c=>c.onclick=()=>{document.querySelectorAll("#chips .chip").forEach(x=>x.classList.remove("on"));c.classList.add("on");filter=c.dataset.k;search="";$("search").value="";$("searchClr").style.display="none";render();});
function clearSearch(){search="";$("search").value="";$("searchClr").style.display="none";render();}
$("search").oninput=()=>{search=$("search").value.trim();$("searchClr").style.display=search?"":"none";render();};
$("search").addEventListener("keydown",e=>{if(e.key==="Escape")clearSearch();});
$("searchClr").onclick=clearSearch;
async function loadState(){const s=await (await fetch("/api/state")).json();
 items=s.items||[];dry=!!s.dry_run;
 $("mode").textContent=dry?"DRY RUN":"LIVE — changes go to Amazon";$("mode").className="pill "+(dry?"dry":"livemode");
 $("live").textContent=s.live?"Amazon connected":"Amazon not connected";$("live").className="pill"+(s.live?" on":"");
 $("q4").style.display=s.q4?"":"none";$("age").textContent=ageTxt(s.scan_ts);
 sel=new Set(items.filter(x=>false).map(x=>x.sku));render();loadHist();loadReport();checkUpd(false);}
const RK={bb_up:["BB \u2191","grey"],bb_down:["BB \u2193","grey"],lost_bb:["LOST BB","red"],won_bb:["WON BB","green"],new_loser:["NEW LOSER","red"],below_floor:["BELOW FLOOR","red"]};
async function loadReport(){try{const r=await (await fetch("/api/report")).json();
 if(!r.ts){$("rptCard").style.display="none";return;}
 $("rptCard").style.display="";
 $("rptWhen").textContent=new Date(r.ts*1000).toLocaleString()+" vs "+new Date(r.prev_ts*1000).toLocaleString();
 const s=r.summary||{};
 $("rptSum").textContent=`${s.proposals||0} need change \u00b7 ${s.losing||0} losing \u00b7 Buy Box moved on ${s.bb_moves||0} \u00b7 lost ${s.lost_bb||0}, won ${s.won_bb||0} \u00b7 ${s.new_losers||0} new loser(s)`;
 $("rptRows").innerHTML=(r.rows||[]).length?('<table>'+r.rows.map(x=>{const k=RK[x.kind]||[x.kind,"grey"];
  return `<tr><td><span class="f ${k[1]}">${k[0]}</span></td><td style="font:600 12px ui-monospace,Menlo">${esc(x.sku)}</td><td class="t" title="${esc(x.title)}">${esc(x.title)}</td><td class="sub">${esc(x.detail)}</td></tr>`;}).join("")+'</table>'):'<div class="small">No notable market moves between the last two scans.</div>';
}catch(e){}}
$("rptToggle").onclick=()=>{const r=$("rptRows");const on=r.style.display==="none";r.style.display=on?"":"none";$("rptToggle").textContent=on?"Hide":"Show";};
async function loadHist(){const h=await (await fetch("/api/history")).json();const tb=$("hist");
 if(!(h.rows||[]).length){tb.innerHTML='<tr><td colspan="8" class="small" style="padding:16px">No changes yet.</td></tr>';return;}
 tb.innerHTML=h.rows.map((r,i)=>`<tr><td class="sub">${esc(r.time||"")}${r.dry_run?' <span class="f grey">DRY</span>':""}</td>
  <td style="font:600 12px ui-monospace,Menlo">${esc(r.sku)}</td><td class="t" title="${esc(r.title)}">${esc(r.title)}</td>
  <td class="num">${money(r.old)}</td><td class="num"><b>${money(r.new)}</b></td>
  <td class="sub">${esc(r.status||"")}</td>
  <td>${r.outcome==="won"?'<span class="f green">WON / HELD</span>':r.outcome==="lost"?'<span class="f red">LOST</span>':'<span class="sub">—</span>'}</td>
  <td>${r.old!=null&&!r.dry_run?`<button data-i="${i}" class="rv">Revert</button>`:""}</td></tr>`).join("");
 tb.querySelectorAll(".rv").forEach(b=>b.onclick=async()=>{const r=h.rows[+b.dataset.i];
  if(!confirm(`Set ${r.sku} back to ${money(r.old)}?`))return;
  const res=await (await fetch("/api/apply",{method:"POST",body:JSON.stringify({changes:[{sku:r.sku,price:r.old}]})})).json();
  msg(res.results&&res.results[0]?r.sku+": "+res.results[0].status:"done",true);loadHist();});}
function bulkSet(kind){
 const t=[...sel];
 if(!t.length){msg("Tick the rows first, then press a Match button.",false);return;}
 let n=0,skip=0;
 t.forEach(sku=>{const x=items.find(i=>i.sku===sku);if(!x)return;
  let v=null;
  if(kind==="bb"){const b=x.buybox||{};if(b.price&&b.who!=="You")v=b.price;}
  else if(kind==="floor"){v=x.floor;}
  else{v=x.lowest_fba;}
  if(v>0){x.edited=Math.round(v*100)/100;n++;}else skip++;});
 render();updApply();
 msg(`Filled ${n} row(s) with the ${kind==="bb"?"Buy Box":(kind==="floor"?"floor":"lowest FBA")} price${skip?` · ${skip} skipped (no price, or you already hold the Buy Box)`:""}. Review, then Update each or Apply selected.`,true);}
$("mBB").onclick=()=>bulkSet("bb");
$("mFBA").onclick=()=>bulkSet("fba");
$("mFLR").onclick=()=>bulkSet("floor");
async function applyOne(sku,btn){
 const x=items.find(i=>i.sku===sku);if(!x)return;
 const price=x.edited!=null?x.edited:x.proposed;
 if(!(price>0))return;
 if(x.floor&&price<x.floor-0.004&&!confirm(`${sku}: ${money(price)} is BELOW the computed floor ${money(x.floor)}. Send anyway?`))return;
 btn.disabled=true;const was=btn.textContent;btn.textContent="…";
 try{const r=await (await fetch("/api/apply",{method:"POST",body:JSON.stringify({changes:[{sku,price}]})})).json();
  const res=(r.results||[])[0]||{};
  if(res.ok){btn.textContent=r.dry_run?"DRY ✓":"✓";btn.classList.add("done");
   if(!r.dry_run){x.price=price;x.proposed=null;x.edited=null;x.why="updated just now";}
   else{x.why="dry run: logged, nothing sent";}
   sel.delete(sku);setTimeout(()=>{if(search)clearSearch();else render();},900);loadHist();}
  else{btn.textContent=was;btn.disabled=false;msg(sku+": "+(res.status||r.error||"failed"),false);}
 }catch(e){btn.textContent=was;btn.disabled=false;msg("Update failed: "+e,false);}}
$("applyBtn").onclick=async()=>{
 const changes=[...sel].map(sku=>{const x=items.find(i=>i.sku===sku);return x?{sku,price:x.edited!=null?x.edited:x.proposed}:null;}).filter(c=>c&&c.price>0);
 if(!changes.length)return;
 const floorWarn=changes.filter(c=>{const x=items.find(i=>i.sku===c.sku);return x&&x.floor&&c.price<x.floor-0.004;});
 let note=dry?`DRY RUN: ${changes.length} change(s) will be logged but NOT sent to Amazon. Continue?`:`Send ${changes.length} price change(s) to Amazon?`;
 if(floorWarn.length)note+=`\n\n⚠ ${floorWarn.length} of them are BELOW the computed floor.`;
 if(!confirm(note))return;
 $("applyBtn").disabled=true;
 try{const r=await (await fetch("/api/apply",{method:"POST",body:JSON.stringify({changes})})).json();
  const ok=(r.results||[]).filter(x=>x.ok).length,bad=(r.results||[]).filter(x=>!x.ok);
  msg(r.dry_run?`Dry run: ${ok} change(s) logged, nothing sent. Flip off Dry run in Settings when you're ready.`:`${ok} price(s) updated on Amazon`+(bad.length?` · ${bad.length} failed: ${bad[0].sku} ${bad[0].status}`:""),!bad.length);
  sel.clear();loadState();}catch(e){msg("Apply failed: "+e,false);}finally{$("applyBtn").disabled=false;}};
$("scanBtn").onclick=async()=>{const r=await (await fetch("/api/scan",{method:"POST"})).json();
 if(r.error){msg(r.error,false);return;}poll();};
async function poll(){$("scanBtn").disabled=true;$("prog").style.display="flex";
 const t=setInterval(async()=>{try{const s=await (await fetch("/api/scan/status")).json();
  $("progTxt").textContent=s.step||"";$("progFill").style.width=(s.total?Math.round(100*s.done/s.total):5)+"%";
  if(s.state==="done"){clearInterval(t);$("prog").style.display="none";$("scanBtn").disabled=false;msg("Scan complete.",true);loadState();}
  if(s.state==="error"){clearInterval(t);$("prog").style.display="none";$("scanBtn").disabled=false;msg("Scan failed: "+s.error,false);}
 }catch(e){}},1500);}
$("gear").onclick=async()=>{const s=await (await fetch("/api/settings")).json();const st=await (await fetch("/api/state")).json();const r=st.rules;
 $("s_dry").checked=!!s.dry_run;$("s_sched").checked=!!s.sched_enabled;const tt=s.scan_times||[];$("s_t1").value=tt[0]||"";$("s_t2").value=tt[1]||"";$("s_upd").value=s.update_url||"";$("s_cid").value=s.client_id||"";$("s_sec").value="";$("s_tok").value="";
 $("s_sec").placeholder=s.has_secret?"•••••• saved":"";$("s_tok").placeholder=s.has_token?"•••••• saved":"";
 $("s_import").style.display=s.fnsku_keys_found?"":"none";$("s_impnote").textContent=s.fnsku_keys_found?"":"(FNSKU Labels keys not found on this Mac)";
 $("r_offset").value=r.offset;$("r_profit").value=r.floor_profit;$("r_defcogs").value=r.default_cogs;$("r_q4until").value=r.q4_until;
 $("r_q4").checked=!!r.q4_match_buybox;$("r_skipamz").checked=!!r.skip_amazon_buybox;$("r_ten").checked=!!r.ten_break;$("r_skucost").checked=!!r.use_sku_cost;
 $("s_ver").textContent="v"+s.version;$("s_note").textContent="";$("dlg").showModal();};
$("s_close").onclick=()=>$("dlg").close();
$("s_import").onclick=async()=>{const r=await (await fetch("/api/settings",{method:"POST",body:JSON.stringify({import_fnsku:true})})).json();$("s_note").textContent=r.message||r.error;};
$("s_save").onclick=async()=>{
 await fetch("/api/rules",{method:"POST",body:JSON.stringify({offset:parseFloat($("r_offset").value),floor_profit:parseFloat($("r_profit").value),default_cogs:parseFloat($("r_defcogs").value),q4_until:$("r_q4until").value.trim(),q4_match_buybox:$("r_q4").checked,skip_amazon_buybox:$("r_skipamz").checked,ten_break:$("r_ten").checked,use_sku_cost:$("r_skucost").checked})});
 const r=await (await fetch("/api/settings",{method:"POST",body:JSON.stringify({dry_run:$("s_dry").checked,sched_enabled:$("s_sched").checked,scan_times:[$("s_t1").value||"07:30",$("s_t2").value||"16:30"],update_url:$("s_upd").value,client_id:$("s_cid").value,client_secret:$("s_sec").value,refresh_token:$("s_tok").value})})).json();
 $("s_note").textContent=r.message||r.error;$("s_sec").value="";$("s_tok").value="";loadState();};
let updBusy=false,curVer="";
async function checkUpd(force){try{const r=await (await fetch("/api/update/check"+(force?"?force=1":""))).json();curVer=r.current||"";
 const pill=$("upd");
 if(r.update_available){pill.style.display="";pill.textContent="New version v"+r.latest+" \u2014 click to update";}else{pill.style.display="none";}
 return r;}catch(e){return null;}}
$("s_chk").onclick=async()=>{$("s_updnote").textContent="Checking\u2026";await $("s_save").onclick();const r=await checkUpd(true);
 $("s_updnote").textContent=!r?"Could not check.":r.error?("Could not check: "+r.error):!r.configured?"Paste the GitHub address above first.":r.update_available?("v"+r.latest+" available \u2014 click the orange pill at the top."):"You have the newest version (v"+r.current+").";};
$("upd").onclick=async()=>{if(updBusy)return;updBusy=true;const pill=$("upd");pill.textContent="Updating\u2026";
 try{const r=await (await fetch("/api/update/apply",{method:"POST"})).json();
  if(r&&r.updated){pill.textContent="Installing v"+r.to+"\u2026 restarting";
   let n=0;const t=setInterval(async()=>{n++;try{const s=await (await fetch("/api/settings")).json();
    if(s.version&&s.version!==r.from){clearInterval(t);location.reload();}}catch(e){}
    if(n>40){clearInterval(t);msg("Update installed \u2014 quit and reopen Tito Repricer.",true);}},1500);return;}
  updBusy=false;pill.style.display="none";msg(r&&r.error?("Update failed: "+r.error):"Already up to date.",!(r&&r.error));
 }catch(e){updBusy=false;}};
$("cSet").onclick=async()=>{const r=await (await fetch("/api/cogs",{method:"POST",body:JSON.stringify({sku:$("cSku").value,cost:$("cCost").value})})).json();
 const m=$("cMsg");m.textContent=r.ok?"Saved. Re-scan (or it applies on the next scan).":"Error: "+r.error;m.className="msg "+(r.ok?"ok":"err");};
$("cImp").onclick=()=>$("csvDlg").showModal();
$("csvClose").onclick=()=>$("csvDlg").close();
$("csvGo").onclick=async()=>{const r=await (await fetch("/api/cogs",{method:"POST",body:JSON.stringify({csv:$("csvTxt").value})})).json();
 $("csvDlg").close();const m=$("cMsg");m.textContent=r.ok?`Imported ${r.imported} costs.`:"Error: "+r.error;m.className="msg "+(r.ok?"ok":"err");};
$("quit").onclick=async()=>{if(window.__nativeApp&&window.webkit&&window.webkit.messageHandlers.app){window.webkit.messageHandlers.app.postMessage("quit");return;}
 try{await fetch("/api/quit",{method:"POST"});}catch(e){}document.body.innerHTML='<div style="font:600 18px -apple-system;padding:60px;text-align:center;color:#6b7078">Tito Repricer stopped. You can close this window.</div>';};
loadState();setInterval(()=>{const s=document.querySelector("#prog");if(s&&s.style.display==="none")loadState();},120000);
if((new URLSearchParams(location.search)).get("scan")==="1")$("scanBtn").click();
</script></body></html>"""


def open_window(url):
    chrome = "/Applications/Google Chrome.app"
    if sys.platform == "darwin" and os.path.exists(chrome):
        if run(["open", "-na", chrome, "--args", "--app=" + url, "--window-size=1320,920"])[0] == 0:
            return
    webbrowser.open(url)


def main():
    global HTTPD
    url = "http://localhost:%d/" % PORT
    srv = None
    for _ in range(6):
        try:
            srv = Server(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            time.sleep(0.5)
    if srv is None:
        print("Already running at", url)
        if not os.environ.get("TR_NO_BROWSER"):
            open_window(url)
        return
    HTTPD = srv
    threading.Thread(target=_sched_loop, daemon=True).start()
    print("Tito Repricer v%s running at %s  (Ctrl+C to stop)" % (VERSION, url))
    if not os.environ.get("TR_NO_BROWSER"):
        threading.Timer(0.6, lambda: open_window(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
