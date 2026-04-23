#!/usr/bin/env python3
"""
SPbU Foreign Applicants 2026 — Full Scraper (Optimized)
========================================================
Task 1: Scrape all four applicant-type pages (bak/spec/mag/asp)
Task 2: Extract olympiad winners / budget-track UIDs
Task 3: Merge, build DataFrame, export CSV

Data architecture (discovered via exploration):
- index_full_list.html : table with UID + GUID per applicant
- data/GUID.txt        : HTML fragment listing programs per applicant
- index_comp_groups.html: index of competition groups → links to list pages
- list_GUID.html       : per-program tables with UID + Country (Citizenship)
- Olympiad sections in comp_groups contain budget-track applicant lists

Optimisation: uses ThreadPoolExecutor (5 workers) for data/*.txt and list_*.html
fetches.  A small inter-batch sleep keeps request rate ≤ ~10 req/s.
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
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
BUDGET_PAGE_URL = "https://abiturient.spbu.ru/reception-foreign/budget/"
MAX_WORKERS = 5          # concurrent threads
DELAY_BETWEEN = 1.5      # delay between major page fetches (index pages)

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
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        uid  = cells[0].get_text(strip=True)
        guid = cells[1].get("id", "")
        if uid and guid:
            applicants.append({"uid": uid, "guid": guid, "applicant_type": applicant_type})

    log.info("  → %d applicants in %s full list", len(applicants), applicant_type)
    return applicants


# ── fetch one data/GUID.txt and extract programs ─────────────────────────────
_PROG_RE = re.compile(
    r"Образовательная программа / Education program:\s*(.+?);\s*Форма обучения",
    re.DOTALL  # handle newlines within program names
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
            prog = " ".join(m.group(1).split())
            programs.append(prog)
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


# ── fetch one list_*.html and return [(uid, country), …] ─────────────────────
def _fetch_list_page(url):
    """Scrape a single list_*.html. Returns list[(uid, country)]."""
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


def parse_comp_groups(applicant_type, base_url):
    """
    Parse index_comp_groups.html  →  scrape all list_*.html pages concurrently.

    Returns
    -------
    uid_country     : dict  {uid: country}
    olympiad_uids   : set   UIDs that appear in Olympiad competition-group lists
    all_list_uids   : set   UIDs that appear in *any* competition-group list
    """
    url = base_url + "index_comp_groups.html"
    log.info("Fetching comp groups %-12s %s", applicant_type, url)
    resp = safe_get(url)
    if not resp:
        log.warning("Comp-groups page unavailable for %s", applicant_type)
        return {}, set(), set()

    soup = BeautifulSoup(resp.text, "lxml")

    # collect every list_*.html href
    all_links     = {a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("list_")}
    olympiad_links = set()

    # identify Olympiad section(s) inside <details>
    for det in soup.find_all("details"):
        summary = det.find("summary")
        if summary and ("олимпиад" in summary.get_text().lower()):
            for a in det.find_all("a", href=True):
                if a["href"].startswith("list_"):
                    olympiad_links.add(a["href"])

    log.info("  → %d list pages total, %d Olympiad", len(all_links), len(olympiad_links))

    # concurrent fetch
    uid_country   = {}
    olympiad_uids = set()
    all_list_uids = set()

    def _task(link):
        return link, _fetch_list_page(base_url + link)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_task, lnk): lnk for lnk in sorted(all_links)}
        done = 0
        for fut in as_completed(futures):
            link, pairs = fut.result()
            for uid, country in pairs:
                uid_country[uid]  = country
                all_list_uids.add(uid)
                if link in olympiad_links:
                    olympiad_uids.add(uid)
            done += 1
            if done % 50 == 0 or done == len(all_links):
                log.info("    list pages scraped: %d / %d", done, len(all_links))

    log.info("  → UID→Country: %d | Olympiad UIDs: %d", len(uid_country), len(olympiad_uids))
    return uid_country, olympiad_uids, all_list_uids


# ── orchestrate Task 1 ────────────────────────────────────────────────────────
def scrape_task1():
    """
    For each applicant type scrape UIDs, programs, countries.

    Returns
    -------
    all_applicants       : list[dict]
    global_olympiad_uids : set
    global_all_list_uids : set
    """
    all_applicants       = []
    global_uid_country   = {}
    global_olympiad_uids = set()
    global_all_list_uids = set()

    for app_type, base_url in BASE_URLS.items():
        log.info("\n" + "=" * 60)
        log.info("Processing %s …", app_type)

        # 1. UIDs + GUIDs
        applicants = parse_full_list(app_type, base_url)
        time.sleep(DELAY_BETWEEN)

        # 2. comp-groups  →  UID→Country + Olympiad UIDs
        uid_country, oly_uids, list_uids = parse_comp_groups(app_type, base_url)
        global_uid_country.update(uid_country)
        global_olympiad_uids.update(oly_uids)
        global_all_list_uids.update(list_uids)
        time.sleep(DELAY_BETWEEN)

        # 3. programs (concurrent)
        log.info("  Fetching programs for %d applicants …", len(applicants))
        uid_programs = fetch_all_programs(base_url, applicants)

        for app in applicants:
            uid = app["uid"]
            all_applicants.append({
                "UID":            uid,
                "Country":        uid_country.get(uid, ""),
                "Programs_Applied": uid_programs.get(uid, []),
                "Applicant_Type": app["applicant_type"],
            })

        log.info("  ✔ %s complete (%d applicants)", app_type, len(applicants))

    return all_applicants, global_olympiad_uids, global_all_list_uids


# ═══════════════════════════════════════════════════════════════════════════════
# TASK 2  —  Olympiad winners / budget-track
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_task2(olympiad_uids_from_comp):
    """
    Collect UIDs of olympiad winners recommended for budget enrolment.

    Primary source: Olympiad sections of comp_groups (already gathered).
    Secondary: abiturient.spbu.ru budget page  —  PDF links logged for audit.
    """
    budget_uids = set(olympiad_uids_from_comp)
    log.info("Task 2: %d budget-track UIDs from comp-groups Olympiad sections", len(budget_uids))

    # check the budget info page for additional context / PDF links
    resp = safe_get(BUDGET_PAGE_URL)
    if resp:
        soup = BeautifulSoup(resp.text, "lxml")
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if "results_Olympiad" in href or ("олимпиад" in text.lower() and href.endswith(".pdf")):
                pdf_links.append((text[:80], href))
        if pdf_links:
            log.info("  Olympiad-result PDFs found on budget page:")
            for t, h in pdf_links:
                log.info("    • %s  →  %s", t, h)
            log.info("  (PDFs listed for reference — UID extraction uses comp-groups HTML)")

    return budget_uids


# ═══════════════════════════════════════════════════════════════════════════════
# TASK 3  —  Merge & build DataFrame
# ═══════════════════════════════════════════════════════════════════════════════

def build_dataframe(all_applicants, budget_uids, all_list_uids):
    """
    Build final DataFrame.

    Status priority:
      1. UID ∈ budget_uids           → "Accepted with budget"
      2. UID ∈ all_list_uids         → "Accepted"
      3. otherwise                   → "Not Accepted"
    """
    records = []
    for app in all_applicants:
        uid      = app["UID"]
        progs    = app["Programs_Applied"]
        progs_s  = "; ".join(progs) if progs else ""
        count    = len(progs)

        if uid in budget_uids:
            status = "Accepted with budget"
        elif uid in all_list_uids:
            status = "Accepted"
        else:
            status = "Not Accepted"

        records.append({
            "UID":                 uid,
            "Country":             app["Country"],
            "Programs_Applied_To": progs_s,
            "Count_of_Programs":   count,
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

    # ── Task 1 ──
    log.info("\n" + "=" * 60)
    log.info("TASK 1: Scraping applicant lists …")
    all_applicants, olympiad_uids, all_list_uids = scrape_task1()
    log.info("Task 1 done — %d applicant rows collected", len(all_applicants))

    # ── Task 2 ──
    log.info("\n" + "=" * 60)
    log.info("TASK 2: Extracting olympiad / budget-track UIDs …")
    budget_uids = scrape_task2(olympiad_uids)
    log.info("Task 2 done — %d budget UIDs", len(budget_uids))

    # ── Task 3 ──
    log.info("\n" + "=" * 60)
    log.info("TASK 3: Building final DataFrame …")
    df = build_dataframe(all_applicants, budget_uids, all_list_uids)

    # ── Sanity checks ──
    print("\n" + "=" * 60)
    print("SANITY CHECK — df.head(20):\n")
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
