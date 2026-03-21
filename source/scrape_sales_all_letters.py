import re
import time
import random
import hashlib
import os
import csv
from urllib.parse import urljoin
from http.cookiejar import MozillaCookieJar
import requests
from bs4 import BeautifulSoup
import pandas as pd

BASE = "https://tarisio.com"
MAKERS_INDEX = f"{BASE}/cozio-archive/browse-the-archive/makers/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; tarisio-scraper; +https://example.com/contact)",
    "Accept-Language": "en-US,en;q=0.9",
}

LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

OUT_CSV  = "tarisio_cozio_sales_ALL.csv"
OUT_COLS = [
    "maker_id", "maker_name_index", "letter",
    "type", "city_maker", "auction_house", "sale_date", "lot",
    "usd", "gbp", "eur", "bold_currency",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def jitter(a=0.3, b=0.8):
    time.sleep(random.uniform(a, b))


def get_soup(session, url, retries=3):
    for k in range(retries):
        r = session.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
        if r.status_code == 200:
            return BeautifulSoup(r.text, "html.parser")
        if r.status_code in (429, 503):
            wait = 2 ** k * 5
            print(f"  [rate limit {r.status_code}] waiting {wait}s...")
            time.sleep(wait)
        else:
            time.sleep(0.8)
    raise RuntimeError(f"GET failed after {retries} retries: {url}")


def parse_core_json(soup):
    for s in soup.find_all("script"):
        if s.string and "var core =" in s.string:
            m = re.search(r"var\s+core\s*=\s*(\{.*?\})", s.string, re.S)
            if m:
                txt = m.group(1)
                mp  = re.search(r'"maxPages"\s*:\s*(\d+)', txt)
                return {"maxPages": int(mp.group(1)) if mp else 1}
    return {"maxPages": 1}


def extract_id_from_url(url):
    m = re.search(r"[?&](?:Maker_ID|ID|id)=(\d+)", url)
    return int(m.group(1)) if m else None


def clean_text(cell):
    t = cell.get_text(" ", strip=True).replace("\xa0", " ").strip()
    return t if t else None


def parse_price(s):
    if not s:
        return None
    s = s.strip()
    if re.search(r"(unsold|withdrawn)", s, re.I):
        return None
    if re.fullmatch(r"-+|—|–|0|€0|£0|\$0", s):
        return None
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def cell_is_bold(td):
    if td.find(["strong", "b"]) is not None:
        return True
    style = td.get("style") or ""
    if "font-weight" in style.lower() and "bold" in style.lower():
        return True
    for child in td.find_all(True, recursive=True):
        if child.name in ("strong", "b"):
            return True
        st = child.get("style") or ""
        if "font-weight" in st.lower() and "bold" in st.lower():
            return True
    return False


def detect_bold_currency(tds):
    bolds = []
    try:
        if cell_is_bold(tds[5]): bolds.append("usd")
        if cell_is_bold(tds[6]): bolds.append("gbp")
        if cell_is_bold(tds[7]): bolds.append("eur")
    except Exception:
        pass
    return bolds[0] if bolds else None


# ── Collect makers for one letter ─────────────────────────────────────────────

def collect_makers_for_letter(session, letter):
    makers   = []
    page_num = 1
    while True:
        if page_num == 1:
            url = f"{MAKERS_INDEX}?letter={letter}"
        else:
            url = f"{BASE}/cozio-archive/browse-the-archive/makers/page/{page_num}/?letter={letter}"
        soup  = get_soup(session, url)
        block = soup.select_one(f"div.letter[rel='{letter}']") or soup.select_one("#az")
        if not block:
            break
        for a in block.select("a[href*='maker/?Maker_ID=']"):
            href = a.get("href")
            name = a.get_text(strip=True)
            if not href:
                continue
            full = urljoin(BASE, href)
            mid  = extract_id_from_url(full)
            if mid:
                makers.append({
                    "maker_id":           mid,
                    "maker_name_index":   name,
                    "maker_profile_url":  full,
                    "letter":             letter,
                })
        core = parse_core_json(soup)
        if page_num >= core.get("maxPages", 1):
            break
        page_num += 1
        jitter()

    # Deduplicate
    seen, uniq = set(), []
    for mk in makers:
        if mk["maker_id"] not in seen:
            seen.add(mk["maker_id"])
            uniq.append(mk)
    return uniq


# ── Parse one price-history page ──────────────────────────────────────────────

def parse_price_history_page(session, url):
    soup  = get_soup(session, url)
    table = soup.select_one("table#price-history")
    rows  = []
    if table:
        for tr in table.find_all("tr"):
            if tr.find("th"):
                continue
            tds = tr.find_all("td")
            if len(tds) < 8:
                continue
            rows.append({
                "type":          clean_text(tds[0]),
                "city_maker":    clean_text(tds[1]),
                "auction_house": clean_text(tds[2]),
                "sale_date":     clean_text(tds[3]),
                "lot":           clean_text(tds[4]),
                "usd":           parse_price(clean_text(tds[5])),
                "gbp":           parse_price(clean_text(tds[6])),
                "eur":           parse_price(clean_text(tds[7])),
                "bold_currency": detect_bold_currency(tds),
            })
    return rows, soup


# ── Scrape all sales for one letter ───────────────────────────────────────────

def scrape_letter(session, letter, done_maker_ids):
    """
    Scrape all price-history pages for makers whose name starts with `letter`.
    Returns a list of sale dicts (each dict includes maker_id, maker_name_index, letter).
    Skips maker_ids already in done_maker_ids.
    """
    makers = collect_makers_for_letter(session, letter)
    print(f"  Letter {letter}: {len(makers)} makers found")

    all_sales = []
    for i, mk in enumerate(makers, 1):
        mid  = mk["maker_id"]
        name = mk["maker_name_index"]

        if mid in done_maker_ids:
            continue

        # Page 1
        url_1        = f"{BASE}/cozio-archive/price-history/?Maker_ID={mid}"
        rows, soup   = parse_price_history_page(session, url_1)

        # Pagination
        core      = parse_core_json(soup)
        max_pages = max(core.get("maxPages", 1), 1)
        for pnum in range(2, max_pages + 1):
            url_p = f"{BASE}/cozio-archive/price-history/page/{pnum}/?Maker_ID={mid}"
            more, _ = parse_price_history_page(session, url_p)
            rows.extend(more)
            jitter()

        # Tag each row with maker info
        for r in rows:
            r["maker_id"]         = mid
            r["maker_name_index"] = name
            r["letter"]           = letter

        all_sales.extend(rows)

        if i % 10 == 0 or i == len(makers):
            print(f"    [{i}/{len(makers)}] {name}  ({len(rows)} sales)")

        jitter()

    return all_sales


# ── CSV append helper ──────────────────────────────────────────────────────────

def append_to_csv(path, rows):
    if not rows:
        return
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(rows)


def load_done_maker_ids(path):
    """Return set of maker_ids already written to out CSV."""
    if not os.path.exists(path):
        return set()
    try:
        df = pd.read_csv(path, usecols=["maker_id"], dtype=str)
        return set(df["maker_id"].dropna().astype(int).tolist())
    except Exception:
        return set()


# ── Session setup ──────────────────────────────────────────────────────────────

cj = MozillaCookieJar()
cj.load("cookies.txt", ignore_discard=True, ignore_expires=True)

session = requests.Session()
session.headers.update(HEADERS)
for c in cj:
    dom = c.domain
    if dom.startswith("#HttpOnly_."): dom = dom[len("#HttpOnly_."):]
    if dom.startswith("www."):        dom = dom[4:]
    session.cookies.set(c.name, c.value, domain=dom, path=c.path or "/")


# ── Main loop ──────────────────────────────────────────────────────────────────

print(f"Output: {OUT_CSV}")
done_maker_ids = load_done_maker_ids(OUT_CSV)
print(f"Already done: {len(done_maker_ids)} makers\n")

total_sales = sum(1 for _ in open(OUT_CSV, encoding="utf-8-sig")) - 1 if os.path.exists(OUT_CSV) else 0

for letter in LETTERS:
    print(f"\n=== Letter {letter} ===")
    try:
        sales = scrape_letter(session, letter, done_maker_ids)
        append_to_csv(OUT_CSV, sales)
        # Update done set so we don't re-scrape within this run
        done_maker_ids.update(r["maker_id"] for r in sales)
        total_sales += len(sales)
        print(f"  → {len(sales)} new sales  (total so far: {total_sales})")
    except Exception as e:
        print(f"  [ERROR] letter {letter}: {e}")
        # Continue with next letter rather than crashing entirely

print(f"\nDone. Total rows in {OUT_CSV}: {total_sales}")
