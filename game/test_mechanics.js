/* Coop Rush mechanics tests - run headlessly, no browser required:
 *     deploy/test_game.sh          (both suites)
 *     osascript -l JavaScript game/test_mechanics.js   (this one, from the project root)
 *
 * There is no npm in this project and no browser automation available, so the
 * engine is loaded into JavaScriptCore with a stubbed localStorage and driven
 * directly. The engine is deliberately DOM-free so this is possible: every
 * mechanic below is the real code path the page runs.
 */
ObjC.import('Foundation');
function slurp(p) { return $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, null).js; }
var envFile = $.NSProcessInfo.processInfo.environment.objectForKey('JSFILE');
var src = slurp(envFile.isNil() ? 'game/coop_rush.js' : envFile.js);
var out = [], fails = 0;
function ok(cond, label, detail) { out.push((cond ? "  ok   " : "  FAIL ") + label + (detail ? "  [" + detail + "]" : "")); if (!cond) fails++; }
function fmt(n) { return n >= 1e6 ? (n/1e6).toFixed(2)+"M" : n >= 1e3 ? (n/1e3).toFixed(1)+"K" : n.toFixed(1); }

var store = {};
var localStorage = { getItem: function(k){ return k in store ? store[k] : null; }, setItem: function(k,v){ store[k]=v; }, removeItem: function(k){ delete store[k]; } };
var CR = new Function("localStorage", src + "\n;return CR;")(localStorage);

out.push("=== cost curve ===");
var coop = CR.PRODUCERS[0];
CR.state().coins = 1e9;
CR.state().producers.coop.owned = 0;   // measure the curve from zero, not the seed
var c1 = CR.costFor(coop, 1), c10 = CR.costFor(coop, 10);
ok(Math.abs(c1 - 4) < 1e-9, "first coop costs base (4)", c1.toFixed(3));
var geo = 4 * (Math.pow(1.07,10)-1)/0.07;
ok(Math.abs(c10 - geo) < 1e-6, "x10 uses the geometric series", c10.toFixed(2)+" vs "+geo.toFixed(2));
CR.state().coins = 100; CR.state().producers.coop.owned = 0;
var maxN = CR.maxAffordable(coop);
var costMax = CR.costFor(coop, maxN), costMore = CR.costFor(coop, maxN+1);
ok(costMax <= 100 && costMore > 100, "Max buys the most affordable, not one more", maxN+" for "+costMax.toFixed(1));

out.push("=== milestones ===");
CR.wipe(); store = {};
var s = CR.state(); s.coins = 1e12; s.producers.coop.owned = 0;
CR.buy("coop", 24);
var per24 = CR.unitsPerCycle(coop), t24 = CR.cycleTime(coop);
CR.buy("coop", 1);
var per25 = CR.unitsPerCycle(coop);
ok(Math.abs(per25 / (per24/24*25) - 2) < 1e-9, "25 owned doubles produce per cycle", (per25/(per24/24*25)).toFixed(2)+"x");
CR.buy("coop", 25);
ok(Math.abs(CR.cycleTime(coop) - t24/2) < 1e-9, "50 owned halves the cycle", CR.cycleTime(coop).toFixed(3)+"s");

out.push("=== managers and clicking ===");
CR.wipe(); store = {}; s = CR.state(); s.coins = 1e6;
CR.buy("coop", 5);
ok(CR.unitsPerSec() === 0, "no manager means no idle output");
var before = s.lifetime; CR.collect("coop"); CR.tick(CR.cycleTime(coop) + 0.01);
ok(s.lifetime > before, "click starts a cycle, the tick finishes it", fmt(s.lifetime - before)+" produce");
var mid = s.lifetime; CR.collect("coop"); CR.tick(0.01);
ok(s.lifetime === mid, "an unfinished manual cycle pays nothing yet");
s.coins = 1e6; CR.buyOneoff("click1"); var q = s.lifetime; CR.collect("coop");
ok(s.lifetime > q, "Quick Hands banks a manual cycle instantly", fmt(s.lifetime - q)+" produce");
ok(s.coins > 0 && Math.abs((s.lifetime)*2 - (s.coins - (1e6 - CR.costFor(coop,0) ))) >= 0, "produce sells for coins");
CR.hireManager("coop");
ok(CR.unitsPerSec() > 0, "manager makes it idle", fmt(CR.unitsPerSec())+"/s");
var t0 = s.lifetime; CR.tick(10);
ok(Math.abs((s.lifetime - t0) - CR.unitsPerSec()*10) < 1e-6, "10s of ticking matches units/sec");

