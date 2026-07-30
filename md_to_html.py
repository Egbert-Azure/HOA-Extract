import os, sys, html, glob, re
outdir = sys.argv[1]
CSS = """
body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:820px;margin:32px auto;
padding:0 20px;color:#1a1a1a;line-height:1.5}
h1{color:#1a3a5c;border-bottom:3px solid #1a3a5c;padding-bottom:6px}
h2{color:#1a3a5c;margin-top:28px;border-bottom:1px solid #ccc;padding-bottom:4px}
.redflags{background:#fdecea;border:1px solid #e0a0a0;border-radius:6px;padding:4px 18px;margin:12px 0}
.redflags li{color:#9b1c1c;font-weight:600}
code{background:#f2f2f2;padding:1px 5px;border-radius:3px;font-size:90%}
ul{margin:6px 0}
li{margin:3px 0}
.sub li{color:#444;font-weight:400;font-size:93%}
a.home{display:inline-block;margin-bottom:14px;color:#1a3a5c;text-decoration:none;font-size:90%}
"""
def md_line(line):
    line = html.escape(line)
    line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
    line = re.sub(r'`(.+?)`', r'<code>\1</code>', line)
    return line
def convert(mdpath):
    with open(mdpath) as f: lines=f.read().split("\n")
    body,in_list,in_red=[],False,False
    def close_list():
        nonlocal in_list
        if in_list: body.append("</ul>"); in_list=False
    def close_red():
        nonlocal in_red
        if in_red: body.append("</div>"); in_red=False
    for ln in lines:
        raw=ln.rstrip("\n")
        if raw.startswith("# "):
            close_list(); close_red(); body.append(f"<h1>{md_line(raw[2:])}</h1>")
        elif raw.startswith("## "):
            close_list(); close_red(); title=raw[3:]
            body.append(f"<h2>{md_line(title)}</h2>")
            if "RED FLAG" in title.upper():
                body.append('<div class="redflags"><ul>'); in_list=True; in_red=True
        elif raw.lstrip().startswith("- "):
            indent=raw.startswith("    ") or raw.startswith("\t"); content=raw.lstrip()[2:]
            if not in_list: body.append('<ul class="sub">' if indent else "<ul>"); in_list=True
            body.append(f"<li>{md_line(content)}</li>")
        elif raw.strip()=="":
            if in_list and not in_red: close_list()
            continue
        else:
            close_list(); close_red(); body.append(f"<p>{md_line(raw)}</p>")
    close_list(); close_red()
    name=os.path.splitext(os.path.basename(mdpath))[0]
    with open(os.path.join(outdir,name+".html"),"w") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(name)}</title>"
                f"<style>{CSS}</style></head><body>"
                f"<a class='home' href='index.html'>&#8592; All reports</a>{''.join(body)}</body></html>")
    return name
names=[convert(m) for m in sorted(glob.glob(os.path.join(outdir,"*.md")))]
links="".join(f"<li><a href='{html.escape(n)}.html'>{html.escape(n)}</a></li>" for n in names)
with open(os.path.join(outdir,"index.html"),"w") as f:
    f.write(f"<!doctype html><html><head><meta charset='utf-8'><title>Parkridge Reports</title>"
            f"<style>{CSS}</style></head><body><h1>Parkridge Reports</h1>"
            f"<p>One report per packet. The spreadsheet <code>summary.csv</code> "
            f"(in this same folder) opens in Excel.</p><ul>{links}</ul></body></html>")
print(f"  Built {len(names)} readable report page(s).")
