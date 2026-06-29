var API=window.location.protocol==="file:"?"http://localhost:8899":window.location.origin;
var PAYLOAD=null;
var KDATA={day:[],week:[],month:[]};
var curPeriod="day";
var crossIdx=-1;
var tipK=null;

function $(id){return document.getElementById(id)}
function cssVar(name){return getComputedStyle(document.documentElement).getPropertyValue(name).trim()}
function setTheme(theme){
  document.documentElement.setAttribute("data-theme",theme);
  localStorage.setItem("theme",theme);
  var btn=$("themeToggle");
  if(btn)btn.textContent=theme==="dark"?"☀️ 亮色":"🌙 暗色";
  if(PAYLOAD){setTimeout(function(){drawKline();drawTrend(PAYLOAD.financials||[])},0)}
}
function initTheme(){
  var saved=localStorage.getItem("theme");
  if(!saved)saved=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";
  setTheme(saved);
  var btn=$("themeToggle");
  if(btn)btn.addEventListener("click",function(){
    setTheme(document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark");
  });
}
function esc(v){
  return String(v===null||v===undefined?"":v)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}
function isNum(v){return v!==null&&v!==undefined&&v!==""&&isFinite(Number(v))}
function r(v,d){return isNum(v)?Number(v).toFixed(d===undefined?1:d):""}
function rmb(v){
  if(!isNum(v))return "";
  v=Number(v);
  if(Math.abs(v)>=1e8)return (v/1e8).toFixed(2)+"亿";
  if(Math.abs(v)>=1e4)return (v/1e4).toFixed(0)+"万";
  return v.toFixed(0);
}
function judge(v,thresholds,labels){
  if(!isNum(v))return ["数据不足","#64748b"];
  v=Number(v);
  for(var i=0;i<thresholds.length;i++){
    if(v>=thresholds[i])return labels[i];
  }
  return labels[labels.length-1];
}
function stockCodeFromLocation(){
  var params=new URLSearchParams(window.location.search);
  var code=params.get("code")||(window.location.hash||"").replace(/^#/,"");
  return /^[0-9]{6}$/.test(code||"")?code:"";
}
function updateBackLinkFromStatus(){
  if(window.location.protocol==="file:")return;
  fetch(API+"/api/status").then(function(r){return r.json()}).then(function(d){
    if(d.latest_ts)$("backLink").href="../astock_screen_"+d.latest_ts+".html";
  }).catch(function(){});
}
function showError(message){
  $("status").className="status error";
  $("status").textContent=message;
  $("content").hidden=true;
}
function showStatus(message){
  $("status").className="status";
  $("status").textContent=message;
  $("status").hidden=false;
  $("content").hidden=true;
}

function loadReport(){
  var code=stockCodeFromLocation();
  if(!code){showError("缺少股票代码，请从选股总表或研报索引进入。");return}
  fetch("data/"+code+".json",{cache:"no-store"})
    .then(function(r){
      if(!r.ok)throw new Error("HTTP "+r.status);
      return r.json();
    })
    .then(function(payload){
      PAYLOAD=payload;
      KDATA=payload.kline||{day:[],week:[],month:[]};
      renderReport(payload);
    })
    .catch(function(e){
      generateMissingReport(code);
    });
}

function generateMissingReport(code){
  if(window.location.protocol==="file:"){
    showError("无法加载 data/"+code+".json。请通过 ./run.sh 启动本地 HTTP 服务后访问该页面。");
    return;
  }
  showStatus("本地还没有这只股票的深度研报，正在生成 "+code+" …");
  fetch(API+"/api/deep?code="+code)
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.done){
        showStatus("研报已生成，正在加载…");
        setTimeout(function(){location.reload()},600);
      }else{
        showError("生成 "+code+" 研报失败: "+(d.error||d.stderr||"未知错误"));
      }
    })
    .catch(function(){
      showError("无法连接本地服务生成 "+code+" 研报，请通过 ./run.sh 启动后访问。");
    });
}

