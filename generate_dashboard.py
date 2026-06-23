#!/usr/bin/env python3
"""
7 o'clock – Tägliche Neusendungs-Dashboard-Generierung
Wird täglich um 8 Uhr morgens über GitHub Actions ausgeführt.
Zieht Daten via Shopify Admin GraphQL API, berechnet KPIs, generiert index.html.
"""

import os
import sys
import json
import html
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import urllib.request
import urllib.error

# ============================================================
# Konfiguration
# ============================================================
SHOP_DOMAIN = os.environ.get("SHOPIFY_SHOP_DOMAIN", "")  # z.B. 7-oclock.myshopify.com
ADMIN_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")  # shpat_...
API_VERSION = "2025-01"

if not SHOP_DOMAIN or not ADMIN_TOKEN:
    print("ERROR: SHOPIFY_SHOP_DOMAIN oder SHOPIFY_ADMIN_TOKEN nicht gesetzt", file=sys.stderr)
    sys.exit(1)

GRAPHQL_URL = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/graphql.json"

# Neusendungs-Produkt-Mapping (SKU → Kategorie, Verursacher)
NEUSENDUNG_SKUS = {
    "8684466307404":  ("Sonstiges (alt)",        "sonstiges"),
    "8797853122892":  ("Defekt/Bruch",           "produkt"),
    "9315923165516":  ("Paket zurückgekommen",   "carrier"),
    "15835925807436": ("Artikel fehlt",          "lager"),
    "15835925840204": ("Falscher Artikel",       "lager"),
    "15835925872972": ("Falsche Menge",          "lager"),
    "15835925905740": ("Produktionsfehler",      "produkt"),
    "15835925938508": ("Packstation",            "carrier"),
    "15835926004044": ("Transportschaden",       "carrier"),
    "15835926036812": ("Falsche Adresse",        "kunde"),
    "15835926102348": ("Kulanz",                 "kulanz"),
}
SCHWEIZ_PRODUKT_ID = "15787982324044"

VERURSACHER_LABELS = {
    "lager":     "Lagerfehler (intern)",
    "produkt":   "Produkt/Qualität",
    "carrier":   "Versand (Carrier)",
    "kunde":     "Kundenfehler",
    "kulanz":    "Kulanz",
    "sonstiges": "Sonstiges",
}
VERURSACHER_COLOR = {
    "lager":     "#6b8e7f",
    "produkt":   "#c4934a",
    "carrier":   "#7d8fa3",
    "kunde":     "#b08968",
    "kulanz":    "#a892b8",
    "sonstiges": "#9a9a9a",
}

MONATE_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni",
             "Juli", "August", "September", "Oktober", "November", "Dezember"]
WOCHENTAGE_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


# ============================================================
# Shopify API – Multi-Auth-Strategie
# ============================================================
# Automation Tokens (atkn_...) nutzen Bearer Auth.
# Klassische Tokens (shpat_...) nutzen X-Shopify-Access-Token.
# Wir probieren beides durch.

def _headers_variants():
    """Liefert mögliche Header-Kombinationen je nach Token-Format."""
    variants = []
    if ADMIN_TOKEN.startswith("atkn_"):
        # Neue Automation Tokens: Bearer Auth
        variants.append({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ADMIN_TOKEN}",
            "Accept": "application/json",
        })
        # Fallback
        variants.append({
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": ADMIN_TOKEN,
            "Accept": "application/json",
        })
    else:
        # Klassisch: X-Shopify-Access-Token
        variants.append({
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": ADMIN_TOKEN,
            "Accept": "application/json",
        })
        variants.append({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ADMIN_TOKEN}",
            "Accept": "application/json",
        })
    return variants

_WORKING_HEADERS = None

