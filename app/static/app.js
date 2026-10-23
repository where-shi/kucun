"use strict";
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const number = value => typeof value === "number" && Number.isFinite(value);
const show = value => number(value) ? String(value) : "待核";
const sizeLabel = row => `美码 US ${row.size} / 中国码 ${row.cnSize || "待确认"}`;
function compareSizes(a,b) {
  return number(a.sizeOrder)&&number(b.sizeOrder)?a.sizeOrder-b.sizeOrder:String(a.size).localeCompare(String(b.size),undefined,{numeric:true});
}
function sizeHeader(items,size) {
  const values=[...new Set(items.filter(row=>row.size===size).map(row=>row.cnSize || "待确认"))];
  return `<span class="size-us">美码 US ${esc(size)}</span><small class="size-cn">中国码 ${esc(values.length===1?values[0]:"待确认")}</small>`;
}
const sum = (items, field) => items.reduce((n, item) => n + (number(item[field]) ? item[field] : 0), 0);
const labels = {today:"今日提醒",matrix:"按尺码补货",plans:"全部方案与货款",purchases:"采购与到货",sales:"销量与本轮",data:"数据与记录"};
const state = {data:null,page:"matrix",style:null,days:35,view:"stock",sku:null,planId:null,purchaseId:null,purchaseBatchId:"all",edits:{},priceEdits:{},tierPriceEdits:{},invalid:{},dayEdits:{},rec:null,recKey:"",search:"",busy:false,audit:[],eventId:null,noticeTimer:null,saved:false};
const rows = () => (state.data?.rows || []);
const styleRows = () => rows().filter(row => row.style === state.style);
const styles = () => [...new Map(rows().map(row => [row.style,{id:row.style,label:row.styleLabel}])).values()].sort((a,b)=>Number(seasonalHold(a.id))-Number(seasonalHold(b.id)));
const styleName = id => styles().find(item => item.id===id)?.label || id;
const plan = () => state.data?.plans?.find(item => item.id===state.planId) || null;
const purchase = () => state.data?.purchases?.find(item=>item.id===state.purchaseId) || null;
const historicalBatches = () => state.data?.historical_batches || [];
const currentPurchase = item => item.status!=="cancelled"&&!item.shipment_archived_at&&!item.reconciled_snapshot_id;
const allStylePurchases = () => (state.data?.purchases || []).filter(item=>item.style===state.style).sort((a,b)=>String(a.created_at).localeCompare(String(b.created_at))||a.id.localeCompare(b.id));
const activeStylePurchases = () => allStylePurchases().filter(currentPurchase);
const visiblePurchases = () => {const active=activeStylePurchases();return active.some(item=>item.id===state.purchaseBatchId)?active.filter(item=>item.id===state.purchaseBatchId):active;};
const batchName = item => `第 ${allStylePurchases().findIndex(p=>p.id===item.id)+1} 批 · ${item.reference}`;
const planStatus = item => item?.status==="cancelled"?"已取消":item?.status==="confirmed"?"已采购":"草稿";
function purchasedForSku(sku) {
  const batches=visiblePurchases().flatMap(item=>item.lines.filter(line=>line.sku===sku).map(line=>({item,line})));
  return {batches,ordered:batches.reduce((n,{line})=>n+line.ordered,0),shipped:batches.reduce((n,{line})=>n+line.shipped,0),received:batches.reduce((n,{line})=>n+line.received,0)};
}
function pickPurchasedSku() {
  if(state.view==="purchased"&&!purchasedForSku(state.sku).ordered)state.sku=styleRows().find(row=>purchasedForSku(row.sku).ordered>0)?.sku || styleRows()[0]?.sku;
}
const editable = () => !plan() || plan().status==="draft";
const editKey = () => state.planId || "new:"+state.style;
const edits = () => state.edits[editKey()] || (state.edits[editKey()]={});
const dirty = () => Object.keys(state.priceEdits).length>0 || Object.keys(state.tierPriceEdits).length>0 || Object.values(state.edits).some(value => Object.keys(value).length) || Object.values(state.invalid).some(value=>Object.keys(value).length) || Object.entries(state.dayEdits).some(([key,value])=>!key.startsWith("new:")&&state.data?.plans?.find(item=>item.id===key)?.target_days!==value);
const styleRec = () => state.recKey===`${state.style}:${state.days}` ? state.rec : null;
const resultFor = sku => {
  const found=styleRec()?.evaluated.find(item=>item.row.sku===sku);
  if(!found)return undefined;
  return seasonalHold(found.row.style)?{...found.result,status:"seasonal_hold",label:"春夏再看",quantity:null,reason:stylePreference(found.row.style).season_note || "已设为春夏再看，本轮暂不补。仍可查看库存、销量及现货缺口，已有手改草稿继续保留。"}:found.result;
};
function stylePreference(style=state.style) { return state.data?.style_preferences?.[style] || {}; }
const money = cents => new Intl.NumberFormat("zh-CN",{style:"currency",currency:"CNY"}).format(cents/100);
function priceValue(style=state.style) {
  return Object.hasOwn(state.priceEdits,style)?state.priceEdits[style]:(stylePreference(style).purchase_unit_cny ?? "");
}
function priceCents(value) {
  if(value===""||value===null||value===undefined)return null;
  const n=Number(value);return Number.isFinite(n)&&n>=0&&n<=100000&&Math.abs(n*100-Math.round(n*100))<1e-7?Math.round(n*100):null;
}
function priceTiers(style=state.style,useEdits=false) {
  const edits=useEdits?(state.tierPriceEdits[style] || {}):{};
  return (stylePreference(style).purchase_price_tiers || []).map((tier,index)=>({...tier,unit_cny:Object.hasOwn(edits,index)?edits[index]:tier.unit_cny}));
}
function rowPriceCents(row,style,useEdits=false) {
  const tiers=priceTiers(style,useEdits);
  if(tiers.length){const cn=Number(row?.cnSize);const tier=row?.cnSize!=null?tiers.find(t=>cn>=t.cn_min&&cn<=t.cn_max):null;return tier?priceCents(tier.unit_cny):null;}
  return priceCents(useEdits?priceValue(style):stylePreference(style).purchase_unit_cny);
}
function linesCost(lines,style,useEdits=false) {
  const bySku=new Map(rows().map(row=>[row.sku,row]));let total=0,missing=0;
  for(const line of lines){const cents=rowPriceCents(bySku.get(line.sku),style,useEdits);if(cents===null)missing+=line.quantity;else total+=line.quantity*cents;}
  return {total,missing};
}
function priceSummary(style) {
  const tiers=priceTiers(style);const pref=stylePreference(style);
  const known=tiers.length?tiers.map(t=>priceCents(t.unit_cny)).filter(c=>c!==null):[priceCents(pref.purchase_unit_cny)].filter(c=>c!==null);
  if(!known.length)return "待填";
  const min=Math.min(...known),max=Math.max(...known);
  return `${tiers.length?"按尺码 ":""}${money(min)}${max!==min?`–${money(max)}`:""}${tiers.some(t=>priceCents(t.unit_cny)===null)?"（部分待填）":""}`;
}
function currentCostText() {
  const invalid=state.invalid[editKey()] || {};
  const lines=mergedLines().filter(line=>!Object.hasOwn(invalid,line.sku));const cost=linesCost(lines,state.style,true);
  if(!sum(lines,"quantity")&&priceCents(priceValue())===null&&!priceTiers().length)return "填入单价后核算";
  return `${cost.missing?"已知部分 ":""}${money(cost.total)}${cost.missing?`（另有${cost.missing}双缺单价）`:""}${Object.keys(invalid).length?"（部分数量未填完整）":""}`;
}
function renderCurrentCost() {
  const preference=stylePreference();const tiers=priceTiers(state.style,true);const included=preference.purchase_price_includes_domestic_freight;
  const inputs=tiers.length?tiers.map((tier,index)=>`<label>中国码 ${tier.cn_min}–${tier.cn_max}（元 / 双${included?"，含国内运费":""}）<input type="number" min="0" max="100000" step="0.01" data-price-tier="${index}" value="${esc(tier.unit_cny ?? "")}" placeholder="单价未知"></label>`).join(""):`<label>参考采购单价（元 / 双）<input type="number" min="0" max="100000" step="0.01" data-purchase-price="${esc(state.style)}" value="${esc(priceValue())}" placeholder="单价未知"></label>`;
  return `<section class="purchase-cost-card" aria-label="本款货款估算"><div><span>本款预计采购金额${included?" · 已含国内运费":""}</span><strong data-current-cost>${esc(currentCostText())}</strong><small>按这次已填写数量计算 · 改双数会同步更新</small></div><div class="cost-price-entry">${inputs}<button type="button" class="small" data-action="save-price">保存单价</button></div><p>${esc(preference.purchase_price_note || "单价尚未记录，可自行填写。")}${preference.purchase_price_date?` · ${esc(preference.purchase_price_date)}`:""}</p><small>${included?"国内运费已并入上述采购价，金额不再另加运费；未含发往FBA的头程等额外费用。":"人民币采购金额估算；单价已含费用以记录为准，未含运费、额外包装和头程等另计。"}</small></section>`;
}
function renderBatchCost() {
  const latest=new Map();
  [...(state.data.plans || [])].filter(p=>p.status==="draft").sort((a,b)=>String(b.updated_at).localeCompare(String(a.updated_at))).forEach(p=>{if(!latest.has(p.style))latest.set(p.style,p);});
  const selected=[...latest.values()].filter(p=>sum(p.lines,"quantity")>0);
  let total=0,quantity=0,missing=0;
  const body=selected.map(p=>{const qty=sum(p.lines,"quantity");const cost=linesCost(p.lines,p.style);quantity+=qty;missing+=cost.missing;total+=cost.total;return `<tr><td>${esc(styleName(p.style))}</td><td>${qty} 双</td><td>${esc(priceSummary(p.style))}${stylePreference(p.style).purchase_price_includes_domestic_freight?'<small class="muted"> 已含国内运费</small>':""}</td><td>${cost.missing===qty?"待核算":`${money(cost.total)}${cost.missing?`（${cost.missing}双待计）`:""}`}</td><td><button class="small" data-plan="${esc(p.id)}">查看方案</button></td></tr>`;}).join("");
  return `<section class="panel batch-cost" aria-label="这批鞋货款核算"><div class="heading"><div><h2>这批鞋，大概要多少钱</h2><p class="muted">每款取最近保存的一份补货草稿，未填数量不计；同款其他方案不重复加。</p></div><div class="batch-cost-total"><span>${missing?"已知单价部分货款":"预计采购货款"}</span><strong>${money(total)}</strong><small>${quantity} 双 · ${selected.length} 款${missing?` · 另有 ${missing} 双缺单价，尚未计入`:""}</small></div></div>${selected.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>款式</th><th>本次双数</th><th>参考单价 / 双</th><th>预计货款</th><th></th></tr></thead><tbody>${body}</tbody></table></div>`:'<p class="muted">先保存补货数量，这里就会合计。</p>'}<p class="muted">按已保存数量和参考单价估算人民币货款，不代表已下单。单价已含费用按各款记录计入，不重复加；未含的头程等额外费用另计。可回到尺码页修改单价。${dirty()?"当前另有未保存修改，保存后再看此合计。":""}</p></section>`;
}
const firstShipmentOnly = () => !!styleRec()?.evaluated.length && styleRec().evaluated.every(item=>item.result.status==="planned");
function seasonalHold(style=state.style) { return stylePreference(style).replenishment_mode==="seasonal_hold"; }
function styleArrivalSummary(style=state.style) {
  const preference=stylePreference(style);
  if(preference.arrival_status!=="arrived")return "";
  return `<div class="callout green"><strong>已确认到仓</strong><p>${esc(preference.arrival_note)}</p><small>到仓反馈：${esc(dateText(preference.arrival_reported_date))} · 不代表整批到齐，数量以系统库存分项为准。</small></div>`;
}
function renderArrivalConfirmations() {
  const arrived=styles().filter(item=>stylePreference(item.id).arrival_status==="arrived");
  return arrived.length?`<div class="panel" style="margin-top:18px"><h2>款式到仓确认</h2>${arrived.map(item=>`<h3>${esc(item.label)}</h3>${styleArrivalSummary(item.id)}`).join("")}</div>`:"";
}
function productImagePath(style=state.style) {
  const path=stylePreference(style).image_path;
  return typeof path==="string"&&/^\/product-images\/[^?#\\\u0000-\u0020]+$/.test(path)?path:null;
}
function productImage(style,className="product-thumb") {
  const path=productImagePath(style);
  return `<span class="${className}">${path?`<img src="${esc(path)}" alt="${esc(styleName(style))} 款式识别图" loading="lazy" decoding="async" data-product-image><span class="image-fallback" hidden>图片暂无法显示</span>`:'<span class="image-fallback">图片<br>待关联</span>'}</span>`;
}
function chosen(sku) {
  if(Object.hasOwn(edits(),sku)) return edits()[sku];
  const line=plan()?.lines?.find(line=>line.sku===sku);
  return line ? {quantity:line.quantity,manual:line.manual,note:line.note || ""} : null;
}
function inputQuantity(sku) {
  if(Object.hasOwn(state.invalid[editKey()] || {},sku))return state.invalid[editKey()][sku];
  return chosen(sku)?.quantity ?? "";
}
function updatePlanGrid(sku,value,valid=true) {
  const row=rows().find(item=>item.sku===sku);if(!row)return;
  document.querySelectorAll("[data-plan-cell]").forEach(cell=>{
    if(cell.dataset.planCell!==sku)return;
    cell.textContent=valid?String(value):"未填";
    const button=cell.closest("button");button.classList.toggle("plan-filled",valid&&value>0);
    button.setAttribute("aria-label",`${row.colorLabel} ${sizeLabel(row)} ${valid?value:"未填"} ${valid?"我已填写":"请填完整"}`);
  });
  document.querySelectorAll("[data-plan-cell-state]").forEach(cell=>{if(cell.dataset.planCellState===sku)cell.textContent=valid?"我已填写":"请填完整";});
}
function mergedLines() {
  const found=new Map((plan()?.lines || []).map(line=>[line.sku,{...line}]));
  for(const [sku,line] of Object.entries(edits())) found.set(sku,{sku,...line});
  return [...found.values()];
}
function localToday() {
  const values=Object.fromEntries(new Intl.DateTimeFormat("en-US",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date()).map(part=>[part.type,part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
function dateText(value) {
  if(!value)return "未填写";
  const text=String(value);if(/^\d{4}-\d{2}-\d{2}$/.test(text))return text;
  const date=new Date(text);if(Number.isNaN(date.getTime()))return text;
  const parts=Object.fromEntries(new Intl.DateTimeFormat("en-US",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).formatToParts(date).map(part=>[part.type,part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}（北京时间）`;
}
function alertError(error) {
  const message=typeof error==="string" ? error : error.message || "操作失败，请稍后重试。";
  $("#error").hidden=false;
  $("#error").innerHTML=`${esc(message)}${error.status===409 ? '<br><strong>已有更新，当前修改尚未写入。请先记录手改内容，再刷新本地数据查看最新版本。</strong>' : ""}<button type="button" class="small" data-action="dismiss-error">知道了</button>`;
}
function notify(message) {
  clearTimeout(state.noticeTimer);$("#notice").textContent=message;$("#notice").hidden=false;
  state.noticeTimer=setTimeout(()=>{$("#notice").hidden=true;},7000);
}
async function api(path,options={}) {
  const writing=options.method && options.method!=="GET";
  const headers={...(writing?{"Content-Type":"application/json","X-App-Token":state.data?.token || state.data?.metadata?.token || ""}:{})};
  const response=await fetch(path,{...options,headers:{...headers,...options.headers},cache:"no-store"});
  const body=await response.json().catch(()=>({error:"服务器未返回可读取的数据。"}));
  if(!response.ok){const error=new Error(body.error || `请求失败（${response.status}）`);error.status=response.status;throw error;}
  return body;
}
async function write(path,method,body) { return api(path,{method,body:JSON.stringify(body)}); }
async function action(fn) {
  if(state.busy)return;
  state.busy=true;$("#app").classList.add("loading");$("#modal-content").classList.add("loading");
  try{await fn();}catch(error){alertError(error);if($("#modal").open){const box=$("#modal-error");if(box){box.textContent=error.message;box.hidden=false;}}}
  finally{state.busy=false;$("#app").classList.remove("loading");$("#modal-content").classList.remove("loading");updateSaveState();}
}
function updateSaveState() {
  $("#save-state").textContent=state.busy?"正在处理…":dirty()?"有修改尚未保存":state.saved?"已保存到本机":"已从本地读取";
}
function pickDefaultPlan() {
  const candidates=(state.data.plans || []).filter(item=>item.style===state.style && item.status==="draft");
  candidates.sort((a,b)=>String(b.updated_at).localeCompare(String(a.updated_at)));
  state.planId=candidates[0]?.id || null;
  state.days=state.dayEdits[editKey()] || candidates[0]?.target_days || 35;
}
async function bootstrap(initial=false) {
  state.data=await api("/api/bootstrap");
  if(!state.data.rows?.length)throw new Error("当前没有可用的商品资料。请检查本地来源文件。");
  if(!styles().some(item=>item.id===state.style))state.style=styles()[0].id;
  if(initial)pickDefaultPlan();
  if(state.planId&&!plan())state.planId=null;
  $("#connection").textContent="本地已连接";$("#connection").className="tag";
  const md=state.data.metadata;
  $("#source-time").textContent=`销量截至美西 ${md.salesEnd || "待核"} · 库存快照：${md.inventoryCapturedLocal || "待核"} · 刷新本地数据只读取已保存资料`;
  await loadRecommendations();
  render();
}
async function loadRecommendations() {
  const key=`${state.style}:${state.days}`;
  state.rec=null;state.recKey=key;
  const response=await api(`/api/recommendations?style=${encodeURIComponent(state.style)}&days=${state.days}`);
  if(state.recKey!==key)return;
  state.rec=response;
  if(!styleRows().some(row=>row.sku===state.sku))state.sku=response.evaluated.find(item=>item.result.status==="suggest")?.row.sku || styleRows()[0]?.sku;
  pickPurchasedSku();
}
async function selectStyle(id) {
  state.style=id;state.sku=null;pickDefaultPlan();state.rec=null;render();
  await loadRecommendations();render();
}
async function changeDays(days) {
  if(!editable()){notify("已采购方案保留原数量。请新建草稿再调整天数。");return;}
  state.days=Number(days);state.dayEdits[editKey()]=state.days;state.rec=null;render();await loadRecommendations();render();
}
function render() {
  if(!state.data)return;
  const navButton=key=>`<button type="button" data-page="${key}" class="${state.page===key?"active":""}" ${state.page===key?'aria-current="page"':""}>${labels[key]}</button>`;
  const secondary=["plans","purchases","data"];
  $("#navigation").innerHTML=["matrix","sales"].map(navButton).join("")+`<details class="nav-more ${secondary.includes(state.page)?"active":""}"><summary>${secondary.includes(state.page)?`更多 · ${labels[state.page]}`:"更多"}</summary><div class="nav-more-items">${secondary.map(navButton).join("")}</div></details>`;
  const views={today:renderToday,matrix:renderMatrix,plans:renderPlans,purchases:renderPurchases,sales:renderSales,data:renderData};
  $("#app").innerHTML=views[state.page]();updateSaveState();
}
function selectStyleHTML() {
  return `<label class="section-picker">当前款式<select data-action="select-style">${styles().map(item=>`<option value="${esc(item.id)}" ${item.id===state.style?"selected":""}>${esc(item.label)}${seasonalHold(item.id)?" · 春夏再看":""}</option>`).join("")}</select></label>`;
}
function sidebar() {
  return `<aside class="style-sidebar"><label>找款式<input type="search" id="style-search" placeholder="名称或款号" value="${esc(state.search)}"></label><div class="style-list">${styles().map(item=>`<button type="button" class="style-button ${item.id===state.style?"active":""} ${seasonalHold(item.id)?"style-seasonal":""}" data-style="${esc(item.id)}" data-search="${esc((item.label+" "+item.id).toLowerCase())}" ${!item.label.toLowerCase().includes(state.search.toLowerCase())?"hidden":""}>${productImage(item.id,"style-thumb")}<span class="style-label"><strong>${esc(item.label.replace(item.id,"").trim() || item.id)}</strong><small>${esc(item.id)}</small>${seasonalHold(item.id)?'<span class="season-tag">春夏再看</span>':""}</span></button>`).join("")}</div></aside>`;
}
function renderMatrix() {
  const hold=seasonalHold();const rawRec=styleRec();const rec=hold&&rawRec?{...rawRec,quantity:0,suggestedCount:0}:rawRec;const selected=plan();
  const purchasedView=state.view==="purchased";
  return `${renderWorkReminders()}<div class="workspace decision-workspace">${sidebar()}<section><div class="panel">
    ${purchasedView?"":'<div class="instructions"><span><b>1</b>选款看建议</span><span><b>2</b>采用建议再修改</span><span><b>3</b>保存补货草稿</span></div>'}
    <div class="heading"><div class="product-heading">${productImagePath()?`<button class="image-preview-button" data-action="preview-image" aria-label="查看 ${esc(styleName(state.style))} 商品大图">${productImage(state.style)}<span>看大图</span></button>`:productImage(state.style)}<div><h1>${esc(styleName(state.style))}</h1><span class="muted">${styleRows().length} 个颜色尺码 · FBA 补货参考</span></div></div><span class="${hold?"season-tag":"tag"}">${purchasedView?"已采购 · 跟进进度":hold?"春夏再看":"建议先填，你来决定"}</span></div>
    ${purchasedView?"":styleArrivalSummary()+historicalSummary()+variantSupplySummary()}
    ${purchasedView?"":`<div class="controls"><span class="muted">新货到仓后，想再卖多少天</span>${[30,35,40].map(days=>`<button type="button" data-days="${days}" class="${state.days===days?"active":""}" ${!editable()?"disabled":""}>${days} 天</button>`).join("")}</div>`}
    ${purchasedView?"":hold?(state.view==="recommend"?`<div class="callout seasonal-note"><strong>春夏再看 · 本轮暂不补</strong><p>${esc(stylePreference().season_note || "这款先留到春夏再评估。库存、销量和现货缺口仍可查看，已保存的手改数量继续保留。")}</p></div>`:""):rec?decisionSummary():""}
    ${!purchasedView&&rec?inventoryOverview():""}
    ${["recommend","plan"].includes(state.view)?`<div class="decision-actions"><button type="button" class="primary" data-action="adopt" ${!editable()||!rec||hold||firstShipmentOnly()?"disabled":""}>${firstShipmentOnly()?"首发请逐码填写":"一键带入建议"}</button><button type="button" data-action="save-plan" ${!editable()?"disabled":""}>保存我的数量</button>${editable()?'<button type="button" class="primary" data-action="confirm-purchase">我已采购，确认数量</button>':`<span class="tag">本方案${planStatus(selected)}</span>`}<span class="muted">本次已填 <strong data-draft-total>${sum(mergedLines(),"quantity")}</strong> 双 · ${editable()?"保存是草稿；采购完成后再点确认":selected?.status==="cancelled"?"本方案已取消登记":"已按此数量登记采购"}</span></div>`:""}
    <div class="controls"><span class="muted">格子看什么</span>${[["recommend","建议补货"],["plan","补货方案"],["purchased","已采购"],["stock","FBA 可售"],["inbound","已发在途"],["coverage","现货天数"],["sales","近 7 天销量"]].map(([value,label])=>`<button type="button" class="small ${state.view===value?"active":""}" data-view="${value}">${label}</button>`).join("")}</div>
    ${purchasedView?renderPurchasedOverview():""}
    ${state.view==="plan"?`<p class="plan-matrix-note"><strong>${selected?esc(selected.name):"本款补货草稿"}</strong> · 格子显示你这次填写的双数；<strong>— 是未填写，0 是明确不补</strong>。点格子即可在上方修改，保存后可下次继续。</p>`:""}
    ${purchasedView?'<p class="matrix-explainer">大数字是当前跟进批次的<strong>已采购双数</strong>，下方显示已购未发、部分发货或到仓进度。点格子看该码的采购明细；— 表示没有正在跟进的采购。</p>':'<p class="matrix-explainer"><span class="recommend-outline-key"></span><strong>蓝框 / “补”标记：建议追加的尺码</strong>。点一个格子，上方就显示库存、在途、补货原因和数量输入框。</p>'}
    ${state.view==="inbound"?'<p class="matrix-explainer">格子只显示实际有数量的状态，例如<strong>调仓中 3 双</strong>；同一码分在几个状态时分别列出，0不重复写。状态待核会明确标出，采购和历史批次不另加进来。</p>':""}
    ${renderDetail()}
    ${purchasedView||state.view==="inbound"||rec?matrixTable():'<div class="empty">正在计算这款的建议…</div>'}
    <p class="muted">${purchasedView?"这里只跟进已采购批次，不把采购双数另加到FBA可售或系统在途；领星核实整批发出后移入历史，不重复计入待发采购。":"深蓝外圈表示当前选中。格子中的数字按上方所选视图显示；补货建议和本次填写量分开，保存仍是草稿。"}</p>
  </div>${purchasedView?"":renderPlanSummary()}</section></div>`;
}
function renderPlanSummary() {
  return `<section class="panel plan-summary" aria-label="本款方案汇总"><div class="plan-summary-main"><div class="plan-summary-total"><strong id="plan-total">${sum(mergedLines(),"quantity")}</strong><span>双 · ${planStatus(plan())}</span></div><div class="plan-summary-cost"><span>参考货款</span><strong data-current-cost>${esc(currentCostText())}</strong></div><button type="button" data-action="plan-options">方案详情</button></div></section>`;
}
function renderWorkReminders() {
  const unknown=rows().filter(row=>!row.inventoryKnown);
  const drafts=new Set((state.data.plans || []).filter(item=>item.status==="draft"&&sum(item.lines || [],"quantity")>0).map(item=>item.style)).size;
  const pending=(state.data.purchases || []).filter(currentPurchase).length;
  const historical=historicalBatches().filter(item=>!number(item.total_quantity)||!number(item.received_quantity)||item.received_quantity<item.total_quantity).length;
  const items=[unknown.length?`<button type="button" class="quiet small" data-goto-sku="${esc(unknown[0].sku)}">${unknown.length} 个尺码库存待核</button>`:"",drafts?`<button type="button" class="quiet small" data-page="plans">${drafts} 款已存补货草稿</button>`:"",pending?`<span>${pending} 批采购待发</span>`:"",historical?`<button type="button" class="quiet small" data-page="purchases">${historical} 批历史到货需跟进</button>`:""].filter(Boolean);
  return items.length?`<div class="work-reminders"><strong>今日提醒</strong>${items.join("")}</div>`:"";
}
function renderPurchasedOverview() {
  const active=activeStylePurchases();const batches=visiblePurchases();const lines=batches.flatMap(item=>item.lines);
  const ordered=sum(lines,"ordered"),shipped=sum(lines,"shipped"),received=sum(lines,"received");
  const history=allStylePurchases().filter(item=>!currentPurchase(item));
  const actions=`<div class="controls"><label>查看采购批次<select id="purchase-batch-filter"><option value="all">全部待发采购 · ${active.length} 批</option>${active.map(item=>`<option value="${esc(item.id)}" ${item.id===state.purchaseBatchId?"selected":""}>${esc(batchName(item))} · ${sum(item.lines,"ordered")} 双</option>`).join("")}</select></label><button type="button" data-action="new-purchase-batch">再来一批采购</button></div>`;
  const batchCards=batches.map(item=>`<div class="purchased-batch"><div class="heading"><div><strong>${esc(batchName(item))}</strong><p>已购 ${sum(item.lines,"ordered")} 双 · 领星已核实发出 ${item.lines.reduce((n,line)=>n+(line.system_shipped || 0),0)} 双</p></div><button type="button" class="small" data-cancel-purchase="${esc(item.id)}">取消这批采购登记</button></div><p class="muted">${item.shipment_matches?.length?"已匹配领星货件："+esc([...new Set(item.shipment_matches.map(m=>m.shipment_id))].join("、")):"等待领星逐批核实发货；原有在途不抵扣本批采购。"}</p></div>`).join("");
  const past=history.length?`<details class="purchase-history"><summary>已发货 / 已取消历史（${history.length} 批）</summary>${history.map(item=>`<div class="purchased-batch"><strong>${esc(batchName(item))}</strong><p>${esc(purchaseLabel(item))} · ${sum(item.lines,"ordered")} 双</p>${item.cancel_note?`<p class="muted">${esc(item.cancel_note)}</p>`:""}${(item.shipment_matches || []).map(m=>`<p class="muted">领星货件 ${esc(m.shipment_id)} · 已核实 ${sum(m.lines,"quantity")} 双</p>`).join("")}</div>`).join("")}</details>`:"";
  if(!batches.length)return `${actions}<div class="callout"><strong>这款暂无正在跟进的采购</strong><p>保存补货方案仍是草稿，实际下单后才会显示在这里。</p></div>${past}`;
  const facts=[["已采购",ordered],["已购未发",ordered-shipped],["已发未到",shipped-received],["已到仓",received]];
  return `${actions}<div class="purchased-overview"><h2>已采购 ${ordered} 双 · ${batches.length} 批</h2><div class="metrics">${facts.map(([label,value])=>`<div class="metric"><span>${label}</span><strong>${value}<small> 双</small></strong></div>`).join("")}</div><p class="muted">以领星具体货件核实本批发货；整批核实发出后移入历史，系统在途继续在“已发在途”查看。</p>${batchCards}</div>${past}`;
}
function renderPurchasedDetail(row) {
  const facts=purchasedForSku(row.sku);
  return `<section id="sku-advice" class="selected-advice" aria-label="所选尺码采购详情" aria-live="polite"><div class="heading"><div><span class="muted">当前选中尺码 · 已采购</span><h3>${esc(row.colorLabel)} · ${esc(sizeLabel(row))}</h3></div><strong class="selected-advice-result">${facts.ordered?`已采购 ${facts.ordered} 双`:"暂无采购"}</strong></div><div class="selected-advice-facts">${[["已采购",facts.ordered],["已购未发",facts.ordered-facts.shipped],["已发未到",facts.shipped-facts.received],["已到仓",facts.received]].map(([label,value])=>`<div><span>${label}</span><strong>${value} 双</strong></div>`).join("")}</div>${facts.batches.map(({item,line})=>`<div class="purchased-batch"><strong>${esc(batchName(item))}</strong><p>本码已购 ${line.ordered} 双 · 已发 ${line.shipped} 双 · 已到 ${line.received} 双</p><p class="muted">整批：${esc(purchaseLabel(item))} · ${esc(shipmentTimingText(item))}</p></div>`).join("") || '<p class="muted">该尺码没有正在跟进的采购。</p>'}</section>`;
}
function inventoryOverview() {
  if(state.view==="inbound"){
    const states=inboundFacts({}).states.map(({key,label})=>{
      const values=styleRows().map(row=>inboundFacts(row).states.find(item=>item.key===key).quantity);
      const known=values.filter(number),total=known.reduce((a,b)=>a+b,0),missing=values.length-known.length;
      return {label,total,missing,value:missing?(total?`${total}+`:"待核"):String(total)};
    }).filter(item=>item.total>0||item.missing>0);
    return states.length?`<div class="metrics flow-overview" aria-label="本款在途与仓内状态">${states.map(item=>`<div class="metric"><span>${item.label}</span><strong>${item.value}<small>${item.value==="待核"?"":" 双"}</small></strong>${item.missing?`<small>${item.missing} 个尺码未取到</small>`:""}</div>`).join("")}</div>`:'<p class="muted">本款暂无已发在途或仓内流转库存。</p>';
  }
  const items=styleRows();const invTotal=key=>{const values=items.map(row=>row.inventory?.[key]);const missing=values.filter(value=>!number(value)).length;return `${values.reduce((a,b)=>a+(number(b)?b:0),0)}${missing?"+":""}`;};
  const warehouse=items.reduce((n,row)=>n+["afn_inbound_receiving_quantity","reserved_fc_transfers","reserved_fc_processing"].reduce((t,key)=>t+(number(row.inventory?.[key])?row.inventory[key]:0),0),0);
  const missing=items.some(row=>["afn_inbound_receiving_quantity","reserved_fc_transfers","reserved_fc_processing"].some(key=>!number(row.inventory?.[key])));
  return `<div class="metrics"><div class="metric"><span>FBA 当前可售</span><strong>${invTotal("afn_fulfillable_quantity")}<small> 双</small></strong></div><div class="metric"><span>已发在途</span><strong>${invTotal("afn_inbound_shipped_quantity")}<small> 双</small></strong></div><div class="metric"><span>接收 / 调仓 / 处理中</span><strong>${warehouse}${missing?"+":""}<small> 双</small></strong></div><div class="metric"><span>${firstShipmentOnly()?"首发数量 · 你来配码":"我的补货建议 · 你来确认"}</span><strong>${firstShipmentOnly()?"待填写":seasonalHold()?0:styleRec().quantity}<small>${firstShipmentOnly()?"":" 双"}</small></strong></div></div>${items.some(row=>!row.inventoryKnown||Object.values(row.inventory || {}).some(value=>value===null))?'<p class="muted">带 + 的合计只含已知数量，尚有库存未取到；原始空白保留，没有算作 0。</p>':""}`;
}
function decisionSummary() {
  const rec=styleRec();const arrived=stylePreference().arrival_status==="arrived";
  const provisional=rec.evaluated.filter(item=>item.result.status!=="planned"&&(item.result.provisional||item.result.status==="supply")).length;
  const text=firstShipmentOnly()?"这款计划首发，请先填写各码发仓数量":rec.quantity>0?`这款建议补 ${rec.quantity} 双，分在 ${rec.suggestedCount} 个尺码`:
    arrived?"这款刚到仓，建议本次先不补，先卖现有货":"这款建议本次先不追加";
  if(firstShipmentOnly())return `<div class="callout"><strong>${esc(text)}</strong><p>${rec.plannedCount} 个颜色尺码，数量尚未确定；点格子配量并保存草稿。未发出的计划不计在途，也不按零销量判断不用发。</p></div>`;
  return `<div class="callout ${rec.quantity>0?"green":""}"><strong>${esc(text)}</strong><p>${rec.noBuyCount || 0} 个尺码暂不追加${provisional?`，其中 ${provisional} 个采用保守建议`:""}${rec.plannedCount?`；另有 ${rec.plannedCount} 个尺码计划首发，由你配量`:""}。采用建议后，可继续改数量，由你最终确认。</p></div><p class="muted">按发出后约 ${rec.transitDays || transitDays()} 天到仓，另覆盖到仓后 ${state.days} 天；已有在途和仓内货计入覆盖，计划中未发货不计入。暂未额外计工厂生产时间；这是估算，实际到仓时间仍看物流。</p>`;
}
function inboundFacts(row) {
  const inv=row.inventory || {};
  const known=value=>number(value)&&value>=0?value:null;
  const states=[["afn_inbound_shipped_quantity","已发在途"],["reserved_fc_transfers","调仓中"],["reserved_fc_processing","仓内处理中"],["afn_inbound_receiving_quantity","接收中"]].map(([key,label])=>({key,label,quantity:known(inv[key])}));
  return {shipped:states[0].quantity,receiving:states[3].quantity,states,
    active:states.filter(item=>item.quantity>0),unknown:states.filter(item=>item.quantity===null)};
}
function variantSupplySummary() {
  const groups=[...new Map(styleRows().filter(row=>row.supplyNote).map(row=>[row.color,row])).values()];
  return groups.length?`<div class="callout"><strong>这些颜色的发货安排</strong>${groups.map(row=>`<p><b>${esc(row.colorLabel)}</b>：${esc(row.supplyNote)}</p>`).join("")}</div>`:"";
}
function matrixTable(view=state.view) {
  const items=styleRows();const colors=[...new Map(items.map(row=>[row.color,row.colorLabel])).entries()];
  const sizes=[...new Set([...items].sort(compareSizes).map(row=>row.size))];
  return `<div class="table-wrap"><table class="matrix ${view==="inbound"?"matrix-inbound":""}"><thead><tr><th>颜色 / 尺码</th>${sizes.map(size=>`<th scope="col">${sizeHeader(items,size)}</th>`).join("")}</tr></thead><tbody>${colors.map(([color,label])=>{const staged=items.find(row=>row.color===color&&row.supplyStage);return `<tr><th>${esc(label)}${staged?`<small class="variant-stage">${esc(staged.colorSupplyLabel || (staged.supplyStage==="planned"?"计划发 · 未发出":"已发 · 未到仓"))}</small>`:""}</th>${sizes.map(size=>{
    const row=items.find(item=>item.color===color&&item.size===size);if(!row)return '<td class="dash">—</td>';
    const result=resultFor(row.sku) || (["purchased","inbound"].includes(view)?{quantity:null,label:view==="purchased"?"已采购":"库存状态",status:"inventory"}:null);if(!result)return '<td>…</td>';
    const flow=view==="inbound"?inboundFacts(row):null;
    let main=number(result.quantity)?result.quantity:result.label;let small=result.status==="suggest"?"建议补货":result.label;
    if(view==="stock"){main=row.fbaReplenishmentMode==="fbm_only"?"FBM":show(row.inventory?.afn_fulfillable_quantity);small=row.fbaReplenishmentMode==="fbm_only"?"自发货":"当前可售";}
    if(view==="inbound"){main=flow.active[0]?.quantity ?? (flow.unknown.length?"待核":0);small=flow.active[0]?.label || (flow.unknown.length?"状态未取全":"暂无在途／仓内货");}
    if(view==="sales"){main=show(row.n7);small="FBA 普通销量";}
    if(view==="coverage"){main=number(result.daily)&&result.daily>0&&number(row.inventory?.afn_fulfillable_quantity)?(row.inventory.afn_fulfillable_quantity/result.daily).toFixed(1):"待评估";small="天 · 当前现货";}
    if(view==="plan"){const entry=chosen(row.sku);const invalid=Object.hasOwn(state.invalid[editKey()] || {},row.sku);main=invalid?"未填":entry?entry.quantity:"—";small=invalid?"请填完整":entry?(entry.manual?"我已填写":"已带入建议"):"未填写";}
    if(view==="purchased"){const facts=purchasedForSku(row.sku);main=facts.ordered || "—";small=!facts.ordered?"未采购":facts.received===facts.ordered?"已到齐 · 待核库存":facts.received>0?`已到 ${facts.received} / ${facts.ordered}`:facts.shipped===facts.ordered?"已发货":facts.shipped>0?`已发 ${facts.shipped} / ${facts.ordered}`:"已购未发";}
    const receiving=view==="inbound"?flow.active.slice(1).map(item=>`<small class="flow-state">${item.label} <b>${item.quantity}</b></small>`).join("")+flow.unknown.map(item=>`<small class="flow-state">${item.label}待核</small>`).join(""):"";
    const extraLabel=view==="inbound"?flow.active.slice(1).map(item=>`，${item.label} ${item.quantity} 双`).join("")+flow.unknown.map(item=>`，${item.label}待核`).join(""):"";
    const recommended=view!=="purchased"&&result.status==="suggest"&&number(result.quantity)&&result.quantity>0;
    const adviceLabel=view==="purchased"?"":recommended?`，建议补 ${result.quantity} 双`:`，${result.label}`;
    return `<td><button type="button" class="neutral ${["plan","purchased"].includes(view)&&number(main)&&main>0?"plan-filled":""} ${recommended?"recommend-outline":""} ${state.sku===row.sku?"selected":""}" data-sku="${esc(row.sku)}" aria-pressed="${state.sku===row.sku}" aria-controls="sku-advice" aria-label="${esc(`${label} ${sizeLabel(row)} ${main} ${small}${extraLabel}${adviceLabel}`)}">${recommended?'<span class="matrix-rec-marker" aria-hidden="true">补</span>':""}<span ${view==="plan"?`data-plan-cell="${esc(row.sku)}"`:""}>${esc(main)}</span><small ${view==="plan"?`data-plan-cell-state="${esc(row.sku)}"`:""}>${esc(small)}</small>${receiving}</button></td>`;
  }).join("")}</tr>`;}).join("")}</tbody></table></div>`;
}
function renderDetail() {
  const row=styleRows().find(row=>row.sku===state.sku);if(!row)return "";
  if(state.page==="matrix"&&state.view==="purchased")return renderPurchasedDetail(row);
  const result=resultFor(row.sku) || (state.view==="inbound"?{quantity:null,label:"库存状态",status:"inventory"}:null);if(!result)return "";
  const inv=row.inventory || {};const entry=chosen(row.sku);
  const warehouseValues=[inv.afn_inbound_receiving_quantity,inv.reserved_fc_transfers,inv.reserved_fc_processing];
  const warehouse=warehouseValues.every(number)?warehouseValues.reduce((a,b)=>a+b,0):null;
  const qty=value=>number(value)?String(value):"未取到";
  const recommended=result.status==="suggest"&&result.quantity>0;
  const advice=recommended?`建议补 ${result.quantity} 双`:result.label;
  const multiple=Number.isInteger(row.orderMultiple)&&row.orderMultiple>1?row.orderMultiple:1;
  const reason=result.daily>0&&["suggest","covered"].includes(result.status)?`近14天售出 ${show(row.n14)} 双、近28天售出 ${show(row.n28)} 双，按较快的日均销量估算，等货及到仓后共 ${result.transitDays+result.coverDays} 天约需 ${result.planningDemand} 双。减去现货 ${qty(inv.afn_fulfillable_quantity)} 双、已发在途 ${qty(inv.afn_inbound_shipped_quantity)} 双和仓内货 ${qty(warehouse)} 双，这次建议补 ${result.quantity} 双${multiple>1?`（按每码${multiple}双倍数取整）`:""}。${result.lowSample?"销量样本少，先作小量配码参考。":""}`:result.reason;
  const flow=inboundFacts(row);
  const flowFacts=[...flow.active.map(item=>[item.label,`${item.quantity} 双`]),...flow.unknown.map(item=>[item.label,"待核"])];
  const facts=state.page==="matrix"&&state.view==="inbound"?(flowFacts.length?flowFacts:[["当前在途／仓内状态","暂无在途／仓内货"]]):[["FBA 当前可售",qty(inv.afn_fulfillable_quantity)+" 双"],["已发在途",qty(inv.afn_inbound_shipped_quantity)+" 双"],["接收 / 调仓 / 处理中",qty(warehouse)+" 双"],["预计共卖",result.daily>0?`约 ${qty(result.planningDemand)} 双`:"暂无稳定销量"]];
  return `<section id="sku-advice" class="selected-advice" aria-label="所选尺码补货详情" aria-live="polite"><div class="heading"><div><span class="muted">当前选中尺码</span><h3>${esc(row.colorLabel)} · ${esc(sizeLabel(row))}</h3></div><strong class="selected-advice-result ${recommended?"to-buy":""}">${esc(advice)}</strong></div>
    <div class="selected-advice-facts">${facts.map(([label,value])=>`<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div>
    ${row.supplyNote?`<p class="variant-supply-note"><strong>发货安排：</strong>${esc(row.supplyNote)}</p>`:""}
    ${row.listingStatusText?`<p class="variant-supply-note"><strong>领星链接状态：${esc(row.listingStatusText)}</strong> · FBA当前可售只计即时可履约数量；调仓、处理中和接收中单列，不以可售0判断能否下单。<small>链接状态核对：${esc(dateText(row.listingStatusCapturedAtISO))}</small></p>`:""}
    <p class="selected-advice-reason"><strong>为什么这样建议：</strong>${esc(reason)}</p>
    ${result.bridgeWarning?`<p class="bridge-warning">${esc(result.bridgeWarning)}</p>`:""}
    <div class="selected-advice-entry"><label>这个尺码，你决定补几双<input type="number" min="0" step="1" data-quantity="${esc(row.sku)}" value="${esc(inputQuantity(row.sku))}" placeholder="${number(result.quantity)?`建议 ${result.quantity}`:"未填写"}" ${!editable()?"disabled":""}></label><span>双</span>${number(result.quantity)&&!seasonalHold()?`<button type="button" class="small" data-action="fill-selected" ${!editable()||entry?.manual?"disabled":""}>填入建议 ${result.quantity} 双</button>`:""}${state.page==="matrix"&&["recommend","plan"].includes(state.view)?"":`<button type="button" class="primary" data-action="save-plan" ${!editable()?"disabled":""}>保存我的数量</button>`}<small>${entry?.manual?"已手改（含 0），自动建议会保留":"可改成你想补的数量，也可填 0"}</small></div>
    <details><summary>展开销量、库存分项与批次依据</summary><div class="detail-grid">${[["近 7 / 前 7 天",`${show(row.n7)} / ${show(row.p7)}`],["近 14 / 28 天",`${show(row.n14)} / ${show(row.n28)}`],["接收中",inv.afn_inbound_receiving_quantity],["调仓",inv.reserved_fc_transfers],["仓内处理中",inv.reserved_fc_processing],["订单预留",inv.reserved_customerorders],["预留总数",inv.afn_reserved_quantity],["计划中未发",inv.afn_inbound_working_quantity],["原表账面库存",row.stockValue]].map(([label,value])=>`<div><span>${esc(label)}</span><strong>${esc(typeof value==="string"?value:qty(value))}</strong></div>`).join("")}</div><p class="muted">${esc(result.assumption || "")} 预留总数不与子项相加；已发及仓内货计入计划，和当前可售分列。</p><p class="sku">${esc(row.sku)}</p><p class="muted">${esc(row.batchNote || "暂无批次说明")}</p></details>
  </section>`;
}
function moqNote() {
  if(seasonalHold())return '<p class="muted">春夏再看期间不作起订量提醒，现有草稿数量保留。</p>';
  const total=sum(mergedLines(),"quantity");
  const row=styleRows()[0] || {};const details=[];
  if(Number.isInteger(row.minimumOrder)&&row.minimumOrder>0)details.push(`参考起订量 ${row.minimumOrder} 双`);
  if(Number.isInteger(row.orderMultiple)&&row.orderMultiple>1)details.push(`每码按 ${row.orderMultiple} 双倍数建议`);
  if(Number.isInteger(row.cartonQuantity)&&row.cartonQuantity>0)details.push(`每箱 ${row.cartonQuantity} 双`);
  return `<p class="muted">${details.length?details.join("；"):"起订量和装箱要求尚未记录，请向厂家核对"}。当前 ${total} 双；按实际需要决定，不自动凑数。</p>`;
}
function renderToday() {
  return `<div class="page"><h1>今日提醒</h1>${renderWorkReminders()}<button type="button" data-page="matrix">回到尺码页</button></div>`;
}
function renderPlans() {
  const all=[...(state.data.plans || [])].sort((a,b)=>String(b.updated_at).localeCompare(String(a.updated_at)));
  const current=plan();
  return `<div class="page"><div class="heading"><div><h1>全部方案与货款</h1><p class="muted">查看整批预算、已存草稿和历史方案。</p></div><button class="primary" data-action="new-plan">新建方案</button></div>
    ${renderBatchCost()}
    <div class="plans-workspace"><div class="panel"><h2>已保存方案</h2>${all.length?all.map(item=>`<div class="list-card"><div><strong>${esc(item.name)}</strong><p>${esc(styleName(item.style))} · ${item.target_days} 天 · ${sum(item.lines || [],"quantity")} 双 · ${planStatus(item)}</p><p>${esc(dateText(item.updated_at))}</p></div><button data-plan="${esc(item.id)}" class="${current?.id===item.id?"active":""}">打开</button></div>`).join(""):'<div class="empty"><strong>还没有保存方案</strong>在“按尺码补货”采用建议，或新建一份草稿。</div>'}</div>
    <div class="panel">${current?`<div class="heading"><h2>${esc(current.name)}</h2><span class="tag">${planStatus(current)}</span></div><p class="muted">参考 ${current.target_days} 天 · 已保存</p>
    <p class="plan-matrix-note">格子显示本次方案数量，— 是未填写，0 是明确不补；点选尺码，在上方修改后保存。</p>
    ${renderCurrentCost()}${renderDetail()}${matrixTable("plan")}
    <div class="link-row"><button data-page="matrix">回到尺码页${current.status==="draft"?"修改":"查看"}</button><a class="button" href="/api/plans/${encodeURIComponent(current.id)}/export.csv">导出 CSV</a>${current.status==="draft"?'<button class="primary" data-action="confirm-purchase">我已采购，确认数量</button>':""}</div>
    <p class="muted" style="margin-top:14px">导出文件供单款沟通核对；已采购方案保留当时数量。</p>`:'<div class="empty">选择左侧一份方案查看明细。</div>'}</div></div></div>`;
}
function purchaseLabel(item) {
  if(item.status==="cancelled")return "已取消登记";
  if(item.shipment_archived_at)return "领星已核实发货 · 已归档";
  const total=sum(item.lines || [],"ordered"),shipped=sum(item.lines || [],"shipped"),received=sum(item.lines || [],"received");
  if(item.reconciled_snapshot_id)return "已接收并核对";
  if(received===total && total>0)return "已接收 · 待核库存";
  if(received>0)return "部分接收";
  if(shipped===total && total>0)return "已发货";
  if(shipped>0)return "部分发货";
  return "已购未发";
}
function historicalPending(item) {
  return number(item.total_quantity)&&number(item.received_quantity)?Math.max(0,item.total_quantity-item.received_quantity):null;
}
function transitDays() { return state.data?.planning_settings?.transit_days || 30; }
function addCalendarDays(value,days) {
  if(!/^\d{4}-\d{2}-\d{2}$/.test(value || ""))return null;
  const date=new Date(`${value}T00:00:00Z`);if(Number.isNaN(date.getTime()))return null;
  date.setUTCDate(date.getUTCDate()+days);return date.toISOString().slice(0,10);
}
function timingFor(item) {
  const saved=item.timing || {};
  const actualDates=(item.events || []).filter(event=>event.type==="shipped"&&/^\d{4}-\d{2}-\d{2}$/.test(event.date || "")).map(event=>event.date).sort();
  const lastActual=actualDates[actualDates.length-1];
  const shipDate=saved.ship_date || lastActual || item.expected_ship_date || null;
  const shipKind=saved.ship_date_kind || (lastActual?"actual":item.expected_ship_date?"planned":null);
  const days=saved.transit_days || transitDays();
  const availableDate=item.expected_available_date || saved.expected_available_date || addCalendarDays(shipDate,days);
  return {...saved,ship_date:shipDate,ship_date_kind:shipKind,expected_available_date:availableDate,transit_days:days,
    ship_date_basis:saved.ship_date_basis || (lastActual?"依据最新一次实际发货记录":item.expected_ship_date?"依据填写的预计发货日期":null),
    available_date_basis:item.expected_available_date?"采用你明确填写的预计可售日期":saved.available_date_basis || (availableDate?`按发货后约 ${days} 天估算`:null)};
}
function shortDate(value) {
  const match=String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if(!match)return value || "";
  return `${match[1]===localToday().slice(0,4)?"":match[1]+"/"}${Number(match[2])}/${Number(match[3])}`;
}
function shipmentTimingText(item) {
  const timing=timingFor(item);
  const date=timing.ship_date;
  if(!date)return "发货时间待确认";
  if(timing.ship_date_kind==="actual")return `${shortDate(date)} 实际发出`;
  if(timing.ship_date_kind==="estimated")return `约 ${shortDate(date)} 发出（估算）`;
  return `预计 ${shortDate(date)} 发货`;
}
function arrivalSummary(item,received,remaining) {
  const date=timingFor(item).expected_available_date;
  const receivedText=number(received)?`已到 ${received} 双`:"已到数量待确认";
  if(remaining===0)return `${receivedText}，本批已到齐`;
  const remainingText=number(remaining)?`余 ${remaining} 双`:"其余货量";
  if(!date)return `${receivedText}，${remainingText}；发货时间待确认`;
  const estimate=`${receivedText}，${remainingText}预计 ${shortDate(date)} 左右到仓 / 可售（估算）`;
  return date<localToday()?`${estimate}；估算时间已过，待核最新进度`:estimate;
}
function timingDetails(item) {
  const timing=timingFor(item);
  const notes=[];
  if(timing.ship_date_basis)notes.push(`发货日期依据：${timing.ship_date_basis}`);
  if(timing.available_date_basis)notes.push(`到仓 / 可售估算：${timing.available_date_basis}`);
  else if(timing.expected_available_date)notes.push(`到仓 / 可售按发货后约 ${timing.transit_days || transitDays()} 天估算。`);
  if(item.received_date)notes.push(`实际到货日期：${item.received_date}`);
  if(item.source_note)notes.push(item.source_note);
  return notes.length?`<details><summary>日期与记录依据</summary>${notes.map(note=>`<p class="audit-note">${esc(note)}</p>`).join("")}</details>`:"";
}
function historicalSummary() {
  const items=historicalBatches().filter(item=>item.style===state.style);
  if(!items.length)return "";
  return `<div class="callout">${items.map(item=>`<p><strong>原有批次：</strong>${esc(arrivalSummary(item,item.received_quantity,historicalPending(item)))}</p><p>${esc(shipmentTimingText(item))}${number(item.received_quantity)&&item.received_quantity>0?" · 到货已确认":""}</p>`).join("")}<p>FBA 可售依库存快照，到货量不重复加现货。</p><button type="button" class="small" data-page="purchases">看历史批次到货明细</button></div>`;
}
function renderHistoricalBatches() {
  const items=[...historicalBatches()].sort((a,b)=>String(b.updated_at).localeCompare(String(a.updated_at)));
  if(!items.length)return "";
  return `<div class="panel" style="margin-top:18px"><h2>历史批次到货进度</h2><p class="muted">原有批次，只跟踪到货进度；FBA 可售依库存快照，不将到货量重复加现货。</p>${items.map(item=>`<section class="detail"><div class="heading"><div><h3>${esc(item.reference)}</h3><p class="muted">${esc(styleName(item.style))}</p></div><span class="tag amber">${historicalPending(item)===0?"已到齐":number(item.received_quantity)&&item.received_quantity>0?"部分到货":"跟踪中"}</span></div><p class="strong">${esc(arrivalSummary(item,item.received_quantity,historicalPending(item)))}</p><p class="muted">${esc(shipmentTimingText(item))}${number(item.received_quantity)&&item.received_quantity>0?" · 到货已确认":""}</p><div class="metrics"><div class="metric"><span>原批次总量</span><strong>${esc(show(item.total_quantity))}<small> 双</small></strong></div><div class="metric"><span>已发</span><strong>${esc(show(item.shipped_quantity))}<small> 双</small></strong></div><div class="metric"><span>已到</span><strong>${esc(show(item.received_quantity))}<small> 双</small></strong></div><div class="metric"><span>待到</span><strong>${esc(show(historicalPending(item)))}<small> 双</small></strong></div></div><details><summary>查看已到货的 ${(item.received_lines || []).length} 个颜色尺码</summary><div class="table-wrap"><table class="data-table"><thead><tr><th>颜色 / 尺码</th><th>本次确认已到</th></tr></thead><tbody>${(item.received_lines || []).map(line=>{const row=rows().find(row=>row.sku===line.sku);return `<tr><td>${esc(row?`${row.colorLabel} ${sizeLabel(row)}`:line.sku)}</td><td>${esc(show(line.quantity))} 双</td></tr>`;}).join("") || '<tr><td colspan="2">逐码到货明细待补</td></tr>'}</tbody></table></div><p class="muted">这里只列已经确认的接收明细；其余待到货的逐码分配不作推定。</p></details>${timingDetails(item)}</section>`).join("")}</div>`;
}
function renderPurchases() {
  const all=[...(state.data.purchases || [])].sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)));
  if(!purchase()&&all.length)state.purchaseId=all[0].id;
  const item=purchase();
  if(!all.length)return `<div class="page"><div class="page-intro"><h1>采购与到货</h1><p class="muted">先看已有批次的到货进度与预计时间。</p></div>${renderArrivalConfirmations()}${renderHistoricalBatches()}<div class="panel" style="margin-top:18px"><div class="heading"><div><h2>新增采购</h2><p class="muted">实际下单后，从已保存的补货方案登记。</p></div><button data-page="plans">去补货方案</button></div></div></div>`;
  return `<div class="page"><div class="page-intro"><h1>采购与到货</h1><p class="muted">新采购从已确认下单的方案登记；原有历史批次在下方查看。发货和接收填写本次实际发生的数量。</p></div><div class="grid-2"><div class="panel"><h2>采购批次</h2>${all.length?all.map(p=>`<div class="list-card"><div><strong>${esc(styleName(p.style))}</strong><p>${esc(p.reference)} · ${sum(p.lines || [],"ordered")} 双 · ${purchaseLabel(p)}</p></div><button data-purchase="${esc(p.id)}" class="${item?.id===p.id?"active":""}">查看</button></div>`).join(""):'<div class="empty"><strong>还没有登记采购</strong>实际下单后，在尺码页或“补货方案”点“我已采购，确认数量”。<p style="margin-top:15px"><button data-page="plans">去补货方案</button></p></div>'}</div>
    <div class="panel">${item?renderPurchaseDetail(item):'<div class="empty">选择一个采购批次查看。</div>'}</div></div>${renderArrivalConfirmations()}${renderHistoricalBatches()}</div>`;
}
function renderPurchaseDetail(item) {
  const lines=item.lines || [];const full=lines.length&&lines.every(line=>line.received===line.ordered);
  return `<div class="heading"><h2>${esc(item.reference)}</h2><span class="tag">${purchaseLabel(item)}</span></div><p>${esc(styleName(item.style))}</p><p class="strong">${esc(arrivalSummary(item,sum(lines,"received"),sum(lines,"ordered")-sum(lines,"received")))}</p><p class="muted">${esc(shipmentTimingText(item))}</p>${timingDetails(item)}<p class="muted">预计时间与实际记录分开；接收记录只记批次进度，不增加系统库存。</p>
  <div class="table-wrap"><table class="data-table"><thead><tr><th>颜色 / 尺码</th><th>已购</th><th>已发</th><th>已收</th></tr></thead><tbody>${lines.map(line=>{const row=rows().find(row=>row.sku===line.sku);return `<tr><td>${esc(row?`${row.colorLabel} ${sizeLabel(row)}`:line.sku)}</td><td>${line.ordered}</td><td>${line.shipped}</td><td>${line.received}</td></tr>`;}).join("")}</tbody></table></div>
  ${item.status==="cancelled"?'<p class="muted">已取消登记，仅保留历史；原库存和外部订单不变。</p>':full?item.reconciled_snapshot_id?`<div class="callout green">已核对至快照 ${esc(item.reconciled_snapshot_id)}。${esc(item.reconciliation_note || item.reconciled_note || "")}</div>`:`<div class="callout">本批已接收完成。先导入接收后的新库存快照，再核对本批已包含其中。采购时的旧快照不能用于核对。</div>${item.source_snapshot_id===state.data.metadata.snapshot_id?'<button data-page="data">去导入后续库存快照</button>':'<button data-action="reconcile">核对已体现在系统库存</button>'}`:`<button class="primary" data-action="purchase-event">登记本次发货 / 接收</button>`}
  <details open><summary>实际发生记录（${(item.events || []).length} 次）</summary>${(item.events || []).length?(item.events || []).map(event=>`<div class="list-card"><div><strong>${event.type==="shipped"?"发货":"接收"} · ${esc(event.date)}</strong><p>本次 ${sum(event.lines || [],"quantity")} 双</p></div></div>`).join(""):'<p class="muted" style="margin-top:10px">尚无实际发货或接收记录。</p>'}</details>`;
}
function cycleFor(row) {
  return state.data.cycles?.[row.sku] || {baseline:row.baseline,baselineDate:row.baselineDate,baselineKind:row.baselineKind,currentSold:row.currentSold,revision:0};
}
function renderSales() {
  const all=styleRows();
  return `<div class="page"><div class="heading"><div><h1>销量与本轮表现</h1><p class="muted">累计售出永久保留。本轮售出是观察起点之后的销量，不代表精确追溯某批货卖了多少。</p></div>${selectStyleHTML()}</div>
  <div class="panel"><div class="metrics"><div class="metric"><span>FBA 近 7 天</span><strong>${sum(all,"n7")}</strong></div><div class="metric"><span>FBA 前 7 天</span><strong>${sum(all,"p7")}</strong></div><div class="metric"><span>FBA 近 28 天</span><strong>${sum(all,"n28")}</strong></div><div class="metric"><span>FBM 近 7 天</span><strong>${sum(all,"fbm7")}</strong></div></div>
  <p class="muted">本轮起点按逐码保存的日期和依据计算。未确认的起点保持空白；已记录到货不再次加库存。</p>
  <div class="table-wrap"><table class="data-table"><thead><tr><th>颜色 / 尺码</th><th>FBA 7 天</th><th>前 7 天</th><th>14 天</th><th>28 天</th><th>FBM 7 天</th><th>累计售出</th><th>起点累计</th><th>本轮售出</th><th>观察起点</th><th></th></tr></thead><tbody>${all.map(row=>{const cycle=cycleFor(row);const round=number(cycle.roundSold)?cycle.roundSold:number(cycle.baseline)&&number(cycle.currentSold)?cycle.currentSold-cycle.baseline:null;return `<tr><td>${esc(row.colorLabel)} ${esc(sizeLabel(row))}</td><td>${show(row.n7)}</td><td>${show(row.p7)}</td><td>${show(row.n14)}</td><td>${show(row.n28)}</td><td>${show(row.fbm7)}</td><td>${show(cycle.currentSold)}</td><td>${number(cycle.baseline)?cycle.baseline:"未确认"}</td><td>${number(round)?round:"待确认"}</td><td>${esc(cycle.baselineDate || "待确认")}<br><span class="muted">${esc(cycle.baselineKind || "")}</span></td><td><button class="small" data-cycle="${esc(row.sku)}">设起点</button></td></tr>`;}).join("")}</tbody></table></div>
  <p class="muted">销量来源截至美西 ${esc(state.data.metadata.salesEnd)}，普通销售与换货、退货、退款区分。FBM 与 FBA 分列。</p></div></div>`;
}
function renderData() {
  const md=state.data.metadata;
  return `<div class="page"><div class="heading"><div><h1>数据与记录</h1><p class="muted">查看资料时点、保存记录与页面意见。</p></div><button data-action="backup" class="primary">创建本地备份</button></div>
  <div class="grid-2"><div class="panel"><h2>当前数据来源</h2><div class="detail-grid"><div><span>销量截止 · 美西</span><strong>${esc(md.salesEnd)}</strong></div><div><span>库存采集 · 北京时间</span><strong>${esc(md.inventoryCapturedLocal)}</strong></div><div><span>款式 / 变体</span><strong>${styles().length} / ${rows().length}</strong></div><div><span>快照编号</span><strong>${esc(md.snapshot_id)}</strong></div><div><span>软件版本</span><strong>${esc(md.version)}</strong></div><div><span>读写接口</span><strong>本地已连接</strong></div></div><p class="muted">当前来自现有资料副本。方案、采购和本轮起点保存在本地数据库。刷新本地数据不会抓取新的领星数据。</p><div class="link-row"><button data-action="refresh">刷新本地数据</button><button data-action="import-snapshot">导入新快照</button></div></div>
  <div class="panel"><h2>你的页面意见</h2><p>哪里看不懂、想加什么字段，都可以先记下来，之后逐项改。</p><button data-action="feedback">记录一条意见</button>${[...(state.data.feedback || [])].sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at))).slice(0,4).map(item=>`<div class="list-card"><div><strong>${esc(item.text)}</strong><p>${esc(dateText(item.created_at))}</p></div></div>`).join("")}</div></div>
  <div class="panel" style="margin-top:18px"><div class="heading"><h2>最近修改记录</h2><button data-action="audit">更新记录</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>动作</th><th>对象</th><th>记录</th></tr></thead><tbody>${state.audit.length?state.audit.slice(0,60).map(entry=>`<tr><td>${esc(dateText(entry.created_at || entry.timestamp))}</td><td>${esc(auditLabel(entry.action || entry.event || entry.type))}</td><td>${esc(entry.entity_id || entry.target_id || entry.entity || "")}</td><td class="audit-note">${esc(auditDetail(entry))}</td></tr>`).join(""):'<tr><td colspan="4">暂无显示记录，点“更新记录”读取。</td></tr>'}</tbody></table></div></div></div>`;
}
function auditLabel(value) {
  const names={"snapshot.import":"导入快照","plan.create":"新建方案","plan.update":"修改方案","plan.adopt":"采用建议","plan.confirm":"方案登记采购","purchase.confirm":"确认采购","purchase.shipped":"登记发货","purchase.received":"登记接收","purchase.reconcile":"核对库存","feedback.create":"页面意见","backup.create":"创建备份","cycle.update":"修改本轮起点"};
  return names[value] || value || "修改";
}
function auditDetail(entry) {
  const after=entry.after;
  if(after?.lines)return `${after.lines.length} 个尺码 · ${after.name || after.reference || ""}${after.revision?" · 版本 "+after.revision:""}`;
  if(after?.text)return after.text;
  if(after?.baselineKind)return `起点 ${after.baselineDate || "未确认"} · 起点累计 ${after.baseline ?? "未确认"} · ${after.baselineKind}`;
  const detail=entry.details || entry.payload || entry.detail || entry.data || after || "";
  return typeof detail==="object"?JSON.stringify(detail):detail;
}
function putPlan(value) {
  const index=state.data.plans.findIndex(item=>item.id===value.id);
  if(index>=0)state.data.plans[index]=value;else state.data.plans.push(value);
  state.planId=value.id;
}
function putPurchase(value) {
  const index=state.data.purchases.findIndex(item=>item.id===value.id);
  if(index>=0)state.data.purchases[index]=value;else state.data.purchases.push(value);
  state.purchaseId=value.id;
}
async function ensurePlan() {
  if(plan()){if(!editable())throw new Error("这份方案已经登记采购，请新建草稿调整。");return plan();}
  const previous=editKey();
  const value=await write("/api/plans","POST",{style:state.style,name:`${styleName(state.style)} · ${localToday()}`,target_days:state.days});
  putPlan(value);state.edits[value.id]=state.edits[previous] || {};delete state.edits[previous];delete state.dayEdits[previous];return value;
}
async function savePlan(silent=false) {
  if(Object.keys(state.invalid[editKey()] || {}).length){state.sku=Object.keys(state.invalid[editKey()])[0];render();throw new Error("有补货数量尚未填完整。请输入 0 或正整数；如果不补，请明确填 0。");}
  let current=await ensurePlan();
  const pending=Object.entries(edits()).map(([sku,value])=>({sku,quantity:value.quantity,manual:true,note:value.note || ""}));
  if(pending.length || current.target_days!==state.days){
    current=await write(`/api/plans/${encodeURIComponent(current.id)}`,"PUT",{revision:current.revision,target_days:state.days,lines:pending});
    putPlan(current);state.edits[current.id]={};
  }
  delete state.dayEdits[current.id];state.saved=true;
  if(!silent)notify("补货草稿已保存到本机。你的手改数量已保留。");
  render();return current;
}
async function adopt() {
  if(seasonalHold())throw new Error("这款已设为春夏再看，本轮暂不采用自动补货建议。已有手改草稿继续保留。");
  let current=await savePlan(true);
  current=await write(`/api/plans/${encodeURIComponent(current.id)}/adopt`,"POST",{revision:current.revision});
  putPlan(current);await loadRecommendations();render();notify("可用建议已带入并保存。你可以继续改数字，再保存草稿。");
}
function openModal(html) {
  $("#modal-content").innerHTML=`${html}<div id="modal-error" class="callout" role="alert" hidden></div>`;
  if(!$("#modal").open)$("#modal").showModal();
}
function closeModal() { $("#modal").close(); }
function planOptionsModal() {
  const current=plan();
  openModal(`<div class="heading"><h2 id="plan-options-title">方案详情</h2><button type="button" data-action="close-modal">关闭</button></div><p><strong>${esc(current?.name || styleName(state.style)+" · 当前草稿")}</strong> · ${sum(mergedLines(),"quantity")} 双 · ${planStatus(current)}</p>${renderCurrentCost()}<div id="moq-note">${moqNote()}</div><div class="link-row"><button type="button" data-action="new-plan">新建另一份方案</button>${current?`<a class="button" href="/api/plans/${encodeURIComponent(current.id)}/export.csv">导出已保存方案 CSV</a>`:""}<button type="button" data-page="plans">全部方案与货款</button></div><p class="muted" style="margin-top:14px">配码在尺码格子中修改和保存；导出只包含已保存数量。</p>`);
}
function newPlanModal() {
  openModal(`<h2>新建补货方案</h2><p class="muted">每次只处理一款，方便后续核对。</p><form id="new-plan-form"><div class="form-grid"><label class="full">款式<select name="style">${styles().map(item=>`<option value="${esc(item.id)}" ${item.id===state.style?"selected":""}>${esc(item.label)}</option>`).join("")}</select></label><label class="full">方案名称<input name="name" required maxlength="120" value="${esc(styleName(state.style)+" · "+localToday())}"></label><label>参考需求天数<select name="days">${[30,35,40].map(day=>`<option ${day===state.days?"selected":""}>${day}</option>`).join("")}</select></label></div><div class="form-actions"><button type="button" data-action="close-modal">取消</button><button class="primary" type="submit">新建并去配码</button></div></form>`);
}
function feedbackModal() {
  openModal(`<h2>记录页面意见</h2><p class="muted">例如：看不懂哪个数字、希望把哪个内容放在前面。</p><form id="feedback-form"><label>你的意见<textarea name="text" required maxlength="4000" placeholder="直接写你的想法…"></textarea></label><div class="form-actions"><button type="button" data-action="close-modal">取消</button><button type="submit" class="primary">保存意见</button></div></form>`);
}
function previewImageModal() {
  const path=productImagePath();if(!path)return;
  const preference=stylePreference();
  openModal(`<div class="heading"><h2>${esc(styleName(state.style))}</h2><button type="button" data-action="close-modal">关闭</button></div>${productImage(state.style,"product-image-large")}<p class="muted">款式识别图；各颜色、尺码以配码表为准。</p>${preference.image_source?`<details><summary>图片来源</summary><p class="audit-note">${esc(preference.image_source)}</p></details>`:""}`);
}
function confirmModal() {
  const current=plan();if(!current)return;
  if(current.status!=="draft"){alertError("这份方案已经登记采购，不需要再次确认。");return;}
  if(Object.keys(edits()).length){alertError("这份方案还有未保存的手改，请先回到尺码页保存，再登记采购。");return;}
  if(!sum(current.lines || [],"quantity")){alertError("方案还没有大于 0 的数量，请先填写并保存补货数量。");return;}
  const bySku=new Map(styleRows().map(row=>[row.sku,row]));
  const details=current.lines.filter(line=>line.quantity>0).map(line=>{const row=bySku.get(line.sku);return `<tr><td>${esc(row?`${row.colorLabel} · ${sizeLabel(row)}`:line.sku)}</td><td>${line.quantity} 双</td></tr>`;}).join("");
  openModal(`<h2>确认这份方案已采购</h2><p><strong>${esc(current.name)}</strong> · <strong>${sum(current.lines,"quantity")} 双</strong></p><p class="muted">核对下面数量与实际下单一致。确认后会显示在同一排的“已采购”里，发货和到仓随后再记录。</p><div class="table-wrap purchase-confirm-lines"><table class="data-table"><thead><tr><th>颜色 / 尺码</th><th>本次已采购</th></tr></thead><tbody>${details}</tbody></table></div><form id="confirm-form"><label>本批名称 / 订单号（可修改）<input name="reference" required maxlength="200" value="${esc(current.name)}"></label><details><summary>填写预计发货、到仓时间（可不填）</summary><p class="muted">留空时，等实际发货后按约 ${transitDays()} 天估算到仓。</p><div class="form-grid"><label>预计发货日期<input type="date" name="expected_ship_date"></label><label>预计可售日期<input type="date" name="expected_available_date"></label></div></details><label class="check-label"><input type="checkbox" required name="confirmed">我已实际下单，以上颜色、尺码和数量正确</label><div class="form-actions"><button type="button" data-action="close-modal">返回修改</button><button type="submit" class="primary">确认已采购</button></div></form>`);
}
function cancelPurchaseModal(id) {
  const item=state.data.purchases.find(item=>item.id===id);if(!item||item.status==="cancelled")return;
  openModal(`<h2>取消这批采购登记</h2><p><strong>${esc(batchName(item))}</strong> · ${sum(item.lines,"ordered")} 双</p><p>取消后移入本页历史，不再作为待发采购提醒；配码及领星货件凭据保留。</p><p class="muted">这里只取消软件里的登记。厂家订单、系统在途和FBA可售不会改变。</p><form id="cancel-purchase-form" data-purchase-id="${esc(id)}"><label>取消说明（可修改）<input name="note" required value="取消本批采购登记"></label><label class="check-label"><input type="checkbox" required>确认取消这一批，其他采购批次保留</label><div class="form-actions"><button type="button" data-action="close-modal">返回</button><button type="submit" class="primary">确认取消登记</button></div></form>`);
}
function eventModal() {
  const item=purchase();if(!item)return;
  state.eventId=crypto.randomUUID();
  openModal(`<h2>登记本次实际进度</h2><p>${esc(item.reference)}</p><form id="event-form"><div class="form-grid"><label>本次记录<select name="type" id="event-type"><option value="shipped">实际发货</option><option value="received">实际接收</option></select></label><label>实际发生日期<input type="date" name="date" required max="${localToday()}" value="${localToday()}"></label></div><p class="muted" style="margin-top:12px">只填本次新增数量。留空的尺码不登记。</p><div class="table-wrap"><table class="data-table"><thead><tr><th>颜色 / 尺码</th><th>已购 / 已发 / 已收</th><th>本次双数</th></tr></thead><tbody>${item.lines.map(line=>{const row=rows().find(row=>row.sku===line.sku);return `<tr><td>${esc(row?`${row.colorLabel} ${sizeLabel(row)}`:line.sku)}</td><td>${line.ordered} / ${line.shipped} / ${line.received}</td><td><input type="number" min="0" max="${line.ordered-line.shipped}" step="1" data-event-sku="${esc(line.sku)}" aria-label="${esc(line.sku)} 本次数量" placeholder="留空"></td></tr>`;}).join("")}</tbody></table></div><div class="form-actions"><button type="button" data-action="close-modal">取消</button><button class="primary" type="submit">保存本次实际记录</button></div></form>`);
}
function reconcileModal() {
  const item=purchase();if(!item)return;
  openModal(`<h2>核对已体现在系统库存</h2><p>${esc(item.reference)}</p><div class="callout">当前库存快照：${esc(state.data.metadata.inventoryCapturedLocal)}。需要接收后的新快照，并确认这批货已包含其中，才能解除本批的待核提醒。</div><form id="reconcile-form"><label>核对依据<textarea name="note" required maxlength="2000" placeholder="填写核对过的来源、日期和数量"></textarea></label><label class="check-label"><input type="checkbox" required name="confirmed">我确认本批已包含在当前系统库存快照中</label><p class="muted">此操作只关联批次与快照，不加减库存。</p><div class="form-actions"><button type="button" data-action="close-modal">返回</button><button class="primary" type="submit">保存核对记录</button></div></form>`);
}
function importModal() {
  openModal(`<h2>导入新的销量与库存快照</h2><p>选择为本软件准备的完整快照文件。导入前会自动创建一份本地备份。</p><p class="muted">可以在聊天里让我准备并导入。现有方案、采购、手改和本轮起点会继续保留；不会重复累计销量或加库存。</p><form id="import-form"><label>快照文件（JSON）<input type="file" id="snapshot-file" accept=".json,application/json" required></label><div class="form-actions"><button type="button" data-action="close-modal">取消</button><button type="submit" class="primary">备份并导入</button></div></form>`);
}
function cycleModal(sku) {
  const row=rows().find(row=>row.sku===sku);if(!row)return;const cycle=cycleFor(row);
  openModal(`<h2>设置本轮观察起点</h2><p>${esc(row.styleLabel)} · ${esc(row.colorLabel)} ${esc(sizeLabel(row))}</p><p class="muted">这里只保存观察起点。累计售出 ${show(cycle.currentSold)} 双继续保留；未确认可保持为空。</p><form id="cycle-form" data-sku="${esc(sku)}"><div class="form-grid"><label>起点日期<input type="date" name="baselineDate" max="${localToday()}" value="${esc(cycle.baselineDate || "")}"></label><label>起点时累计售出<input type="number" min="0" step="1" name="baseline" value="${number(cycle.baseline)?cycle.baseline:""}" placeholder="未确认"></label><label class="full">依据 / 估算说明<input name="baselineKind" maxlength="500" value="${esc(cycle.baselineKind || "")}" placeholder="例如：估算日期，或核对的到货记录"></label></div><div class="form-actions"><button type="button" data-action="close-modal">取消</button><button type="submit" class="primary">保存观察起点</button></div></form>`);
}
async function fetchAudit() {
  const data=await api("/api/audit");state.audit=data.entries || [];if(state.page==="data")render();
}
document.addEventListener("click",event=>{
  if(!event.target.closest(".nav-more"))document.querySelector(".nav-more[open]")?.removeAttribute("open");
  const button=event.target.closest("button,[data-page]");if(!button || state.busy)return;
  if(button.dataset.page){if($("#modal").open)closeModal();state.page=button.dataset.page;render();if(state.page==="data")action(fetchAudit);return;}
  if(button.dataset.style){action(()=>selectStyle(button.dataset.style));return;}
  if(button.dataset.days){action(()=>changeDays(button.dataset.days));return;}
  if(button.dataset.view){state.view=button.dataset.view;pickPurchasedSku();render();return;}
  if(button.dataset.cancelPurchase){cancelPurchaseModal(button.dataset.cancelPurchase);return;}
  if(button.dataset.sku){state.sku=button.dataset.sku;render();$("#sku-advice")?.scrollIntoView({block:"nearest",behavior:"auto"});return;}
  if(button.dataset.gotoSku){const row=rows().find(row=>row.sku===button.dataset.gotoSku);action(async()=>{await selectStyle(row.style);state.sku=row.sku;state.page="matrix";render();});return;}
  if(button.dataset.plan){action(async()=>{const selected=state.data.plans.find(item=>item.id===button.dataset.plan);state.planId=selected.id;state.style=selected.style;state.days=state.dayEdits[selected.id] || selected.target_days;state.sku=null;await loadRecommendations();render();});return;}
  if(button.dataset.purchase){state.purchaseId=button.dataset.purchase;render();return;}
  if(button.dataset.cycle){cycleModal(button.dataset.cycle);return;}
  const type=button.dataset.action;
  if(type==="dismiss-error"){$("#error").hidden=true;return;}
  if(type==="close-modal"){closeModal();return;}
  if(type==="new-plan"){newPlanModal();return;}
  if(type==="plan-options"){planOptionsModal();return;}
  if(type==="new-purchase-batch"){newPlanModal();$("#new-plan-form [name='name']").value=`${styleName(state.style)} · 第 ${allStylePurchases().length+1} 批采购`;return;}
  if(type==="feedback"){feedbackModal();return;}
  if(type==="preview-image"){previewImageModal();return;}
  if(type==="confirm-purchase"){action(async()=>{if(!editable())throw new Error("这份方案已经登记采购，不需要再次确认。");if(!sum(mergedLines(),"quantity"))throw new Error("请先填写大于0的采购数量。");await savePlan(true);confirmModal();});return;}
  if(type==="purchase-event"){eventModal();return;}
  if(type==="reconcile"){reconcileModal();return;}
  if(type==="import-snapshot"){importModal();return;}
  if(type==="fill-selected"){
    const result=resultFor(state.sku);if(!editable()||!number(result?.quantity)||seasonalHold())return;
    if(chosen(state.sku)?.manual){notify("已保留你手改的数量，可直接在输入框继续修改。");return;}
    edits()[state.sku]={quantity:result.quantity,manual:true,note:result.reason || ""};
    delete (state.invalid[editKey()] || {})[state.sku];render();return;
  }
  if(type==="adopt"){action(adopt);return;}
  if(type==="save-plan"){action(()=>savePlan());return;}
  if(type==="save-price"){action(async()=>{
    const preference=stylePreference();const tiers=priceTiers(state.style,true);
    const payload={revision:preference.revision,purchase_price_note:`你在补货软件中手动填写的${tiers.length?"分尺码":"参考"}采购价${preference.purchase_price_includes_domestic_freight?"；已含国内运费，不再另加":"；费用包含范围待按报价核对"}`,purchase_price_date:localToday()};
    if(tiers.length){payload.purchase_price_tiers=tiers.map(t=>{const cents=priceCents(t.unit_cny);if(t.unit_cny!==""&&t.unit_cny!==null&&cents===null)throw new Error("采购单价请填0至100000元，最多两位小数。");return {...t,unit_cny:cents===null?null:cents/100};});}
    else {const value=priceValue();const cents=priceCents(value);if(value!==""&&cents===null)throw new Error("采购单价请填0至100000元，最多两位小数。");payload.purchase_unit_cny=value===""?null:cents/100;}
    const result=await write(`/api/styles/${encodeURIComponent(state.style)}`,"PUT",payload);
    state.data.style_preferences[state.style]=result;delete state.priceEdits[state.style];delete state.tierPriceEdits[state.style];render();if($("#modal").open&&$("#plan-options-title"))planOptionsModal();notify("采购单价已保存，金额按各尺码数量自动核算。");
  });return;}
  if(type==="audit"){action(fetchAudit);return;}
  if(type==="backup"){action(async()=>{const data=await write("/api/backup","POST",{});notify("本地备份已创建："+(data.path || data.filename || ""));await fetchAudit();});return;}
  if(type==="refresh"){action(async()=>{await bootstrap();notify("已读取本地最新保存内容，未存手改仍保留；业务来源快照日期见顶部。");});}
});
document.addEventListener("keydown",event=>{
  if(event.key==="Escape"&&!$("#modal").open){const menu=document.querySelector(".nav-more[open]");if(menu){menu.removeAttribute("open");menu.querySelector("summary").focus();}}
});
document.addEventListener("input",event=>{
  const input=event.target;
  if(input.dataset.priceTier!==undefined){const edits=state.tierPriceEdits[state.style] || (state.tierPriceEdits[state.style]={});edits[input.dataset.priceTier]=input.value;document.querySelectorAll("[data-current-cost]").forEach(el=>el.textContent=currentCostText());updateSaveState();return;}
  if(input.dataset.purchasePrice){state.priceEdits[input.dataset.purchasePrice]=input.value;document.querySelectorAll("[data-current-cost]").forEach(el=>el.textContent=currentCostText());updateSaveState();return;}
  if(input.id==="style-search"){
    state.search=input.value;document.querySelectorAll(".style-button").forEach(button=>{button.hidden=!button.dataset.search.includes(input.value.toLowerCase());});return;
  }
  if(input.dataset.quantity){
    if(!editable())return;
    const invalid=state.invalid[editKey()] || (state.invalid[editKey()]={});
    if(input.value===""){invalid[input.dataset.quantity]="";input.setCustomValidity("如不补货请填 0；留空尚未保存。");updatePlanGrid(input.dataset.quantity,null,false);document.querySelectorAll("[data-current-cost]").forEach(el=>el.textContent=currentCostText());updateSaveState();return;}
    const quantity=Number(input.value);
    if(!Number.isInteger(quantity)||quantity<0){invalid[input.dataset.quantity]=input.value;input.setCustomValidity("请输入 0 或正整数。");input.reportValidity();updatePlanGrid(input.dataset.quantity,null,false);document.querySelectorAll("[data-current-cost]").forEach(el=>el.textContent=currentCostText());updateSaveState();return;}
    delete invalid[input.dataset.quantity];
    input.setCustomValidity("");edits()[input.dataset.quantity]={quantity,manual:true,note:chosen(input.dataset.quantity)?.note || ""};
    updatePlanGrid(input.dataset.quantity,quantity);
    if(input.dataset.quantity===state.sku&&$('[data-action="fill-selected"]'))$('[data-action="fill-selected"]').disabled=true;
    document.querySelectorAll("[data-quantity]").forEach(other=>{if(other!==input&&other.dataset.quantity===input.dataset.quantity)other.value=quantity;});
    if($("#plan-total"))$("#plan-total").textContent=sum(mergedLines(),"quantity");
    document.querySelectorAll("[data-draft-total]").forEach(element=>element.textContent=sum(mergedLines(),"quantity"));
    if($("#moq-note"))$("#moq-note").innerHTML=moqNote();
    document.querySelectorAll("[data-current-cost]").forEach(el=>el.textContent=currentCostText());
    updateSaveState();
  }
});
document.addEventListener("change",event=>{
  if(event.target.id==="purchase-batch-filter"){state.purchaseBatchId=event.target.value;pickPurchasedSku();render();return;}
  if(event.target.dataset.action==="select-style")action(()=>selectStyle(event.target.value));
  if(event.target.id==="event-type"){
    const item=purchase();document.querySelectorAll("[data-event-sku]").forEach(input=>{
      const line=item.lines.find(line=>line.sku===input.dataset.eventSku);
      input.max=event.target.value==="shipped"?line.ordered-line.shipped:line.shipped-line.received;
    });
  }
});
document.addEventListener("submit",event=>{
  const form=event.target;if(!form.closest("#modal"))return;event.preventDefault();
  const values=Object.fromEntries(new FormData(form));
  action(async()=>{
    if(form.id==="new-plan-form"){
      if(!plan()&&(Object.keys(edits()).length||Object.keys(state.invalid[editKey()] || {}).length))await savePlan(true);
      const result=await write("/api/plans","POST",{style:values.style,name:values.name.trim(),target_days:Number(values.days)});
      state.style=result.style;state.days=result.target_days;putPlan(result);state.sku=null;state.page="matrix";state.view="plan";closeModal();await loadRecommendations();render();notify("新草稿已保存。现在可以采用建议并调整配码。");
    }else if(form.id==="feedback-form"){
      const result=await write("/api/feedback","POST",{text:values.text.trim(),context:{page:state.page,style:state.style,plan_id:state.planId}});
      state.data.feedback.push(result);closeModal();render();notify("意见已保存在本机，后续可据此修改。");
    }else if(form.id==="cancel-purchase-form"){
      const item=state.data.purchases.find(item=>item.id===form.dataset.purchaseId);
      await write(`/api/purchases/${encodeURIComponent(item.id)}/cancel`,"POST",{revision:item.revision,confirmed:true,note:values.note.trim()});
      closeModal();state.purchaseBatchId="all";state.page="matrix";state.view="purchased";await bootstrap();notify("这批采购登记已取消，记录已移入历史；其他批次保留。");
    }else if(form.id==="confirm-form"){
      const current=plan();const body={revision:current.revision,confirmed:true,reference:values.reference.trim()};
      for(const field of ["expected_ship_date","expected_available_date"])if(values[field])body[field]=values[field];
      const result=await write(`/api/plans/${encodeURIComponent(current.id)}/confirm`,"POST",body);
      putPurchase(result);closeModal();state.page="matrix";state.view="purchased";state.purchaseBatchId=result.id;state.sku=result.lines[0]?.sku || null;await bootstrap();notify("已确认采购，已切换到“已采购”格子；发货由领星逐批核实。");
    }else if(form.id==="event-form"){
      const item=purchase();const lines=[...form.querySelectorAll("[data-event-sku]")].filter(input=>input.value!==""&&Number(input.value)>0).map(input=>({sku:input.dataset.eventSku,quantity:Number(input.value)}));
      if(!lines.length)throw new Error("请至少填写一个尺码本次实际发生的数量。");
      const result=await write(`/api/purchases/${encodeURIComponent(item.id)}/events`,"POST",{revision:item.revision,event_id:state.eventId,type:values.type,date:values.date,lines});
      putPurchase(result);state.eventId=null;closeModal();await loadRecommendations();render();notify("本次实际记录已保存；系统库存保持按来源快照显示。");
    }else if(form.id==="reconcile-form"){
      const item=purchase();const result=await write(`/api/purchases/${encodeURIComponent(item.id)}/reconcile`,"POST",{revision:item.revision,snapshot_id:state.data.metadata.snapshot_id,confirmed:true,note:values.note.trim()});
      putPurchase(result);closeModal();await loadRecommendations();render();notify("批次与当前快照的核对记录已保存。");
    }else if(form.id==="cycle-form"){
      const sku=form.dataset.sku;const row=rows().find(row=>row.sku===sku);const cycle=cycleFor(row);
      const baseline=values.baseline===""?null:Number(values.baseline);
      if((baseline===null)!==(!values.baselineDate))throw new Error("观察起点日期和起点累计需要一起填写，或一起留空。");
      const result=await write(`/api/cycles/${encodeURIComponent(sku)}`,"PUT",{revision:cycle.revision,baselineDate:values.baselineDate || null,baseline,baselineKind:values.baselineKind.trim() || null});
      state.data.cycles[sku]={...cycle,...result,currentSold:cycle.currentSold,roundSold:number(result.baseline)&&number(cycle.currentSold)?cycle.currentSold-result.baseline:null};closeModal();render();notify("本轮起点已保存，累计售出继续保留。");
    }else if(form.id==="import-form"){
      const file=$("#snapshot-file").files[0];if(!file)throw new Error("请选择快照文件。");
      let document;try{document=JSON.parse(await file.text());}catch{throw new Error("这个文件不是可读取的快照 JSON，请检查文件。");}
      await write("/api/backup","POST",{});
      const result=await write("/api/snapshots","POST",{document});
      closeModal();await bootstrap();await fetchAudit();notify(result.imported?"新快照已导入，原有方案、采购和本轮起点已保留。":"相同快照已经导入，没有重复写入。");
    }
  });
});
document.addEventListener("error",event=>{const image=event.target;if(!image.matches?.("img[data-product-image]"))return;image.hidden=true;const fallback=image.parentElement.querySelector(".image-fallback");if(fallback)fallback.hidden=false;},true);
window.addEventListener("beforeunload",event=>{if(dirty()){event.preventDefault();event.returnValue="";}});
bootstrap(true).catch(error=>{$("#connection").textContent="连接未完成";$("#connection").className="tag amber";$("#app").innerHTML='<div class="empty"><strong>暂时无法读取本地数据</strong>请确认本地服务正在运行，然后点击“刷新本地数据”。</div>';alertError(error);});
