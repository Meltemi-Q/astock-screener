var API=window.location.protocol==="file:"?"http://localhost:8899":window.location.origin;
var PAYLOAD=null;
var curAsset="bond";
var curPeriod="day";

function $(id){return document.getElementById(id)}
function esc(v){return String(v===null||v===undefined?"":v).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}
function isNum(v){return v!==null&&v!==undefined&&v!==""&&isFinite(Number(v))}
function r(v,d){return isNum(v)?Number(v).toFixed(d===undefined?1:d):"-"}
function pct(v){return isNum(v)?Number(v).toFixed(2)+"%":"-"}
function yi(v){return isNum(v)?Number(v).toFixed(2)+"亿":"-"}
function moneyYi(v){if(!isNum(v))return "-";v=Number(v);return (Math.abs(v)>=1e8?(v/1e8).toFixed(2)+"亿":v.toFixed(0))}
function miniProgressMarkup(message){return '<span>'+esc(message)+'</span><span class="mini-progress" aria-hidden="true"><span></span></span>'}
function setInlineProgress(el,message){if(!el)return;el.classList.add("active");el.innerHTML=miniProgressMarkup(message)}
function clearInlineProgress(el,message){if(!el)return;el.classList.remove("active");el.textContent=message||""}
function setTheme(theme){document.documentElement.setAttribute("data-theme",theme);localStorage.setItem("theme",theme);var b=$("themeToggle");if(b)b.textContent=theme==="dark"?"☀️ 亮色":"🌙 暗色";if(PAYLOAD)setTimeout(drawKline,0)}
function initTheme(){var s=localStorage.getItem("theme")||"light";setTheme(s);var b=$("themeToggle");if(b)b.onclick=function(){setTheme(document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark")}}
function codeFromLocation(){var p=new URLSearchParams(window.location.search);var c=p.get("code")||(window.location.hash||"").replace(/^#/,"");return /^[0-9]{6}$/.test(c||"")?c:""}
function showStatus(msg,working){$("status").className=working?"status working":"status";$("status").innerHTML=working?miniProgressMarkup(msg):esc(msg);$("status").hidden=false;$("content").hidden=true}
function showError(msg){$("status").className="status error";$("status").textContent=msg;$("status").hidden=false;$("content").hidden=true}

function loadReport(){
  var code=codeFromLocation();
  if(!code){showError("缺少可转债代码，请从双低筛选页进入。");return}
  fetch("data/"+code+".json",{cache:"no-store"})
    .then(function(r){if(!r.ok)throw new Error("HTTP "+r.status);return r.json()})
    .then(function(p){PAYLOAD=p;renderReport(p)})
    .catch(function(){generateMissingReport(code)})
}

function generateMissingReport(code){
  if(window.location.protocol==="file:"){showError("无法加载 data/"+code+".json。请通过 ./run.sh 启动 HTTP 服务后访问。");return}
  showStatus("本地还没有这只可转债的深度分析，正在生成 "+code+" …",true);
  fetch(API+"/api/cbond/deep?code="+code)
    .then(function(r){return r.json()})
    .then(function(d){if(d.done){showStatus("已生成，正在加载…",true);setTimeout(function(){location.reload()},700)}else{showError("生成失败: "+(d.error||d.stderr_tail||"未知错误"))}})
    .catch(function(e){showError("无法连接服务生成详情: "+e.message)})
}

function renderReport(data){
  var meta=data.meta||{}, bond=data.bond||{}, scores=data.scores||{}, quote=data.stock_quote||{}, fy=(data.financials||[]).slice(-1)[0]||{};
  document.title=(meta.name||"可转债")+"("+meta.code+") 深度分析";
  $("title").innerHTML=esc(meta.name||"")+" <span style=\"font-size:16px;color:#64748b\">"+esc(meta.code||"")+"</span>";
  $("subtitle").textContent="正股 "+(meta.stock_name||"-")+"("+meta.stock_code+") · "+(meta.industry||"未分类")+" · "+(bond.status||"");
  $("status").hidden=true;$("content").hidden=false;
  renderKpis(bond,quote,fy,data);
  renderDecision(data);
  renderScores(scores);
  renderRiskBox(bond,data);
  renderFinancials(data.financials||[]);
  renderAnalysis(data);
  bindToolbar();
  drawKline();
  $("footer").textContent="数据来源：东方财富公开接口 + A股正股财务/K线 · AI分析由 DeepSeek 生成，仅供参考不构成投资建议 · 生成于 "+(meta.generated_at||"");
}

function renderKpis(bond,quote,fy,data){
  var items=[
    ["green",r(bond.price,2),"转债现价"],
    ["blue",pct(bond.premium_rt),"转股溢价率"],
    ["yellow",r(bond.double_low,2),"双低值"],
    ["",esc(bond.rating||"-"),"评级"],
    ["",yi(bond.remaining_scale),"剩余规模"],
    ["",r(bond.remaining_years,2)+"年","剩余年限"],
    ["blue",r(bond.convert_value,2),"转股价值"],
    ["",r(quote.pe_ttm,1),"正股PE(TTM)"],
    ["green",r(fy.roe,1)+"%","正股ROE"],
    ["green",r((data.scores||{}).total,1),"综合分"]
  ];
  $("kpis").innerHTML=items.map(function(x){return '<div class="kpi '+x[0]+'"><div class="val">'+x[1]+'</div><div class="lbl">'+esc(x[2])+'</div></div>'}).join("");
}

function renderDecision(data){
  var bond=data.bond||{}, scores=data.scores||{};
  var aiAction=data.analysis&&data.analysis.action;
  var mainAction=aiAction?("AI建议: "+aiAction):(data.action||"观察");
  var subAction=aiAction?("量化状态 "+(data.action||"-")+" · 机械状态 "+(bond.status||"-")):("机械状态 "+(bond.status||"-"));
  var thesis=(data.analysis&&data.analysis.bond_thesis)||defaultThesis(data);
  $("decisionBand").innerHTML='<div class="decision-main"><div class="decision-label">当前动作</div><div class="decision-action">'+esc(mainAction)+'</div><div class="decision-score">综合分 '+esc(r(scores.total,1))+'/100 · '+esc(subAction)+'</div></div>'
    +'<div class="decision-copy">'+esc(thesis)+'</div>';
}
function defaultThesis(data){
  var b=data.bond||{};
  if(b.status==="买入候选")return "机械规则通过，适合放入小仓或候选篮子继续复核；候选不足 10 只时不建议一次性构建完整组合。";
  if(b.status==="观察")return "未触发硬排除，但价格、溢价率或双低值不够理想，适合等待轮动机会。";
  return "触发排雷条件，暂不进入双低篮子。";
}

function renderScores(scores){
  var names={total:"综合",double_low:"双低性",price_safety:"价格安全",premium:"溢价率",credit:"信用",scale_liquidity:"规模流动性",maturity:"期限",stock_quality:"正股质量"};
  $("scoreGrid").innerHTML=Object.keys(names).map(function(k){return '<div class="score-item"><div class="score">'+esc(r(scores[k],1))+'</div><div class="name">'+esc(names[k])+'</div></div>'}).join("");
}

function renderRiskBox(bond,data){
  var lines=[
    ["排雷原因",bond.risk_reasons||"无"],
    ["到期日",bond.maturity_date||"-"],
    ["赎回触发价",r(bond.redeem_trigger_price,2)],
    ["回售触发价",r(bond.resale_trigger_price,2)],
    ["转股价",r(bond.convert_price,2)],
    ["建议纪律",data.action==="篮子候选"||data.action==="小仓试跑"?"单只不超过 5-8%，125-130 或强赎风险出现时主动止盈/轮动":"不强行买入，等待双低值和风险项改善"]
  ];
  $("riskBox").innerHTML=lines.map(function(x){return '<div class="risk-line"><span>'+esc(x[0])+'</span><span>'+esc(x[1])+'</span></div>'}).join("");
}

function renderFinancials(rows){
  if(!rows.length){$("financialRows").innerHTML='<tr><td class="l" colspan="8">暂无正股财务数据</td></tr>';return}
  $("financialRows").innerHTML=rows.slice().reverse().map(function(d){
    return '<tr><td class="l">'+esc(d.year)+'</td><td>'+esc(moneyYi(d.rev))+'</td><td>'+esc(moneyYi(d.netp))+'</td><td>'+esc(r(d.roe,1))+'%</td><td>'+esc(r(d.gm,1))+'%</td><td>'+esc(r(d.nm,1))+'%</td><td>'+esc(r(d.debt,1))+'%</td><td>'+esc(r(d.ocf_ratio,2))+'</td></tr>'
  }).join("");
}

function renderAnalysis(data){
  var a=data.analysis, code=(data.meta||{}).code;
  if(!a){
    $("analysisBlock").innerHTML='<div class="section"><h2>DeepSeek 可转债分析</h2><div class="ai-placeholder"><p>AI 分析未运行。</p><p>将从双低性、下跌保护、正股弹性、强赎/回售风险和轮动纪律分析这只转债。</p><button id="aiAnalyzeBtn">开始 AI 分析</button><div class="ai-progress" id="aiProgress">分析中…</div></div></div>';
    $("aiAnalyzeBtn").onclick=function(){runAiAnalysis(code)};
    return;
  }
  var score=isNum(a.cbond_score)?a.cbond_score:(data.scores||{}).total;
  var cards=[
    ["双低性",a.double_low_view],
    ["下跌保护",a.downside_protection],
    ["正股弹性",a.equity_optionality],
    ["正股质量",a.underlying_quality],
    ["强赎/回售/到期",a.call_put_risk],
    ["轮动计划",a.rotation_plan],
    ["关键风险",a.key_risks]
  ];
  $("analysisBlock").innerHTML='<div class="section"><h2>DeepSeek 可转债分析</h2><div class="ai-meta"><span>AI动作: <b>'+esc(a.action||data.action||"-")+'</b></span><span>AI分: <b>'+esc(r(score,1))+'/100</b></span><span>信心: <b>'+esc(a.confidence||"-")+'</b></span></div>'
    +'<div class="thesis"><h4>一句话结论</h4><p>'+esc(a.bond_thesis||"暂无")+'</p></div>'
    +'<div class="ai-grid">'+cards.map(function(c){return '<div class="ai-card"><h4>'+esc(c[0])+'</h4><p>'+esc(c[1]||"暂无")+'</p></div>'}).join("")+'</div></div>';
}

function runAiAnalysis(code){
  var btn=$("aiAnalyzeBtn"), prog=$("aiProgress");
  btn.disabled=true;btn.textContent="分析中…";setInlineProgress(prog,"DeepSeek 分析中…");
  fetch(API+"/api/cbond/deep?code="+code)
    .then(function(r){return r.json()})
    .then(function(d){if(d.done){setInlineProgress(prog,"完成，正在刷新…");setTimeout(function(){location.reload()},700)}else{clearInlineProgress(prog,"失败: "+(d.error||"未知错误"));btn.disabled=false;btn.textContent="重试"}})
    .catch(function(e){clearInlineProgress(prog,"网络错误: "+e.message);btn.disabled=false;btn.textContent="重试"});
}

function bindToolbar(){
  document.querySelectorAll("#assetSwitch button").forEach(function(b){b.onclick=function(){curAsset=b.getAttribute("data-asset");document.querySelectorAll("#assetSwitch button").forEach(function(x){x.classList.remove("on")});b.classList.add("on");drawKline()}});
  document.querySelectorAll("#periodSwitch button").forEach(function(b){b.onclick=function(){curPeriod=b.getAttribute("data-period");document.querySelectorAll("#periodSwitch button").forEach(function(x){x.classList.remove("on")});b.classList.add("on");drawKline()}});
}
function calcMA(data,n){var out=[];for(var i=0;i<data.length;i++){if(i<n-1){out.push(null);continue}var s=0;for(var j=i-n+1;j<=i;j++)s+=Number(data[j].close);out.push(s/n)}return out}
function prepareCanvas(cv,h){var W=cv.parentElement.clientWidth-4,H=h;cv.width=W*2;cv.height=H*2;cv.style.width=W+"px";cv.style.height=H+"px";var ctx=cv.getContext("2d");ctx.setTransform(1,0,0,1,0,0);ctx.scale(2,2);return [ctx,W,H]}
function drawEmpty(cv,text){var b=prepareCanvas(cv,380),ctx=b[0],W=b[1],H=b[2];ctx.clearRect(0,0,W,H);ctx.fillStyle="#64748b";ctx.font="13px sans-serif";ctx.textAlign="center";ctx.fillText(text,W/2,H/2)}
function drawKline(){
  var cv=$("cvKline"), data=(((PAYLOAD||{}).kline||{})[curAsset]||{})[curPeriod]||[];
  if(!data.length){drawEmpty(cv,curAsset==="bond"?"暂无转债K线数据":"暂无正股K线数据");return}
  var b=prepareCanvas(cv,380),ctx=b[0],W=b[1],H=b[2],n=data.length,padL=50,padR=12,padT=12,chartH=250,volTop=286,volH=70;
  var maxH=Number(data[0].high),minL=Number(data[0].low),maxV=0;
  for(var i=0;i<n;i++){maxH=Math.max(maxH,Number(data[i].high));minL=Math.min(minL,Number(data[i].low));maxV=Math.max(maxV,Number(data[i].volume||0))}
  maxH*=1.02;minL*=0.98;var range=maxH-minL||1,gap=(W-padL-padR)/n,cw=Math.max(1,gap*.42);
  ctx.clearRect(0,0,W,H);ctx.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue("--border").trim();ctx.lineWidth=.5;
  for(i=0;i<=4;i++){var y=padT+chartH*i/4,p=maxH-range*i/4;ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(W-padR,y);ctx.stroke();ctx.fillStyle="#64748b";ctx.font="9px sans-serif";ctx.textAlign="right";ctx.fillText(p.toFixed(2),padL-4,y+3)}
  function yPrice(p){return padT+(maxH-p)/range*chartH}
  for(i=0;i<n;i++){var d=data[i],x=padL+i*gap+gap/2,o=Number(d.open),c=Number(d.close),hi=Number(d.high),lo=Number(d.low),up=c>=o,color=up?"#16a34a":"#dc2626";ctx.strokeStyle=color;ctx.fillStyle=color;ctx.beginPath();ctx.moveTo(x,yPrice(hi));ctx.lineTo(x,yPrice(lo));ctx.stroke();var y=Math.min(yPrice(o),yPrice(c)),h=Math.max(1,Math.abs(yPrice(o)-yPrice(c)));ctx.fillRect(x-cw/2,y,cw,h);var vh=maxV?Number(d.volume||0)/maxV*volH:0;ctx.globalAlpha=.35;ctx.fillRect(x-cw/2,volTop+volH-vh,cw,vh);ctx.globalAlpha=1}
  [[5,"#f59e0b"],[10,"#2563eb"],[20,"#16a34a"]].forEach(function(pair){var ma=calcMA(data,pair[0]);ctx.strokeStyle=pair[1];ctx.lineWidth=1.2;ctx.beginPath();var started=false;for(var k=0;k<n;k++){if(ma[k]===null)continue;var x=padL+k*gap+gap/2,y=yPrice(ma[k]);if(!started){ctx.moveTo(x,y);started=true}else ctx.lineTo(x,y)}ctx.stroke()});
}

initTheme();
loadReport();
