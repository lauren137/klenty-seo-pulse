"""
SEO Pulse snapshot builder for GitHub Actions.

Pulls GA4 + Google Search Console data and writes the JSON files the
dashboard (site/index.html) reads from site/data/.

Environment:
  GOOGLE_CREDENTIALS   service-account JSON (GitHub secret)
  GA4_PROPERTY_ID      numeric GA4 property id (GitHub secret)
  GSC_SITE             optional, e.g. "sc-domain:klenty.com" or "https://www.klenty.com/"
                       (if empty, the first Search Console property containing klenty.com is used)
  EXCLUDE_COUNTRIES    optional, comma-separated GA4 country names treated as bot traffic
                       (default "China,Singapore")
Run:  python snapshot.py            -> real data into site/data
      python snapshot.py --demo     -> made-up data, for testing the page locally
"""
import datetime as dt
import json
import os
import random
import sys
from collections import defaultdict
from urllib.parse import urlparse

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "data")
HOST_HINT = "klenty.com"
TRACKED = ["US", "GB", "CA", "FR", "IN", "AU", "DE", "AE"]
# GA4 reports country names; Search Console reports ISO-3166 alpha-3 codes
GA_COUNTRY = {"United States": "US", "United Kingdom": "GB", "Canada": "CA", "France": "FR",
              "India": "IN", "Australia": "AU", "Germany": "DE", "United Arab Emirates": "AE"}
GSC_COUNTRY = {"usa": "US", "gbr": "GB", "can": "CA", "fra": "FR", "ind": "IN", "aus": "AU", "deu": "DE", "are": "AE"}
CP_TOP = 150
DAILY_DAYS = 486
EXCLUDE = [c.strip() for c in os.environ.get("EXCLUDE_COUNTRIES", "China,Singapore").split(",") if c.strip()]

iso = lambda d: d.isoformat()
day = lambda s: dt.date.fromisoformat(s)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def norm_path(p):
    if not p or p.startswith("("):
        return None
    if p.startswith("http"):
        u = urlparse(p)
        if HOST_HINT not in u.netloc:
            return None
        p = u.path
    p = p.split("?")[0].split("#")[0]
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p or "/"


def r4(x):
    return None if x is None else round(x, 4)


def r1(x):
    return None if x is None else round(x, 1)


# ---------------------------------------------------------------- windows
def windows(today, gsc_latest):
    """Must match presetRanges() in index.html."""
    yest = today - dt.timedelta(days=1)
    out = {}
    for n in (7, 28, 90):
        g_end, s_end = yest, gsc_latest
        out["d%d" % n] = {"days": n,
                          "ga": span(g_end, n), "gsc": span(s_end, n), "custom": None}
    first = today.replace(day=1)
    lm_end = first - dt.timedelta(days=1)
    lm_start = lm_end.replace(day=1)
    presets = {"lm": (lm_start, lm_end),
               "l6m": (today - dt.timedelta(days=182), yest),
               "l12m": (today - dt.timedelta(days=365), yest)}
    if today.day > 1:
        presets["mtd"] = (first, yest)
    for k, (a, b) in presets.items():
        n = (b - a).days + 1
        w = span(b, n)
        out["p_" + k] = {"days": n, "ga": w, "gsc": dict(w), "custom": {"start": iso(a), "end": iso(b)}}
    return out


def span(end, n):
    start = end - dt.timedelta(days=n - 1)
    p_end = start - dt.timedelta(days=1)
    p_start = p_end - dt.timedelta(days=n - 1)
    return {"start": iso(start), "end": iso(end), "pStart": iso(p_start), "pEnd": iso(p_end)}


