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
IRRIGATION_OVER_FLOOR = 25.0  # alert if irrigation acct YTD runs over budget by more than this (%)
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

    # Utility-payee cash-out tracing (PSE electricity, City-of-Redmond water/stormwater),
    # including the GL account each draft posts to. Flags metered water (billing-period
    # descriptions) landing in an irrigation account (59200 pre-Jul-2025, 59030 after)
    # rather than an account named "Water".
    up = extract_utility_payees(text)
    if up:
        d["utility_payees"] = up
        ytd_act, ytd_bud = extract_irrigation_budget(text)
        if ytd_bud and ytd_bud > 0:
            d["irrigation_ytd_actual"] = ytd_act
            d["irrigation_ytd_budget"] = ytd_bud
            d["irrigation_pct_over_ytd"] = round((ytd_act / ytd_bud - 1) * 100, 0)

    return d


# ---- Payee-level utility drafts WITH the GL account each posts to ----
UTIL_PAYEES = {
    "pse_electric": r"Puget\s+Sound\s+Energy",
    "redmond_utility": r"City\s+of\s+Redmond\s*-?\s*Utility\s+Billing",
}
IRRIGATION_GL = ("59030", "59200")   # accounts where Redmond metered water lands
DATE_RANGE = r"\d{1,2}[./]\d{1,2}"    # a billing period in the desc => consumption, not repair


def extract_utility_payees(text):
    """For each utility-payee draft, capture the amount AND the GL account it posts to
    (the GL line typically follows the draft line in the check register / GL detail).
    Redmond drafts with a billing-period description that land in an irrigation account
    are flagged water_in_irrigation. Returns {payee: {count, total,
    water_misfiled_total, lines:[{amount, gl, water_in_irrigation}]}}."""
    out = {}
    lines = [l.rstrip() for l in text.splitlines()]
    for key, payee in UTIL_PAYEES.items():
        hits = []
        for i, l in enumerate(lines):
            if re.search(payee, l, re.I) and re.search(r"Inv\s*#", l, re.I):
                amts = re.findall(r"(-?[\d,]+\.\d{2})", l)
                if not amts:
                    continue
                amt = abs(float(amts[-1].replace(",", "")))
                gl_num, gl_desc = "?", "(GL not found)"
                for j in range(i + 1, min(i + 4, len(lines))):
                    m = re.search(r"(\d{5})\s*-\s*([^\n\r]+)", lines[j])
                    if m:
                        gl_num, gl_desc = m.group(1), m.group(2).strip()[:80]
                        break
                water_in_irrig = (
                    key == "redmond_utility"
                    and gl_num in IRRIGATION_GL
                    and re.search(DATE_RANGE, gl_desc)
                    and "storm" not in gl_desc.lower()
                )
                hits.append({"amount": amt, "gl": f"{gl_num} - {gl_desc}",
                             "water_in_irrigation": water_in_irrig})
        if hits:
            # de-dupe identical (amount, gl) pairs that appear on both register & GL pages
            seen, uniq = set(), []
            for h in hits:
                sig = (h["amount"], h["gl"])
                if sig not in seen:
                    seen.add(sig)
                    uniq.append(h)
            out[key] = {
                "count": len(uniq),
                "total": round(sum(h["amount"] for h in uniq), 2),
                "water_misfiled_total": round(
                    sum(h["amount"] for h in uniq if h["water_in_irrigation"]), 2),
                "lines": uniq,
            }
    return out


# Irrigation-account budget-vs-actual row (label wraps across lines, then 7 figures:
# Actual Budget Var YTD-Actual YTD-Budget YTD-Var Annual-Budget). 59200 pre-rename, 59030 after.
def extract_irrigation_budget(text):
    """Return (ytd_actual, ytd_budget) for the irrigation acct, or (None, None).
    YTD-vs-YTD is the honest mid-year comparison; a full-year over-budget figure only
    lands correctly in the December packet (where YTD == full year).

    Robust form: the account number can be followed by anything (label may wrap,
    say 'Repairs & Maintenance', or be truncated by OCR) before the seven money
    figures. We scan ALL occurrences and keep the one that is actually a budget
    row (exactly seven money figures in a run), ignoring check-register lines that
    have the same label but only one trailing number."""
    money = r"\(?(-?[\d,]*\.\d{2})\)?"   # [\d,]* (not +) so leading-decimal like ".11" matches
    # 59030/59200, then up to ~40 non-digit chars (the label, possibly wrapped),
    # then seven money figures separated by whitespace.
    pat = (r"59(?:030|200)\b[^\d\n]{0,40}?[A-Za-z][^\d]{0,40}?"
           + money + (r"[ \t\r\n]+" + money) * 6)
    best = None
    for m in re.finditer(pat, text, re.I):
        nums = [float(g.replace(",", "")) for g in m.groups()]
        # sanity: annual budget (last) should be the largest-ish; YTD budget > 0
        if nums[4] > 0:
            best = nums
            break
    if not best:
        return None, None
    return best[3], best[4]   # YTD actual, YTD budget


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
    red = d.get("utility_payees", {}).get("redmond_utility")
    if red and red.get("water_misfiled_total", 0) > 0:
        pct = d.get("irrigation_pct_over_ytd")
        if pct is not None and pct > IRRIGATION_OVER_FLOOR:
            ytd = d["irrigation_ytd_actual"]
            bud = d["irrigation_ytd_budget"]
            flags.append(
                f"Metered water ${red['water_misfiled_total']:,.2f} in irrigation acct; "
                f"acct YTD ${ytd:,.2f} vs ${bud:,.2f} budget ({pct:+.0f}% YTD)")
        elif pct is None:
            flags.append(f"Metered water ${red['water_misfiled_total']:,.2f} posted to an "
                         f"irrigation acct (budget line not parsed — check manually)")
    return flags


def write_report(name, d, flags, out_dir):
    lines = [f"# {name}", ""]
    if flags:
        lines.append("## RED FLAGS")
        lines += [f"- {f}" for f in flags]
        lines.append("")
    lines.append("## Extracted metrics")
    for k, v in d.items():
        if k in ("ap_aged_items", "utility_payees"):
            continue
        lines.append(f"- **{k}**: {v}")

    up = d.get("utility_payees")
    if up:
        lines.append("")
        lines.append("## Utility drafts")
        for payee, info in up.items():
            label = {"pse_electric": "PSE (Electricity)",
                     "redmond_utility": "City of Redmond (Water/Stormwater)"}.get(payee, payee)
            header = f"- **{label}** — {info['count']} drafts, ${info['total']:,.2f} total"
            if info.get("water_misfiled_total", 0) > 0:
                header += f"  (water in irrigation acct: ${info['water_misfiled_total']:,.2f})"
            lines.append(header)
            for h in info["lines"]:
                mark = "  [WATER]" if h["water_in_irrigation"] else ""
                gl = h["gl"].strip()
                lines.append(f"    - ${h['amount']:,.2f} -> {gl}{mark}")

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
            "water_misfiled": d.get("utility_payees", {}).get(
                "redmond_utility", {}).get("water_misfiled_total", ""),
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