/* Pure, provisional inventory-gap rules. No purchasing or source-file writes. */
const ShoeRecommendation = (() => {
  const isNumber = value => typeof value === "number" && Number.isFinite(value);
  const supplyFields = [
    ["afn_inbound_shipped_quantity", "系统在途"],
    ["afn_inbound_receiving_quantity", "接收中"],
    ["afn_inbound_working_quantity", "计划中"],
    ["reserved_fc_transfers", "调仓"],
    ["reserved_fc_processing", "仓内处理中"]
  ];
  function recommend(row, days=35) {
    const inv=row.inventory || {};
    const stock=inv.afn_fulfillable_quantity;
    const base={quantity:null,gap:null,target:null,daily:null,lowSample:false,supplies:[]};
    if(!row.inventoryKnown || !isNumber(stock) || stock < 0)
      return {...base,status:"unknown",label:"待核",reason:"系统库存缺失，先核对库存；这个尺码不自动带入数量。"};
    if(!isNumber(row.n14) || !isNumber(row.n28) || row.n14<0 || row.n28<0)
      return {...base,status:"unknown",label:"待核",reason:"销量资料不完整，先补齐数据。"};
    const daily=Math.max(row.n14/14,row.n28/28);
    if(daily===0)
      return {...base,status:"observe",label:"观察",daily,reason:"近期没有普通销售记录，先观察；也可能受缺货影响，不直接判定不用补。"};
    const target=Math.ceil(daily*days-1e-9);
    const gap=Math.max(0,target-stock);
    const supplies=supplyFields.filter(([key])=>isNumber(inv[key])&&inv[key]>0).map(([key,label])=>({label,quantity:inv[key]}));
    const calc={...base,gap,target,daily,lowSample:row.n28<5,supplies};
    if(gap===0)
      return {...calc,quantity:0,status:"covered",label:"0",reason:`按当前销量，现货已覆盖这${days}天的参考需求，暂不追加。补货等待期需另算。`};
    if(supplies.length)
      return {...calc,status:"supply",label:"先跟货",reason:`现货相对${days}天需求约差${gap}双，但已有${supplies.map(s=>s.label+s.quantity+"双").join("、")}。先核这些货何时可用，再决定新补多少；不把缺口直接当采购数。`};
    const unknownSupply=supplyFields.some(([key])=>!isNumber(inv[key]));
    if(unknownSupply)
      return {...calc,status:"unknown",label:"待核",reason:`现货约差${gap}双，但已有供应数据不完整，先核对再带入。`};
    let quantity=gap;
    const multiple=Number.isInteger(row.orderMultiple)&&row.orderMultiple>=1&&row.orderMultiple<=1000000?row.orderMultiple:1;
    quantity=Math.ceil(quantity/multiple)*multiple;
    const rounding=quantity!==gap?`，按该款每变体${multiple}双倍数取${quantity}双`:"";
    return {...calc,quantity,status:"suggest",label:String(quantity),reason:`按近期销量参考${days}天需求约${target}双，现货${stock}双，初步可考虑补${gap}双${rounding}。${row.n28<5?"销量样本少，先作为小量配码参考。":""}尚未计入等新货期间的消耗。`};
  }
  function summarize(rows,days=35) {
    const evaluated=rows.map(row=>({row,result:recommend(row,days)}));
    return {
      evaluated,
      quantity:evaluated.reduce((n,{result})=>n+(result.quantity||0),0),
      suggestedCount:evaluated.filter(({result})=>result.status==="suggest").length,
      pendingSupply:evaluated.filter(({result})=>result.status==="supply").length,
      unknown:evaluated.filter(({result})=>result.status==="unknown").length,
      observe:evaluated.filter(({result})=>result.status==="observe").length
    };
  }
  function adopt(rows,days,existing) {
    const result={...existing};
    for(const row of rows) {
      const suggested=recommend(row,days);
      if(suggested.status==="suggest" && !Object.prototype.hasOwnProperty.call(result,row.sku))
        result[row.sku]=suggested.quantity;
    }
    return result;
  }
  return {recommend,summarize,adopt};
})();
if(typeof module !== "undefined")module.exports=ShoeRecommendation;
