# screener-downloader

Downloads a company's annual reports and quarterly concall transcripts/PPTs
from [screener.in](https://www.screener.in) into a local folder.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python screener_downloader.py "TCS"
python screener_downloader.py "Reliance Industries"
python screener_downloader.py RELIANCE --standalone
python screener_downloader.py INFY --list-only        # preview links, no downloads
python screener_downloader.py INFY --annual-only
python screener_downloader.py INFY --concalls-only --with-rec
```

Files are saved under `downloads/<TICKER>_<Company_Name>/`:

```
downloads/
  TCS_Tata_Consultancy_Services_Ltd/
    annual_reports/
      Financial_Year_2026.pdf
      Financial_Year_2025.pdf
      ...
    concalls/
      Jul_2026_transcript.pdf
      Jul_2026_ppt.pdf
      ...
```

Re-running the same command skips files already downloaded, so it's safe to
stop and resume.

## Notes

- If the company name matches multiple listings on screener.in (e.g.
  "Reliance"), you'll be shown a numbered list to pick from.
- Some quarters don't have every document (a PPT wasn't always published,
  etc.) — those are just skipped, not an error.
- A handful of documents are hosted directly on the company's own investor-
  relations site rather than on screener.in/BSE. Some of those sites run bot
  protection that can block scripted downloads; if a file fails after 3
  retries, its direct URL is printed at the end so you can open it in a
  browser by hand.
- Use `--out <path>` to change where files are saved (default: `./downloads`).
