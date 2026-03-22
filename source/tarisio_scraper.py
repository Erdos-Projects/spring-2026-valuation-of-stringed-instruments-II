#!/usr/bin/env python3
"""
tarisio_scraper.py
==================
Unified Tarisio / Cozio Archive scraper.
Produces three CSV databases:

  sales.csv       — one row per auction sale
  instruments.csv — one row per instrument
  makers.csv      — one row per maker

The three tables are linked by maker_id and instrument_id.

Entry points
------------
  # Full crawl from scratch (slow — thousands of pages):
  python tarisio_scraper.py --out-dir ./data

  # Incremental: only scrape makers / instruments / sales not yet in the CSVs:
  python tarisio_scraper.py --out-dir ./data --resume

  # Limit to N makers (useful for testing):
  python tarisio_scraper.py --out-dir ./data --max-makers 10

  # Skip one of the three tables (if you only need to refresh one):
  python tarisio_scraper.py --out-dir ./data --skip-instruments --skip-sales

Network note
------------
  The script uses one requests.Session per thread (thread-safe).
  Default: 6 workers.  Reduce with --workers 2 if you get 429 errors.
"""

import re
import os
import csv
import time
import argparse
import threading
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# 0.  Constants & shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

BASE = "https://tarisio.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}

# Thread-local session (one per worker thread — requests.Session is not thread-safe)
_local = threading.local()

def _session():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.s = s
    return _local.s


def _parser():
    try:
        import lxml  # noqa
        return "lxml"
    except ImportError:
        return "html.parser"

PARSER = _parser()


def clean(x):
    if not x:
        return ""
    return re.sub(r"\s+", " ", str(x).replace("\xa0", " ")).strip()


