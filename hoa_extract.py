#!/usr/bin/env python3
"""
hoa_extract.py - Drop TMT financial packets / board packages in, get key metrics + red flags out.

Usage:
    python3 hoa_extract.py report1.pdf report2.pdf ...
    python3 hoa_extract.py ~/Downloads/Parkridge_*.pdf

Handles all three formats TMT produces:
  1. Zip packets WITH embedded per-page .txt (mid-2025 onward)        -> read directly (fast, exact)
  2. Zip packets of page JPEGs only (older, often misnamed .pdf)      -> OCR via tesseract
  3. Real text PDFs (portal exports / emailed packets like Nov-25+)   -> pdftotext

Requirements (macOS):  brew install tesseract poppler
Requirements (Linux):  apt install tesseract-ocr poppler-utils

Output: one markdown summary per file + combined summary.csv in ./hoa_extract_out/
"""
import concurrent.futures
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

OUT_DIR = "hoa_extract_out"

# ---------- thresholds for red flags (edit to taste) ----------
EXHIBIT_A_BUNDLE = 98.33      # contracted Office & Technology bundle, $/mo (eff. Jan 2025)
NET_CASH_FLOOR = 2000.00      # alert if Net Available Cash below this
APY_FLOOR = 1.00              # alert if reserve APY below this (%)
# ---------------------------------------------------------------


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def is_zip(path):
    with open(path, "rb") as f:
        return f.read(4) == b"PK\x03\x04"


def ocr_page(args):
    img, txt_base = args
    if not os.path.exists(txt_base + ".txt"):
        subprocess.run(["tesseract", img, txt_base], capture_output=True)
        # tabular fallback improves number capture on statement pages
        r = subprocess.run(["tesseract", img, "stdout", "--psm", "6"],
                           capture_output=True, text=True)
        if r.stdout:
            with open(txt_base + ".txt", "a") as f:
                f.write("\n" + r.stdout)