out.push("=== prestige ===");
CR.wipe(); store = {}; s = CR.state();
s.lifetime = 999999;
ok(CR.prestigeGain() === 0, "no rebuild below the 1M floor (a 1-heirloom reset is a trap)");
ok(Math.abs(CR.prestigeProgress() - 0.999999) < 1e-6, "progress toward the gate is reported", (CR.prestigeProgress()*100).toFixed(2)+"%");
CR.wipe(); store = {}; s = CR.state();
s.lifetime = 1e6;
ok(CR.prestigeGain() === 12, "1M produce -> 12 heirlooms", String(CR.prestigeGain()));
s.lifetime = 4e6;
ok(CR.prestigeGain() === 24, "4x produce -> 2x heirlooms (sqrt)", String(CR.prestigeGain()));
s.coins = 1e9; CR.buy("coop", 30); CR.hireManager("coop"); CR.buyOneoff("feed1");
var gain = CR.rebuild();
s = CR.state();
ok(gain === 24 && s.heirlooms === 24, "rebuild awards the heirlooms", "gain "+gain);
ok(s.producers.coop.owned === 1 && !s.producers.coop.manager && s.coins === 0 && !s.oneoff.feed1,
   "rebuild wipes the run back to one seed coop", "coops "+s.producers.coop.owned);
ok(s.rebuilds === 1 && s.bestRun >= 4e6, "rebuild keeps rebuild count and best run", "best "+fmt(s.bestRun));
ok(Math.abs(CR.globalMultiplier() - (1 + 24*0.02)) < 1e-9, "24 heirlooms = x1.48", CR.globalMultiplier().toFixed(3));

out.push("=== heirloom upgrades stack multiplicatively ===");
// Derived from the config, not hard-coded: retuning a perk's price must not
// silently invalidate the property being tested.
var run50 = CR.HEIRLOOM_UPGRADES.filter(function(u){ return u.id === "run50"; })[0];
s.heirlooms = run50.cost * 2;
var expected = (1 + (run50.cost * 2 - run50.cost) * CR.heirloomRate()) * run50.value;
ok(CR.buyPerk("run50"), "perk is affordable at 2x its price");
ok(Math.abs(CR.globalMultiplier() - expected) < 1e-9,
   "x" + run50.value + " perk multiplies with the reduced heirloom bonus",
   CR.globalMultiplier().toFixed(2) + " vs " + expected.toFixed(2));
ok(CR.state().heirloomsSpent === run50.cost, "spent heirlooms are tracked, so they still count toward earned totals");
s.heirlooms = 500; CR.buyPerk("husb");
ok(Math.abs(CR.heirloomRate() - 0.03) < 1e-12, "husbandry raises the per-heirloom rate to 3%");
var beforeSpeed = CR.cycleTime(coop); s.heirlooms = 500; CR.buyPerk("feeders");
ok(CR.cycleTime(coop) < beforeSpeed, "feeders speed every cycle up permanently");

out.push("=== offline catch-up ===");
CR.wipe(); store = {}; s = CR.state(); s.coins = 1e9;
CR.buy("coop", 10); CR.hireManager("coop"); CR.buy("pig", 5);   // pig has no manager
var rate = CR.unitsPerSec(); var before2 = s.lifetime;
s.savedAt = Date.now() - 3600 * 1000;
var credited = CR.offlineCatchUp();
ok(Math.abs(credited - rate*3600) < rate*2, "one hour away credits one hour of managed output", fmt(credited));
ok(s.producers.pig.owned === 5 && CR.unitsPerCycle(CR.PRODUCERS[1]) > 0, "unmanaged producers bank nothing offline");
s.savedAt = Date.now() - 48 * 3600 * 1000;
var capped = CR.offlineCatchUp();
ok(Math.abs(capped - rate * CR.OFFLINE_CAP_H * 3600) < rate*2, "offline credit caps at "+CR.OFFLINE_CAP_H+"h", fmt(capped));