def graphql(query, variables=None):
    global _WORKING_HEADERS
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")

    headers_to_try = [_WORKING_HEADERS] if _WORKING_HEADERS else _headers_variants()

    last_error = None
    for headers in headers_to_try:
        if headers is None:
            continue
        req = urllib.request.Request(GRAPHQL_URL, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if "errors" in data:
                err_str = json.dumps(data["errors"])
                # Auth-Fehler? Versuche nächsten Header
                if "401" in err_str or "unauthorized" in err_str.lower() or "Invalid API" in err_str:
                    last_error = f"GraphQL errors with this auth: {err_str}"
                    continue
                print(f"ERROR: GraphQL errors: {data['errors']}", file=sys.stderr)
                sys.exit(1)
            _WORKING_HEADERS = headers  # erfolgreichen Header merken
            return data["data"]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {e.code}: {body}"
            if e.code in (401, 403):
                continue
            print(f"ERROR: Shopify API {last_error}", file=sys.stderr)
            sys.exit(1)

    print(f"ERROR: Alle Auth-Varianten fehlgeschlagen. Letzter Fehler: {last_error}", file=sys.stderr)
    print(f"Token-Präfix: {ADMIN_TOKEN[:6]}..., Shop: {SHOP_DOMAIN}", file=sys.stderr)
    sys.exit(1)


def fetch_orders(query_str, page_size=250):
    """Paginiert durch alle Bestellungen für eine Query."""
    gql = """
    query Orders($q: String!, $cursor: String) {
      orders(first: %d, query: $q, after: $cursor, sortKey: CREATED_AT) {
        edges {
          cursor
          node {
            id
            name
            createdAt
            customer { firstName lastName }
            shippingAddress { countryCodeV2 }
            lineItems(first: 50) {
              edges {
                node {
                  title
                  quantity
                  sku
                  product { id title productType tags }
                  originalUnitPriceSet { shopMoney { amount } }
                }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """ % page_size
    all_orders = []
    cursor = None
    while True:
        data = graphql(gql, {"q": query_str, "cursor": cursor})
        edges = data["orders"]["edges"]
        all_orders.extend(e["node"] for e in edges)
        page_info = data["orders"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return all_orders


def fetch_orders_count(query_str):
    gql = "query Total($q: String!) { ordersCount(query: $q, limit: 10000) { count } }"
    data = graphql(gql, {"q": query_str})
    return data.get("ordersCount", {}).get("count", 0)


# ============================================================
# Analyse
# ============================================================
def is_neusendung(line_item):
    sku = line_item.get("sku")
    product_id = (line_item.get("product") or {}).get("id", "")
    product_id_num = product_id.split("/")[-1] if product_id else ""
    if sku and sku in NEUSENDUNG_SKUS:
        return NEUSENDUNG_SKUS[sku]
    if product_id_num == SCHWEIZ_PRODUKT_ID:
        return ("Schweiz", "sonstiges")
    return None


def analyze(orders, target_date_iso):
    """target_date_iso = YYYY-MM-DD (Vortag)"""
    cases_yesterday = []  # nur Vortag
    cat_yesterday = defaultdict(int)
    ver_yesterday = defaultdict(int)

    trend_30d = defaultdict(int)  # YYYY-MM-DD: count
    top_original_30d = defaultdict(int)  # title: quantity

    target_start = target_date_iso

    for order in orders:
        created = order["createdAt"][:10]  # YYYY-MM-DD
        items = [e["node"] for e in order["lineItems"]["edges"]]

        neusendungen = []
        original_items = []

        for item in items:
            ns = is_neusendung(item)
            if ns:
                kategorie, verursacher = ns
                neusendungen.append({
                    "kategorie": kategorie,
                    "verursacher": verursacher,
                    "quantity": item.get("quantity", 1),
                })
            else:
                title = item.get("title") or ""
                if "neusendung" not in title.lower():
                    original_items.append({
                        "title": title,
                        "quantity": item.get("quantity", 1),
                    })

        if not neusendungen:
            continue

        # Trend (gesamter 30-Tage-Zeitraum)
        trend_30d[created] += sum(n["quantity"] for n in neusendungen)
        for o in original_items:
            top_original_30d[o["title"]] += o["quantity"]

        # Vortag-Detail
        if created == target_start:
            for n in neusendungen:
                cat_yesterday[n["kategorie"]] += 1
                ver_yesterday[n["verursacher"]] += 1
            cust = order.get("customer") or {}
            cases_yesterday.append({
                "name": order["name"],
                "customer": f"{cust.get('firstName','')} {cust.get('lastName','')}".strip() or "–",
                "country": (order.get("shippingAddress") or {}).get("countryCodeV2") or "–",
                "neusendungen": neusendungen,
                "original_items": original_items,
            })

    total_yesterday = sum(cat_yesterday.values())
    avg_30d = sum(trend_30d.values()) / max(len(trend_30d), 1) if trend_30d else 0
    top_kat = max(cat_yesterday.items(), key=lambda x: x[1]) if cat_yesterday else None

    return {
        "target_date": target_date_iso,
        "total_yesterday": total_yesterday,
        "cases_yesterday": cases_yesterday,
        "cat_yesterday": dict(cat_yesterday),
        "ver_yesterday": dict(ver_yesterday),
        "trend_30d": dict(trend_30d),
        "top_original_30d": sorted(top_original_30d.items(), key=lambda x: -x[1])[:10],
        "avg_30d": avg_30d,
        "top_kat": top_kat,
    }


# ============================================================
# HTML
# ============================================================
def german_date(iso_date):
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{WOCHENTAGE_DE[d.weekday()]}, {d.day}. {MONATE_DE[d.month-1]} {d.year}"


def short_date(iso_date):
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{d.day:02d}.{d.month:02d}."


def esc(s):
    return html.escape(str(s) if s is not None else "")


def generate_html(analysis, total_orders_yesterday, target_date_iso):
    # Trend-Tage chronologisch (letzte 30 Tage, auch wenn 0)
    end = datetime.strptime(target_date_iso, "%Y-%m-%d")
    start = end - timedelta(days=29)
    trend_days = []
    for i in range(30):
        d = start + timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        trend_days.append((key, analysis["trend_30d"].get(key, 0)))

    total_y = analysis["total_yesterday"]
    quote = (total_y / total_orders_yesterday * 100) if total_orders_yesterday else 0
    avg = analysis["avg_30d"]

    # Alerts
    alerts = []
    if total_y == 0:
        alerts.append(("good", "Keine Neusendungen heute", "Heute keine einzige Neusendung — sauberer Tag für den Kundenservice."))
    elif avg > 0 and total_y > avg * 1.5:
        pct = round((total_y/avg - 1) * 100)
        alerts.append(("bad", "Über dem Durchschnitt", f"Gestern {total_y} Neusendungen — das sind {pct}% über dem 30-Tage-Schnitt ({avg:.1f})."))
    elif avg > 0 and 0 < total_y < avg * 0.5:
        pct = round((1 - total_y/avg) * 100)
        alerts.append(("good", "Unter dem Durchschnitt", f"Nur {total_y} Neusendungen gestern — {pct}% unter dem 30-Tage-Schnitt."))

    if total_y >= 3 and analysis["cat_yesterday"]:
        top_kat_name, top_kat_count = max(analysis["cat_yesterday"].items(), key=lambda x: x[1])
        share = top_kat_count / total_y * 100
        if share >= 50:
            alerts.append(("warn", f"Dominante Kategorie: {top_kat_name}",
                f"{share:.0f}% aller Neusendungen ({top_kat_count} von {total_y}) gehen auf '{top_kat_name}'. Lieferant/Verpackung/Prozess prüfen."))

    # Chart-Daten
    ver_labels = [VERURSACHER_LABELS.get(v, v) for v in analysis["ver_yesterday"].keys()]
    ver_data = list(analysis["ver_yesterday"].values())
    ver_colors = [VERURSACHER_COLOR.get(v, "#999") for v in analysis["ver_yesterday"].keys()]

    trend_labels = [short_date(d) for d, _ in trend_days]
    trend_values = [v for _, v in trend_days]

    # KPI-Karten
    top_kat_display = analysis["top_kat"][0] if analysis["top_kat"] else "–"
    top_kat_count_display = analysis["top_kat"][1] if analysis["top_kat"] else 0

    # Kategorien-Liste
    sorted_kats = sorted(analysis["cat_yesterday"].items(), key=lambda x: -x[1])
    kat_html = ""
    for kat, count in sorted_kats:
        # finde Verursacher für Farbe
        ver = "sonstiges"
        for case in analysis["cases_yesterday"]:
            for n in case["neusendungen"]:
                if n["kategorie"] == kat:
                    ver = n["verursacher"]
                    break
        color = VERURSACHER_COLOR.get(ver, "#999")
        pct = count / total_y * 100 if total_y else 0
        kat_html += f"""<div class="cat-row">
          <div class="cat-dot" style="background:{color}"></div>
          <div class="cat-name">{esc(kat)}</div>
          <div class="cat-count">{count}</div>
          <div class="cat-pct">{pct:.0f}%</div>
        </div>"""
    if not kat_html:
        kat_html = '<div class="empty">Keine Neusendungen heute.</div>'

    # Top Original-Produkte
    top_prods_html = ""
    if analysis["top_original_30d"]:
        for title, count in analysis["top_original_30d"]:
            top_prods_html += f'<tr><td>{esc(title)}</td><td class="num">{count}</td></tr>'
    else:
        top_prods_html = '<tr><td colspan="2" class="empty">Keine Daten verfügbar.</td></tr>'

    # Fälle gestern
    cases_html = ""
    if analysis["cases_yesterday"]:
        for c in analysis["cases_yesterday"]:
            kategorien = ", ".join(n["kategorie"] for n in c["neusendungen"])
            ver_set = list({n["verursacher"] for n in c["neusendungen"]})
            ver_tags = " ".join(
                f'<span class="tag tag-{v}">{esc(VERURSACHER_LABELS.get(v, v))}</span>'
                for v in ver_set
            )
            if c["original_items"]:
                origs = "<br>".join(
                    f'{esc(o["title"])}' + (f' × {o["quantity"]}' if o["quantity"] > 1 else '')
                    for o in c["original_items"]
                )
            else:
                origs = '<span style="color:var(--ink-soft); font-style:italic;">(keine)</span>'
            cases_html += f"""<tr>
              <td><strong>{esc(c["name"])}</strong></td>
              <td>{esc(c["customer"])}</td>
              <td>{esc(c["country"])}</td>
              <td>{esc(kategorien)}</td>
              <td>{ver_tags}</td>
              <td>{origs}</td>
            </tr>"""
    else:
        cases_html = '<tr><td colspan="6" class="empty">🎉 Keine Neusendungen am Vortag.</td></tr>'

    # Alerts HTML
    alerts_html = ""
    for typ, title, text in alerts:
        alerts_html += f'<div class="alert {typ}"><strong>{esc(title)}</strong>{esc(text)}</div>'

    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y um %H:%M Uhr (UTC)")

    # Trend Bar-Farben: Vortag hervorgehoben
    trend_colors = ["#2d2a26" if d == target_date_iso else "#c4b5a0" for d, _ in trend_days]

    html_doc = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Neusendungs-Dashboard | 7 o'clock</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js"></script>
<style>
:root {{
  color-scheme: light;
  --bg: #faf8f5;
  --card: #ffffff;
  --ink: #1a1a1a;
  --ink-soft: #6b6b6b;
  --line: #ebe6df;
  --accent: #2d2a26;
  --good: #4a7c59;
  --warn: #c4934a;
  --bad: #b04848;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
  background: var(--bg);
  color: var(--ink);
  font-size: 14px;
  line-height: 1.55;
  padding: 40px 24px;
}}
.wrap {{ max-width: 1200px; margin: 0 auto; }}
header {{ margin-bottom: 36px; padding-bottom: 24px; border-bottom: 1px solid var(--line); }}
.brand {{ font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: var(--ink-soft); margin-bottom: 8px; }}
h1 {{ font-size: 28px; font-weight: 500; letter-spacing: -0.5px; }}
.subtitle {{ color: var(--ink-soft); font-size: 14px; margin-top: 8px; }}
.timestamp {{ font-size: 12px; color: var(--ink-soft); margin-top: 12px; font-style: italic; }}
h2 {{ font-size: 13px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; color: var(--ink-soft); margin-bottom: 16px; }}
.section {{ margin-bottom: 36px; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 32px; }}
.kpi {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 20px; }}
.kpi-label {{ font-size: 11px; color: var(--ink-soft); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }}
.kpi-value {{ font-size: 28px; font-weight: 500; letter-spacing: -0.5px; }}
.kpi-sub {{ font-size: 12px; color: var(--ink-soft); margin-top: 6px; }}
.two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 24px; }}
.chart-wrap {{ position: relative; height: 280px; }}
.cat-list {{ display: flex; flex-direction: column; }}
.cat-row {{ display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--line); }}
.cat-row:last-child {{ border-bottom: none; }}
.cat-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
.cat-name {{ flex: 1; font-size: 13px; }}
.cat-count {{ font-weight: 600; font-size: 13px; }}
.cat-pct {{ color: var(--ink-soft); font-size: 12px; margin-left: 8px; min-width: 50px; text-align: right; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--ink-soft); text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line); font-weight: 600; }}
td {{ padding: 12px; border-bottom: 1px solid var(--line); font-size: 13px; vertical-align: top; }}
tr:last-child td {{ border-bottom: none; }}
td.num {{ text-align: right; font-weight: 600; }}
td.empty {{ text-align: center; color: var(--ink-soft); font-style: italic; padding: 24px; }}
.tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; }}
.tag-lager {{ background: rgba(107,142,127,0.15); color: #4a6b5d; }}
.tag-produkt {{ background: rgba(196,147,74,0.15); color: #8a6730; }}
.tag-carrier {{ background: rgba(125,143,163,0.15); color: #5a6878; }}
.tag-kunde {{ background: rgba(176,137,104,0.15); color: #7a5d44; }}
.tag-kulanz {{ background: rgba(168,146,184,0.15); color: #6e5a82; }}
.tag-sonstiges {{ background: rgba(154,154,154,0.15); color: #5a5a5a; }}
.alert {{ background: rgba(196,147,74,0.1); border-left: 3px solid var(--warn); padding: 14px 18px; border-radius: 4px; font-size: 13px; margin-bottom: 10px; }}
.alert.bad {{ background: rgba(176,72,72,0.08); border-left-color: var(--bad); }}
.alert.good {{ background: rgba(74,124,89,0.08); border-left-color: var(--good); }}
.alert strong {{ display: block; margin-bottom: 4px; }}
.empty {{ text-align: center; padding: 40px 20px; color: var(--ink-soft); font-style: italic; }}
footer {{ margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--line); font-size: 11px; color: var(--ink-soft); text-align: center; }}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="brand">7 O'CLOCK · KUNDENSERVICE</div>
  <h1>Neusendungs-Dashboard</h1>
  <div class="subtitle">{esc(german_date(target_date_iso))}</div>
  <div class="timestamp">Aktualisiert am {esc(now_str)}</div>
</header>

{f'<div class="section">{alerts_html}</div>' if alerts_html else ''}

<div class="kpi-grid">
  <div class="kpi">
    <div class="kpi-label">Neusendungen gestern</div>
    <div class="kpi-value">{total_y}</div>
    <div class="kpi-sub">{len(analysis["cases_yesterday"])} Bestellungen betroffen</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">Quote</div>
    <div class="kpi-value">{quote:.2f}%</div>
    <div class="kpi-sub">{total_orders_yesterday} Bestellungen gesamt</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">30-Tage-Schnitt</div>
    <div class="kpi-value">{avg:.1f}</div>
    <div class="kpi-sub">Neusendungen pro Tag</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">Top-Kategorie</div>
    <div class="kpi-value" style="font-size: 18px;">{esc(top_kat_display)}</div>
    <div class="kpi-sub">{top_kat_count_display} Fälle</div>
  </div>
</div>

<div class="section two-col">
  <div class="card">
    <h2>Nach Verursacher (gestern)</h2>
    <div class="chart-wrap"><canvas id="chart-verursacher"></canvas></div>
  </div>
  <div class="card">
    <h2>Nach Kategorie (gestern)</h2>
    <div class="cat-list">{kat_html}</div>
  </div>
</div>

<div class="section card">
  <h2>30-Tage-Trend</h2>
  <div class="chart-wrap" style="height: 240px;"><canvas id="chart-trend"></canvas></div>
</div>

<div class="section card">
  <h2>Top 10 nachgesendete Original-Produkte (30 Tage)</h2>
  <table>
    <thead><tr><th>Artikel</th><th style="text-align:right">Anzahl</th></tr></thead>
    <tbody>{top_prods_html}</tbody>
  </table>
</div>

<div class="section card">
  <h2>Alle Neusendungen vom {esc(german_date(target_date_iso))}</h2>
  <table>
    <thead><tr>
      <th>Bestellung</th>
      <th>Kunde</th>
      <th>Land</th>
      <th>Kategorie</th>
      <th>Verursacher</th>
      <th>Nachzuliefern</th>
    </tr></thead>
    <tbody>{cases_html}</tbody>
  </table>
</div>

<footer>
  7 o'clock · Interner Kundenservice-Report · Vertraulich<br>
  Datenquelle: Shopify Live-Daten · Aktualisierung täglich um 8 Uhr morgens
</footer>

</div>

<script>
if (document.getElementById('chart-verursacher') && {len(ver_data)} > 0) {{
  new Chart(document.getElementById('chart-verursacher'), {{
    type: 'doughnut',
    data: {{
      labels: {json.dumps(ver_labels, ensure_ascii=False)},
      datasets: [{{
        data: {json.dumps(ver_data)},
        backgroundColor: {json.dumps(ver_colors)},
        borderWidth: 0,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ position: 'right', labels: {{ boxWidth: 12, font: {{ size: 12 }} }} }}
      }}
    }}
  }});
}}

new Chart(document.getElementById('chart-trend'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(trend_labels)},
    datasets: [{{
      label: 'Neusendungen',
      data: {json.dumps(trend_values)},
      backgroundColor: {json.dumps(trend_colors)},
      borderRadius: 3,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 10 }} }} }},
      y: {{ beginAtZero: true, ticks: {{ precision: 0 }} }}
    }}
  }}
}});
</script>

</body>
</html>
"""
    return html_doc


# ============================================================
# Main
# ============================================================
def main():
    # Zeitzone: Berlin (CEST/CET). Wir wollen den "Vortag" in Berliner Zeit.
    # GitHub Actions läuft in UTC. Für Berlin = UTC+1 oder UTC+2.
    # Pragmatisch: Wir nehmen UTC-Vortag, das ist ~95% identisch und Bestellungen werden in UTC gespeichert.
    today_utc = datetime.now(timezone.utc).date()
    target_date = today_utc - timedelta(days=1)
    target_date_iso = target_date.strftime("%Y-%m-%d")

    # 30-Tage-Fenster für Trend
    start_30d = target_date - timedelta(days=29)

    print(f"Target date (Vortag): {target_date_iso}", file=sys.stderr)
    print(f"Trend-Zeitraum: {start_30d} bis {target_date_iso}", file=sys.stderr)

    # Shopify Query bauen
    sku_or = " OR ".join(f"sku:{sku}" for sku in NEUSENDUNG_SKUS.keys())
    query_str = f"({sku_or}) AND created_at:>='{start_30d}T00:00:00Z' AND created_at:<='{target_date_iso}T23:59:59Z'"

    print("Lade Bestellungen mit Neusendungs-Items...", file=sys.stderr)
    orders = fetch_orders(query_str)
    print(f"  → {len(orders)} Bestellungen gefunden", file=sys.stderr)

    print("Lade Gesamt-Bestellzahl Vortag...", file=sys.stderr)
    count_query = f"created_at:>='{target_date_iso}T00:00:00Z' AND created_at:<='{target_date_iso}T23:59:59Z'"
    total_orders = fetch_orders_count(count_query)
    print(f"  → {total_orders} Bestellungen am Vortag", file=sys.stderr)

    print("Analysiere...", file=sys.stderr)
    analysis = analyze(orders, target_date_iso)

    print("Generiere HTML...", file=sys.stderr)
    html_out = generate_html(analysis, total_orders, target_date_iso)

    output_path = os.environ.get("OUTPUT_PATH", "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"Dashboard gespeichert: {output_path}", file=sys.stderr)
    print(f"  → {analysis['total_yesterday']} Neusendungen gestern, 30-Tage-Schnitt: {analysis['avg_30d']:.1f}", file=sys.stderr)


if __name__ == "__main__":
    main()
