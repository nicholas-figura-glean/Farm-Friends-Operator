/* Coop Rush balance tests - run headlessly, no browser required:
 *     deploy/test_game.sh          (both suites)
 *     osascript -l JavaScript game/test_balance.js   (this one, from the project root)
 *
 * There is no npm in this project and no browser automation available, so the
 * engine is loaded into JavaScriptCore with a stubbed localStorage and driven
 * directly. The engine is deliberately DOM-free so this is possible: every
 * mechanic below is the real code path the page runs.
 */
ObjC.import('Foundation');
function slurp(p) { return $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, null).js; }
var src = slurp("game/coop_rush.js");
var store = {};
var localStorage = { getItem: function(k){ return k in store ? store[k] : null; }, setItem: function(k,v){ store[k]=v; } };
var CR = new Function("localStorage", src + "\n;return CR;")(localStorage);
function fmt(n){ if(!isFinite(n))return "inf"; var u=["","K","M","B","T","Qa","Qi"],t=0; while(n>=1000&&t<u.length-1){n/=1000;t++;} return n.toFixed(2)+u[t]; }

var out = [];
function shop() {
  var st = CR.state(), acted = false;
  for (var i=0;i<CR.PRODUCERS.length;i++){ var d=CR.PRODUCERS[i],p=st.producers[d.id];
    if (p.owned>0 && !p.manager && st.coins>=CR.managerCost(d)) { CR.hireManager(d.id); acted=true; } }
  for (var j=CR.ONEOFF.length-1;j>=0;j--){ var u=CR.ONEOFF[j];
    if (!st.oneoff[u.id] && st.coins>=u.cost*3) { CR.buyOneoff(u.id); acted=true; } }
  var pick=null, best=0;
  for (var k=0;k<CR.PRODUCERS.length;k++){ var def=CR.PRODUCERS[k], cost=CR.costFor(def,1);
    if (st.coins<cost) continue;
    var per=(CR.unitsPerCycle(def)||def.yield*CR.globalMultiplier())/CR.cycleTime(def);
    var each=per/Math.max(1,st.producers[def.id].owned);
    if (each/cost>best){best=each/cost;pick=def;} }
  if (pick){ CR.buy(pick.id,1); acted=true; }
  return acted;
}
function perks() {
  var st = CR.state();
  for (var i=0;i<CR.HEIRLOOM_UPGRADES.length;i++){ var u=CR.HEIRLOOM_UPGRADES[i];
    // spend only when it leaves most of the bonus intact: AdCap's own guidance is
    // that an upgrade costing under ~10% of your angels is nearly always worth it
    if (!st.perks[u.id] && st.heirlooms >= u.cost*4) CR.buyPerk(u.id); }
}
var DT=0.5, t=0, rebuilds=[], nextLog=0;
var heirloomsAtLastRebuild = 0;
while (t < 6*3600) {
  CR.tick(DT); t+=DT;
  var guard=0; while(shop() && guard++<80){}
  // click whenever idle income is zero and nothing is affordable - what a player does
  var cp = CR.state().producers.coop;
  if (!cp.manager && cp.progress === 0 && CR.state().coins < CR.costFor(CR.PRODUCERS[0],1)) CR.collect("coop");
  perks();
  // Rebuild when the run would at least double total heirlooms (the standard rule).
  var gain = CR.prestigeGain(), have = CR.state().heirlooms + CR.state().heirloomsSpent;
  if (gain > 0 && (have === 0 || gain >= have)) {
    var got = CR.rebuild();
    rebuilds.push({ t: t, gain: got, total: CR.state().heirlooms + CR.state().heirloomsSpent });
  }
  if (t >= nextLog) {
    var st = CR.state();
    out.push("  t+" + (t/60).toFixed(0).padStart(3) + "m  produce " + fmt(st.lifetime).padStart(9)
      + "  " + fmt(CR.unitsPerSec()).padStart(9) + "/s  mult x" + fmt(CR.globalMultiplier()).padStart(8)
      + "  heirlooms " + String(st.heirlooms).padStart(6)
      + "  managers " + CR.PRODUCERS.filter(function(d){return st.producers[d.id].manager;}).length + "/7"
      + "  rebuilds " + st.rebuilds);
    nextLog += 1800;
  }
}
out.push("");
out.push("rebuilds: " + rebuilds.length + " -> " + rebuilds.map(function(r){ return (r.t/60).toFixed(0)+"m:+"+r.gain; }).join(", "));
var st = CR.state();
out.push("perks bought: " + Object.keys(st.perks).join(", "));
out.push("one-off bought this run: " + Object.keys(st.oneoff).length + "/" + CR.ONEOFF.length);
out.push("producers owned: " + CR.PRODUCERS.map(function(d){ return d.id+"="+st.producers[d.id].owned; }).join(" "));
out.push("all-time produce: " + fmt(st.allTime) + " | best run " + fmt(st.bestRun));
out.join("\n");