def _sort_key(name):
    """Unicode-normalised lowercase sort key (strips accents, ignores case).
    Works correctly for 'Last, First' names already stored that way."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def get_soup(url, timeout=12, retries=3):
    sess = _session()
    for k in range(retries):
        try:
            r = sess.get(url, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return BeautifulSoup(r.text, PARSER)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                time.sleep(10 * (k + 1))
            elif k < retries - 1:
                time.sleep(0.5 * (k + 1))
            else:
                return None
        except Exception:
            if k < retries - 1:
                time.sleep(0.5 * (k + 1))
            else:
                return None
    return None


def _section_h2(soup, title):
    """Return the <h2> tag whose text matches title (case-insensitive)."""
    for h2 in soup.find_all("h2"):
        if h2.get_text(strip=True).lower() == title.lower():
            return h2
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  Makers list  →  {maker_id: maker_url}
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_maker_ids():
    """
    Crawl the browse-makers index pages and return a dict
    {maker_id (int): maker_name (str)}.
    """
    makers = {}
    # The archive is paginated alphabetically; we follow pagination links.
    url = BASE + "/cozio-archive/browse-the-archive/makers/"
    visited = set()
    queue   = [url]

    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        soup = get_soup(url)
        if not soup:
            continue

        # Each maker is a link like /cozio-archive/browse-the-archive/makers/maker/?Maker_ID=XXX
        for a in soup.find_all("a", href=re.compile(r"Maker_ID=\d+")):
            m = re.search(r"Maker_ID=(\d+)", a["href"])
            if m:
                mid  = int(m.group(1))
                name = clean(a.get_text())
                if mid not in makers:
                    makers[mid] = name

        # Follow pagination (Next page links)
        for a in soup.find_all("a", href=re.compile(r"/cozio-archive/browse-the-archive/makers/")):
            href = a["href"]
            full = href if href.startswith("http") else BASE + href
            if full not in visited and "Maker_ID" not in href:
                queue.append(full)

    return makers


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  Maker page  →  profile + sales rows + instrument IDs
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_price_cell(td):
    """Extract numeric value from a price <td>, return float or None.
    A value of 0 is treated as missing (not a real price)."""
    txt = clean(td.get_text())
    txt = re.sub(r"[^\d.]", "", txt)
    try:
        v = float(txt) if txt else None
        return v if v else None  # 0.0 → None
    except ValueError:
        return None


def scrape_maker_page(maker_id, index_name=None):
    """
    Scrape a single maker page.

    Returns a dict with keys:
      'profile'  : dict  (maker_name, role, date, info, city)
      'sales'    : list of dicts  (one per sale row)
      'inst_ids' : list of int  (instrument IDs linked from this page)

    index_name : the name in 'Last, First' format from the makers-index page;
                 used as the canonical maker_name when provided.
    """
    url  = BASE + f"/cozio-archive/browse-the-archive/makers/maker/?Maker_ID={maker_id}"
    soup = get_soup(url)
    if not soup:
        return None

    result = {"profile": {}, "sales": [], "inst_ids": []}

    # ── 2a. Profile ───────────────────────────────────────────────────────────
    # Prefer the index name (already in "Last, First" format).
    # Fall back to the page <title> if index_name is absent.
    if index_name:
        maker_name = index_name
    else:
        maker_name = ""
        title_tag = soup.find("title")
        if title_tag:
            title_txt = clean(title_tag.get_text())
            maker_name = re.sub(r"\s*\|\s*Tarisio\s*$", "", title_txt, flags=re.I).strip()
            if re.search(r"(?i)maker\s+profile", maker_name):
                maker_name = ""

    # Breadcrumb fallback (only if we still have no name)
    if not maker_name:
        bc = soup.find("ul", class_="breadcrumbs")
        if bc:
            last_li = bc.find("li", class_="last")
            if last_li:
                maker_name = clean(last_li.get_text())

    # Role + date: from <p class="details"> (first one only — second is "Price History")
    # Possible formats:
    #   "Violin maker: 1720–1790"          → role="Violin maker", date="1720–1790"
    #   "(c. 1798 – 1814)"                 → role="",             date="c. 1798–1814"
    #   "Violin maker (fl. 1889–1914)"     → role="Violin maker", date="fl. 1889–1914"
    # Rule: role always ends with "maker" (or "mak." etc.) — truncate there.
    role, date_str = "", ""
    details_tags = soup.find_all("p", class_="details")
    details = None
    for dt in details_tags:
        txt_test = clean(dt.get_text())
        if txt_test.lower() not in ("price history", ""):
            details = dt
            break

    if details:
        txt = clean(details.get_text(" "))
        txt = (txt.replace("\uFB02", "fl").replace("\uFB01", "fi")
                  .replace("&fllig;", "fl").replace("&ffllig;", "ffl")
                  .replace("&filig;", "fi").replace("&ndash;", "–")
                  .replace("&mdash;", "—"))

        # Try colon split first: "Violin maker: 1720–1790"
        m = re.match(r"^(.*?[a-zA-Z])\s*:\s*(.+)$", txt, re.S)
        if m:
            role_raw = clean(m.group(1))
            date_raw = clean(m.group(2))
        else:
            # No colon — might be date-only like "(c. 1798 – 1814)"
            role_raw = ""
            date_raw = txt

        # Truncate role at the LAST occurrence of "maker" (inclusive)
        # e.g. "Bow maker / Violin maker" → "Bow maker / Violin maker"
        maker_m = None
        for mm in re.finditer(r"(?i)\bmaker\b", role_raw):
            maker_m = mm
        if maker_m:
            role = clean(role_raw[:maker_m.end()])
        else:
            role = role_raw

        # Now extract the date from date_raw (or from full txt if no colon)
        _DATE_PAT = re.compile(
            r"\(?\s*(?:"
            r"fl\.?\s*\d{4}\s*[-–]\s*(?:(?:c\.|after|before)\s*)?\d{4}"
            r"|fl\.?\s*\d{4}\s*[-–]\s*(?:after|before)\s+\d{4}"
            r"|fl\.?\s*\d{4}\s*[-–]\s*"
            r"|fl\.?\s*\d{4}"
            r"|b\.?\s*\d{4}"
            r"|d\.?\s*\d{4}"
            r"|c\.?\s*\d{4}\s*[-–]\s*(?:(?:c\.|after|before)\s*)?\d{4}"
            r"|c\.?\s*\d{4}"
            r"|\d{4}\s*[-–]\s*(?:(?:c\.|after|before)\s*)?\d{4}"
            r"|\d{4}\s*[-–]\s*"
            r")\s*\)?",
            re.I,
        )
        # Search in combined in case date spans the colon
        combined = (role_raw + " " + date_raw).strip()
        dm = _DATE_PAT.search(combined)
        if dm:
            date_str = clean(dm.group(0)).strip("()").strip()
            # Recompute role as everything before the date match
            role_part = combined[:dm.start()].strip()
            # Truncate at the LAST occurrence of "maker"
            maker_m2 = None
            for mm in re.finditer(r"(?i)\bmaker\b", role_part):
                maker_m2 = mm
            if maker_m2:
                role = clean(role_part[:maker_m2.end()])
            elif role_part:
                role = clean(role_part)
        elif date_raw and not date_str:
            date_str = date_raw.strip("()").strip()

        # A date must contain at least one digit — otherwise it's a parsing artefact
        if date_str and not re.search(r"\d", date_str):
            date_str = ""
        # Final cleanup
        # Role must contain "maker" — if not, it's a parsing artefact
        if role and not re.search(r"(?i)\bmaker\b", role):
            role = ""
        # Strip stray leading/trailing punctuation from role
        role = re.sub(r"^[^A-Za-z]+|[^A-Za-z)]+$", "", role).strip()
        # Strip unmatched leading parens and junk from date
        date_str = re.sub(r"^[^0-9a-zA-Z(fl.c]+", "", date_str).strip()
        date_str = re.sub(r"^\(+\s*(?=[^0-9])", "", date_str).strip()

    # Biographical info: first substantial <p> in .home-text or .generic-text
    # Skip login prompts, auction-record teasers, and "view all" links.
    # NOTE: "read less" / "read more" are stripped from the paragraph text
    # before the check — they are often injected as inline links inside the <p>
    # that contains the actual bio, and should not disqualify the whole paragraph.
    info = ""
    _SKIP_INFO = re.compile(
        r"(?i)"
        r"tarisio\s+account"
        r"|log\s*in"
        r"|sign\s*up"
        r"|subscribe"
        r"|register"
        r"|auction\s+record\s+for\s+this\s+maker"
        r"|view\s+all\s+auction\s+(price|result)"
        r"|price\s+history"
        r"|^\s*read\s+less\s*$"           # only skip if the entire paragraph is this
        r"|^\s*read\s+more\s*$"           # idem
        r"|we\s+have\s+sent\s+you\s+an\s+email"
        r"|confirm\s+(your\s+)?registration"
        r"|click\s+the\s+button\s+below"
        r"|email\s+you\s+a\s+link"
        r"|generate\s+(your\s+)?tarisio\s+password"
        r"|there\s+was\s+(a\s+)?problem\s+sending"
        r"|sending\s+(your\s+)?activ"
        # Instrument page titles: "Violin - c. 1743 Cremona" or "Violin Bow - 1820 Paris"
        r"|^(violin|viola|cello|double\s+bass|bow|violin\s+bow|viola\s+bow|cello\s+bow)\s*[-–]"
    )
    _NAV_FRAGMENTS = re.compile(r"(?i)\bRead\s+(?:less|more)\b")

    for div in soup.find_all("div", class_=re.compile(r"home-text|generic-text")):
        for p in div.find_all("p"):
            raw = clean(p.get_text())
            # Strip inline nav link fragments ("Read less", "Read more") before
            # applying the skip filter — they are often injected inside bio paragraphs.
            txt = _NAV_FRAGMENTS.sub("", raw).strip()
            # Normalise Unicode ligatures AND literal HTML entity &fllig;
            txt = (txt.replace("\uFB02", "fl")
                      .replace("\uFB01", "fi")
                      .replace("&fllig;", "fl")
                      .replace("&ffllig;", "ffl")
                      .replace("&filig;", "fi"))
            if len(txt) > 40 and not _SKIP_INFO.search(txt):
                info = txt
                break
        if info:
            break

    # Fallback: <meta name="description"> often contains the bio on Tarisio pages
    if not info:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            candidate = clean(meta["content"])
            candidate = _NAV_FRAGMENTS.sub("", candidate).strip()
            if len(candidate) > 40 and not _SKIP_INFO.search(candidate):
                info = candidate

    # City: from "MORE FROM <City>" link
    city = ""
    for a in soup.find_all("a", href=re.compile(r"/browse-the-archive/cities/\?City_ID=")):
        m2 = re.match(r"(?i)more\s+from\s+(.+)", clean(a.get_text()))
        if m2:
            city = clean(m2.group(1))
            break

    result["profile"] = {
        "maker_id":   maker_id,
        "maker_name": maker_name,
        "role":       role,
        "date":       date_str,
        "info":       info,
        "city":       city,
    }

    # ── 2b. Sales — scrape the dedicated price history page ───────────────────
    # The maker page itself has no sales table; all sales are on the price
    # history page at /cozio-archive/price-history/?Maker_ID=XXXX
    ph_url  = BASE + f"/cozio-archive/price-history/?Maker_ID={maker_id}"
    ph_soup = get_soup(ph_url)
    if ph_soup:
        for table in ph_soup.find_all("table"):
            headers = [clean(th.get_text()).lower()
                       for th in table.find_all("th")]
            if not headers:
                first_tr = table.find("tr")
                if first_tr:
                    headers = [clean(td.get_text()).lower()
                                for td in first_tr.find_all(["th", "td"])]

            if not any(h in headers for h in ("date", "sale date", "usd", "price")):
                continue

            col = {h: i for i, h in enumerate(headers)}

            def _get(cells, key, *aliases):
                for k in (key, *aliases):
                    if k in col and col[k] < len(cells):
                        return clean(cells[col[k]].get_text())
                return ""

            for tr in table.find_all("tr")[1:]:
                cells = tr.find_all(["td", "th"])
                if not cells:
                    continue

                sale_date     = _get(cells, "date", "sale date")
                inst_type     = _get(cells, "type", "instrument")
                auction_house = _get(cells, "auction house", "house", "sale")
                lot           = _get(cells, "lot", "lot no", "lot number")

                usd = gbp = eur = None
                bold_currency = None
                for ci, cell in enumerate(cells):
                    h   = headers[ci] if ci < len(headers) else ""
                    val = _parse_price_cell(cell)
                    if val is None:
                        continue
                    if "usd" in h or "$" in h:
                        usd = val
                    elif "gbp" in h or "£" in h or "stg" in h:
                        gbp = val
                    elif "eur" in h or "€" in h:
                        eur = val
                    if cell.find("strong") or cell.find("b"):
                        bold_currency = re.sub(r"[^a-z]", "", h) or None

                if not sale_date:
                    continue

                result["sales"].append({
                    "maker_id":      maker_id,
                    "maker_name":    maker_name,
                    "type":          inst_type,
                    "city_maker":    city,
                    "auction_house": auction_house,
                    "sale_date":     sale_date,
                    "lot":           lot,
                    "usd":           usd,
                    "gbp":           gbp,
                    "eur":           eur,
                    "bold_currency": bold_currency,
                })

    # ── 2c. Instrument IDs ────────────────────────────────────────────────────
    # Links are relative: "../../property/?ID=119636"
    # Match any href containing "property/?ID=" or "/property/?ID="
    for a in soup.find_all("a", href=re.compile(r"property/\?ID=\d+")):
        m = re.search(r"ID=(\d+)", a["href"])
        if m:
            iid = int(m.group(1))
            if iid not in result["inst_ids"]:
                result["inst_ids"].append(iid)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  Instrument page  →  instrument record
# ═══════════════════════════════════════════════════════════════════════════════

# ── Auction-catalog references (from augment_references_from_catalogs.py) ─────

AUCTION_REF_RE = re.compile(r"(?i)\bAuction\s+Catalog(?:ue)?\b")
MONTHS_PAT = r"(January|February|March|April|May|June|July|August|September|October|November|December)"

_DATE_PATS = [
    re.compile(rf"\b{MONTHS_PAT}\s+\d{{1,2}}(?:\s*(?:-|–|—|to)\s*\d{{1,2}})?\s*,\s*\d{{4}}\b", re.I),
    re.compile(rf"\b{MONTHS_PAT}\s+\d{{1,2}}(?:\s*,\s*\d{{1,2}})*(?:\s*&\s*\d{{1,2}})?\s*,\s*\d{{4}}\b", re.I),
    re.compile(rf"\b{MONTHS_PAT}\s+\d{{4}}\b", re.I),
]

def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()

_VARIANT_MAP = {
    "sotheby parke bernet": "Sotheby's", "sotheby's": "Sotheby's",
    "sothebys": "Sotheby's", "sotheby": "Sotheby's",
    "christie's": "Christie's", "christies": "Christie's", "christie": "Christie's",
    "christie & manson": "Christie & Manson",
    "bonhams": "Bonhams",
    "brompton's": "Brompton's", "brompton": "Brompton's",
    "bongartz's": "Bongartz's", "bongartz": "Bongartz's",
    "tarisio private sales": "Tarisio", "tarisio": "Tarisio",
    "t2 auctions": "T2 Auctions", "t2": "T2 Auctions",
    "phillip's": "Phillip's", "phillips": "Phillip's",
    "skinner": "Skinner",
    "ingles & hayday": "Ingles & Hayday",
    "ingles and hayday": "Ingles & Hayday",
    "vichy-enchères": "Vichy-Enchères", "vichy enchères": "Vichy-Enchères",
    "vichy-encheres": "Vichy-Enchères", "vichy encheres": "Vichy-Enchères",
    "guy laurent": "Guy Laurent",
    "dorotheum": "Dorotheum",
    "puttick & simpson": "Puttick & Simpson",
    "freeman's auctions": "Freeman's Auctions", "freemans": "Freeman's Auctions",
    "hôtel drouot": "Hôtel Drouot", "hotel drouot": "Hôtel Drouot",
    "tajan": "Tajan", "ader tajan": "Ader Tajan",
    "claude aguttes": "Claude Aguttes", "aguttes": "Claude Aguttes",
    "butterfield & butterfield": "Butterfield & Butterfield",
    "drouot-richelieu": "Drouot-Richelieu",
    "millon & associés (gilles chancereul)": "Millon & Associés (Gilles Chancereul)",
}
_SORTED_VARIANTS = sorted(_VARIANT_MAP, key=len, reverse=True)


def _extract_catalog_dates(txt):
    dates, seen = [], set()
    for pat in _DATE_PATS:
        for m in pat.finditer(txt):
            d = clean(m.group(0))
            if d not in seen:
                seen.add(d)
                dates.append(d)
    return dates


def _extract_catalog_house(txt):
    low = _norm(txt)
    for var in _SORTED_VARIANTS:
        if var in low:
            return _VARIANT_MAP[var]
    return None


def _scrape_catalog_references(soup):
    """
    Parse the References section for Auction Catalog entries.
    Returns list of (house, date) pairs.
    """
    pairs = []
    ref_h2 = _section_h2(soup, "References")
    if not ref_h2:
        return pairs
    ul = ref_h2.find_next("ul")
    if not ul:
        return pairs

    seen = set()
    for li in ul.find_all("li"):
        txt = clean(li.get_text(" ", strip=True))
        if not AUCTION_REF_RE.search(txt):
            continue
        dates = _extract_catalog_dates(txt)
        house = _extract_catalog_house(txt) or "unknown"
        for d in dates:
            pair = (house, d)
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
        if not dates:
            pair = (house, "unknown")
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


# ── Instrument body (info + physical description) ─────────────────────────────

_QUALITATIVE = re.compile(r"(?i)^(back|top|scroll|ribs?|varnish)\b")
_MEASUREMENT  = re.compile(
    r"(?i)^(length of back|length of stick|upper bouts?|middle bouts?|"
    r"lower bouts?|weight|stick|hair|adjuster|frog|head|button)\b"
)

def _parse_body(soup, existing_info=""):
    home = soup.find("div", class_="home-text")
    if not home:
        return "", ""
    label_parts, phys_parts = [], []
    info_lower = existing_info.lower()
    for p in home.find_all("p"):
        strong = p.find("strong")
        if not strong:
            continue
        key  = clean(strong.get_text())
        rest = clean(p.get_text(" ", strip=True))
        if _QUALITATIVE.match(key):
            field = key.rstrip(":").strip()
            if not re.search(rf"(?i)(^|[|,])\s*{re.escape(field)}\s*:", existing_info):
                phys_parts.append(rest)
        elif _MEASUREMENT.match(key):
            pass
        else:
            label_parts.append(rest)
    return " | ".join(label_parts), " | ".join(phys_parts)


# ── Provenance + known players ────────────────────────────────────────────────

_SOLD_BY  = re.compile(r"(?i)^\s*sold\s+by\b")
_ANON     = re.compile(r"(?i)^\s*(anonymous|current\s+owner|private\s+(collection|owner))\s*$")
_ELLIPSIS = re.compile(r"^\s*\.{2,}\s*$")
_HOUSE_KW = re.compile(
    r"(?i)\b(sotheby|christie|bonham|tarisio|drouot|skinner|"
    r"bongartz|dorotheum|phillips|butterfield|freeman|aguttes|tajan)\b"
)

def _is_owner(name):
    return (name and not _SOLD_BY.search(name) and not _ANON.match(name)
            and not _ELLIPSIS.match(name) and not _HOUSE_KW.search(name))

def _scrape_provenance(soup):
    names, seen = [], set()
    def _add(raw):
        n = clean(raw)
        key = re.sub(r",\s*.+$", "", n).lower().strip()
        if n and _is_owner(n) and key not in seen:
            seen.add(key); names.append(n)
    prov = _section_h2(soup, "Provenance")
    if prov:
        tbl = prov.find_next("table")
        if tbl:
            for tr in tbl.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    _add(tds[1].get_text(" ", strip=True))
    kp = _section_h2(soup, "Known players")
    if kp:
        p = kp.find_next("p")
        if p:
            for part in re.split(r"[,\n]", p.get_text()):
                _add(part.strip())
    return names


# ── Certificates ──────────────────────────────────────────────────────────────

def _scrape_certificates(soup):
    certs = []
    h2 = _section_h2(soup, "Certificates & Documents") or next(
        (h for h in soup.find_all("h2") if "certificate" in h.get_text().lower()), None
    )
    if not h2:
        return certs
    ul = h2.find_next("ul")
    if not ul:
        return certs
    for li in ul.find_all("li"):
        s = li.find("strong")
        if s and "certificate" in s.get_text().lower():
            txt = re.sub(r"(?i)^certificate\s*:\s*", "",
                         clean(li.get_text(" ", strip=True))).strip()
            if txt:
                certs.append(txt)
    return certs


# ── City (from instrument page — "MORE FROM <City>") ─────────────────────────

def _scrape_city_from_instrument(soup):
    for a in soup.find_all("a", href=re.compile(r"/browse-the-archive/cities/\?City_ID=")):
        m = re.match(r"(?i)more\s+from\s+(.+)", clean(a.get_text()))
        if m:
            return clean(m.group(1))
    return ""


# ── Notes (property-notes div) ────────────────────────────────────────────────

def _scrape_notes(soup):
    div = soup.find("div", id="property-notes")
    if not div:
        return ""
    # Strip the "Notes:" label
    txt = clean(div.get_text(" ", strip=True))
    txt = re.sub(r"(?i)^notes\s*:\s*", "", txt).strip()
    return txt


# ── Master instrument scraper ─────────────────────────────────────────────────

def scrape_instrument_page(instrument_id, maker_id=None):
    """
    Scrape a Cozio property page.
    Returns a dict with all instrument fields, or None on failure.
    """
    url  = BASE + f"/cozio-archive/browse-the-archive/property/?ID={instrument_id}"
    soup = get_soup(url)
    if not soup:
        return None

    # instrument type from <h1>: "Maker Name, City, YYYY, nickname"
    # type from <p class="details">
    inst_type = ""
    details   = soup.find("p", class_="details")
    if details:
        txt = clean(details.get_text())
        # "Violin: 12345"  or just "Violin Bow"
        m = re.match(r"([A-Za-z\s]+?)(?::\s*\d+)?$", txt)
        if m:
            inst_type = clean(m.group(1))

    # make_date: year in <h1> after city
    make_date = ""
    h1 = soup.find("h1")
    if h1:
        years = re.findall(r"\b(1[3-9]\d{2}|20[01]\d)\b", h1.get_text())
        if years:
            make_date = years[0]

    # Catalog references
    cat_pairs = _scrape_catalog_references(soup)
    cat_dates  = "; ".join(d for _, d in cat_pairs if d != "unknown")
    cat_houses = "; ".join(h for h, _ in cat_pairs if h != "unknown")

    # year_sale / house_sale from catalog pairs (deduplicated years)
    years_seen, houses_seen = [], []
    for h, d in cat_pairs:
        m = re.search(r"\b(\d{4})\b", d)
        y = m.group(1) if m else None
        if y and y not in years_seen:
            years_seen.append(y)
            houses_seen.append(h)
    year_sale  = "; ".join(years_seen)
    house_sale = "; ".join(houses_seen)

    # Info + physical description
    existing_info = ""   # fresh scrape — no existing info
    label_text, other_info = _parse_body(soup, existing_info)

    # Measurements from info paragraphs
    measurements = {}
    home = soup.find("div", class_="home-text")
    if home:
        for p in home.find_all("p"):
            s = p.find("strong")
            if not s:
                continue
            key = clean(s.get_text()).rstrip(":")
            val_raw = clean(p.get_text(" ", strip=True))
            val = re.sub(re.escape(key) + r"\s*:?\s*", "", val_raw, count=1, flags=re.I).strip()
            kl = key.lower()
            if "length of back" in kl:
                measurements["length_of_back"] = val
            elif "upper bout" in kl:
                measurements["upper_bouts"] = val
            elif "middle bout" in kl:
                measurements["middle_bouts"] = val
            elif "lower bout" in kl:
                measurements["lower_bouts"] = val
            elif "weight" in kl:
                measurements["weight"] = val

    return {
        "instrument_id":            instrument_id,
        "maker_id":                 maker_id,
        "type":                     inst_type,
        "make_date":                make_date,
        "year_sale":                year_sale,
        "house_sale":               house_sale,
        "info":                     label_text,
        "other_info":               other_info,
        "auction_catalog_date":     cat_dates,
        "auction_catalog_house":    cat_houses,
        "notes":                    _scrape_notes(soup),
        "city":                     _scrape_city_from_instrument(soup),
        "provenance_known_players": "; ".join(_scrape_provenance(soup)),
        "certificates":             "; ".join(_scrape_certificates(soup)),
        **measurements,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

SALES_COLS = [
    "maker_id", "maker_name", "type", "city_maker",
    "auction_house", "sale_date", "lot", "usd", "gbp", "eur", "bold_currency",
]

INSTRUMENTS_COLS = [
    "instrument_id", "maker_id", "type", "make_date",
    "year_sale", "house_sale",
    "info", "other_info",
    "auction_catalog_date", "auction_catalog_house",
    "notes", "city",
    "provenance_known_players", "certificates",
    "length_of_back", "upper_bouts", "middle_bouts", "lower_bouts", "weight",
]

MAKERS_COLS = [
    "maker_name", "maker_id", "role", "date", "info",
]


def _load_existing_ids(path, id_col):
    """Return set of already-scraped IDs from a CSV, or empty set."""
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, usecols=[id_col], dtype=str)
        return set(df[id_col].dropna().astype(int).tolist())
    except Exception:
        return set()


def _append_rows(path, rows, cols):
    """Append rows to a CSV, writing header only if file is new."""
    if not rows:
        return
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(rows)


def _dedup_csv(path, id_col):
    """Deduplicate a CSV by id_col in-place, keeping the first occurrence."""
    if not os.path.exists(path):
        return
    try:
        df = pd.read_csv(path, dtype=str)
        before = len(df)
        df = df.drop_duplicates(subset=[id_col], keep="first")
        after = len(df)
        if before != after:
            df.to_csv(path, index=False, encoding="utf-8-sig")
            print(f"  Deduplicated {os.path.basename(path)}: {before} → {after} rows")
    except Exception as e:
        print(f"  [WARN] Could not deduplicate {path}: {e}")


def run(
    out_dir,
    workers=6,
    resume=False,
    max_makers=None,
    skip_makers=False,
    skip_instruments=False,
    skip_sales=False,
    checkpoint_every=50,
):
    os.makedirs(out_dir, exist_ok=True)
    sales_path   = os.path.join(out_dir, "sales.csv")
    instru_path  = os.path.join(out_dir, "instruments.csv")
    makers_path  = os.path.join(out_dir, "makers.csv")

    # ── Step 1: Discover maker IDs ────────────────────────────────────────────
    print("Step 1/3 — Discovering maker IDs ...")
    all_makers = scrape_maker_ids()
    print(f"  Found {len(all_makers)} makers.")

    # Sort by name in proper Unicode-aware alphabetical order.
    # Names from the index are already in "Last, First" format, so sorting
    # on _sort_key(name) gives correct last-name-first alphabetical order.
    all_makers = dict(sorted(all_makers.items(), key=lambda x: _sort_key(x[1])))

    if max_makers:
        all_makers = dict(list(all_makers.items())[:max_makers])

    # ── Step 2: Scrape maker pages (profile + sales + instrument IDs) ─────────
    if not skip_makers and not skip_sales:
        done_maker_ids = _load_existing_ids(makers_path, "maker_id") if resume else set()
        todo_makers    = {k: v for k, v in all_makers.items() if k not in done_maker_ids}
        print(f"\nStep 2/3 — Scraping {len(todo_makers)} maker pages ...")

        maker_rows  = []
        sales_rows  = []
        all_inst_ids = defaultdict(int)  # inst_id → maker_id
        # Track maker_ids written during this run to prevent intra-run duplicates
        seen_maker_ids_this_run: set = set()

        def _process_maker(args):
            mid, mname = args
            # Pass the index name (already "Last, First") as the canonical name
            data = scrape_maker_page(mid, index_name=mname)
            if data is None:
                return mid, None
            return mid, data

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_process_maker, item): item for item in todo_makers.items()}
            for fut in as_completed(futs):
                mid, data = fut.result()
                done += 1

                if data:
                    if not skip_makers:
                        maker_rows.append(data["profile"])
                    if not skip_sales:
                        sales_rows.extend(data["sales"])
                    for iid in data["inst_ids"]:
                        all_inst_ids[iid] = mid

                if done % checkpoint_every == 0 or done == len(todo_makers):
                    print(f"  Makers: {done}/{len(todo_makers)}")
                    if maker_rows:
                        # Deduplicate before writing: skip any maker_id already
                        # written in this run (concurrent futures can produce dupes)
                        deduped = []
                        for row in maker_rows:
                            rid = int(row.get("maker_id", -1))
                            if rid not in seen_maker_ids_this_run:
                                seen_maker_ids_this_run.add(rid)
                                deduped.append(row)
                        _append_rows(makers_path, deduped, MAKERS_COLS)
                        maker_rows = []
                    if sales_rows:
                        _append_rows(sales_path, sales_rows, SALES_COLS)
                        sales_rows = []

        # Flush remainders
        if maker_rows:
            deduped = []
            for row in maker_rows:
                rid = int(row.get("maker_id", -1))
                if rid not in seen_maker_ids_this_run:
                    seen_maker_ids_this_run.add(rid)
                    deduped.append(row)
            _append_rows(makers_path, deduped, MAKERS_COLS)
        if sales_rows:
            _append_rows(sales_path, sales_rows, SALES_COLS)

        # Final deduplication pass on the CSV (catches duplicates from previous runs)
        _dedup_csv(makers_path, "maker_id")

    else:
        # If skipping maker scrape, still collect instrument IDs from makers CSV
        all_inst_ids = {}
        if os.path.exists(instru_path):
            df_i = pd.read_csv(instru_path, usecols=["instrument_id", "maker_id"], dtype=str)
            all_inst_ids = {int(r.instrument_id): int(r.maker_id)
                            for r in df_i.itertuples() if pd.notna(r.maker_id)}
        print(f"\nStep 2/3 — Skipped (using {len(all_inst_ids)} existing instrument IDs).")

    # ── Step 3: Scrape instrument pages ───────────────────────────────────────
    if not skip_instruments:
        done_inst_ids = _load_existing_ids(instru_path, "instrument_id") if resume else set()
        todo_insts    = {iid: mid for iid, mid in all_inst_ids.items()
                         if iid not in done_inst_ids}
        print(f"\nStep 3/3 — Scraping {len(todo_insts)} instrument pages ...")

        inst_rows = []
        done = 0

        def _process_inst(args):
            iid, mid = args
            data = scrape_instrument_page(iid, maker_id=mid)
            return iid, data

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_process_inst, item): item for item in todo_insts.items()}
            for fut in as_completed(futs):
                iid, data = fut.result()
                done += 1
                if data:
                    inst_rows.append(data)
                if done % checkpoint_every == 0 or done == len(todo_insts):
                    print(f"  Instruments: {done}/{len(todo_insts)}")
                    if inst_rows:
                        _append_rows(instru_path, inst_rows, INSTRUMENTS_COLS)
                        inst_rows = []

        if inst_rows:
            _append_rows(instru_path, inst_rows, INSTRUMENTS_COLS)

    print(f"\nDone. Output files in: {out_dir}")
    for p in [sales_path, instru_path, makers_path]:
        if os.path.exists(p):
            n = sum(1 for _ in open(p, encoding="utf-8-sig")) - 1
            print(f"  {os.path.basename(p)}: {n} rows")


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="Unified Tarisio scraper — produces sales.csv, instruments.csv, makers.csv"
    )
    ap.add_argument("--out-dir",           required=True,
                    help="Output directory for the three CSV files")
    ap.add_argument("--workers",           type=int, default=6,
                    help="Number of parallel HTTP threads (default 6)")
    ap.add_argument("--resume",            action="store_true",
                    help="Skip already-scraped IDs (incremental run)")
    ap.add_argument("--max-makers",        type=int, default=None,
                    help="Limit to first N makers (for testing)")
    ap.add_argument("--skip-makers",       action="store_true",
                    help="Do not (re)scrape maker profiles")
    ap.add_argument("--skip-instruments",  action="store_true",
                    help="Do not (re)scrape instrument pages")
    ap.add_argument("--skip-sales",        action="store_true",
                    help="Do not (re)scrape sales tables")
    ap.add_argument("--checkpoint-every",  type=int, default=50,
                    help="Write to disk every N completions (default 50)")
    args = ap.parse_args()

    run(
        out_dir          = args.out_dir,
        workers          = args.workers,
        resume           = args.resume,
        max_makers       = args.max_makers,
        skip_makers      = args.skip_makers,
        skip_instruments = args.skip_instruments,
        skip_sales       = args.skip_sales,
        checkpoint_every = args.checkpoint_every,
    )


if __name__ == "__main__":
    main()