function renderReport(data){
  var meta=data.meta||{}, quote=data.quote||{}, financials=data.financials||[];
  var name=meta.name||"", code=meta.code||"", ind=meta.industry||"";
  document.title=name+"("+code+") 深度研报 | 五层选股";
  $("title").innerHTML=esc(name)+' <span style="font-size:16px;color:#64748b">'+esc(code)+"</span>";
  $("subtitle").textContent=ind+" · "+financials.slice(-3).map(function(d){return d.year}).join("  |  ")+"年";
  $("status").hidden=true;
  $("content").hidden=false;
  renderKpis(quote, financials);
  renderQuantSummary(financials);
  renderFinancialRows(financials);
  renderPeers(data);
  renderAnalysis(data);
  $("footer").textContent="数据来源：东方财富公开接口 · AI分析由 DeepSeek 生成，仅供参考不构成投资建议 · 生成于 "+(meta.generated_at||"");
  bindKlineButtons();
  drawKline();
  setupKlineInteraction();
  drawTrend(financials);
}

function renderKpis(quote, financials){
  var fy=financials.length?financials[financials.length-1]:{};
  var items=[
    ["green",r(quote.price,2),"现价(元)"],
    ["yellow",isNum(quote.min_buy)?quote.min_buy:"","一手(元)"],
    ["blue",r(quote.pe_ttm,1),"PE(TTM)"],
    ["blue",r(quote.pb,1),"PB"],
    ["green",r(fy.roe,1)+"%","ROE"],
    ["green",r(fy.gm,1)+"%","毛利率"],
    ["green",r(fy.nm,1)+"%","净利率"],
    ["yellow",isNum(quote.mktcap_yi)?Number(quote.mktcap_yi).toFixed(0)+"亿":"","总市值"],
    ["",r(fy.debt,1)+"%","负债率"],
    ["",r(fy.roa,1)+"%","ROA"]
  ];
  $("kpis").innerHTML=items.map(function(x){
    return '<div class="kpi '+x[0]+'"><div class="val">'+esc(x[1])+'</div><div class="lbl">'+esc(x[2])+"</div></div>";
  }).join("");
}

function renderQuantSummary(financials){
  var fy=financials.length?financials[financials.length-1]:{};
  var roe=judge(fy.roe,[25,20,15],[["卓越","#16a34a"],["优秀","#2563eb"],["良好","#b45309"],["一般","#64748b"]]);
  var gm=judge(fy.gm,[60,40,30],[["强定价权","#16a34a"],["较强","#2563eb"],["合理","#b45309"],["偏低","#64748b"]]);
  var debt=isNum(fy.debt)?judge(100-Number(fy.debt),[70,50,30],[["极稳健","#16a34a"],["稳健","#2563eb"],["适中","#b45309"],["偏高","#dc2626"]]):["数据不足","#64748b"];
  var ocf=judge(fy.ocf_ratio,[1.5,1.0,0.8],[["现金流充沛","#16a34a"],["健康","#2563eb"],["合格","#b45309"],["需关注","#dc2626"]]);
  var yoy=isNum(fy.netp_yoy)?Number(fy.netp_yoy):0;
  var cagr=isNum(fy.cagr_netp)?Number(fy.cagr_netp):0;
  var items=[
    ["ROE",r(fy.roe,1)+"%",roe],
    ["毛利率",r(fy.gm,1)+"%",gm],
    ["负债率",r(fy.debt,1)+"%",debt],
    ["现金流",r(fy.ocf_ratio,2)+"x",ocf],
    ["净利增速",r(fy.netp_yoy,1)+"%",[yoy>=30?"高增长":(yoy>=10?"稳健":"平缓"),yoy>=20?"#16a34a":(yoy>=10?"#b45309":"#64748b")]],
    ["3年CAGR",r(fy.cagr_netp,1)+"%",[cagr>=25?"高成长":(cagr>=10?"稳定":"低速"),"#64748b"]]
  ];
  $("quantSummary").innerHTML=items.map(function(x){
    return '<div class="qs-item"><span class="qs-label">'+esc(x[0])+'</span>'
      +'<span class="qs-val" style="color:'+x[2][1]+'">'+esc(x[1])+'</span>'
      +'<span class="qs-tag" style="color:'+x[2][1]+'">'+esc(x[2][0])+"</span></div>";
  }).join("");
}

