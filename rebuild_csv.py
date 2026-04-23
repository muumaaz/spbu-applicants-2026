#!/usr/bin/env python3
"""
Rebuild the CSV with corrected Status based on PDF green-highlighting.

Status logic (corrected):
  1. UID appears in PDF green section         → "Accepted with budget"
  2. UID appears in PDF white section          → "Accepted"
  3. UID appears in comp_groups list pages     → "Accepted" 
     (fallback — shouldn't happen if PDFs have all UIDs)
  4. Otherwise                                 → "Not Accepted"

Step 1: Extract green UIDs from all 3 PDFs
Step 2: Extract all PDF UIDs (green + white)
Step 3: Reload original scrape data and reassign status
Step 4: Export corrected CSV
"""
import fitz
import re
import pandas as pd
import requests
from bs4 import BeautifulSoup
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SPbU-Scraper/1.0)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

uid_pattern = re.compile(r'\b26\d{6}\b')

# ── Step 1 & 2: Extract UIDs from PDFs with green/white classification ────────

def extract_pdf_uids(path):
    """
    Extract UIDs from a PDF, classifying each as green-highlighted or not.
    Returns (green_uids: set, all_pdf_uids: set)
    """
    doc = fitz.open(path)
    green_uids = set()
    all_uids = set()
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        
        # Find green-fill rectangles
        green_rects = []
        for d in page.get_drawings():
            fill = d.get("fill")
            if fill and len(fill) >= 3:
                r, g, b = fill[0], fill[1], fill[2]
                if g > 0.4 and g > r and g > b:
                    rect = d.get("rect")
                    if rect:
                        green_rects.append(fitz.Rect(rect))
        
        # Extract UIDs and check overlap with green rects
        for block in page.get_text("dict")["blocks"]:
            if "lines" not in block:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    for match in uid_pattern.finditer(span["text"]):
                        uid = match.group()
                        all_uids.add(uid)
                        span_rect = fitz.Rect(span["bbox"])
                        for gr in green_rects:
                            if span_rect.intersects(gr):
                                green_uids.add(uid)
                                break
    
    doc.close()
    return green_uids, all_uids


log.info("Extracting UIDs from Olympiad result PDFs...")

budget_uids = set()    # green-highlighted = Accepted with budget
accepted_uids = set()  # in PDF but not green = Accepted (participated)

for path, label in [
    ("/app/results_bak_spec.pdf", "Bachelor+Specialist"),
    ("/app/results_mag.pdf", "Master"),
    ("/app/results_asp.pdf", "PhD/Aspirantura"),
]:
    g, a = extract_pdf_uids(path)
    budget_uids.update(g)
    accepted_uids.update(a)
    log.info(f"  {label}: {len(g)} green (budget), {len(a)} total in PDF")

# accepted_uids currently includes green ones too; separate them
accepted_only = accepted_uids - budget_uids

log.info(f"\nPDF extraction complete:")
log.info(f"  Accepted with budget (green): {len(budget_uids)}")
log.info(f"  Accepted (in PDF, not green): {len(accepted_only)}")
log.info(f"  Total in PDFs: {len(accepted_uids)}")


# ── Step 3: Reload original scrape data and reassign status ────────────────────

log.info("\nRe-scraping applicant data...")

BASE_URLS = {
    "Bachelor":   "https://cabinet.spbu.ru/Lists/ForeignersLists/bak/",
    "Specialist": "https://cabinet.spbu.ru/Lists/ForeignersLists/spec/",
    "Master":     "https://cabinet.spbu.ru/Lists/ForeignersLists/mag/",
    "PhD":        "https://cabinet.spbu.ru/Lists/ForeignersLists/asp/",
}

_PROG_RE = re.compile(
    r"Образовательная программа / Education program:\s*(.+?);\s*Форма обучения",
    re.DOTALL
)

def safe_get(url, retries=3, timeout=20):
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=timeout)
            resp.encoding = "utf-8"
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                return None
        except Exception:
            pass
        time.sleep(1)
    return None