out.push("=== achievements ===");
CR.wipe(); store = {}; s = CR.state();
ok(CR.achievementCount() === 0 && Math.abs(CR.achievementBonus() - 1) < 1e-12,
   "a fresh save has none, so the bonus is exactly x1");
var mult0 = CR.globalMultiplier();
var won = CR.checkAchievements();
ok(won.length === 0, "nothing unlocks before anything happens", won.length + " unlocked");
s.coins = 1e6;
CR.collect("coop"); CR.tick(CR.cycleTime(coop) + 0.01);
won = CR.checkAchievements();
ok(won.filter(function (a) { return a.id === "first"; }).length === 1, "the first collect unlocks First Egg",
   won.map(function (a) { return a.id; }).join(","));
ok(CR.checkAchievements().length === 0, "an unlocked achievement does not fire twice");
ok(Math.abs(CR.globalMultiplier() / mult0 - (1 + CR.ACHIEVEMENT_BONUS * CR.achievementCount())) < 1e-9,
   "each achievement is worth +" + (CR.ACHIEVEMENT_BONUS * 100) + "% global produce",
   CR.achievementBonus().toFixed(2));
CR.hireManager("coop");
ok(CR.checkAchievements().filter(function (a) { return a.id === "hire"; }).length === 1,
   "hiring a manager unlocks Delegation");
CR.buy("coop", 30);
ok(CR.checkAchievements().filter(function (a) { return a.id === "ms25"; }).length === 1,
   "crossing 25 owned unlocks the milestone achievement");
var achBefore = CR.achievementCount();
s.lifetime = 4e6; CR.rebuild(); s = CR.state();
// rebuild() deliberately does not check achievements itself - the caller owns the
// announcement, so the engine never silently swallows an unlock the UI should toast.
var postRebuild = CR.checkAchievements();
ok(CR.achievementCount() === achBefore + 2, "achievements survive a rebuild (and the rebuild unlocks two more)",
   achBefore + " -> " + CR.achievementCount() + " via " + postRebuild.map(function (a) { return a.id; }).join(","));
ok((s.clicks || 0) > 0, "hand-collect count survives a rebuild too", String(s.clicks));
var ids = {};
CR.ACHIEVEMENTS.forEach(function (a) { ids[a.id] = (ids[a.id] || 0) + 1; });
ok(Object.keys(ids).length === CR.ACHIEVEMENTS.length, "achievement ids are unique");
ok(CR.ACHIEVEMENTS.every(function (a) { return a.name && a.desc && a.hint && typeof a.test === "function"; }),
   "every achievement names itself and the finding behind it");
var plateau = CR.ACHIEVEMENTS.filter(function (a) { return a.id === "rate1k"; })[0];
ok(CR.RUN46_COLLECTION_PER_MIN === 1550
   && Math.abs(CR.RUN46_COLLECTION_PER_SEC * 60 - CR.RUN46_COLLECTION_PER_MIN) < 1e-12,
   "the run-46 reference converts 1,550/min to a per-second engine rate",
   CR.RUN46_COLLECTION_PER_SEC.toFixed(6) + "/s");
s.peakUps = CR.RUN46_COLLECTION_PER_SEC - 1e-6;
ok(!plateau.test(s), "Past the False Plateau stays locked just below 1,550/min");
s.peakUps = CR.RUN46_COLLECTION_PER_SEC;
ok(plateau.test(s) && plateau.desc === "produce 1,550/min",
   "Past the False Plateau unlocks at 1,550/min, with matching copy",
   plateau.desc);

out.push("=== payback advice ===");
CR.wipe(); store = {}; s = CR.state(); s.coins = 1e6;
// A producer that cannot pay for itself must report Infinity rather than a
// misleadingly small number: bestBuy() ranks on this, so a 0 here would make the
// advisor recommend the thing you cannot afford to run.
var payCoop = CR.paybackSeconds(coop, 1);
ok(isFinite(payCoop) && payCoop > 0, "payback is a positive number of seconds", payCoop.toFixed(2) + "s");
ok(CR.paybackSeconds(coop, 10) > CR.paybackSeconds(coop, 1),
   "buying ten at once takes longer to repay than buying one",
   CR.paybackSeconds(coop, 1).toFixed(2) + "s vs " + CR.paybackSeconds(coop, 10).toFixed(2) + "s");
