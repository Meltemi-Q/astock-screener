#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把第4层定性研判(cache/verdicts/*.json)与量化结果(CSV)合并，产出定性修正后的最终榜单。"""
import os, csv, json, glob, re
from datetime import datetime


def tolerant_load(path):
    """先严格 JSON；失败则按字段正则抢救（容忍 LLM 写出的未转义内层引号/全角逗号）。"""
    raw = open(path, encoding="utf-8").read()
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        pass
    d = {}
    for k in ("idx", "quant_score", "moat_score", "business_model_score",
              "management_score", "industry_score", "qual_score"):
        m = re.search(r'"%s"\s*:\s*(-?\d+(?:\.\d+)?)' % k, raw)
        if m:
            d[k] = float(m.group(1))
    for k in ("code", "name", "industry", "tier", "moat_type", "industry_outlook",
              "competence", "value_trap_risk", "final_verdict", "confidence"):
        m = re.search(r'"%s"\s*:\s*"(.*?)"\s*[,，\n}]' % k, raw)
        if m:
            d[k] = m.group(1)
    # 长文本字段可能含内层引号：贪婪取到 "值",\n  "下一个key 的锚点
    for k in ("thesis", "value_trap_note"):
        m = re.search(r'"%s"\s*:\s*"(.+?)"\s*[,，]?\s*\n\s*["}]' % k, raw, re.S)
        if m:
            d[k] = m.group(1).replace('\n', ' ')
    return d

WORK = os.path.dirname(os.path.abspath(__file__))
TS = datetime.now().strftime("%Y%m%d")

