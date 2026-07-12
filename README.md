# HOA-Extract
# hoa_extract.py — Parkridge financial packet auditor

Drop management-company financial packets in, get key metrics and red flags out.
Built for TMT's formats, but the point of it going forward is **monthly auditing of the
new firm**: run it on every packet the day it arrives, and the red-flag section tells you
in seconds whether anything needs board attention.

## What it does

For each file it extracts:

| Metric | Source label in packet |
|---|---|
| Net Available Cash | Overview p.1 "Net Available Cash" |
| Reserve balance | Balance sheet "Total Cash Reserve" (book figure — includes any inter-fund Due To/From, so it can differ from the bank statement by e.g. the $1,442.38 skipped-December IOU) |
| Reserve APY | Bank statement "Annual percentage yield earned" |
| Monthly reserve interest | "Interest earned / Interest Credit" |
| Office bundle charges | TMT "Bundle and Office" invoice lines (heuristic — verify hits manually) |
| Management fee lines | GL 52600 lines |
| Aged payables | AP Aging rows with anything in the over-30/60/90 columns |

Then it flags: **negative net cash**, net cash below the $2,000 floor, reserve APY below
1%, bundle charges more than 5% over the Exhibit A $98.33, and any aged AP. The
thresholds are the four constants at the top of the script — edit them there when you
sign the new contract (new bundle rate, new cash floor, etc.).

## Installation (one time)

The script is plain Python 3, no pip packages. It needs two command-line tools only for
*scanned* packets; text PDFs work with `poppler` alone.

- **macOS:** `brew install tesseract poppler`
- **Windows:** easiest is WSL/Ubuntu, then the Linux line below. (Native Windows works
  too if you install Tesseract and Poppler and add them to PATH, but WSL is less fuss.)
- **Linux:** `sudo apt install tesseract-ocr poppler-utils`

## Usage

```bash
python3 hoa_extract.py Financials_Approved_jan_2026.pdf
python3 hoa_extract.py ~/Downloads/Parkridge_*.pdf          # whole folder at once
```

Output goes to `./hoa_extract_out/`:
- one `<filename>.md` per packet — RED FLAGS section first, then the extracted metrics
- `summary.csv` — one row per packet, opens in Excel; sort by month to build trend lines

## The three packet formats (handled automatically)

1. **Zip-of-JPEGs misnamed `.pdf`** (older TMT packets, e.g. Dec 2024) → OCR via
   tesseract, parallelized. Slowest path, ~1–2 min per packet.
2. **Zip with embedded per-page `.txt`** (mid-2025 onward) → reads the exact text
   directly. Fast and error-free. If the embedded text is a near-empty placeholder
   (Dec-2024 does this), it falls back to OCR automatically.
3. **Real text PDFs** (portal exports; the Nov-25/Dec-25/Jan-26 packets) → `pdftotext`.

You never need to tell it which is which — it sniffs the file header.

## Known limitations (as of 2026-07-11)

- `office_bundle_charges` is a text heuristic; it catches most bundle invoice lines but
  can miss ones with unusual wording or pick up a stray number — treat hits as pointers
  to verify, not as gospel.
- On OCR'd (oldest) packets the `ap_total` field can grab the wrong "Total" line; the
  per-vendor aged-AP flags are reliable, the total is not.
- "Reserve balance" is the ledger figure. Reconcile against the bank statement page when
  it matters — a mismatch is itself a finding (see December 2025).
- If a future firm uses different report labels, extraction returns "(nothing
  recognized)" — that's the cue to update the regexes in `extract_metrics()`, not a
  crash.

## Regression baseline (verified 2026-07-11)

| Packet | net cash | reserve (book) |
|---|---|---|
| Dec-2024 (OCR) |
| Sep-2025 (embedded txt) |
| Nov-2025 (text PDF) | 
| Dec-2025 (text PDF) |
| Jan-2026 (text PDF) | 