# ---------------------------------------------------------------- data sources
class Live:
    def __init__(self):
        from google.oauth2 import service_account
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from googleapiclient.discovery import build
        info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        scopes = ["https://www.googleapis.com/auth/analytics.readonly",
                  "https://www.googleapis.com/auth/webmasters.readonly"]
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        self.ga = BetaAnalyticsDataClient(credentials=creds)
        self.prop = "properties/" + os.environ["GA4_PROPERTY_ID"].strip()
        self.sc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)
        self.site = os.environ.get("GSC_SITE", "").strip() or self._find_site()
        print("Search Console property:", self.site)

    def _find_site(self):
        sites = self.sc.sites().list().execute().get("siteEntry", [])
        names = [s["siteUrl"] for s in sites if HOST_HINT in s["siteUrl"]]
        if not names:
            sys.exit("No Search Console property containing %s is shared with the service account." % HOST_HINT)
        names.sort(key=lambda s: (not s.startswith("sc-domain:"), s))
        return names[0]

    # --- GA4
    def _filter(self, organic):
        from google.analytics.data_v1beta.types import Filter, FilterExpression, FilterExpressionList
        parts = []
        if EXCLUDE:
            parts.append(FilterExpression(not_expression=FilterExpression(filter=Filter(
                field_name="country", in_list_filter=Filter.InListFilter(values=EXCLUDE)))))
        if organic:
            parts.append(FilterExpression(filter=Filter(field_name="sessionDefaultChannelGroup",
                         string_filter=Filter.StringFilter(value="Organic Search"))))
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else FilterExpression(and_group=FilterExpressionList(expressions=parts))

    def ga_report(self, dims, metrics, ranges, organic):
        from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
        rows, offset = [], 0
        while True:
            req = RunReportRequest(property=self.prop,
                                   dimensions=[Dimension(name=d) for d in dims],
                                   metrics=[Metric(name=m) for m in metrics],
                                   date_ranges=[DateRange(start_date=a, end_date=b, name="r%d" % i)
                                                for i, (a, b) in enumerate(ranges)],
                                   dimension_filter=self._filter(organic),
                                   limit=100000, offset=offset, keep_empty_rows=False)
            res = self.ga.run_report(req)
            names = [h.name for h in res.dimension_headers]
            for r in res.rows:
                d = {names[i]: v.value for i, v in enumerate(r.dimension_values)}
                vals = [float(v.value or 0) for v in r.metric_values]
                rows.append((d, vals))
            offset += len(res.rows)
            if not res.rows or offset >= res.row_count:
                return rows

    def ga_realtime(self):
        from google.analytics.data_v1beta.types import Dimension, Metric, RunRealtimeReportRequest
        res = self.ga.run_realtime_report(RunRealtimeReportRequest(
            property=self.prop, dimensions=[Dimension(name="unifiedScreenName")],
            metrics=[Metric(name="activeUsers")], limit=50))
        top = [{"n": r.dimension_values[0].value, "v": int(r.metric_values[0].value)} for r in res.rows]
        tot = self.ga.run_realtime_report(RunRealtimeReportRequest(
            property=self.prop, metrics=[Metric(name="activeUsers")]))
        total = int(tot.rows[0].metric_values[0].value) if tot.rows else 0
        top.sort(key=lambda t: -t["v"])
        return {"total": total, "top": top[:20]}

    # --- Search Console
    def gsc(self, start, end, dims):
        rows, start_row = [], 0
        while True:
            body = {"startDate": start, "endDate": end, "dimensions": dims,
                    "rowLimit": 25000, "startRow": start_row, "dataState": "final"}
            res = self.sc.searchanalytics().query(siteUrl=self.site, body=body).execute()
            got = res.get("rows", [])
            rows += got
            start_row += len(got)
            if len(got) < 25000:
                return rows