function renderFinancialRows(financials){
  $("financialRows").innerHTML=financials.slice().reverse().map(function(d){
    return "<tr>"
      +'<td class="l">'+esc(d.year)+'</td>'
      +"<td>"+esc(rmb(d.rev))+"</td>"
      +"<td>"+esc(rmb(d.netp))+"</td>"
      +'<td class="'+(isNum(d.roe)&&Number(d.roe)>=15?"pos":"")+'">'+esc(r(d.roe,1))+"%</td>"
      +"<td>"+esc(r(d.gm,1))+"%</td>"
      +'<td class="'+(isNum(d.nm)&&Number(d.nm)>=10?"pos":"")+'">'+esc(r(d.nm,1))+"%</td>"
      +"<td>"+esc(r(d.roa,1))+"%</td>"
      +"<td>"+esc(r(d.debt,1))+"%</td>"
      +"<td>"+esc(r(d.eps,2))+"</td>"
      +'<td class="'+(isNum(d.cf_oper)&&Number(d.cf_oper)>0?"pos":"neg")+'">'+esc(rmb(d.cf_oper))+"</td>"
      +'<td class="'+(isNum(d.ocf_ratio)&&Number(d.ocf_ratio)>=0.8?"pos":"neg")+'">'+esc(r(d.ocf_ratio,2))+"</td>"
      +"</tr>";
  }).join("");
}

function renderPeers(data){
  var meta=data.meta||{}, peers=data.peers||[];
  $("peerTitle").textContent="同行对比（同行业 "+(meta.industry||"")+" 优质标的）";
  $("peerRows").innerHTML=peers.slice(0,8).map(function(p,i){
    var label={"A_可买入":"A","B_优质待跌":"B","C_接近合格":"C"}[p.tier]||"";
    var nameCell=p.has_deep
      ? '<a href="report.html?code='+esc(p.code)+'" class="code">'+esc(p.code)+'</a> <a href="report.html?code='+esc(p.code)+'">'+esc(p.name)+'</a>'
      : '<a href="#" class="code deep-gen" data-code="'+esc(p.code)+'">'+esc(p.code)+'</a> <a href="#" class="deep-gen" data-code="'+esc(p.code)+'">'+esc(p.name)+'</a> <span style="font-size:10px;color:#64748b">一键</span>';
    return "<tr><td>"+(i+1)+"</td><td>"+nameCell+"</td><td>"+esc(r(p.pe,1))+"</td><td>"+esc(r(p.roe,1))+"%</td><td>"+esc(r(p.gm,1))+"%</td><td>"+esc(r(p.mktcap,0))+"亿</td><td>"+esc(label)+"</td></tr>";
  }).join("");
  document.querySelectorAll(".deep-gen").forEach(function(el){
    el.addEventListener("click",function(e){
      e.preventDefault();
      generatePeerReport(el.getAttribute("data-code"),el);
    });
  });
}