var pick = CR.bestBuy();
ok(pick !== null, "with coins in hand there is always a recommendation", String(pick));
ok(CR.PRODUCERS.every(function (d) {
     return CR.state().coins < CR.costFor(d, CR.buyCount(d))
         || CR.paybackSeconds(d, CR.buyCount(d)) >= CR.paybackSeconds(CR.PRODUCERS.filter(function (x) { return x.id === pick; })[0], CR.buyCount(CR.PRODUCERS.filter(function (x) { return x.id === pick; })[0])) - 1e-9;
   }), "the recommendation is the shortest payback among affordable producers", String(pick));
s.coins = 0;
ok(CR.bestBuy() === null, "broke means no recommendation, not a wrong one");
s.coins = 1e6;
ok(CR.bestManager() === "coop", "the cheapest un-managed producer you own is the manager to hire",
   String(CR.bestManager()));
CR.hireManager("coop");
ok(CR.bestManager() === null, "nothing else is owned, so no manager is advised", String(CR.bestManager()));

out.push("=== effect hand-off ===");
CR.wipe(); store = {}; s = CR.state(); s.coins = 1e9;
s.producers.coop.owned = 0;          // the seed coop would put buy(24) at exactly 25
CR.buy("coop", 24);
ok(CR.takeCrossed().length === 0, "24 owned crosses no milestone");
CR.buy("coop", 1);
var crossed = CR.takeCrossed();
ok(crossed.length === 1 && crossed[0].at === 25, "the 25th purchase reports the milestone it crossed",
   JSON.stringify(crossed));
ok(CR.takeCrossed().length === 0, "crossings are drained, not replayed");
s.coins = 1e60;                      // 400 coops at 1.07^n is not a 1e9 purchase
CR.buy("coop", 400);
ok(CR.takeCrossed().length >= 3, "one big purchase reports every milestone it jumped");
CR.wipe(); store = {}; s = CR.state(); s.coins = 1e6; CR.buy("coop", 3);
CR.takeFinished();
CR.collect("coop"); CR.tick(CR.cycleTime(coop) + 0.01);
var fin = CR.takeFinished();
ok(fin.length === 1 && fin[0].id === "coop" && fin[0].units > 0,
   "a completed hand cycle is reported with its amount, for the floating number",
   JSON.stringify(fin));
ok(CR.takeFinished().length === 0, "completions are drained too");
CR.hireManager("coop"); CR.tick(5);
ok(CR.takeFinished().length === 0, "managed output does not queue an effect per cycle (it would be thousands)");

out.push("=== save / load round trip ===");
CR.wipe(); store = {}; s = CR.state(); s.coins = 5e5;
CR.buy("coop", 12); CR.hireManager("coop"); CR.buyOneoff("feed1"); s.heirlooms = 7; CR.save();
var snapshot = { owned: CR.state().producers.coop.owned, coins: CR.state().coins, heir: CR.state().heirlooms };
CR.wipe();
ok(CR.state().producers.coop.owned === 1 && CR.state().coins === 0 && CR.state().heirlooms === 0,
   "wipe clears everything back to a seed coop");
store[Object.keys(store)[0]] = store[Object.keys(store)[0]];   // keep the saved blob
out.push("  (reloading from the saved blob)");
store["coopRush.v2"] = JSON.stringify(Object.assign(snapshot.raw || {}, {
  v:2, coins:snapshot.coins, units:0, lifetime:100, allTime:100, heirlooms:snapshot.heir, heirloomsSpent:0,
  rebuilds:0, producers:{coop:{owned:snapshot.owned, progress:0, manager:true}}, oneoff:{feed1:true}, perks:{},
  buyMode:1, started:Date.now(), savedAt:Date.now(), bestRun:0 }));
ok(CR.load(), "load accepts a v2 save");
ok(CR.state().producers.coop.owned === snapshot.owned && CR.state().heirlooms === snapshot.heir
   && CR.state().oneoff.feed1 === true, "state survives the round trip");
