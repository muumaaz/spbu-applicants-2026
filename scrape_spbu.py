#!/usr/bin/env python3
"""
SPbU Foreign Applicants 2026 — Complete Scraper
================================================
Task 1: Scrape all four applicant-type pages (bak/spec/mag/asp)
         Extract UID, Country, Programs_Applied, Applicant_Type

Task 2: Download Olympiad result PDFs from abiturient.spbu.ru.
        Green-highlighted UIDs = winners ("Accepted with budget").
        Non-green UIDs = participants ("Accepted").

Task 3: Merge, assign Status, build DataFrame, export CSV.

Data architecture:
  cabinet.spbu.ru/Lists/ForeignersLists/{bak,spec,mag,asp}/
    index_full_list.html   → table with UID + GUID per applicant
    data/<GUID>.txt        → HTML fragment listing programs per applicant
    index_comp_groups.html → index of competition groups with links to list_*.html
    list_<GUID>.html       → per-program tables with UID + Country (Citizenship)

  abiturient.spbu.ru/medialibrary/ru/2025/ino/
    results_Olympiad_bak_2026.pdf → Bachelor + Specialist final result list
    results_Olympiad_mag_2026.pdf → Master final result list
    results_Olympiad_asp_2026.pdf → PhD/Aspirantura final result list
    Green-shaded rows in these PDFs = budget-funded olympiad winners.

Optimisation: ThreadPoolExecutor (5 workers) for data/*.txt and list_*.html.
Requirements: requests, beautifulsoup4, lxml, pandas, PyMuPDF (fitz)
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import fitz  # PyMuPDF — for PDF colour extraction
import time
import re
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SPbU-Scraper/1.0)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

BASE_URLS = {
    "Bachelor":   "https://cabinet.spbu.ru/Lists/ForeignersLists/bak/",
    "Specialist": "https://cabinet.spbu.ru/Lists/ForeignersLists/spec/",
    "Master":     "https://cabinet.spbu.ru/Lists/ForeignersLists/mag/",
    "PhD":        "https://cabinet.spbu.ru/Lists/ForeignersLists/asp/",
}

# Olympiad result PDFs (published on abiturient.spbu.ru/reception-foreign/budget/)
OLYMPIAD_PDFS = {
    "bak_spec": "https://abiturient.spbu.ru/medialibrary/ru/2025/ino/results_Olympiad_bak_2026.pdf",
    "mag":      "https://abiturient.spbu.ru/medialibrary/ru/2025/ino/results_Olympiad_mag_2026.pdf",
    "asp":      "https://abiturient.spbu.ru/medialibrary/ru/2025/ino/results_Olympiad_asp_2026.pdf",
}

MAX_WORKERS = 5          # concurrent threads
DELAY_BETWEEN = 1.5      # delay between major page fetches


# ── Helper: robust GET ────────────────────────────────────────────────────────
def safe_get(url, retries=3, timeout=20):
    """Fetch *url* with retries. Returns Response or None."""
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=timeout)
            resp.encoding = "utf-8"
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                log.warning("404 Not Found: %s", url)
                return None
            log.warning("HTTP %s for %s (attempt %d)", resp.status_code, url, attempt + 1)
        except requests.RequestException as exc:
            log.warning("Request error for %s: %s (attempt %d)", url, exc, attempt + 1)
        time.sleep(1)
    log.error("Failed after %d attempts: %s", retries, url)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# TASK 1  —  Scrape applicant lists
# ═══════════════════════════════════════════════════════════════════════════════

def parse_full_list(applicant_type, base_url):
    """Return [{uid, guid, applicant_type}, …] from index_full_list.html."""
    url = base_url + "index_full_list.html"
    log.info("Fetching full list  %-12s %s", applicant_type, url)
    resp = safe_get(url)
    if not resp:
        log.warning("Full-list page unavailable for %s", applicant_type)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table")
    if not table:
        log.warning("No table on full-list page for %s", applicant_type)
        return []

    applicants = []
    for row in table.find_all("tr")[1:]:          # skip header row
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        uid  = cells[0].get_text(strip=True)
        guid = cells[1].get("id", "")             # GUID lives in the <td id="…">
        if uid and guid:
            applicants.append({"uid": uid, "guid": guid, "applicant_type": applicant_type})

    log.info("  → %d applicants in %s full list", len(applicants), applicant_type)
    return applicants


# ── fetch one data/GUID.txt and extract program names ─────────────────────────
_PROG_RE = re.compile(
    r"Образовательная программа / Education program:\s*(.+?);\s*Форма обучения",
    re.DOTALL,   # handle newlines within program names
)

def _fetch_programs(base_url, guid):
    """Return list[str] of program names for one applicant."""
    resp = safe_get(base_url + f"data/{guid}.txt")
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    programs = []
    for p in soup.find_all("p"):
        text = p.get_text(separator=" ", strip=True)
        m = _PROG_RE.search(text)
        if m:
            # Normalise internal whitespace (some entries have \n mid-string)
            programs.append(" ".join(m.group(1).split()))
    return programs


def fetch_all_programs(base_url, applicants):
    """Concurrently fetch programs for every applicant. Returns {uid: [prog, …]}."""
    uid_programs = {}
    total = len(applicants)

    def _task(app):
        return app["uid"], _fetch_programs(base_url, app["guid"])

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_task, a): a for a in applicants}
        done = 0
        for fut in as_completed(futures):
            uid, progs = fut.result()
            uid_programs[uid] = progs
            done += 1
            if done % 300 == 0 or done == total:
                log.info("    programs fetched: %d / %d", done, total)

    return uid_programs


# ── fetch one list_*.html → [(uid, country), …] ──────────────────────────────
def _fetch_list_page(url):
    """Scrape a single list_*.html page. Returns list[(uid, country)]."""
    resp = safe_get(url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table")
    if not table:
        return []
    pairs = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) >= 2:
            uid     = cells[0].get_text(strip=True)
            country = cells[1].get_text(strip=True)
            if uid:
                pairs.append((uid, country))
    return pairs


def scrape_uid_country(base_url, applicant_type):
    """
    Scrape all list_*.html pages linked from index_comp_groups.html.
    Returns dict {uid: country}.
    """
    url = base_url + "index_comp_groups.html"
    log.info("Fetching comp groups %-12s %s", applicant_type, url)
    resp = safe_get(url)
    if not resp:
        log.warning("Comp-groups page unavailable for %s", applicant_type)
        return {}

    soup = BeautifulSoup(resp.text, "lxml")
    links = {a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("list_")}
    log.info("  → %d list pages to scrape", len(links))

    uid_country = {}

    def _task(link):
        return _fetch_list_page(base_url + link)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_task, lnk): lnk for lnk in sorted(links)}
        done = 0
        for fut in as_completed(futures):
            for uid, country in fut.result():
                uid_country[uid] = country
            done += 1
            if done % 50 == 0 or done == len(links):
                log.info("    list pages scraped: %d / %d", done, len(links))

    log.info("  → %d UID→Country mappings", len(uid_country))
    return uid_country


def scrape_task1():
    """
    Task 1: For each applicant type, collect UIDs, programs, and countries.

    Returns list[dict] with keys: UID, Country, Programs_Applied, Applicant_Type
    """
    all_applicants = []

    for app_type, base_url in BASE_URLS.items():
        log.info("\n" + "=" * 60)
        log.info("Processing %s …", app_type)

        # 1. UIDs + GUIDs from full list
        applicants = parse_full_list(app_type, base_url)
        time.sleep(DELAY_BETWEEN)

        # 2. UID→Country from comp-group list pages
        uid_country = scrape_uid_country(base_url, app_type)
        time.sleep(DELAY_BETWEEN)

        # 3. Programs per applicant (concurrent)
        log.info("  Fetching programs for %d applicants …", len(applicants))
        uid_programs = fetch_all_programs(base_url, applicants)

        for app in applicants:
            uid = app["uid"]
            all_applicants.append({
                "UID":              uid,
                "Country":          uid_country.get(uid, ""),
                "Programs_Applied": uid_programs.get(uid, []),
                "Applicant_Type":   app["applicant_type"],
            })

        log.info("  ✔ %s complete (%d applicants)", app_type, len(applicants))

    log.info("\nTask 1 done — %d applicant rows collected", len(all_applicants))
    return all_applicants


# ═══════════════════════════════════════════════════════════════════════════════
# TASK 2  —  Extract olympiad winners from result PDFs
# ═══════════════════════════════════════════════════════════════════════════════

_UID_RE = re.compile(r"\b26\d{6}\b")


def download_pdf(url, local_path):
    """Download a PDF file. Returns True on success."""
    try:
        resp = SESSION.get(url, timeout=30)
        if resp.status_code == 200:
            with open(local_path, "wb") as f:
                f.write(resp.content)
            log.info("  Downloaded %s (%d KB)", local_path, len(resp.content) // 1024)
            return True
        log.warning("  HTTP %d downloading %s", resp.status_code, url)
    except requests.RequestException as exc:
        log.warning("  Download error for %s: %s", url, exc)
    return False


def extract_pdf_uids(path):
    """
    Extract UIDs from an Olympiad result PDF.
    Detects green-shaded background rectangles (RGB where G > R and G > B)
    to distinguish budget-funded winners from regular participants.

    Returns (budget_uids: set, accepted_uids: set)
      - budget_uids:  UIDs in green-highlighted rows (winners)
      - accepted_uids: ALL UIDs in the PDF (winners + participants)
    """
    doc = fitz.open(path)
    budget_uids  = set()
    accepted_uids = set()

    for page in doc:
        # ── find green-fill rectangles on this page ──
        green_rects = []
        for drawing in page.get_drawings():
            fill = drawing.get("fill")
            if fill and len(fill) >= 3:
                r, g, b = fill[0], fill[1], fill[2]
                if g > 0.4 and g > r and g > b:       # green-ish background
                    rect = drawing.get("rect")
                    if rect:
                        green_rects.append(fitz.Rect(rect))

        # ── extract UIDs and check overlap with green rects ──
        for block in page.get_text("dict")["blocks"]:
            if "lines" not in block:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    for match in _UID_RE.finditer(span["text"]):
                        uid = match.group()
                        accepted_uids.add(uid)
                        span_rect = fitz.Rect(span["bbox"])
                        for gr in green_rects:
                            if span_rect.intersects(gr):
                                budget_uids.add(uid)
                                break

    doc.close()
    return budget_uids, accepted_uids


def scrape_task2():
    """
    Task 2: Download Olympiad result PDFs and extract budget vs accepted UIDs.

    Returns (budget_uids: set, accepted_uids: set)
    """
    budget_uids   = set()
    accepted_uids = set()

    for name, url in OLYMPIAD_PDFS.items():
        local_path = f"/tmp/results_{name}.pdf"
        log.info("Processing PDF: %s", name)
        if not download_pdf(url, local_path):
            log.warning("  Skipping %s — download failed", name)
            continue
        b, a = extract_pdf_uids(local_path)
        budget_uids.update(b)
        accepted_uids.update(a)
        log.info("  → %d green (budget), %d total in PDF", len(b), len(a))
        # Clean up
        try:
            os.remove(local_path)
        except OSError:
            pass
        time.sleep(DELAY_BETWEEN)

    log.info("\nTask 2 done:")
    log.info("  Accepted with budget (green): %d", len(budget_uids))
    log.info("  Accepted (in PDF, not green): %d", len(accepted_uids - budget_uids))
    log.info("  Total in PDFs:                %d", len(accepted_uids))

    return budget_uids, accepted_uids


# ═══════════════════════════════════════════════════════════════════════════════
# TASK 3  —  Merge & build DataFrame
# ═══════════════════════════════════════════════════════════════════════════════

def build_dataframe(all_applicants, budget_uids, accepted_uids):
    """
    Build the final DataFrame with status assignment.

    Status priority (applied per row):
      1. UID ∈ budget_uids   → "Accepted with budget"  (green in PDF)
      2. UID ∈ accepted_uids → "Accepted"               (in PDF, not green)
      3. otherwise           → "Not Accepted"            (not in any PDF)
    """
    records = []
    for app in all_applicants:
        uid   = app["UID"]
        progs = app["Programs_Applied"]

        if uid in budget_uids:
            status = "Accepted with budget"
        elif uid in accepted_uids:
            status = "Accepted"
        else:
            status = "Not Accepted"

        records.append({
            "UID":                 uid,
            "Country":             app["Country"],
            "Programs_Applied_To": "; ".join(progs) if progs else "",
            "Count_of_Programs":   len(progs),
            "Status":              status,
            "Applicant":           app["Applicant_Type"],
        })

    df = pd.DataFrame(records)
    df = df.sort_values("UID", ascending=True).reset_index(drop=True)
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    t0 = time.time()
    log.info("SPbU Foreign Applicants 2026 — scraper started")

    # ── Task 1: Scrape applicant lists ──
    log.info("\n" + "=" * 60)
    log.info("TASK 1: Scraping applicant lists …")
    all_applicants = scrape_task1()

    # ── Task 2: Extract budget winners from PDFs ──
    log.info("\n" + "=" * 60)
    log.info("TASK 2: Extracting olympiad winners from result PDFs …")
    budget_uids, accepted_uids = scrape_task2()

    # ── Task 3: Build DataFrame & export ──
    log.info("\n" + "=" * 60)
    log.info("TASK 3: Building final DataFrame …")
    df = build_dataframe(all_applicants, budget_uids, accepted_uids)

    # ── Sanity checks ──
    print("\n" + "=" * 60)
    print("SANITY CHECK — df.head(20):\n")
    pd.set_option("display.max_colwidth", 80)
    pd.set_option("display.width", 200)
    print(df.head(20).to_string(index=False))

    print("\n" + "=" * 60)
    print("Status distribution:\n")
    print(df["Status"].value_counts().to_string())
    print(f"\nTotal rows:          {len(df)}")
    print(f"Unique UIDs:         {df['UID'].nunique()}")
    print(f"\nApplicant type breakdown:\n{df['Applicant'].value_counts().to_string()}")
    print(f"\nCountries represented: {df['Country'].nunique()}")
    print(f"Programs with 0 entries: {(df['Count_of_Programs'] == 0).sum()}")

    # ── Export ──
    out = "spbu_applicants_2026.csv"
    df.to_csv(out, index=False, encoding="utf-8")
    elapsed = time.time() - t0
    log.info("Exported %d rows to %s  (%.1f min elapsed)", len(df), out, elapsed / 60)
    print(f"\n✅ Exported to {out} — {len(df)} rows — {elapsed/60:.1f} min")
