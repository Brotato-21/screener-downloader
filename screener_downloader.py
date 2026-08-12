#!/usr/bin/env python3
"""
screener_downloader.py

Download a company's annual reports and quarterly concall transcripts/PPTs
(and, optionally, earnings-call recordings) from screener.in.

Usage:
    python screener_downloader.py "TCS"
    python screener_downloader.py "Reliance Industries" --out ./downloads
    python screener_downloader.py RELIANCE --standalone
    python screener_downloader.py INFY --list-only          # preview, no downloads
    python screener_downloader.py INFY --annual-only
    python screener_downloader.py INFY --concalls-only --with-rec

Requires: requests, beautifulsoup4, lxml
    pip install -r requirements.txt
"""

import argparse
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

SEARCH_API = "https://www.screener.in/api/company/search/?q={query}"
BASE_URL = "https://www.screener.in"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

EXT_BY_CONTENT_TYPE = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "audio/mpeg": ".mp3",
    "video/mp4": ".mp4",
    "text/html": ".html",  # usually means we hit an error/landing page, not the real file
}


def slugify(text: str) -> str:
    """Turn arbitrary text into a safe filename/folder fragment."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s\-]", "", text).strip()
    return re.sub(r"\s+", "_", text) or "unnamed"


def search_company(session: requests.Session, query: str):
    resp = session.get(SEARCH_API.format(query=query), timeout=15)
    resp.raise_for_status()
    return resp.json()


def pick_company(matches, query):
    if not matches:
        print(f"No company found on screener.in matching '{query}'.")
        sys.exit(1)

    def ticker_of(m):
        # url looks like "/company/<TICKER>/consolidated/" or "/company/<TICKER>/"
        parts = m["url"].strip("/").split("/")
        return parts[1] if len(parts) > 1 else parts[0]

    if len(matches) == 1:
        return matches[0]

    # Short-circuit on an exact ticker or name match so common cases need no prompt.
    q = query.strip().lower()
    for m in matches:
        if q == m["name"].lower() or q == ticker_of(m).lower():
            return m

    print(f"Multiple companies match '{query}':")
    for i, m in enumerate(matches, 1):
        print(f"  {i}. {m['name']} ({ticker_of(m)})")
    while True:
        choice = input(f"Pick 1-{len(matches)}: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            return matches[int(choice) - 1]
        print("Invalid choice, try again.")


def fetch_company_page(session: requests.Session, company: dict, standalone: bool):
    slug = company["url"].strip("/").split("/")[1]
    url = f"{BASE_URL}/company/{slug}/" + ("" if standalone else "consolidated/")
    resp = session.get(url, timeout=20)
    if resp.status_code == 404 and not standalone:
        # Some companies (banks, NBFCs, etc.) only publish standalone financials.
        url = f"{BASE_URL}/company/{slug}/"
        resp = session.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text, url


def parse_annual_reports(soup: BeautifulSoup, page_url: str):
    reports = []
    section = soup.select_one("div.documents.annual-reports")
    if not section:
        return reports
    for li in section.select("ul.list-links li"):
        a = li.find("a", href=True)
        if not a:
            continue
        label = a.get_text(" ", strip=True)
        label = re.sub(r"\s*from\s+\w+\s*$", "", label, flags=re.I).strip()
        reports.append({"label": label, "url": urljoin(page_url, a["href"])})
    return reports


def parse_concalls(soup: BeautifulSoup, page_url: str):
    concalls = []
    section = soup.select_one("div.documents.concalls")
    if not section:
        return concalls
    for li in section.select("ul.list-links li"):
        date_div = li.find("div")
        period = date_div.get_text(strip=True) if date_div else "Unknown"
        entry = {"period": period, "transcript": None, "ppt": None, "rec": None}
        for a in li.find_all("a", href=True):
            title = a.get_text(strip=True)
            if title == "Transcript":
                entry["transcript"] = urljoin(page_url, a["href"])
            elif title == "PPT":
                entry["ppt"] = urljoin(page_url, a["href"])
            elif title == "REC":
                entry["rec"] = urljoin(page_url, a["href"])
        concalls.append(entry)
    return concalls


NON_FILE_EXTENSIONS = {".aspx", ".ashx", ".php", ".jsp", ".asp", ".cgi", ".html", ".htm"}


def guess_extension(url: str, content_type: str) -> str:
    # Prefer Content-Type when the URL's own extension looks like a dynamic
    # web-app endpoint rather than a real file (e.g. BSE's AnnPdfOpen.aspx).
    content_ext = EXT_BY_CONTENT_TYPE.get(content_type.split(";")[0].strip(), "")
    path = urlparse(url).path
    url_ext = Path(unquote(path)).suffix
    if url_ext and len(url_ext) <= 5 and url_ext.lower() not in NON_FILE_EXTENSIONS:
        return url_ext
    return content_ext


def already_downloaded(dest_dir: Path, base_name: str) -> Path | None:
    if not dest_dir.exists():
        return None
    for f in dest_dir.glob(f"{base_name}.*"):
        if f.is_file() and f.stat().st_size > 0:
            return f
    return None


def download(session: requests.Session, url: str, dest_dir: Path, base_name: str, log) -> bool:
    existing = already_downloaded(dest_dir, base_name)
    if existing:
        log(f"    skip (already have {existing.name})")
        return True

    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_dir / f"{base_name}.part"

    for attempt in range(1, 4):
        try:
            with session.get(url, timeout=30, stream=True) as r:
                r.raise_for_status()
                # Use the final (post-redirect) URL: links like BSE's AnnPdfOpen.aspx
                # redirect to the real .pdf and the original URL's extension is misleading.
                ext = guess_extension(r.url, r.headers.get("Content-Type", "")) or ".bin"
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                final_path = dest_dir / f"{base_name}{ext}"
                tmp_path.replace(final_path)
            log(f"    saved {final_path.name} ({final_path.stat().st_size:,} bytes)")
            return True
        except requests.RequestException as e:
            log(f"    attempt {attempt}/3 failed: {e}")
            time.sleep(1.5 * attempt)

    tmp_path.unlink(missing_ok=True)
    log(f"    FAILED: {url}")
    return False


def dedupe_key(seen: dict, key: str) -> str:
    seen[key] = seen.get(key, 0) + 1
    return key if seen[key] == 1 else f"{key}_{seen[key]}"


def main():
    parser = argparse.ArgumentParser(
        description="Download annual reports and quarterly concall transcripts/PPTs from screener.in"
    )
    parser.add_argument("company", help="Company name or ticker, e.g. 'TCS' or 'Reliance Industries'")
    parser.add_argument("--out", default="downloads", help="Output directory (default: ./downloads)")
    parser.add_argument("--standalone", action="store_true", help="Use standalone financials page instead of consolidated")
    parser.add_argument("--with-rec", action="store_true", help="Also download earnings-call audio/video recordings")
    parser.add_argument("--annual-only", action="store_true", help="Only download annual reports")
    parser.add_argument("--concalls-only", action="store_true", help="Only download concall transcripts/PPTs")
    parser.add_argument("--list-only", action="store_true", help="List what would be downloaded, without downloading")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds to wait between downloads (politeness delay)")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    print(f"Searching screener.in for '{args.company}'...")
    matches = search_company(session, args.company)
    company = pick_company(matches, args.company)
    print(f"Using: {company['name']}\n")

    html, page_url = fetch_company_page(session, company, standalone=args.standalone)
    soup = BeautifulSoup(html, "lxml")
    print(f"Fetched: {page_url}")

    ticker = company["url"].strip("/").split("/")[1]
    company_dir = Path(args.out) / f"{ticker}_{slugify(company['name'])}"
    failures = []

    if not args.concalls_only:
        reports = parse_annual_reports(soup, page_url)
        print(f"\nAnnual reports found: {len(reports)}")
        ar_dir = company_dir / "annual_reports"
        for r in reports:
            print(f"- {r['label']}")
            if args.list_only:
                print(f"    {r['url']}")
                continue
            base = slugify(r["label"])
            if not download(session, r["url"], ar_dir, base, print):
                failures.append((r["label"], r["url"]))
            time.sleep(args.delay)

    if not args.annual_only:
        concalls = parse_concalls(soup, page_url)
        print(f"\nConcalls found: {len(concalls)}")
        cc_dir = company_dir / "concalls"
        seen = {}
        for c in concalls:
            print(f"- {c['period']}"
                  + ("" if c["transcript"] else " (no transcript)")
                  + ("" if c["ppt"] else " (no ppt)"))
            base_period = dedupe_key(seen, slugify(c["period"]))

            if args.list_only:
                if c["transcript"]:
                    print(f"    transcript: {c['transcript']}")
                if c["ppt"]:
                    print(f"    ppt: {c['ppt']}")
                if c["rec"] and args.with_rec:
                    print(f"    rec: {c['rec']}")
                continue

            if c["transcript"]:
                if not download(session, c["transcript"], cc_dir, f"{base_period}_transcript", print):
                    failures.append((f"{c['period']} transcript", c["transcript"]))
                time.sleep(args.delay)
            if c["ppt"]:
                if not download(session, c["ppt"], cc_dir, f"{base_period}_ppt", print):
                    failures.append((f"{c['period']} ppt", c["ppt"]))
                time.sleep(args.delay)
            if c["rec"] and args.with_rec:
                if not download(session, c["rec"], cc_dir, f"{base_period}_rec", print):
                    failures.append((f"{c['period']} rec", c["rec"]))
                time.sleep(args.delay)

    if args.list_only:
        print("\n(--list-only: nothing was downloaded)")
        return

    print(f"\nDone. Files saved under: {company_dir.resolve()}")
    if failures:
        print(f"\n{len(failures)} file(s) failed to download automatically (open these manually if needed):")
        for label, url in failures:
            print(f"  - {label}: {url}")


if __name__ == "__main__":
    main()