function renderAnalysis(data){
  var analysis=data.analysis, code=(data.meta||{}).code;
  if(!analysis){
    $("analysisBlock").innerHTML='<div class="section"><h2>AI 定性分析</h2>'
      +'<div class="ai-placeholder"><p style="font-size:15px">AI 定性分析未运行</p>'
      +'<p style="font-size:12px">DeepSeek 将对生意模式、护城河、管理层、成长性、行业地位、风险做深度分析。</p>'
      +'<button id="aiAnalyzeBtn">开始 AI 分析</button><div class="ai-progress" id="aiProgress">分析中…</div></div></div>';
    $("aiAnalyzeBtn").addEventListener("click",function(){runAiAnalysis(code)});
    return;
  }
  var moatColor=Number(analysis.moat_score||0)>=7?"#16a34a":(Number(analysis.moat_score||0)>=5?"#b45309":"#dc2626");
  var trapColor={"低":"#16a34a","中":"#b45309","高":"#dc2626"}[analysis.value_trap_risk]||"#b45309";
  var confColor={"高":"#16a34a","中":"#b45309","低":"#dc2626"}[analysis.confidence]||"#b45309";
  var cards=[
    ["生意模式",analysis.business_model],
    ["护城河",analysis.moat],
    ["成长性",analysis.growth],
    ["行业地位",analysis.industry_position],
    ["管理层与治理",analysis.management],
    ["风险点",analysis.risks,"risk"]
  ];
  $("analysisBlock").innerHTML='<div class="section"><h2>AI 定性分析 (DeepSeek)</h2>'
    +'<div class="ai-meta"><span>护城河评分: <b style="color:'+moatColor+'">'+esc(analysis.moat_score||"?")+'/10</b></span>'
    +'<span>综合定性分: <b style="color:#2563eb">'+esc(analysis.qual_score||"?")+'/100</b></span>'
    +'<span>价值陷阱风险: <b style="color:'+trapColor+'">'+esc(analysis.value_trap_risk||"?")+'</b></span>'
    +'<span>分析信心: <b style="color:'+confColor+'">'+esc(analysis.confidence||"?")+'</b></span></div>'
    +'<div class="ai-grid">'+cards.map(function(c){return '<div class="ai-card '+(c[2]||"")+'"><h4>'+esc(c[0])+'</h4><p>'+esc(c[1]||"暂无")+'</p></div>'}).join("")+'</div>'
    +'<div class="thesis"><h4>一句话投资逻辑</h4><p>「'+esc(analysis.thesis||"暂无")+'」</p></div></div>';
}

function runAiAnalysis(code){
  var btn=$("aiAnalyzeBtn"), prog=$("aiProgress");
  if(!btn)return;
  btn.disabled=true;btn.textContent="分析中…";prog.style.display="block";
  fetch(API+"/api/deep?code="+code)
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.done){prog.textContent="完成，正在刷新…";setTimeout(function(){location.reload()},600)}
      else{prog.textContent="失败: "+(d.error||"未知");btn.disabled=false;btn.textContent="重试"}
    })
    .catch(function(){
      prog.textContent="无法连接服务，请通过 ./run.sh 打开 HTTP 页面";
      btn.disabled=false;btn.textContent="重试";
    });
}

function generatePeerReport(code, el){
  el.textContent="...";
  el.style.pointerEvents="none";
  fetch(API+"/api/deep?code="+code)
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.done){location.href="report.html?code="+code}
      else{el.textContent=code;el.style.pointerEvents="auto";alert("生成失败: "+(d.error||"未知"))}
    })
    .catch(function(){
      el.textContent=code;el.style.pointerEvents="auto";
      alert("无法连接本地服务，请通过 ./run.sh 打开 HTTP 页面");
    });
}