class Demo:
    """Made-up data so the page can be tested without Google access."""
    def __init__(self):
        random.seed(7)
        self.pages = ["/", "/pricing", "/feature/parallel-dialer", "/feature/dynamic-presence"] + \
                     ["/blog/demo-post-%d" % i for i in range(60)]

    def ga_report(self, dims, metrics, ranges, organic):
        rows = []
        f = 0.4 if organic else 1.0
        if dims == ["date"]:
            a, b = day(ranges[0][0]), day(ranges[0][1])
            d = a
            while d <= b:
                rows.append(({"date": d.strftime("%Y%m%d")}, [round(400 * f * random.uniform(.7, 1.3)), random.uniform(.35, .6), .5]))
                d += dt.timedelta(days=1)
            return rows
        for i, (a, b) in enumerate(ranges):
            for p in self.pages:
                for c in (GA_COUNTRY if "country" in dims else [None]):
                    d = {"dateRange": "r%d" % i, "landingPage": p}
                    if c:
                        d["country"] = c
                    s = round(random.uniform(0, 80) * f)
                    rows.append((d, [s, random.uniform(.3, .7), random.uniform(.3, .7)][:len(metrics)]))
        return rows

    def ga_realtime(self):
        return {"total": 9, "top": [{"n": "Demo page | Klenty", "v": 5}, {"n": "Pricing | Klenty", "v": 4}]}

    def gsc(self, start, end, dims):
        if dims == ["date"]:
            a, b, out = day(start), day(end), []
            while a <= b:
                out.append({"keys": [iso(a)], "clicks": random.randint(50, 120), "impressions": random.randint(9000, 16000), "position": random.uniform(18, 28)})
                a += dt.timedelta(days=1)
            return out
        out = []
        for p in self.pages:
            for c in (GSC_COUNTRY if "country" in dims else [None]):
                k = ["https://www.klenty.com" + p] + ([c] if c else [])
                out.append({"keys": k, "clicks": random.randint(0, 60), "impressions": random.randint(10, 4000), "position": random.uniform(1, 40)})
        return out


# ---------------------------------------------------------------- builders
def gsc_latest_date(src, today):
    rows = src.gsc(iso(today - dt.timedelta(days=10)), iso(today), ["date"])
    dates = [r["keys"][0] for r in rows if r.get("impressions")]
    return day(max(dates)) if dates else today - dt.timedelta(days=3)


def build_daily(src, today):
    start, end = iso(today - dt.timedelta(days=DAILY_DAYS)), iso(today - dt.timedelta(days=1))
    by = defaultdict(dict)
    for organic, key in ((True, "org"), (False, "tot")):
        for d, (s, b, _) in src.ga_report(["date"], ["sessions", "bounceRate", "engagementRate"], [(start, end)], organic):
            ds = dt.datetime.strptime(d["date"], "%Y%m%d").date().isoformat()
            by[ds][key] = s
            by[ds]["b" + key] = b
    for r in src.gsc(start, end, ["date"]):
        g = by[r["keys"][0]]
        g["clicks"], g["impr"], g["pos"] = r["clicks"], r["impressions"], r["position"]
    rows = []
    for ds in sorted(by):
        g = by[ds]
        rows.append([ds, int(g.get("org", 0)), r4(g.get("borg")), int(g.get("tot", 0)),
                     g.get("clicks"), g.get("impr"), r1(g.get("pos")) if g.get("pos") is not None else None])
    return by, {"generatedAt": now_iso(), "cols": ["date", "org", "bOrg", "tot", "clicks", "impr", "pos"], "rows": rows}


def dates(a, b):
    a, b = day(a), day(b)
    return [iso(a + dt.timedelta(days=i)) for i in range((b - a).days + 1)]