def extract_text(path, workdir):
    """Return full text of the document, whatever format it is."""
    if is_zip(path):
        pages_dir = os.path.join(workdir, "pages")
        os.makedirs(pages_dir, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            z.extractall(pages_dir)
        # Newer TMT packets (mid-2025 onward) embed a .txt per page inside the
        # zip. Prefer those: exact text, no OCR errors, ~100x faster.
        txts = sorted(
            (f for f in os.listdir(pages_dir) if f.lower().endswith(".txt")),
            key=lambda f: int(re.sub(r"\D", "", f) or 0),
        )
        if txts:
            parts = []
            for f in txts:
                with open(os.path.join(pages_dir, f), errors="replace") as fh:
                    parts.append(fh.read())
            joined = "\n\f\n".join(parts)
            # Some older packets ship EMPTY or near-empty .txt placeholders
            # (e.g. Dec-2024: 31 files totaling ~2 KB of stray characters).
            # Only trust the embedded text if it averages real content per
            # page; otherwise fall through to OCR of the JPEGs.
            if len(joined.strip()) >= max(3000, 100 * len(txts)):
                return joined
        imgs = sorted(
            (f for f in os.listdir(pages_dir) if f.lower().endswith((".jpeg", ".jpg", ".png"))),
            key=lambda f: int(re.sub(r"\D", "", f) or 0),
        )
        jobs = [(os.path.join(pages_dir, f), os.path.join(pages_dir, f + "_ocr")) for f in imgs]
        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
            list(ex.map(ocr_page, jobs))
        parts = []
        for _, base in jobs:
            try:
                with open(base + ".txt") as f:
                    parts.append(f.read())
            except FileNotFoundError:
                pass
        return "\n\f\n".join(parts)
    # real PDF
    r = sh(["pdftotext", "-layout", path, "-"])
    if r.returncode == 0 and len(r.stdout.strip()) > 100:
        return r.stdout
    return ""


MONEY = r"\(?\$?\s?(-?[\d,]+\.\d{2})\)?"


def money(m, paren_negative=True):
    raw, s = m.group(0), m.group(1)
    v = float(s.replace(",", ""))
    if paren_negative and "(" in raw:
        v = -abs(v)
    return v


def find_all_money(pattern, text, flags=re.I):
    return [money(m) for m in re.finditer(pattern, text, flags)]


def extract_metrics(text):
    d = {}

    m = re.search(r"Net Available Cash\s*" + MONEY, text, re.I)
    if m:
        d["net_available_cash"] = money(m)
    else:
        # OCR sometimes separates label and value; skip round chart-axis labels
        m = re.search(r"Net Available Cash[\s\S]{0,300}?\(?\$\s?(-?[\d,]+\.\d{2})\)?", text, re.I)
        if m and not m.group(1).endswith("000.00"):
            d["net_available_cash"] = money(m)

    m = re.search(r"Total Cash Reserve\s*" + MONEY, text, re.I)
    if m:
        d["reserve_balance"] = money(m)

    m = re.search(r"(?:Annual percentage yield(?: earned)?|APY)[\s\S]{0,200}?(\d+\.\d+)\s*%", text, re.I)
    if m:
        d["reserve_apy_pct"] = float(m.group(1))

    m = re.search(r"Interest (?:earned|Credit)\s*" + MONEY, text, re.I)
    if m:
        d["monthly_reserve_interest"] = money(m)

    # TMT bundle / office invoices (several label variants across years)
    vals = find_all_money(
        r"(?:office bundle|Bundle and Office|Office & Tech(?:nology)?)[^\n$]{0,60}" + MONEY, text)
    if vals:
        d["office_bundle_charges"] = sorted(set(round(v, 2) for v in vals if 20 < v < 1500))

    vals = find_all_money(r"(?:52600|Management Contract)[^\n$]{0,40}" + MONEY, text)
    if vals:
        d["management_fee_lines"] = sorted(set(round(v, 2) for v in vals if 500 < v < 5000))

    # AP aging: capture over-30/60/90 exposure per provider line
    ap = []
    ap_block = re.search(r"AP Aging.*?(?=\n\f|\Z)", text, re.S | re.I)
    if ap_block:
        for line in ap_block.group(0).splitlines():
            nums = re.findall(r"(-?[\d,]+\.\d{2})", line)
            if len(nums) >= 5 and not line.strip().lower().startswith("total"):
                cur, o30, o60, o90, tot = [float(n.replace(",", "")) for n in nums[-5:]]
                if o30 + o60 + o90 > 0.005:
                    ap.append({"line": line.strip()[:90],
                               "over30": o30, "over60": o60, "over90": o90})
        m = re.search(r"^Total\s+.*?(-?[\d,]+\.\d{2})\s*$", ap_block.group(0), re.M)
        if m:
            d["ap_total"] = float(m.group(1).replace(",", ""))
            tot_nums = re.findall(r"(-?[\d,]+\.\d{2})", m.group(0))
            if not ap and len(tot_nums) >= 5:
                _, o30, o60, o90, _ = [float(n.replace(",", "")) for n in tot_nums[-5:]]
                if o30 + o60 + o90 > 0.005:
                    ap.append({"line": "AP AGING TOTAL (vendor rows not parsed)",
                               "over30": o30, "over60": o60, "over90": o90})
    if ap:
        d["ap_aged_items"] = ap

    m = re.search(r"Fines and Violations[^\n]*?([\d,]+\.\d{0,2}|\b\d{1,4}\b)\s*$",
                  text, re.M | re.I)
    if m:
        d["fine_income_note"] = m.group(0).strip()[:120]

    return d


def red_flags(d):
    flags = []
    nc = d.get("net_available_cash")
    if nc is not None:
        if nc < 0:
            flags.append(f"NET CASH NEGATIVE: ${nc:,.2f}")
        elif nc < NET_CASH_FLOOR:
            flags.append(f"Net available cash below ${NET_CASH_FLOOR:,.0f} floor: ${nc:,.2f}")
    apy = d.get("reserve_apy_pct")
    if apy is not None and apy < APY_FLOOR:
        flags.append(f"Reserve APY {apy}% (below {APY_FLOOR}% floor)")
    for v in d.get("office_bundle_charges", []):
        if v > EXHIBIT_A_BUNDLE * 1.05:
            flags.append(f"Office bundle ${v:,.2f} exceeds Exhibit A ${EXHIBIT_A_BUNDLE} "
                         f"(+{(v/EXHIBIT_A_BUNDLE-1)*100:.0f}%)")
    for item in d.get("ap_aged_items", []):
        worst = "90+" if item["over90"] else ("60+" if item["over60"] else "30+")
        amt = item["over90"] or item["over60"] or item["over30"]
        flags.append(f"AP aged {worst} days: ${amt:,.2f} — {item['line'][:70]}")
    return flags


def write_report(name, d, flags, out_dir):
    lines = [f"# {name}", ""]
    if flags:
        lines.append("## RED FLAGS")
        lines += [f"- {f}" for f in flags]
        lines.append("")
    lines.append("## Extracted metrics")
    for k, v in d.items():
        if k == "ap_aged_items":
            continue
        lines.append(f"- **{k}**: {v}")
    if not d:
        lines.append("- (nothing recognized — layout may have changed; check manually)")
    path = os.path.join(out_dir, name + ".md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def main(paths):
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    for p in paths:
        if not os.path.exists(p):
            print(f"skip (not found): {p}")
            continue
        name = os.path.splitext(os.path.basename(p))[0]
        print(f"processing {name} ...")
        with tempfile.TemporaryDirectory() as wd:
            text = extract_text(p, wd)
        if not text.strip():
            print("  no text extracted")
            continue
        d = extract_metrics(text)
        flags = red_flags(d)
        rp = write_report(name, d, flags, OUT_DIR)
        print(f"  -> {rp}  ({len(flags)} red flag(s))")
        rows.append({
            "file": name,
            "net_available_cash": d.get("net_available_cash", ""),
            "reserve_balance": d.get("reserve_balance", ""),
            "reserve_apy_pct": d.get("reserve_apy_pct", ""),
            "ap_total": d.get("ap_total", ""),
            "office_bundle_max": max(d.get("office_bundle_charges", [0]) or [0]),
            "red_flags": len(flags),
        })
    if rows:
        with open(os.path.join(OUT_DIR, "summary.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nCombined summary: {OUT_DIR}/summary.csv")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if not shutil.which("tesseract"):
        print("warning: tesseract not found — scanned packets will be skipped "
              "(brew install tesseract)")
    main(sys.argv[1:])