function bindKlineButtons(){
  document.querySelectorAll(".kline-bar button").forEach(function(btn){
    btn.addEventListener("click",function(){
      curPeriod=btn.getAttribute("data-period");
      crossIdx=-1;
      document.querySelectorAll(".kline-bar button").forEach(function(b){b.classList.remove("on")});
      btn.classList.add("on");
      drawKline();
      if(tipK)tipK.innerHTML="在K线上点击查看详情";
    });
  });
}
function calcMA(data,n){
  var result=[];
  for(var i=0;i<data.length;i++){
    if(i<n-1){result.push(null);continue}
    var sum=0;for(var j=i-n+1;j<=i;j++)sum+=Number(data[j].close);
    result.push(sum/n);
  }
  return result;
}
function prepareCanvas(cv,h){
  var W=cv.parentElement.clientWidth-4,H=h;
  cv.width=W*2;cv.height=H*2;
  cv.style.width=W+"px";cv.style.height=H+"px";
  var ctx=cv.getContext("2d");
  ctx.setTransform(1,0,0,1,0,0);
  ctx.scale(2,2);
  return [ctx,W,H];
}
function drawEmptyCanvas(cv,text,h){
  var box=prepareCanvas(cv,h),ctx=box[0],W=box[1],H=box[2];
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle="#64748b";ctx.font="13px sans-serif";ctx.textAlign="center";
  ctx.fillText(text,W/2,H/2);
}
function drawKline(){
  var data=KDATA[curPeriod]||[], cv=$("cvKline");
  if(!data.length){drawEmptyCanvas(cv,"暂无 K 线数据",380);return}
  var box=prepareCanvas(cv,380),ctx=box[0],W=box[1],H=box[2];
  var n=data.length,chartH=H*0.68,volH=H*0.18,padL=50,padR=10,padT=10;
  var maxH=Number(data[0].high),minL=Number(data[0].low),maxV=0;
  for(var i=0;i<n;i++){
    maxH=Math.max(maxH,Number(data[i].high));
    minL=Math.min(minL,Number(data[i].low));
    maxV=Math.max(maxV,Number(data[i].volume||0));
  }
  maxH*=1.02;minL*=0.98;
  var priceRange=maxH-minL||1,gap=(W-padL-padR)/n,candleW=Math.max(1,gap*0.42);
  ctx.clearRect(0,0,W,H);
  ctx.strokeStyle="#e2e8f0";ctx.lineWidth=0.5;
  for(i=0;i<=4;i++){
    var y=padT+chartH*i/4,p=maxH-priceRange*i/4;
    ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(W-padR,y);ctx.stroke();
    ctx.fillStyle="#64748b";ctx.font="9px sans-serif";ctx.textAlign="right";ctx.fillText(p.toFixed(2),padL-4,y+3);
  }
  for(i=0;i<n;i++){
    var x=padL+i*gap,vh=maxV?Number(data[i].volume||0)/maxV*volH:0,vy=padT+chartH+10+volH-vh;
    ctx.fillStyle=Number(data[i].close)>=Number(data[i].open)?"rgba(22,163,74,0.28)":"rgba(220,38,38,0.24)";
    ctx.fillRect(x-candleW/2,vy,candleW,vh);
  }
  ctx.fillStyle="#64748b";ctx.font="9px sans-serif";ctx.textAlign="right";ctx.fillText("VOL",padL-4,padT+chartH+20);
  for(i=0;i<n;i++){
    x=padL+i*gap;
    var oy=padT+(maxH-Number(data[i].open))/priceRange*chartH;
    var cy=padT+(maxH-Number(data[i].close))/priceRange*chartH;
    var hy=padT+(maxH-Number(data[i].high))/priceRange*chartH;
    var ly=padT+(maxH-Number(data[i].low))/priceRange*chartH;
    var up=Number(data[i].close)>=Number(data[i].open);
    ctx.strokeStyle=up?"#16a34a":"#dc2626";ctx.fillStyle=ctx.strokeStyle;
    ctx.beginPath();ctx.moveTo(x,hy);ctx.lineTo(x,ly);ctx.stroke();
    ctx.fillRect(x-candleW/2,Math.min(oy,cy),candleW,Math.max(1,Math.abs(cy-oy)));
  }
  function drawMA(ma,color){
    ctx.strokeStyle=color;ctx.lineWidth=1.2;ctx.beginPath();
    var started=false;
    for(var j=0;j<n;j++){
      if(ma[j]===null)continue;
      var mx=padL+j*gap,my=padT+(maxH-ma[j])/priceRange*chartH;
      if(!started){ctx.moveTo(mx,my);started=true}else ctx.lineTo(mx,my);
    }
    ctx.stroke();
  }
  drawMA(calcMA(data,5),"#ca8a04");drawMA(calcMA(data,10),"#ea580c");drawMA(calcMA(data,20),"#dc2626");drawMA(calcMA(data,60),"#15803d");
  var skip=Math.max(1,Math.floor(n/8));
  ctx.fillStyle="#64748b";ctx.font="9px sans-serif";ctx.textAlign="center";
  for(i=0;i<n;i+=skip)ctx.fillText(String(data[i].date||"").slice(5),padL+i*gap,padT+chartH+volH+22);
  drawPinnedCrosshair();
}
function setupKlineInteraction(){
  var cv=$("cvKline");
  if(tipK)return;
  tipK=document.createElement("div");
  tipK.className="tip-k";
  tipK.innerHTML="在K线上点击查看详情";
  cv.parentElement.appendChild(tipK);
  cv.addEventListener("click",function(e){
    var idx=klineHit(e);
    if(idx===crossIdx){crossIdx=-1;showTip(-1,false);drawKline();return}
    crossIdx=idx;showTip(idx,true);drawKline();
  });
  cv.addEventListener("mousemove",function(e){
    if(crossIdx>=0)return;
    showTip(klineHit(e),false);
  });
  cv.addEventListener("mouseleave",function(){if(crossIdx<0)tipK.innerHTML="在K线上点击查看详情"});
}
function klineHit(e){
  var data=KDATA[curPeriod]||[];
  if(!data.length)return-1;
  var rect=$("cvKline").getBoundingClientRect(),W=$("cvKline").parentElement.clientWidth-4,padL=50,padR=10;
  var gap=(W-padL-padR)/data.length;
  var idx=Math.round((e.clientX-rect.left-padL)/gap);
  return idx>=0&&idx<data.length?idx:-1;
}
function showTip(idx,pinned){
  var data=KDATA[curPeriod]||[];
  if(idx<0||!data[idx]){tipK.innerHTML="在K线上点击查看详情";return}
  var d=data[idx],up=Number(d.close)>=Number(d.open),chg=((Number(d.close)-Number(d.open))/Number(d.open)*100).toFixed(2);
  tipK.innerHTML='<b style="color:#172033">'+esc(d.date)+'</b>'
    +" 开<b>"+r(d.open,2)+"</b> 收<b style=\"color:"+(up?"#16a34a":"#dc2626")+"\">"+r(d.close,2)+"</b>"
    +" 高"+r(d.high,2)+" 低"+r(d.low,2)
    +' <span style="color:'+(up?"#16a34a":"#dc2626")+'">'+(up?"+":"")+chg+"%</span>"
    +" 量"+(Number(d.volume||0)/10000).toFixed(0)+"万手"
    +(pinned?' <span style="color:#64748b;font-size:10px">·已固定·点击取消</span>':"");
}
function drawPinnedCrosshair(){
  var data=KDATA[curPeriod]||[];
  if(crossIdx<0||!data[crossIdx])return;
  var cv=$("cvKline"),ctx=cv.getContext("2d"),W=cv.parentElement.clientWidth-4,H=380;
  ctx.setTransform(1,0,0,1,0,0);ctx.scale(2,2);
  var chartH=H*0.68,padL=50,padR=10,padT=10,n=data.length,gap=(W-padL-padR)/n;
  var maxH=Number(data[0].high),minL=Number(data[0].low);
  for(var i=0;i<n;i++){maxH=Math.max(maxH,Number(data[i].high));minL=Math.min(minL,Number(data[i].low))}
  maxH*=1.02;minL*=0.98;
  var x=padL+crossIdx*gap,cy=padT+(maxH-Number(data[crossIdx].close))/(maxH-minL||1)*chartH;
  ctx.strokeStyle="rgba(58,134,255,0.22)";ctx.lineWidth=1;ctx.setLineDash([3,5]);
  ctx.beginPath();ctx.moveTo(x,padT);ctx.lineTo(x,padT+chartH);ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle="rgba(58,134,255,0.7)";ctx.beginPath();ctx.arc(x,cy,3,0,Math.PI*2);ctx.fill();
}