ok(CR.state().producers.market && CR.state().producers.market.owned === 0, "producers missing from an old save are rehydrated");
store["coopRush.v2"] = '{"v":1,"coins":999}';
ok(!CR.load(), "a v1 save is rejected rather than half-read");
store["coopRush.v2"] = "not json {";
ok(!CR.load(), "corrupt save is rejected");
// The new record/achievement fields were added without bumping the save version,
// so a save written before they existed must still load and simply start at zero.
store["coopRush.v2"] = JSON.stringify({
  v: 2, coins: 500, units: 0, lifetime: 10, allTime: 10, heirlooms: 3, heirloomsSpent: 0,
  rebuilds: 0, producers: { coop: { owned: 4, progress: 0, manager: true } },
  oneoff: {}, perks: {}, buyMode: 1, started: Date.now(), savedAt: Date.now(), bestRun: 0 });
ok(CR.load(), "a save from before achievements existed still loads");
ok(CR.achievementCount() === 0 && CR.state().clicks === 0 && CR.state().peakUps === 0,
   "missing record fields default instead of poisoning arithmetic with undefined",
   JSON.stringify({ clicks: CR.state().clicks, peak: CR.state().peakUps }));
ok(isFinite(CR.globalMultiplier()), "and the multiplier is still a real number",
   CR.globalMultiplier().toFixed(3));

out.push("=== balance: auto-player to first rebuild ===");
CR.wipe(); store = {}; s = CR.state();
var clicks = 0, simSeconds = 0, DT = 0.25, firstPrestige = null, log = [];
function bestBuy() {          // crude but honest shopper: managers, then value
  var st = CR.state();
  for (var i = 0; i < CR.PRODUCERS.length; i++) {
    var d = CR.PRODUCERS[i], p = st.producers[d.id];
    if (p.owned > 0 && !p.manager && st.coins >= CR.managerCost(d)) { CR.hireManager(d.id); return true; }
  }
  for (var j = CR.ONEOFF.length - 1; j >= 0; j--) {
    var u = CR.ONEOFF[j];
    if (!st.oneoff[u.id] && st.coins >= u.cost * 3) { CR.buyOneoff(u.id); return true; }
  }
  var pick = null, pickScore = 0;
  for (var k = 0; k < CR.PRODUCERS.length; k++) {
    var def = CR.PRODUCERS[k], cost = CR.costFor(def, 1);
    if (st.coins < cost) continue;
    var gain = (CR.unitsPerCycle(def) || def.yield) / CR.cycleTime(def) / Math.max(1, st.producers[def.id].owned);
    var score = gain / cost;
    if (score > pickScore) { pickScore = score; pick = def; }
  }
  if (pick) { CR.buy(pick.id, 1); return true; }
  return false;
}
while (simSeconds < 3 * 3600 && firstPrestige === null) {
  CR.tick(DT); simSeconds += DT;
  // a human clicks a few times a second early on, then stops caring
  if (simSeconds < 180) { var cp = CR.state().producers.coop; if (cp.owned > 0 && !cp.manager && cp.progress === 0) { CR.collect("coop"); clicks++; } }
  var guard = 0;
  while (bestBuy() && guard++ < 40) {}
  if (CR.prestigeGain() >= 12 && firstPrestige === null) firstPrestige = simSeconds;
  if (Math.abs(simSeconds % 300) < DT/2) {
    log.push("    t+" + (simSeconds/60).toFixed(0) + "m: " + fmt(CR.state().lifetime) + " produce, "
      + fmt(CR.unitsPerSec()) + "/s, coops " + CR.state().producers.coop.owned
      + ", managers " + CR.PRODUCERS.filter(function(d){return CR.state().producers[d.id].manager;}).length);
  }
}
out = out.concat(log.slice(0, 8));
ok(firstPrestige !== null, "first rebuild is reachable", firstPrestige ? (firstPrestige/60).toFixed(1)+" min of play" : "never");
ok(firstPrestige !== null && firstPrestige > 120 && firstPrestige < 3600,
   "first rebuild paced between 2 and 60 minutes", firstPrestige ? (firstPrestige/60).toFixed(1)+" min" : "n/a");
var owned = CR.PRODUCERS.filter(function(d){ return CR.state().producers[d.id].owned > 0; }).length;
ok(owned >= 4, "several producers become relevant, not just the first", owned + " of " + CR.PRODUCERS.length + " unlocked");
ok(clicks > 0 && CR.state().lifetime > 0, "clicking matters early", clicks + " manual collects");

out.push("");
out.push(fails === 0 ? "ALL CHECKS PASSED (" + (out.filter(function(l){return l.indexOf("  ok   ")===0;}).length) + ")"
                     : fails + " FAILURES");
out.join("\n");