def kpi_series(by, w):
    def agg(a, b):
        org = tot = bo = bt = 0.0
        for d in dates(a, b):
            g = by.get(d, {})
            o, t = g.get("org", 0), g.get("tot", 0)
            org += o; tot += t
            bo += o * (g.get("borg") or 0); bt += t * (g.get("btot") or 0)
        return org, (bo / org if org else None), tot, (bt / tot if tot else None)

    def agg_gsc(a, b):
        cl = im = pw = 0.0
        n = 0
        for d in dates(a, b):
            g = by.get(d, {})
            if g.get("impr") is not None:
                n += 1; cl += g["clicks"]; im += g["impr"]; pw += g["pos"] * g["impr"]
        return (cl if n else None), (im if n else None), (pw / im if im else None)

    ga, sc = w["ga"], w["gsc"]
    o0, bo0, t0, bt0 = agg(ga["start"], ga["end"])
    o1, bo1, t1, bt1 = agg(ga["pStart"], ga["pEnd"])
    c0, i0, p0 = agg_gsc(sc["start"], sc["end"])
    c1, i1, p1 = agg_gsc(sc["pStart"], sc["pEnd"])
    kpi = {"org": [int(o0), int(o1)], "tot": [int(t0), int(t1)], "bOrg": [r4(bo0), r4(bo1)], "bTot": [r4(bt0), r4(bt1)],
           "clicks": [c0 and int(c0), c1 and int(c1)], "impr": [i0 and int(i0), i1 and int(i1)],
           "pos": [p0 and round(p0, 3), p1 and round(p1, 3)]}

    def ser(key, rng):
        return {"cur": [[d, by.get(d, {}).get(key)] for d in dates(rng["start"], rng["end"])],
                "prev": [[d, by.get(d, {}).get(key)] for d in dates(rng["pStart"], rng["pEnd"])]}
    series = {"org": ser("org", ga), "tot": ser("tot", ga), "clicks": ser("clicks", sc), "impr": ser("impr", sc)}
    for s in series.values():
        for part in s.values():
            for p in part:
                if isinstance(p[1], float):
                    p[1] = int(p[1])
    return kpi, series


def ga_pages(src, w, with_country):
    """Return {(path[,cc]): {orgS:[c,p], totS:[c,p], bOrg:[c,p], eng:[c,p]}}"""
    ga = w["ga"]
    ranges = [(ga["start"], ga["end"]), (ga["pStart"], ga["pEnd"])]
    dims = ["landingPage", "country"] if with_country else ["landingPage"]
    out = defaultdict(lambda: {"orgS": [0, 0], "totS": [0, 0], "bw": [0.0, 0.0], "ew": [0.0, 0.0]})
    for organic in (True, False):
        for d, vals in src.ga_report(dims, ["sessions", "bounceRate", "engagementRate"], ranges, organic):
            p = norm_path(d.get("landingPage"))
            if not p:
                continue
            key = p
            if with_country:
                cc = GA_COUNTRY.get(d.get("country"))
                if not cc:
                    continue
                key = (p, cc)
            i = 0 if d.get("dateRange", "r0") == "r0" else 1
            s, b, e = vals
            o = out[key]
            if organic:
                o["orgS"][i] += s; o["bw"][i] += s * b; o["ew"][i] += s * e
            else:
                o["totS"][i] += s
    for o in out.values():
        o["bOrg"] = [o["bw"][i] / o["orgS"][i] if o["orgS"][i] else None for i in (0, 1)]
        o["eng"] = [o["ew"][i] / o["orgS"][i] if o["orgS"][i] else None for i in (0, 1)]
    return out


def gsc_pages(src, w, with_country):
    sc = w["gsc"]
    dims = ["page", "country"] if with_country else ["page"]
    out = defaultdict(lambda: {"clicks": [0, 0], "impr": [0, 0], "pw": [0.0, 0.0]})
    for i, (a, b) in enumerate(((sc["start"], sc["end"]), (sc["pStart"], sc["pEnd"]))):
        for r in src.gsc(a, b, dims):
            p = norm_path(r["keys"][0])
            if not p:
                continue
            key = p
            if with_country:
                cc = GSC_COUNTRY.get(r["keys"][1])
                if not cc:
                    continue
                key = (p, cc)
            o = out[key]
            o["clicks"][i] += r["clicks"]; o["impr"][i] += r["impressions"]; o["pw"][i] += r["position"] * r["impressions"]
    for o in out.values():
        o["pos"] = [round(o["pw"][i] / o["impr"][i], 1) if o["impr"][i] else None for i in (0, 1)]
    return out