function drawTrend(financials){
  var cv=$("cvTrend");
  if(!financials.length){drawEmptyCanvas(cv,"暂无财务趋势数据",280);return}
  var box=prepareCanvas(cv,280),ctx=box[0],W=box[1],H=box[2];
  var years=financials.map(function(d){return d.year});
  var revs=financials.map(function(d){return isNum(d.rev)?Number(d.rev)/1e8:0});
  var netps=financials.map(function(d){return isNum(d.netp)?Number(d.netp)/1e8:0});
  var roes=financials.map(function(d){return isNum(d.roe)?Number(d.roe):0});
  var gms=financials.map(function(d){return isNum(d.gm)?Number(d.gm):0});
  var maxRev=Math.max.apply(null,revs.concat(netps).concat([1]));
  var maxPct=Math.max.apply(null,roes.concat(gms).concat([50]));
  var n=financials.length,barW=(W-100)/n*0.35,groupW=(W-100)/n;
  ctx.clearRect(0,0,W,H);
  for(var i=0;i<n;i++){
    var x=60+i*groupW,bh=revs[i]/maxRev*(H-80),y=H-40-bh;
    ctx.fillStyle="rgba(58,134,255,0.7)";ctx.fillRect(x-barW,y,barW,bh);
    ctx.fillStyle="#2563eb";ctx.font="9px sans-serif";ctx.textAlign="center";ctx.fillText(revs[i].toFixed(1),x-barW/2,y-4);
    bh=netps[i]/maxRev*(H-80);y=H-40-bh;
    ctx.fillStyle="rgba(22,163,74,0.65)";ctx.fillRect(x,y,barW,bh);
    ctx.fillStyle="#16a34a";ctx.fillText(netps[i].toFixed(1),x+barW/2,y-4);
  }
  function drawLine(vals,color,dashed){
    ctx.strokeStyle=color;ctx.lineWidth=2;if(dashed)ctx.setLineDash([4,3]);else ctx.setLineDash([]);
    ctx.beginPath();
    for(var j=0;j<n;j++){
      var x=60+j*groupW,y=H-40-(vals[j]/maxPct*(H-80));
      if(j===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
    }
    ctx.stroke();ctx.setLineDash([]);
  }
  drawLine(roes,"#b45309",false);drawLine(gms,"#64748b",true);
  for(i=0;i<n;i++){
    x=60+i*groupW;var ry=H-40-(roes[i]/maxPct*(H-80));
    ctx.fillStyle="#b45309";ctx.beginPath();ctx.arc(x,ry,3,0,Math.PI*2);ctx.fill();
    ctx.font="10px sans-serif";ctx.textAlign="center";ctx.fillText(roes[i]+"%",x,ry-8);
    ctx.fillStyle="#64748b";ctx.fillText(years[i],x,H-4);
  }
  ctx.fillStyle="#2563eb";ctx.fillRect(60,H-20,10,10);
  ctx.fillStyle="#64748b";ctx.font="10px sans-serif";ctx.textAlign="left";ctx.fillText("营收(亿)",74,H-11);
  ctx.fillStyle="#16a34a";ctx.fillRect(130,H-20,10,10);ctx.fillText("净利(亿)",144,H-11);
  ctx.strokeStyle="#b45309";ctx.beginPath();ctx.moveTo(210,H-15);ctx.lineTo(230,H-15);ctx.stroke();ctx.fillText("ROE%",234,H-11);
  ctx.strokeStyle="#64748b";ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(290,H-15);ctx.lineTo(310,H-15);ctx.stroke();ctx.setLineDash([]);ctx.fillText("毛利率%",314,H-11);
}

window.addEventListener("resize",function(){
  if(PAYLOAD){drawKline();drawTrend(PAYLOAD.financials||[])}
});
document.addEventListener("DOMContentLoaded",function(){
  initTheme();
  updateBackLinkFromStatus();
  loadReport();
});