L4_HTML = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股第4层定性修正终榜</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1115;color:#e6e8eb;font-size:13px;line-height:1.55}
.summary{max-width:960px;margin:0 auto;padding:24px 22px}
.summary h1{font-size:20px}.summary h2{font-size:17px;margin-top:22px;border-bottom:1px solid #232936;padding-bottom:6px}
.summary h3{font-size:15px;margin-top:18px;color:#c7cdd6}
.summary b{color:#fff}.summary blockquote{color:#8b93a1;border-left:3px solid #2a3140;margin:8px 0;padding:2px 12px;font-size:12px}
.summary ul{margin:6px 0;padding-left:22px}.summary li{margin:3px 0}
.summary p{margin:6px 0}
.tbar{position:sticky;top:0;background:#12151c;border-top:1px solid #232936;border-bottom:1px solid #232936;padding:10px 20px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;z-index:10}
input{background:#1a1f29;border:1px solid #2a3140;color:#e6e8eb;border-radius:7px;padding:7px 10px;font-size:13px;outline:none;width:150px}
.btn{cursor:pointer;background:#1a1f29;border:1px solid #2a3140;color:#c7cdd6;border-radius:7px;padding:7px 12px;font-size:12px}
.btn.on{background:#3a86ff;border-color:#3a86ff;color:#fff}
.cnt{color:#8b93a1;font-size:12px;margin-left:auto}
.wrap{overflow:auto}
table{border-collapse:collapse;width:100%;white-space:nowrap}
th{position:sticky;top:0;background:#1a1f29;color:#c7cdd6;padding:8px 10px;text-align:right;cursor:pointer;border-bottom:2px solid #2a3140;font-size:12px}
th.l,td.l{text-align:left}td{padding:6px 10px;text-align:right;border-bottom:1px solid #1c212b}
tr:hover td{background:#161b24}
.v买入候选{color:#3ddc84;font-weight:700}.v值得观察{color:#7fb3ff}.v规避{color:#ff6b6b;font-weight:700}.v数据不足{color:#9aa4b2}.v未评{color:#5a6270}
.th{text-align:left;white-space:normal;max-width:420px;color:#aeb6c2;font-size:12px}
.pos{color:#3ddc84}.neg{color:#ff6b6b}.code{color:#7fb3ff}
</style></head><body>
<div class="summary">__SUMMARY__<div style="color:#5a6270;font-size:11px;margin-top:20px">生成于 __TS__ · 数据东方财富 · 定性基于模型知识库非实时联网</div></div>
<div class="tbar">
<input id="q" placeholder="搜索代码/名称…">
<button class="btn on" data-v="all">全部</button>
<button class="btn" data-v="买入候选">🟢买入候选</button>
<button class="btn" data-v="值得观察">🔵值得观察</button>
<button class="btn" data-v="规避">🔴规避</button>
<button class="btn" data-v="数据不足">⚪数据不足</button>
<span class="cnt" id="cnt"></span>
</div>
<div class="wrap"><table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table></div>
<script>
var ROWS=__ROWS__;
var COLS=[["rk","#","n"],["code","代码","s"],["name","名称","s"],["ind","行业","s"],["vd","定性结论","s"],
["bl","综合","n"],["qt","量化","n"],["ql","定性","n"],["moat","护城河","n"],["mtype","护城河类型","s"],
["out","行业","s"],["trap","陷阱","s"],["conf","信心","s"],["pe","PE","n"],["disc","折让%","n"],["th","一句话逻辑","s"]];
var st={v:"all",q:"",sk:"bl",sd:-1};
function fmt(x){return x===null||x===undefined||x===""?"":x}
function head(){document.getElementById("head").innerHTML=COLS.map(function(c){
 var ar=st.sk===c[0]?(st.sd<0?" ▼":" ▲"):"";var cl=c[2]==="s"?"l":"";
 return '<th class="'+cl+'" data-k="'+c[0]+'">'+c[1]+ar+'</th>'}).join("")}
function rowHTML(r){return '<tr>'+COLS.map(function(c){var k=c[0],v=r[k];
 if(k==="vd")return '<td class="l v'+v+'">'+v+'</td>';
 if(k==="code")return '<td class="l code">'+v+'</td>';
 if(k==="name"||k==="mtype"||k==="ind"||k==="out"||k==="trap"||k==="conf")return '<td class="l">'+fmt(v)+'</td>';
 if(k==="th")return '<td class="th">'+fmt(v)+'</td>';
 if(k==="disc"){var cl=v>0?"pos":(v<0?"neg":"");return '<td class="'+cl+'">'+fmt(v)+'</td>'}
 return '<td>'+fmt(v)+'</td>'}).join("")+'</tr>'}
function render(){
 var q=st.q.trim().toLowerCase();
 var rs=ROWS.filter(function(r){
  if(st.v!=="all"&&r.vd!==st.v)return false;
  if(q&&String(r.code).indexOf(q)<0&&r.name.toLowerCase().indexOf(q)<0)return false;
  return true});
 var typ=(COLS.find(function(c){return c[0]===st.sk})||[])[2];
 rs.sort(function(a,b){var x=a[st.sk],y=b[st.sk];
  if(x===null||x===undefined||x==="")x=typ==="n"?-1e18:"";if(y===null||y===undefined||y==="")y=typ==="n"?-1e18:"";
  if(typ==="n")return (x-y)*st.sd;return (x<y?-1:x>y?1:0)*st.sd});
 document.getElementById("body").innerHTML=rs.map(rowHTML).join("");
 document.getElementById("cnt").textContent="显示 "+rs.length+" / "+ROWS.length+" 只"}
document.getElementById("head").addEventListener("click",function(e){var k=e.target.getAttribute("data-k");if(!k)return;
 if(st.sk===k)st.sd=-st.sd;else{st.sk=k;st.sd=(COLS.find(function(c){return c[0]===k})[2]==="n")?-1:1}head();render()});
document.querySelectorAll(".btn").forEach(function(b){b.addEventListener("click",function(){
 document.querySelectorAll(".btn").forEach(function(x){x.classList.remove("on")});b.classList.add("on");st.v=b.getAttribute("data-v");render()})});
document.getElementById("q").addEventListener("input",function(e){st.q=e.target.value;render()});
head();render();
</script></body></html>"""

# 1) 量化结果（最新一份 CSV）
csvs = sorted(glob.glob(os.path.join(WORK, "results", "astock_screen_*.csv")))
quant = {}
for r in csv.DictReader(open(csvs[-1], encoding="utf-8-sig")):
    quant[r["code"]] = r

# 2) 定性研判。优先按 _codes.json 的文件索引 join；缺失时回退到 verdict 自带 code。
codes_path = os.path.join(WORK, "cache", "_codes.json")
try:
    codes = json.load(open(codes_path, encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError, OSError):
    codes = []
verdicts = {}
for fp in glob.glob(os.path.join(WORK, "cache", "verdicts", "*.json")):
    try:
        i = int(os.path.basename(fp).split(".")[0])
        v = tolerant_load(fp)
    except Exception:
        continue
    code = codes[i] if 0 <= i < len(codes) else str(v.get("code") or "")
    if not code:
        continue
    v["code"] = code  # 用权威代码覆盖
    verdicts[code] = v

# 3) 合并 + 综合打分
VPRI = {"买入候选": 0, "值得观察": 1, "规避": 2, "数据不足": 3}
TRAP = {"低": 1.0, "中": 0.85, "高": 0.6}
CONF = {"高": 1.0, "中": 0.95, "低": 0.8}

rows = []
for code, q in quant.items():
    if q["tier"][:1] not in ("A", "B"):
        continue
    v = verdicts.get(code)
    qs = float(q["score"])
    if v:
        ql = float(v.get("qual_score") or 0)
        blended = (0.45 * qs + 0.55 * ql) * TRAP.get(v.get("value_trap_risk"), 0.85) * CONF.get(v.get("confidence"), 0.9)
        rows.append({**q, "v": v, "quant": qs, "qual": ql, "blended": round(blended, 1),
                     "vpri": VPRI.get(v.get("final_verdict"), 4)})
    else:
        rows.append({**q, "v": None, "quant": qs, "qual": None, "blended": round(qs * 0.5, 1), "vpri": 5})

rows.sort(key=lambda r: (r["vpri"], -r["blended"]))

# 4) Markdown 报告
def cell(x):
    return "" if x in (None, "") else str(x)

lines = [f"# A股五层选股 · 第4层定性修正终榜（生成于 {datetime.now():%Y-%m-%d %H:%M}）\n"]
sumfp = os.path.join(WORK, "cache", "_summary.md")
if os.path.exists(sumfp):
    lines.append(open(sumfp, encoding="utf-8").read().strip() + "\n\n---\n")

lines.append("## 定性修正后排序（按 买入候选→观察→规避→数据不足，组内按综合分）\n")
lines.append("综合分 = (0.45×量化 + 0.55×定性) × 陷阱系数(高0.6/中0.85/低1.0) × 信心系数(低0.8)\n")
hdr = ("| # | 代码 | 名称 | 行业 | 结论 | 综合 | 量化 | 定性 | 护城河 | 行业 | 陷阱 | 信心 | PE | 折让% | 一句话逻辑 |")
lines.append(hdr)
lines.append("|" + "---|" * 14)
for i, r in enumerate(rows, 1):
    v = r["v"] or {}
    moat = f'{cell(v.get("moat_score"))}·{cell(v.get("moat_type"))}' if v else ""
    lines.append("| {i} | {code} | {name} | {ind} | {vd} | {bl} | {qs} | {ql} | {moat} | {out} | {trap} | {conf} | {pe} | {disc} | {th} |".format(
        i=i, code=r["code"], name=r["name"], ind=r["industry"],
        vd=cell(v.get("final_verdict")) or "未评", bl=r["blended"], qs=cell(r["quant"]),
        ql=cell(r["qual"]), moat=moat, out=cell(v.get("industry_outlook")),
        trap=cell(v.get("value_trap_risk")), conf=cell(v.get("confidence")),
        pe=cell(r.get("pe_ttm")), disc=cell(r.get("discount")),
        th=(cell(v.get("thesis")).replace("|", "／")[:60])))

md = os.path.join(WORK, "results", f"astock_layer4_final_{TS}.md")
open(md, "w", encoding="utf-8").write("\n".join(lines))

# 4.5) HTML 版（渲染摘要 + 可排序/筛选的108行表）
def esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def inline(t):
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(t))

def md2html(mdtxt):
    out, inlist = [], False
    for ln in mdtxt.split("\n"):
        s = ln.rstrip()
        is_li = bool(re.match(r"^(\d+\.|-)\s", s))
        if is_li and not inlist:
            out.append("<ul>"); inlist = True
        if not is_li and inlist:
            out.append("</ul>"); inlist = False
        if not s:
            out.append("")
        elif s.startswith("### "):
            out.append("<h3>" + inline(s[4:]) + "</h3>")
        elif s.startswith("## "):
            out.append("<h2>" + inline(s[3:]) + "</h2>")
        elif s.startswith("# "):
            out.append("<h1>" + inline(s[2:]) + "</h1>")
        elif s.startswith("> "):
            out.append("<blockquote>" + inline(s[2:]) + "</blockquote>")
        elif is_li:
            out.append("<li>" + inline(re.sub(r"^(\d+\.|-)\s", "", s)) + "</li>")
        else:
            out.append("<p>" + inline(s) + "</p>")
    if inlist:
        out.append("</ul>")
    return "\n".join(out)

summary_html = md2html(open(sumfp, encoding="utf-8").read()) if os.path.exists(sumfp) else ""
hrows = []
for i, r in enumerate(rows, 1):
    v = r["v"] or {}
    hrows.append({
        "rk": i, "code": r["code"], "name": r["name"], "ind": r["industry"],
        "vd": v.get("final_verdict", "未评"), "bl": r["blended"],
        "qt": r["quant"], "ql": r.get("qual"), "moat": v.get("moat_score"),
        "mtype": v.get("moat_type", ""), "out": v.get("industry_outlook", ""),
        "trap": v.get("value_trap_risk", ""), "conf": v.get("confidence", ""),
        "pe": r.get("pe_ttm", ""), "disc": r.get("discount", ""),
        "th": v.get("thesis", ""),
    })
html = L4_HTML.replace("__SUMMARY__", summary_html) \
             .replace("__ROWS__", json.dumps(hrows, ensure_ascii=False)) \
             .replace("__TS__", datetime.now().strftime("%Y-%m-%d %H:%M"))
html_path = os.path.join(WORK, "results", f"astock_layer4_final_{TS}.html")
open(html_path, "w", encoding="utf-8").write(html)

# 5) 全量 CSV
out_csv = os.path.join(WORK, "results", f"astock_layer4_full_{TS}.csv")
cols = ["rank", "code", "name", "industry", "tier", "final_verdict", "blended", "quant_score", "qual_score",
        "moat_score", "moat_type", "business_model_score", "management_score", "industry_outlook",
        "competence", "value_trap_risk", "value_trap_note", "confidence", "pe_ttm", "peg", "discount",
        "thesis", "key_risks"]
with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(cols)
    for i, r in enumerate(rows, 1):
        v = r["v"] or {}
        w.writerow([i, r["code"], r["name"], r["industry"], r["tier"], v.get("final_verdict", "未评"),
                    r["blended"], r["quant"], r.get("qual", ""), v.get("moat_score", ""), v.get("moat_type", ""),
                    v.get("business_model_score", ""), v.get("management_score", ""), v.get("industry_outlook", ""),
                    v.get("competence", ""), v.get("value_trap_risk", ""), v.get("value_trap_note", ""),
                    v.get("confidence", ""), r.get("pe_ttm", ""), r.get("peg", ""), r.get("discount", ""),
                    v.get("thesis", ""), "; ".join(v.get("key_risks", []) or [])])

# 6) 终端摘要
buckets = {}
for r in rows:
    vd = (r["v"] or {}).get("final_verdict", "未评")
    buckets[vd] = buckets.get(vd, 0) + 1
print(f"合并完成：{len(rows)} 只 | 定性已评 {len(verdicts)} 只")
print("结论分布：", buckets)
print(f"📄 终榜: {md}")
print(f"📊 全量: {out_csv}")
print("\n【定性修正后 Top 15】")
for i, r in enumerate(rows[:15], 1):
    v = r["v"] or {}
    print(f"{i:>2}. {r['code']} {r['name']:<8} 综合{r['blended']:>5} "
          f"[{v.get('final_verdict','未评')}] 陷阱{v.get('value_trap_risk','-')} "
          f"信心{v.get('confidence','-')} | {r['industry']}")