def parse_full_list(base_url):
    resp = safe_get(base_url + "index_full_list.html")
    if not resp: return []
    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table")
    if not table: return []
    rows = table.find_all("tr")[1:]
    applicants = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2: continue
        uid = cells[0].get_text(strip=True)
        guid = cells[1].get("id", "")
        if uid and guid:
            applicants.append({"uid": uid, "guid": guid})
    return applicants

def fetch_programs(base_url, guid):
    resp = safe_get(base_url + f"data/{guid}.txt")
    if not resp: return []
    soup = BeautifulSoup(resp.text, "lxml")
    programs = []
    for p in soup.find_all("p"):
        text = p.get_text(separator=" ", strip=True)
        m = _PROG_RE.search(text)
        if m:
            programs.append(" ".join(m.group(1).split()))
    return programs

def get_uid_country(base_url):
    resp = safe_get(base_url + "index_comp_groups.html")
    if not resp: return {}
    soup = BeautifulSoup(resp.text, "lxml")
    links = {a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("list_")}
    uid_country = {}
    
    def _task(link):
        r = safe_get(base_url + link)
        if not r: return []
        s = BeautifulSoup(r.text, "lxml")
        t = s.find("table")
        if not t: return []
        pairs = []
        for row in t.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) >= 2:
                uid = cells[0].get_text(strip=True)
                country = cells[1].get_text(strip=True)
                if uid: pairs.append((uid, country))
        return pairs
    
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_task, lnk): lnk for lnk in links}
        for fut in as_completed(futures):
            for uid, country in fut.result():
                uid_country[uid] = country
    
    return uid_country

# Scrape all four types
all_records = []

for app_type, base_url in BASE_URLS.items():
    log.info(f"Processing {app_type}...")
    
    applicants = parse_full_list(base_url)
    uid_country = get_uid_country(base_url)
    
    log.info(f"  {len(applicants)} applicants, {len(uid_country)} country mappings")
    log.info(f"  Fetching programs...")
    
    def _task(app):
        return app["uid"], fetch_programs(base_url, app["guid"])
    
    uid_programs = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_task, a): a for a in applicants}
        done = 0
        for fut in as_completed(futures):
            uid, progs = fut.result()
            uid_programs[uid] = progs
            done += 1
            if done % 300 == 0:
                log.info(f"    {done}/{len(applicants)} programs fetched")
    
    for app in applicants:
        uid = app["uid"]
        progs = uid_programs.get(uid, [])
        
        # Status logic — corrected with PDF data
        if uid in budget_uids:
            status = "Accepted with budget"
        elif uid in accepted_only:
            status = "Accepted"
        else:
            status = "Not Accepted"
        
        all_records.append({
            "UID": uid,
            "Country": uid_country.get(uid, ""),
            "Programs_Applied_To": "; ".join(progs) if progs else "",
            "Count_of_Programs": len(progs),
            "Status": status,
            "Applicant": app_type,
        })
    
    log.info(f"  ✔ {app_type} done")
    time.sleep(1.5)

# ── Step 4: Build DataFrame and export ─────────────────────────────────────────

df = pd.DataFrame(all_records)
df = df.sort_values("UID", ascending=True).reset_index(drop=True)

print("\n" + "=" * 60)
print("SANITY CHECK — df.head(20):\n")
pd.set_option('display.max_colwidth', 80)
pd.set_option('display.width', 200)
print(df.head(20).to_string(index=False))

print("\n" + "=" * 60)
print("Status distribution:\n")
print(df["Status"].value_counts().to_string())
print(f"\nTotal rows:          {len(df)}")
print(f"Unique UIDs:         {df['UID'].nunique()}")
print(f"\nApplicant type breakdown:\n{df['Applicant'].value_counts().to_string()}")
print(f"\nCountries represented: {df['Country'].nunique()}")

out = "spbu_applicants_2026.csv"
df.to_csv(out, index=False, encoding="utf-8")
log.info(f"\n✅ Exported corrected CSV to {out} ({len(df)} rows)")
print(f"\n✅ Exported to {out} — {len(df)} rows")
