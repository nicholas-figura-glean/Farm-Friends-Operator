/* Coop Rush - an idle/incremental farm game.
 *
 * Genre mechanics follow AdVenture Capitalist, whose numbers are well documented:
 * per-producer cost curves of cost = base * coeff^owned with coefficients in the
 * 1.07-1.15 range, cycle times halved at 25/50/100/200/300/400 owned, managers
 * that auto-restart cycles (the thing that actually makes an idle game idle), and
 * a prestige currency drawn from a square root of lifetime earnings that grants a
 * small percentage each and stacks multiplicatively with prestige-bought upgrades.
 *
 * The theme is an epistemic time capsule. A short run-46 collection cohort appeared
 * capped at ~1,550 units/min and was promoted into policy; later leaderboard evidence
 * superseded that cap and supports herd-size scaling over the observed range. It does
 * not establish unlimited scaling. The upgrades preserve that wrong turn without
 * presenting it as current game mechanics.
 *
 * Self-contained: no imports, no network, no framework. State lives in one object,
 * persists to localStorage, and the whole simulation is pure enough to run
 * headlessly in a test harness (see CR.__test).
 */
"use strict";

const CR = (function () {
  const SAVE_KEY = "coopRush.v2";
  const SAVE_EVERY = 4;          // seconds between autosaves
  const OFFLINE_CAP_H = 4;       // hours of idle production credited on return
  // Game-economy exchange rate: inspired by an observed 2-coin egg price, but one
  // abstract "produce" unit here can represent eggs, crops, wool, or market output.
  const SELL_PRICE = 2;
  const RUN46_COLLECTION_PER_MIN = 1550;
  const RUN46_COLLECTION_PER_SEC = RUN46_COLLECTION_PER_MIN / 60;

  // Cost coefficients rise down the list: early producers stay cheap to stack,
  // late ones are bought for their milestone multipliers rather than their price.
  const PRODUCERS = [
    { id: "coop",   name: "Chicken Coop",   icon: "\u{1F414}", cost: 4,        coeff: 1.07, cycle: 0.8,  yield: 1,     note: "the primary engine in the run-50 collection cohort" },
    { id: "pig",    name: "Truffle Pigs",   icon: "\u{1F437}", cost: 60,       coeff: 1.08, cycle: 3,    yield: 6,     note: "100 of them produced 2 truffles a run" },
    { id: "hive",   name: "Beehive Row",    icon: "\u{1F41D}", cost: 720,      coeff: 1.09, cycle: 6,    yield: 30,    note: "the wildflowers finally pay off" },
    { id: "wool",   name: "Wool Shed",      icon: "\u{1F411}", cost: 8640,     coeff: 1.10, cycle: 12,   yield: 140,   note: "negligible collected wool in the run-50 cohort" },
    { id: "dairy",  name: "Dairy Barn",     icon: "\u{1F404}", cost: 103680,   coeff: 1.11, cycle: 24,   yield: 700,   note: "negligible collected milk in the run-50 cohort" },
    { id: "wheat",  name: "Wheat Field",    icon: "\u{1F33E}", cost: 1244160,  coeff: 1.12, cycle: 48,   yield: 3600,  note: "here the crop timers actually advance" },
    { id: "market", name: "Farmers Market", icon: "\u{1F3EA}", cost: 14929920, coeff: 1.13, cycle: 96,   yield: 20000, note: "late-game output beyond the 2-coin exchange abstraction" },
  ];

  // Alternating milestones: more per cycle, then faster cycles. Same thresholds
  // AdCap uses, because they are paced well - each one lands just as the previous
  // producer stops feeling exciting.
  const MILESTONES = [
    { at: 25,  kind: "yield", mult: 2, label: "x2 produce" },
    { at: 50,  kind: "speed", mult: 2, label: "2x faster" },
    { at: 100, kind: "yield", mult: 2, label: "x2 produce" },
    { at: 200, kind: "speed", mult: 2, label: "2x faster" },
    { at: 300, kind: "yield", mult: 3, label: "x3 produce" },
    { at: 400, kind: "speed", mult: 2, label: "2x faster" },
  ];

  // One-off upgrades: bought once with coins, wiped by a rebuild. Named after the
  // real findings, in the order you can afford them.
  const ONEOFF = [
    { id: "feed1",    name: "Better Feed",        cost: 500,        icon: "\u{1F33F}", target: "all",    kind: "yield", mult: 1.5, desc: "all producers x1.5 produce" },
    { id: "click1",   name: "Quick Hands",        cost: 1200,       icon: "\u{1F44B}", target: "click",  kind: "instant", mult: 1, desc: "hand-collected cycles finish instantly" },
    { id: "coop3",    name: "Insulated Coop",     cost: 3500,       icon: "\u{1F414}", target: "coop",   kind: "yield", mult: 3,   desc: "Chicken Coop x3 produce" },
    { id: "pig3",     name: "Truffle Map",        cost: 20000,      icon: "\u{1F437}", target: "pig",    kind: "yield", mult: 3,   desc: "Truffle Pigs x3 produce" },
    { id: "bulk",     name: "Bulk Feeder",        cost: 45000,      icon: "\u{1F6B0}", target: "all",    kind: "speed", mult: 1.15,desc: "all cycles 15% faster" },
    { id: "hive3",    name: "Queen Excluder",     cost: 160000,     icon: "\u{1F41D}", target: "hive",   kind: "yield", mult: 3,   desc: "Beehive Row x3 produce" },
    { id: "lift1",    name: "Ceiling Lift I",     cost: 750000,     icon: "\u{1F4C8}", target: "all",    kind: "yield", mult: 2,   desc: "the run-46 collection cap was superseded: all x2" },
    { id: "wool3",    name: "Shearing Rig",       cost: 2400000,    icon: "\u{1F411}", target: "wool",   kind: "yield", mult: 3,   desc: "Wool Shed x3 produce" },
    { id: "night",    name: "Night Shift",        cost: 9000000,    icon: "\u{1F319}", target: "all",    kind: "speed", mult: 1.25,desc: "all cycles 25% faster" },
    { id: "dairy3",   name: "Milking Parlour",    cost: 32000000,   icon: "\u{1F404}", target: "dairy",  kind: "yield", mult: 3,   desc: "Dairy Barn x3 produce" },
    { id: "lift2",    name: "Ceiling Lift II",    cost: 150000000,  icon: "\u{1F4C8}", target: "all",    kind: "yield", mult: 2.5, desc: "all producers x2.5 produce" },
    { id: "wheat3",   name: "Combine Harvester",  cost: 600000000,  icon: "\u{1F33E}", target: "wheat",  kind: "yield", mult: 3,   desc: "Wheat Field x3 produce" },
    { id: "market3",  name: "Market Franchise",   cost: 4000000000, icon: "\u{1F3EA}", target: "market", kind: "yield", mult: 3,   desc: "Farmers Market x3 produce" },
    { id: "golden",   name: "Golden Egg",         cost: 25000000000,icon: "\u{1F95A}", target: "all",    kind: "yield", mult: 4,   desc: "all producers x4 produce" },
  ];

  // Heirloom upgrades: bought with the prestige currency and kept forever. These
  // multiply with the per-heirloom bonus rather than replacing it, so buying one
  // is a real trade - you spend heirlooms that were themselves giving you a bonus.
  const HEIRLOOM_UPGRADES = [
    { id: "hand",  name: "Farmhand",           cost: 25,    icon: "\u{1F9D1}", kind: "manager", value: "coop", desc: "every rebuild starts with the Chicken Coop manager - the farm restarts itself" },
    { id: "husb",  name: "Heirloom Husbandry", cost: 60,    icon: "\u{1F423}", kind: "rate",  value: 0.03, desc: "each heirloom gives 3% instead of 2%" },
    { id: "run50", name: "The Run-50 Lesson",  cost: 150,   icon: "\u{1F9EA}", kind: "mult",  value: 3,    desc: "x3 all produce - an experiment answers only the question it actually measured" },
    { id: "feeders",name: "Automatic Feeders", cost: 400,   icon: "\u{2699}",  kind: "speed", value: 1.3,  desc: "all cycles 30% faster, permanently" },
    { id: "husb2", name: "Selective Breeding", cost: 900,   icon: "\u{1F9EC}", kind: "rate",  value: 0.05, desc: "each heirloom gives 5%" },
    { id: "start", name: "Founding Flock",     cost: 2000,  icon: "\u{1F3C1}", kind: "start", value: 25,   desc: "start every rebuild with 25 coops, already managed" },
    { id: "ledger",name: "Keeper of the Ledger",cost: 5000, icon: "\u{1F4D3}", kind: "mult",  value: 5,    desc: "x5 all produce" },
  ];

  /* ---- achievements -----------------------------------------------------
   *
   * Pure predicates over S, so they are testable and cost nothing to evaluate.
   * Each one is worth ACHIEVEMENT_BONUS, which folds into globalMultiplier(): a
   * badge you cannot spend is a badge nobody chases. They are also the game's
   * only narrator - each unlock names the real-farm measurement it is mocking.
   */
  const ACHIEVEMENT_BONUS = 0.01;    // +1% global produce each

  const ACHIEVEMENTS = [
    { id: "first",    icon: "\u{1F423}", name: "First Egg",           desc: "collect anything at all",
      hint: "click the coop",                    test: s => s.allTime >= 1 },
    { id: "hire",     icon: "\u{1F9D1}", name: "Delegation",           desc: "hire your first manager",
      hint: "a manager is what makes it idle",   test: s => managerCount(s) >= 1 },
    { id: "ms25",     icon: "\u{1F3C5}", name: "Twenty-Five Strong",   desc: "reach 25 of any producer",
      hint: "the first milestone",               test: s => peakOwned(s) >= 25 },
    { id: "clicks",   icon: "\u{1F44B}", name: "Manual Labour",        desc: "collect 250 cycles by hand",
      hint: "before the managers took over",     test: s => (s.clicks || 0) >= 250 },
    { id: "seven",    icon: "\u{1F69C}", name: "Full Roster",          desc: "own all seven producers",
      hint: "including species negligible in the run-50 collection cohort", test: s => ownedKinds(s) >= PRODUCERS.length },
    { id: "auto",     icon: "\u{2699}",  name: "Hands Off",            desc: "every producer has a manager",
      hint: "the farm runs itself now",          test: s => managerCount(s) >= PRODUCERS.length },
    { id: "wool",     icon: "\u{1F411}", name: "Wool, At Last",        desc: "own 100 of the Wool Shed",
      hint: "wool was negligible in the run-50 collection cohort", test: s => s.producers.wool.owned >= 100 },
    { id: "milk",     icon: "\u{1F95B}", name: "Milk, At Last",        desc: "own 100 of the Dairy Barn",
      hint: "milk was negligible in the run-50 collection cohort", test: s => s.producers.dairy.owned >= 100 },
    { id: "wheat",    icon: "\u{1F33E}", name: "The Timer Advanced",   desc: "own a Wheat Field",
      hint: "one historical crop probe stayed at 0% for 27 minutes", test: s => s.producers.wheat.owned >= 1 },
    { id: "ceiling",  icon: "\u{1F4C8}", name: "Ceiling Lifted",       desc: "buy Ceiling Lift I",
      hint: "the promoted run-46 collection cap was superseded", test: s => !!s.oneoff.lift1 },
    { id: "rate1k",   icon: "\u{1F680}", name: "Past the False Plateau", desc: "produce 1,550/min",
      hint: "the short run-46 collection cohort appeared capped at this rate", test: s => (s.peakUps || 0) >= RUN46_COLLECTION_PER_SEC },
    { id: "million",  icon: "\u{1F4B0}", name: "Seven Figures",        desc: "1M produce in one run",
      hint: "the rebuild gate",                  test: s => s.bestRun >= 1e6 || s.lifetime >= 1e6 },
    { id: "rebuild",  icon: "\u{1F504}", name: "Heirloom Keeper",      desc: "rebuild the farm once",
      hint: "prestige is the real progress bar", test: s => s.rebuilds >= 1 },
    { id: "rebuild5", icon: "\u{1F3C6}", name: "Generational",         desc: "rebuild five times",
      hint: "heirlooms compound",                test: s => s.rebuilds >= 5 },
    { id: "perks",    icon: "\u{1F9EC}", name: "Bloodline",            desc: "own every heirloom upgrade",
      hint: "permanent, through every rebuild",  test: s => perkCount(s) >= HEIRLOOM_UPGRADES.length },
    { id: "oneoff",   icon: "\u{1F6D2}", name: "Cleared the Shelf",    desc: "buy all 14 one-off upgrades",
      hint: "in a single run",                   test: s => oneoffCount(s) >= ONEOFF.length },
    { id: "billion",  icon: "\u{1F30D}", name: "Ten Figures",          desc: "1B produce all-time",
      hint: "the fantasy, fully realised",       test: s => s.allTime >= 1e9 },
    { id: "offline",  icon: "\u{1F319}", name: "It Ran Without You",   desc: "bank offline production",
      hint: "four hours of it, maximum",         test: s => (s.offlineBanked || 0) >= 1 },
  ];

  function managerCount(s) { let n = 0; for (const d of PRODUCERS) if (s.producers[d.id].manager) n++; return n; }
  function ownedKinds(s) { let n = 0; for (const d of PRODUCERS) if (s.producers[d.id].owned > 0) n++; return n; }
  function peakOwned(s) { let n = 0; for (const d of PRODUCERS) n = Math.max(n, s.producers[d.id].owned); return n; }
  function perkCount(s) { let n = 0; for (const u of HEIRLOOM_UPGRADES) if (s.perks[u.id]) n++; return n; }
  function oneoffCount(s) { let n = 0; for (const u of ONEOFF) if (s.oneoff[u.id]) n++; return n; }

  const BASE_HEIRLOOM_RATE = 0.02;   // +2% per heirloom, AdCap's default
  const PRESTIGE_SCALE = 1e6;        // lifetime units for the first heirlooms
  const PRESTIGE_K = 12;             // heirlooms = K * sqrt(lifetime / SCALE)
  // A floor as well as a curve. Without it the sqrt hands out its first heirloom
  // at ~7k produce, so the button would offer a rebuild worth +2% while claiming
  // it needs 1M - and a 1-heirloom rebuild is a trap, not a decision. The genre
  // rule of thumb is to reset only when the currency at least doubles.
  const PRESTIGE_MIN_UNITS = 1e6;

  let S = blank();
  let lastTick = 0;
  let saveTimer = 0;
  let dirty = true;                  // re-render structure only when it changes

  function blank() {
    const producers = {};
    for (const p of PRODUCERS) producers[p.id] = { owned: 0, progress: 0, manager: false };
    // You start owning one coop, exactly as AdCap hands you one lemonade stand.
    // Starting with nothing and no coins is unplayable - there is no first move -
    // and the headless auto-player caught precisely that: 40 minutes, zero purchases.
    producers.coop.owned = 1;
    return {
      v: 2,
      coins: 0,
      units: 0,             // produce this run
      lifetime: 0,          // produce since the last rebuild, drives prestige
      allTime: 0,           // produce across every run, never reset
      heirlooms: 0,
      heirloomsSpent: 0,
      rebuilds: 0,
      producers: producers,
      oneoff: {},
      perks: {},
      buyMode: 1,           // 1, 10, 100, or "max"
      started: Date.now(),
      savedAt: Date.now(),
      bestRun: 0,
      // Records and achievement inputs. Kept on the state object (not in module
      // scope) so they persist and so every predicate stays a pure function of S.
      achieved: {},
      clicks: 0,
      buys: 0,
      handCollected: 0,
      peakUps: 0,
      offlineBanked: 0,
      playSeconds: 0,
    };
  }

  /* ---- derived numbers -------------------------------------------------- */

  function heirloomRate() {
    let rate = BASE_HEIRLOOM_RATE;
    for (const u of HEIRLOOM_UPGRADES) {
      if (S.perks[u.id] && u.kind === "rate") rate = Math.max(rate, u.value);
    }
    return rate;
  }

  // Heirloom bonus and heirloom-bought multipliers stack multiplicatively, which
  // is what makes spending heirlooms a genuine decision instead of a strict loss.
  // Every unlocked achievement is worth ACHIEVEMENT_BONUS. With none unlocked
  // this is exactly 1, so it cannot shift the documented heirloom arithmetic.
  function achievementCount() {
    let n = 0;
    for (const a of ACHIEVEMENTS) if (S.achieved[a.id]) n++;
    return n;
  }

  function achievementBonus() {
    return 1 + achievementCount() * ACHIEVEMENT_BONUS;
  }

  function globalMultiplier() {
    let mult = 1 + S.heirlooms * heirloomRate();
    mult *= achievementBonus();
    for (const u of HEIRLOOM_UPGRADES) {
      if (S.perks[u.id] && u.kind === "mult") mult *= u.value;
    }
    for (const u of ONEOFF) {
      if (S.oneoff[u.id] && u.target === "all" && u.kind === "yield") mult *= u.mult;
    }
    return mult;
  }

  function speedDivisor(id) {
    let div = 1;
    for (const u of ONEOFF) {
      if (S.oneoff[u.id] && u.kind === "speed" && (u.target === "all" || u.target === id)) div *= u.mult;
    }
    for (const u of HEIRLOOM_UPGRADES) {
      if (S.perks[u.id] && u.kind === "speed") div *= u.value;
    }
    return div;
  }

  function milestonesHit(owned) {
    return MILESTONES.filter(m => owned >= m.at);
  }

  function nextMilestone(owned) {
    return MILESTONES.find(m => owned < m.at) || null;
  }

  function unitsPerCycle(def) {
    const st = S.producers[def.id];
    if (!st.owned) return 0;
    let mult = 1;
    for (const m of milestonesHit(st.owned)) if (m.kind === "yield") mult *= m.mult;
    for (const u of ONEOFF) {
      if (S.oneoff[u.id] && u.kind === "yield" && u.target === def.id) mult *= u.mult;
    }
    return def.yield * st.owned * mult * globalMultiplier();
  }

  function cycleTime(def) {
    const st = S.producers[def.id];
    let div = speedDivisor(def.id);
    for (const m of milestonesHit(st.owned)) if (m.kind === "speed") div *= m.mult;
    return Math.max(0.05, def.cycle / div);
  }

  function unitsPerSec() {
    let total = 0;
    for (const def of PRODUCERS) {
      const st = S.producers[def.id];
      if (!st.owned || !st.manager) continue;   // unmanaged output needs clicks
      total += unitsPerCycle(def) / cycleTime(def);
    }
    return total;
  }

  function hasInstant() {
    for (const u of ONEOFF) if (S.oneoff[u.id] && u.kind === "instant") return true;
    return false;
  }

  /* ---- costs ------------------------------------------------------------ */

  // Geometric series: base * coeff^owned * (coeff^n - 1) / (coeff - 1).
  function costFor(def, n) {
    const owned = S.producers[def.id].owned;
    const c = def.coeff;
    return def.cost * Math.pow(c, owned) * (Math.pow(c, n) - 1) / (c - 1);
  }

  function maxAffordable(def) {
    const owned = S.producers[def.id].owned;
    const c = def.coeff;
    const first = def.cost * Math.pow(c, owned);
    if (S.coins < first) return 0;
    const n = Math.log(1 + (S.coins * (c - 1)) / first) / Math.log(c);
    return Math.max(0, Math.floor(n + 1e-9));
  }

  function buyCount(def) {
    return S.buyMode === "max" ? Math.max(1, maxAffordable(def)) : S.buyMode;
  }

  function managerCost(def) {
    return def.cost * 20;
  }

  /* ---- advice -----------------------------------------------------------
   *
   * The genre's real decision is "which of seven costs curves do I feed next",
   * and doing that arithmetic by hand is exactly the busywork the operator's own
   * rules.py exists to delete. So the game does it too: payback() is the seconds
   * a purchase takes to repay itself in coins, and the shortest one wins.
   *
   * A producer with no manager is only counted at the rate it would run at if it
   * had one - otherwise an unmanaged card looks infinitely good (it costs coins
   * and produces nothing until clicked) and the advice would be nonsense.
   */
  function paybackSeconds(def, n) {
    const count = n || buyCount(def);
    const price = costFor(def, count);
    const st = S.producers[def.id];
    const before = st.owned;
    // Marginal rate: what those n units add, milestones included.
    st.owned = before + count;
    const after = unitsPerCycle(def) / cycleTime(def);
    st.owned = before;
    const now = before ? unitsPerCycle(def) / cycleTime(def) : 0;
    const gain = (after - now) * SELL_PRICE;
    if (!(gain > 0)) return Infinity;
    return price / gain;
  }

  // The producer whose next purchase repays itself soonest, among those you can
  // actually afford right now. Null when nothing is affordable.
  function bestBuy() {
    let best = null, bestPay = Infinity;
    for (const def of PRODUCERS) {
      const n = buyCount(def);
      if (S.coins < costFor(def, n)) continue;
      const pay = paybackSeconds(def, n);
      if (pay < bestPay) { bestPay = pay; best = def.id; }
    }
    return best;
  }

  // Managers are bought for uptime rather than for a rate increase, so they are
  // ranked separately: the cheapest affordable manager on a producer you own.
  function bestManager() {
    let best = null, bestCost = Infinity;
    for (const def of PRODUCERS) {
      const st = S.producers[def.id];
      if (st.manager || !st.owned) continue;
      const cost = managerCost(def);
      if (cost <= S.coins && cost < bestCost) { bestCost = cost; best = def.id; }
    }
    return best;
  }

  /* ---- actions ---------------------------------------------------------- */

  // Buying can cross a milestone, which is the moment worth celebrating. The
  // engine reports which ones were crossed so the UI does not have to diff state.
  let lastCrossed = [];

  function buy(id, count) {
    const def = PRODUCERS.find(p => p.id === id);
    if (!def) return false;
    const n = count || buyCount(def);
    if (n <= 0) return false;
    const price = costFor(def, n);
    if (S.coins < price) return false;
    const before = S.producers[id].owned;
    S.coins -= price;
    S.producers[id].owned += n;
    S.buys += 1;
    lastCrossed = MILESTONES.filter(m => before < m.at && before + n >= m.at)
                            .map(m => ({ id: id, at: m.at, label: m.label }));
    dirty = true;
    return true;
  }

  function takeCrossed() {
    const out = lastCrossed;
    lastCrossed = [];
    return out;
  }

  function hireManager(id) {
    const def = PRODUCERS.find(p => p.id === id);
    const st = S.producers[id];
    if (!def || st.manager || S.coins < managerCost(def)) return false;
    S.coins -= managerCost(def);
    st.manager = true;
    dirty = true;
    return true;
  }

  // Clicking STARTS a cycle; the tick finishes it. That is the AdCap loop, and it
  // is what makes the progress bar mean something before you own a manager.
  // Quick Hands upgrades this to an instant bank, which is the actual reward.
  function collect(id) {
    const def = PRODUCERS.find(p => p.id === id);
    const st = S.producers[id];
    if (!def || !st.owned) return false;
    if (st.manager) return false;              // already running itself
    S.clicks += 1;
    if (hasInstant()) {
      const units = unitsPerCycle(def);
      award(units);
      S.handCollected += units;
      st.progress = 0;
      return units;
    }
    if (st.progress > 0) return false;         // this cycle is already under way
    st.progress = 1e-6;
    return true;
  }

  /* ---- achievements ------------------------------------------------------ */

  // Returns only what was newly unlocked, so the caller can announce it without
  // tracking previous state. Cheap enough to call every frame (18 predicates).
  function checkAchievements() {
    const won = [];
    for (const a of ACHIEVEMENTS) {
      if (S.achieved[a.id]) continue;
      let hit = false;
      try { hit = !!a.test(S); } catch (e) { hit = false; }
      if (hit) {
        S.achieved[a.id] = Date.now();
        won.push(a);
      }
    }
    if (won.length) dirty = true;
    return won;
  }

  function award(units) {
    if (!(units > 0)) return;
    S.units += units;
    S.lifetime += units;
    S.allTime += units;
    S.coins += units * SELL_PRICE;
  }

  function buyOneoff(id) {
    const u = ONEOFF.find(x => x.id === id);
    if (!u || S.oneoff[id] || S.coins < u.cost) return false;
    S.coins -= u.cost;
    S.oneoff[id] = true;
    dirty = true;
    return true;
  }

  function buyPerk(id) {
    const u = HEIRLOOM_UPGRADES.find(x => x.id === id);
    if (!u || S.perks[id] || S.heirlooms < u.cost) return false;
    S.heirlooms -= u.cost;
    S.heirloomsSpent += u.cost;
    S.perks[id] = true;
    dirty = true;
    return true;
  }

  /* ---- prestige --------------------------------------------------------- */

  function prestigeGain() {
    if (S.lifetime < PRESTIGE_MIN_UNITS) return 0;
    const total = Math.floor(PRESTIGE_K * Math.sqrt(S.lifetime / PRESTIGE_SCALE));
    return Math.max(0, total);
  }

  // 0..1 toward being allowed to rebuild at all, for the UI to show instead of a
  // flat "0 heirlooms" that gives the player nothing to aim at.
  function prestigeProgress() {
    return Math.max(0, Math.min(1, S.lifetime / PRESTIGE_MIN_UNITS));
  }

  // Rebuild wipes the run - producers, coins, one-off upgrades - and keeps
  // heirlooms and everything bought with them. Spent heirlooms still counted
  // toward the total that earned them, so spending never lowers future gains.
  function rebuild() {
    const gain = prestigeGain();
    if (gain <= 0) return 0;
    const keep = {
      heirlooms: S.heirlooms + gain,
      heirloomsSpent: S.heirloomsSpent,
      perks: S.perks,
      rebuilds: S.rebuilds + 1,
      allTime: S.allTime,
      bestRun: Math.max(S.bestRun, S.lifetime),
      buyMode: S.buyMode,
      started: S.started,
      // Records and achievements survive a rebuild by definition: they are the
      // only thing in the game that tracks the player rather than the run.
      achieved: S.achieved,
      clicks: S.clicks,
      buys: S.buys,
      handCollected: S.handCollected,
      peakUps: S.peakUps,
      offlineBanked: S.offlineBanked,
      playSeconds: S.playSeconds,
    };
    S = Object.assign(blank(), keep);
    applyStartPerks();
    dirty = true;
    save();
    return gain;
  }

  // Without one of these, a rebuild drops you back to hand-clicking a single coop
  // with no coins - which a 6-hour headless run showed flatlining completely once
  // the player stops clicking. Farmhand is deliberately the cheapest perk.
  function applyStartPerks() {
    for (const u of HEIRLOOM_UPGRADES) {
      if (!S.perks[u.id]) continue;
      if (u.kind === "manager") {
        S.producers[u.value].manager = true;
      } else if (u.kind === "start") {
        S.producers.coop.owned = Math.max(S.producers.coop.owned, u.value);
        S.producers.coop.manager = true;
      }
    }
  }

  function wipe() {
    S = blank();
    dirty = true;
    save();
  }

  /* ---- simulation ------------------------------------------------------- */

  // Cycles a hand-started producer completed during the last tick, so the UI can
  // float the number over the right card. Drained by takeFinished().
  let finished = [];

  function takeFinished() {
    const out = finished;
    finished = [];
    return out;
  }

  function tick(dt) {
    if (!(dt > 0)) return;
    for (const def of PRODUCERS) {
      const st = S.producers[def.id];
      if (!st.owned) continue;
      const time = cycleTime(def);
      if (st.manager) {
        // Managed producers run continuously. Crediting fractional cycles keeps
        // long frames and offline catch-up exact instead of dropping remainders.
        const cycles = dt / time;
        award(unitsPerCycle(def) * cycles);
      } else if (st.progress > 0) {
        st.progress += dt / time;
        if (st.progress >= 1) {
          const units = unitsPerCycle(def);
          award(units);
          S.handCollected += units;
          st.progress = 0;
          finished.push({ id: def.id, units: units });
        }
      }
    }
    S.playSeconds = (S.playSeconds || 0) + dt;
    const ups = unitsPerSec();
    if (ups > (S.peakUps || 0)) S.peakUps = ups;
  }

  /* ---- persistence ------------------------------------------------------ */

  function save() {
    S.savedAt = Date.now();
    try { localStorage.setItem(SAVE_KEY, JSON.stringify(S)); } catch (e) { /* private mode */ }
  }

  function load() {
    let raw = null;
    try { raw = localStorage.getItem(SAVE_KEY); } catch (e) { return false; }
    if (!raw) return false;
    let data = null;
    try { data = JSON.parse(raw); } catch (e) { return false; }
    if (!data || typeof data !== "object" || data.v !== 2) return false;
    S = Object.assign(blank(), data);
    // Rehydrate producers defensively: a save from an older producer list must
    // not leave holes that every later read has to guard against.
    for (const def of PRODUCERS) {
      S.producers[def.id] = Object.assign({ owned: 0, progress: 0, manager: false },
                                          (data.producers || {})[def.id] || {});
    }
    S.oneoff = data.oneoff || {};
    S.perks = data.perks || {};
    S.achieved = data.achieved || {};
    return true;
  }

  function offlineCatchUp() {
    const away = (Date.now() - (S.savedAt || Date.now())) / 1000;
    const credited = Math.max(0, Math.min(away, OFFLINE_CAP_H * 3600));
    if (credited < 1) return 0;
    const before = S.lifetime;
    tick(credited);
    takeFinished();                  // offline cycles are not worth floating
    const banked = S.lifetime - before;
    S.offlineBanked = (S.offlineBanked || 0) + banked;
    return banked;
  }

  return {
    PRODUCERS, ONEOFF, HEIRLOOM_UPGRADES, MILESTONES, ACHIEVEMENTS,
    SELL_PRICE, RUN46_COLLECTION_PER_MIN, RUN46_COLLECTION_PER_SEC,
    OFFLINE_CAP_H, ACHIEVEMENT_BONUS,
    state: () => S,
    blank, load, save, wipe,
    buy, hireManager, collect, buyOneoff, buyPerk,
    costFor, maxAffordable, buyCount, managerCost,
    paybackSeconds, bestBuy, bestManager,
    checkAchievements, achievementCount, achievementBonus,
    takeFinished, takeCrossed,
    unitsPerCycle, cycleTime, unitsPerSec, globalMultiplier, heirloomRate,
    hasInstant, milestonesHit, nextMilestone,
    prestigeGain, prestigeProgress, PRESTIGE_MIN_UNITS, rebuild, tick, offlineCatchUp, applyStartPerks,
    setBuyMode: m => { S.buyMode = m; dirty = true; },
    isDirty: () => dirty,
    clearDirty: () => { dirty = false; },
    markDirty: () => { dirty = true; },
    tickState: { get last() { return lastTick; }, set last(v) { lastTick = v; },
                 get saveTimer() { return saveTimer; }, set saveTimer(v) { saveTimer = v; } },
  };
})();