def merged(gap, scp):
    keys = set(gap) | set(scp)
    for k in keys:
        a = gap.get(k) or {"orgS": [0, 0], "totS": [0, 0], "bOrg": [None, None], "eng": [None, None]}
        b = scp.get(k) or {"clicks": [0, 0], "impr": [0, 0], "pos": [None, None]}
        if not (sum(a["orgS"]) + sum(a["totS"]) + sum(b["clicks"]) + sum(b["impr"])):
            continue
        yield k, a, b


def build_window(src, by, w, rt):
    kpi, series = kpi_series(by, w)
    rows = []
    for p, a, b in merged(ga_pages(src, w, False), gsc_pages(src, w, False)):
        rows.append([p, int(a["orgS"][0]), int(a["orgS"][1]), int(a["totS"][0]), int(a["totS"][1]),
                     r4(a["bOrg"][0]), r4(a["bOrg"][1]), r4(a["eng"][0]),
                     int(b["clicks"][0]), int(b["clicks"][1]), int(b["impr"][0]), int(b["impr"][1]), b["pos"][0], b["pos"][1]])
    rows.sort(key=lambda r: (-r[1], -r[8], -r[10]))
    doc = {"generatedAt": now_iso(), "botsFiltered": bool(EXCLUDE), "days": w["days"], "ga": w["ga"], "gsc": w["gsc"],
           "kpi": kpi, "series": series,
           "cols": ["p", "orgS0", "orgS1", "totS0", "totS1", "bOrg0", "bOrg1", "eng0", "clicks0", "clicks1", "impr0", "impr1", "pos0", "pos1"],
           "rows": rows, "rowsTotal": len(rows)}
    if w["custom"]:
        doc["custom"] = w["custom"]
    if rt is not None:
        doc["rt"] = rt
    return doc


def build_cp(src, w):
    per = defaultdict(list)
    for (p, cc), a, b in merged(ga_pages(src, w, True), gsc_pages(src, w, True)):
        per[cc].append([p, int(a["orgS"][0]), int(a["orgS"][1]), int(a["totS"][0]), int(a["totS"][1]),
                        r4(a["bOrg"][0]), r4(a["bOrg"][1]),
                        int(b["clicks"][0]), int(b["clicks"][1]), int(b["impr"][0]), int(b["impr"][1]), b["pos"][0], b["pos"][1]])
    by_country, totals = {}, {}
    for cc in TRACKED:
        lst = per.get(cc, [])
        totals[cc] = sum(1 for r in lst if r[1] + r[3] + r[7] + r[9] > 0)
        lst.sort(key=lambda r: (-r[1], -r[3], -r[7]))
        by_country[cc] = lst[:CP_TOP]
    doc = {"generatedAt": now_iso(), "days": w["days"], "ga": w["ga"], "gsc": w["gsc"],
           "cols": ["p", "orgS0", "orgS1", "totS0", "totS1", "bOrg0", "bOrg1", "clicks0", "clicks1", "impr0", "impr1", "pos0", "pos1"],
           "byCountry": by_country, "pagesTotal": totals}
    if w["custom"]:
        doc["custom"] = w["custom"]
    return doc


def write(name, doc, meta):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name + ".json"), "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    meta[name] = doc["generatedAt"]
    print("wrote", name)


def main():
    src = Demo() if "--demo" in sys.argv else Live()
    today = dt.datetime.now(dt.timezone.utc).date()
    gsc_last = gsc_latest_date(src, today)
    by, daily = build_daily(src, today)
    meta = {}
    write("daily", daily, meta)
    try:
        rt = src.ga_realtime()
    except Exception as e:  # realtime is a nice-to-have
        print("realtime skipped:", e)
        rt = None
    for name, w in windows(today, gsc_last).items():
        write(name, build_window(src, by, w, rt if name.startswith("d") else None), meta)
        write("cp_" + name, build_cp(src, w), meta)
    with open(os.path.join(OUT, "meta.json"), "w") as f:
        json.dump(meta, f)


if __name__ == "__main__":
    main()
