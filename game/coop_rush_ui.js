/* Coop Rush UI: rendering and input for the CR simulation.
 *
 * Deliberately dependency-free (it does not borrow the dashboard's helpers), so
 * the standalone export needs nothing but this file, the engine and the stylesheet.
 *
 * Three rendering paths on purpose:
 *   - structure (cards, upgrade lists) is rebuilt only when CR reports it dirty
 *   - numbers are patched every frame through cached element references
 *   - effects (floating numbers, sparks) are appended nodes with an expiry, swept
 *     by the same frame loop
 * Rebuilding 20 cards at 60fps would drop input and make buttons feel dead.
 *
 * Everything time-based here is driven by the frame loop rather than setTimeout:
 * the headless harness stubs setTimeout as a no-op, so a toast or a float that
 * relied on a timer to disappear would be untestable and would leak on a real
 * page whenever the tab was backgrounded.
 */
"use strict";

(function () {
  const SUFFIX = ["", "K", "M", "B", "T", "Qa", "Qi", "Sx", "Sp", "Oc", "No", "Dc", "Ud", "Dd"];

  function fmt(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "0";
    // Idle numbers really do run away: showing "0" for Infinity (the old bug) would
    // read as a broken game rather than a very good one.
    if (!isFinite(n)) return "\u221E";
    if (n < 0) return "-" + fmt(-n);
    if (n < 1000) return n < 10 && n % 1 !== 0 ? n.toFixed(1) : String(Math.floor(n));
    let tier = Math.floor(Math.log10(n) / 3);
    if (tier >= SUFFIX.length) tier = SUFFIX.length - 1;
    const scaled = n / Math.pow(1000, tier);
    return (scaled < 10 ? scaled.toFixed(2) : scaled < 100 ? scaled.toFixed(1) : Math.floor(scaled)) + SUFFIX[tier];
  }

  function secs(s) {
    if (s == null || !isFinite(s)) return "never";
    if (s < 1) return s.toFixed(2) + "s";
    if (s < 60) return s.toFixed(1) + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + Math.floor(s % 60) + "s";
    return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  }

  const el = id => document.getElementById(id);
  const refs = {};   // per-card element cache, rebuilt with the structure
  const EVENTS = []; // bounded, in-memory explanation of what changed this session

  // The harness's DOM stub implements only what the UI actually uses, and effects
  // need createElement/removeChild which a read-only stub has no reason to have.
  // Feature-detect rather than require it: the game must degrade to "no confetti",
  // never to "no game".
  const CAN_FX = typeof document.createElement === "function";
  const MOTION = (function () {
    try {
      return !(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } catch (e) { return true; }
  })();

  function esc(v) {
    return String(v === undefined || v === null ? "" : v)
      .replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ---- structure -------------------------------------------------------- */

  function buildProducers() {
    const host = el("cr-producers");
    if (!host) return;
    host.innerHTML = CR.PRODUCERS.map(def => {
      const st = CR.state().producers[def.id];
      const locked = st.owned === 0;
      return `<div class="cr-prod${locked ? " locked" : ""}" data-id="${def.id}">
        <div class="cr-prod-icon" data-act="collect" data-id="${def.id}" title="Collect one cycle by hand">${def.icon}</div>
        <div class="cr-prod-body">
          <div class="cr-prod-top">
            <b>${esc(def.name)}</b>
            <span class="cr-owned" data-f="owned">0</span>
          </div>
          <div class="cr-track"><i data-f="bar" style="width:0%"></i><span data-f="cycle"></span></div>
          <div class="cr-prod-meta"><span data-f="rate"></span><span class="cr-note">${esc(def.note)}</span></div>
          <div class="cr-milestone"><span class="cr-mstrack"><i data-f="msbar" style="width:0%"></i></span><span class="cr-mstext" data-f="ms"></span></div>
        </div>
        <div class="cr-prod-buy">
          <button class="cr-btn buy" data-act="buy" data-id="${def.id}">
            <span class="cr-btn-label">Buy <b data-f="buyn">1</b></span>
            <span class="cr-btn-cost" data-f="cost">-</span>
          </button>
          <button class="cr-btn mgr" data-act="manager" data-id="${def.id}">
            <span class="cr-btn-label">Manager</span>
            <span class="cr-btn-cost" data-f="mgr">-</span>
          </button>
        </div>
        <div class="cr-fx" data-f="fx"></div>
      </div>`;
    }).join("");

    for (const def of CR.PRODUCERS) {
      const card = host.querySelector(`.cr-prod[data-id="${def.id}"]`);
      const pick = f => card.querySelector(`[data-f="${f}"]`);
      refs[def.id] = {
        card, owned: pick("owned"), bar: pick("bar"), cycle: pick("cycle"), rate: pick("rate"),
        ms: pick("ms"), msbar: pick("msbar"), cost: pick("cost"), buyn: pick("buyn"),
        mgr: pick("mgr"), fx: pick("fx"),
        buyBtn: card.querySelector("button.buy"), mgrBtn: card.querySelector("button.mgr"),
      };
    }
  }

  function buildUpgrades() {
    const oneoff = el("cr-oneoff");
    if (oneoff) {
      const s = CR.state();
      const rows = CR.ONEOFF.map(u => {
        const owned = !!s.oneoff[u.id];
        return { u, owned };
      });
      // Owned upgrades sink to the bottom so the next purchase is always on top.
      rows.sort((a, b) => (a.owned - b.owned) || (a.u.cost - b.u.cost));
      oneoff.innerHTML = rows.map(({ u, owned }) => `
        <button class="cr-up${owned ? " owned" : ""}" data-act="oneoff" data-id="${u.id}" ${owned ? "disabled" : ""}>
          <span class="cr-up-icon">${u.icon}</span>
          <span class="cr-up-text"><b>${esc(u.name)}</b><small>${esc(u.desc)}</small></span>
          <span class="cr-up-cost">${owned ? "owned" : fmt(u.cost) + "c"}</span>
        </button>`).join("");
    }
    const perks = el("cr-perks");
    if (perks) {
      const s = CR.state();
      perks.innerHTML = CR.HEIRLOOM_UPGRADES.map(u => {
        const owned = !!s.perks[u.id];
        return `<button class="cr-up${owned ? " owned" : ""}" data-act="perk" data-id="${u.id}" ${owned ? "disabled" : ""}>
          <span class="cr-up-icon">${u.icon}</span>
          <span class="cr-up-text"><b>${esc(u.name)}</b><small>${esc(u.desc)}</small></span>
          <span class="cr-up-cost">${owned ? "owned" : fmt(u.cost) + " \u{1F423}"}</span>
        </button>`;
      }).join("");
    }
  }

  // Achievements are the only place the game narrates the real farm's findings,
  // so a locked one still shows its name and the measurement behind it. Hiding
  // them would waste the best part.
  function buildAchievements() {
    const host = el("cr-achievements");
    if (!host) return;
    const s = CR.state();
    host.innerHTML = CR.ACHIEVEMENTS.map(a => {
      const won = !!s.achieved[a.id];
      return `<div class="cr-ach${won ? " won" : ""}" title="${esc(a.desc)}">
        <span class="cr-ach-icon">${a.icon}</span>
        <span class="cr-ach-text"><b>${esc(a.name)}</b><small>${esc(won ? a.hint : a.desc)}</small></span>
      </div>`;
    }).join("");
  }

  function buildAll() {
    buildProducers();
    buildUpgrades();
    buildAchievements();
    CR.clearDirty();
  }

  /* ---- effects ----------------------------------------------------------
   *
   * Floating numbers exist because an idle game's whole feedback loop is "did
   * that do anything". A coin counter that jumps from 4.31K to 4.33K answers
   * that badly; a "+18" drifting off the coop answers it instantly.
   */

  const FX = [];              // { node, host, until }
  const FX_MAX = 40;          // hard cap: a held-down click must not grow the DOM

  function spawnFX(host, cls, text, style, ms) {
    if (!CAN_FX || !host || !host.appendChild) return null;
    if (FX.length >= FX_MAX) sweepFX(true);
    if (FX.length >= FX_MAX) return null;
    const node = document.createElement("span");
    // classList when the host provides it, className otherwise: the same reason
    // cls() exists, and a throw here would happen inside the click handler.
    if (node.classList) { for (const name of cls.split(" ")) if (name) node.classList.add(name); }
    else node.className = cls;
    node.textContent = text;
    if (style && node.style) {
      for (const key in style) { try { node.style[key] = style[key]; } catch (e) { /* stub */ } }
    }
    host.appendChild(node);
    FX.push({ node: node, host: host, until: Date.now() + (ms || 1000) });
    return node;
  }

  function sweepFX(force) {
    const now = Date.now();
    for (let i = FX.length - 1; i >= 0; i--) {
      const item = FX[i];
      if (!force && item.until > now) continue;
      if (item.host && item.host.removeChild) {
        try { item.host.removeChild(item.node); } catch (e) { /* already gone */ }
      }
      FX.splice(i, 1);
      if (force && FX.length < FX_MAX - 4) break;
    }
  }

  function floatUnits(id, units, cls) {
    floatText(id, "+" + fmt(units), cls);
  }

  function floatText(id, text, cls) {
    const r = refs[id];
    if (!r) return;
    const left = 20 + Math.random() * 45;
    spawnFX(r.fx, "cr-float" + (cls ? " " + cls : ""), text,
            { left: left.toFixed(1) + "%" }, 1100);
  }

  // A small egg burst on a hand-collect. Six is enough to read as an event and
  // cheap enough that holding the mouse down cannot cost a frame.
  function burst(id) {
    if (!MOTION) return;
    const r = refs[id];
    if (!r) return;
    for (let i = 0; i < 6; i++) {
      const angle = (Math.PI * 2 * i) / 6 + Math.random();
      spawnFX(r.fx, "cr-spark", "\u{1F95A}", {
        left: "32px",
        "--dx": (Math.cos(angle) * (26 + Math.random() * 22)).toFixed(0) + "px",
        "--dy": (Math.sin(angle) * (22 + Math.random() * 20) - 14).toFixed(0) + "px",
      }, 760);
    }
  }

  // Values that "pop" when they grow. Held in a map keyed by element id with an
  // expiry so the class comes off on a later frame instead of on a timer.
  const POPS = {};

  function pop(node, key) {
    if (!node || !node.classList) return;
    node.classList.add("cr-pop");
    POPS[key] = { node: node, until: Date.now() + 220 };
  }

  function sweepPops() {
    const now = Date.now();
    for (const key in POPS) {
      if (POPS[key].until > now) continue;
      if (POPS[key].node.classList) POPS[key].node.classList.remove("cr-pop");
      delete POPS[key];
    }
  }

  /* ---- toasts -----------------------------------------------------------
   *
   * A queue rather than one element: unlocking three achievements in the same
   * frame is normal after an offline catch-up, and the old single-slot toast
   * showed only the last one.
   */

  const TOASTS = [];
  const TOAST_MS = 4200;
  let toastHTML = "";

  function eventClock(ms) {
    const d = new Date(ms), pad = n => String(n).padStart(2, "0");
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function recordEvent(message, kind) {
    EVENTS.unshift({ text: String(message), kind: kind || "", at: Date.now() });
    while (EVENTS.length > 6) EVENTS.pop();
    paintEvents();
  }

  function paintEvents() {
    const host = el("cr-event-feed");
    if (!host) return;
    host.innerHTML = EVENTS.length ? EVENTS.map(item =>
      `<div class="game-event${item.kind ? " " + esc(item.kind) : ""}"><time>${eventClock(item.at)}</time><span>${esc(item.text)}</span></div>`
    ).join("") : `<div class="game-event"><time>now</time><span>Farm ready. Begin manually, then automate.</span></div>`;
  }

  function toast(message, kind) {
    TOASTS.push({ text: String(message), kind: kind || "", until: Date.now() + TOAST_MS });
    while (TOASTS.length > 4) TOASTS.shift();
    recordEvent(message, kind);
    paintToasts();
  }

  function paintToasts() {
    const host = el("cr-toast");
    if (!host) return;
    const now = Date.now();
    for (let i = TOASTS.length - 1; i >= 0; i--) if (TOASTS[i].until <= now) TOASTS.splice(i, 1);
    const html = TOASTS.map(t =>
      `<div class="cr-toast-item${t.kind ? " " + esc(t.kind) : ""}">${esc(t.text)}</div>`).join("");
    if (html === toastHTML) return;
    toastHTML = html;
    host.innerHTML = html;
    // The container is only interactive-looking while it holds something.
    if (host.classList) host.classList.toggle("show", TOASTS.length > 0);
  }

  /* ---- per-frame numbers ------------------------------------------------ */

  const PREV = {};   // last painted numeric values, for pop detection

  function paint() {
    const s = CR.state();
    const ups = CR.unitsPerSec();
    const activeLines = CR.PRODUCERS.filter(def => s.producers[def.id].owned > 0);
    const managedLines = activeLines.filter(def => s.producers[def.id].manager);
    const autoPct = activeLines.length ? managedLines.length / activeLines.length * 100 : 0;
    const autoState = !managedLines.length ? "Manual setup"
      : managedLines.length === activeLines.length ? "Running hands-free" : "Partially automated";
    setText("cr-auto-pct", Math.round(autoPct) + "%");
    setText("cr-auto-state", autoState);
    setText("cr-auto-detail", activeLines.length
      ? managedLines.length + " of " + activeLines.length + " active producer lines run themselves."
      : "Buy a producer, then hire its manager to begin autonomous production.");
    setText("cr-managed-count", managedLines.length + " / " + activeLines.length + " active lines managed");
    const ring = el("cr-auto-ring");
    if (ring && ring.style) ring.style["--pct"] = autoPct.toFixed(1) + "%";
    const nextManager = CR.bestManager();
    if (nextManager) {
      const nextDef = CR.PRODUCERS.find(def => def.id === nextManager);
      setText("cr-next-auto", "Next automation: hire the " + nextDef.name + " manager for " + fmt(CR.managerCost(nextDef)) + "c.");
    } else if (activeLines.length && managedLines.length === activeLines.length) {
      setText("cr-next-auto", "Every active producer is automated. Unlock the next tier or optimize payback.");
    } else {
      setText("cr-next-auto", "Manual producers need a click to start each cycle.");
    }
    setText("cr-units", fmt(s.lifetime));
    setText("cr-coins", fmt(s.coins));
    setText("cr-ups", fmt(ups) + "/s");
    setText("cr-mult", "x" + fmt(CR.globalMultiplier()));
    setText("cr-heirlooms", fmt(s.heirlooms));
    setText("cr-rebuilds", String(s.rebuilds));
    setText("cr-alltime", fmt(s.allTime));
    setText("cr-best", fmt(Math.max(s.bestRun, s.lifetime)));
    setText("cr-rate", (CR.heirloomRate() * 100).toFixed(0) + "%");

    // Records panel: the numbers that describe the player rather than the run.
    const wonCount = CR.achievementCount();
    setText("cr-ach-count", wonCount + " / " + CR.ACHIEVEMENTS.length);
    setText("cr-ach-bonus", "x" + CR.achievementBonus().toFixed(2));
    setText("cr-clicks", fmt(s.clicks || 0));
    setText("cr-peak", fmt(s.peakUps || 0) + "/s");
    setText("cr-playtime", secs(s.playSeconds || 0));
    setText("cr-hand", fmt(s.handCollected || 0));
    setText("cr-banked", fmt(s.offlineBanked || 0));
    // Compare like-for-like per-second rates against the superseded run-46
    // collection proxy. The evidence was 1,550 per minute, not per second.
    const run46Proxy = CR.RUN46_COLLECTION_PER_SEC;
    setText("cr-vs-real", ups > 0 ? fmt(ups / run46Proxy) + "x" : "0x");

    const gain = CR.prestigeGain();
    setText("cr-gain", fmt(gain));
    const prog = el("cr-prestige-bar");
    if (prog && prog.style) prog.style.width = (CR.prestigeProgress() * 100).toFixed(1) + "%";
    const btn = el("cr-rebuild");
    if (btn) {
      btn.disabled = gain <= 0;
      if (gain > 0) {
        btn.textContent = `Rebuild the farm for ${fmt(gain)} heirlooms`;
      } else {
        // Show the distance to the gate, not a bare zero.
        const pct = (CR.prestigeProgress() * 100).toFixed(1);
        btn.textContent = `Rebuild at ${fmt(CR.PRESTIGE_MIN_UNITS)} produce (${pct}%)`;
      }
    }

    // Which purchase repays itself soonest. Doing this arithmetic by hand every
    // 20 seconds is the busywork this whole project exists to delete, so the game
    // shows the answer instead of hiding it behind seven cost curves.
    const best = CR.bestBuy();
    const bestMgr = CR.bestManager();
    setText("cr-advice", adviceText(best, bestMgr));

    for (const def of CR.PRODUCERS) {
      const r = refs[def.id];
      if (!r) continue;
      const st = s.producers[def.id];
      const n = CR.buyCount(def);
      const cost = CR.costFor(def, n);
      const affordable = s.coins >= cost;
      const ownedText = fmt(st.owned);
      if (r.owned.textContent !== ownedText) {
        if (PREV["own-" + def.id] != null && st.owned > PREV["own-" + def.id]) pop(r.owned, "own-" + def.id);
        r.owned.textContent = ownedText;
      }
      PREV["own-" + def.id] = st.owned;
      r.buyn.textContent = s.buyMode === "max" ? (CR.maxAffordable(def) || 1) : s.buyMode;
      r.cost.textContent = fmt(cost) + "c";
      r.buyBtn.disabled = !affordable;
      cls(r.buyBtn, "ready", affordable);
      cls(r.buyBtn, "best", def.id === best);
      cls(r.card, "locked", st.owned === 0);
      cls(r.card, "running", st.manager || st.progress > 0);

      if (st.manager) {
        r.mgrBtn.hidden = true;
      } else {
        r.mgrBtn.hidden = false;
        const mc = CR.managerCost(def);
        r.mgr.textContent = fmt(mc) + "c";
        r.mgrBtn.disabled = s.coins < mc || st.owned === 0;
        cls(r.mgrBtn, "ready", s.coins >= mc && st.owned > 0);
        cls(r.mgrBtn, "best", def.id === bestMgr);
      }

      const time = CR.cycleTime(def);
      const per = CR.unitsPerCycle(def);
      r.cycle.textContent = st.owned ? secs(time) : "-";
      const pay = st.owned || affordable ? CR.paybackSeconds(def, n) : Infinity;
      r.rate.textContent = st.owned
        ? `${fmt(per)} produce / cycle \u00b7 ${fmt(per / time)}/s${st.manager ? "" : " (needs clicks)"}`
          + (isFinite(pay) ? ` \u00b7 pays back in ${secs(pay)}` : "")
        : `${fmt(def.yield)} produce / ${secs(def.cycle)}`;
      const pct = st.manager ? 100 : Math.min(100, st.progress * 100);
      if (r.bar.style) r.bar.style.width = pct.toFixed(1) + "%";
      cls(r.bar, "auto", st.manager);

      const next = CR.nextMilestone(st.owned);
      if (next) {
        const prev = CR.MILESTONES.filter(m => m.at < next.at).pop();
        const from = prev ? prev.at : 0;
        const frac = Math.max(0, Math.min(1, (st.owned - from) / (next.at - from)));
        r.ms.textContent = `${next.at - st.owned} more \u2192 ${next.label}`;
        if (r.msbar.style) r.msbar.style.width = (frac * 100).toFixed(1) + "%";
      } else {
        r.ms.textContent = "all milestones earned";
        if (r.msbar.style) r.msbar.style.width = "100%";
      }
    }

    for (const node of document.querySelectorAll('#cr-oneoff .cr-up:not(.owned)')) {
      const u = CR.ONEOFF.find(x => x.id === node.dataset.id);
      if (u) cls(node, "ready", s.coins >= u.cost);
    }
    for (const node of document.querySelectorAll('#cr-perks .cr-up:not(.owned)')) {
      const u = CR.HEIRLOOM_UPGRADES.find(x => x.id === node.dataset.id);
      if (u) cls(node, "ready", s.heirlooms >= u.cost);
    }

    for (const b of document.querySelectorAll("#cr-buymode button")) {
      const mode = b.dataset.mode === "max" ? "max" : Number(b.dataset.mode);
      cls(b, "on", s.buyMode === mode);
    }

    paintToasts();
    sweepPops();
  }

  function adviceText(best, bestMgr) {
    if (bestMgr) {
      const def = CR.PRODUCERS.find(d => d.id === bestMgr);
      return `Hire the ${def.name} manager (${fmt(CR.managerCost(def))}c) \u2014 press M`;
    }
    if (!best) return "Collect by hand until you can afford something \u2014 space bar";
    const def = CR.PRODUCERS.find(d => d.id === best);
    const pay = CR.paybackSeconds(def, CR.buyCount(def));
    return `Best value: ${def.name}, repays itself in ${secs(pay)} \u2014 press B`;
  }

  // classList is the one DOM API the read-only harness stub omits, and paint()
  // runs 60 times a second: a throw here would take the whole tab down.
  function cls(node, name, on) {
    if (node && node.classList) node.classList.toggle(name, !!on);
  }

  function setText(id, text) {
    const node = el(id);
    if (node && node.textContent !== text) node.textContent = text;
  }

  /* ---- input ------------------------------------------------------------ */

  function announce(won) {
    for (const a of won) toast(`${a.icon}  ${a.name} \u2014 ${a.desc}`, "ach");
  }

  // Every mutation funnels through here so effects, achievement checks and the
  // structural rebuild happen in exactly one place. Missing one of the three is
  // how a purchase ends up with no feedback at all.
  function after() {
    for (const c of CR.takeCrossed()) {
      const def = CR.PRODUCERS.find(d => d.id === c.id);
      floatText(c.id, c.label, "milestone");
      toast(`${def.icon}  ${def.name} hit ${c.at} \u2014 ${c.label}`, "ms");
    }
    announce(CR.checkAchievements());
    if (CR.isDirty()) buildAll();
    paint();
  }

  function doCollect(id) {
    const before = CR.state().lifetime;
    const result = CR.collect(id);
    if (!result) return false;
    burst(id);
    const gained = CR.state().lifetime - before;
    if (gained > 0) floatUnits(id, gained);   // Quick Hands: instant bank
    return true;
  }

  function onClick(event) {
    const target = event.target.closest("[data-act]");
    if (!target) return;
    const id = target.dataset.id;
    switch (target.dataset.act) {
      case "buy": {
        const def = CR.PRODUCERS.find(x => x.id === id);
        const amount = def ? CR.buyCount(def) : 0;
        if (CR.buy(id) && def) recordEvent(`Bought ${amount} ${def.name}${amount === 1 ? "" : "s"}.`, "buy");
        break;
      }
      case "manager":
        if (CR.hireManager(id)) toast("Manager hired \u2014 it runs itself now.", "ms");
        break;
      case "collect": doCollect(id); break;
      case "oneoff": {
        const u = CR.ONEOFF.find(x => x.id === id);
        if (CR.buyOneoff(id) && u) toast(`${u.icon}  ${u.name} \u2014 ${u.desc}`, "ms");
        break;
      }
      case "perk": {
        const u = CR.HEIRLOOM_UPGRADES.find(x => x.id === id);
        if (CR.buyPerk(id) && u) toast(`${u.icon}  ${u.name} \u2014 permanent.`, "ms");
        break;
      }
      case "mode": {
        const mode = target.dataset.mode === "max" ? "max" : Number(target.dataset.mode);
        CR.setBuyMode(mode);
        break;
      }
      case "rebuild": {
        const gain = CR.prestigeGain();
        if (gain <= 0) return;
        if (!confirm(`Rebuild the farm?\n\nYou gain ${fmt(gain)} heirloom hens (+${(CR.heirloomRate()*100).toFixed(0)}% produce each, forever).\n\nYou lose this run: producers, coins and one-off upgrades.\nYou keep: heirlooms, everything bought with them, and every achievement.`)) return;
        CR.rebuild();
        toast(`Rebuilt. +${fmt(gain)} heirloom hens.`, "ms");
        break;
      }
      case "wipe":
        if (!confirm("Erase everything, including heirlooms, achievements and all-time produce?")) return;
        CR.wipe();
        toast("Save erased.");
        break;
      default: return;
    }
    after();
  }

  function onKey(event) {
    const panel = el("tab-game");
    if (panel && panel.hidden) return;                       // dashboard: other tab
    if (event.target && /input|textarea/i.test(event.target.tagName)) return;
    const keys = { "1": 1, "2": 10, "3": 100, "4": "max" };
    if (keys[event.key] !== undefined) { CR.setBuyMode(keys[event.key]); paint(); return; }
    const key = String(event.key || "").toLowerCase();
    // B and M play the advisor's recommendation. Two keys is the whole mid-game
    // loop, which is the point: the interesting decision is when to rebuild.
    if (key === "b") {
      const best = CR.bestBuy();
      if (best) {
        const def = CR.PRODUCERS.find(d => d.id === best), amount = def ? CR.buyCount(def) : 0;
        if (CR.buy(best) && def) recordEvent(`Bought ${amount} ${def.name}${amount === 1 ? "" : "s"} on advisor recommendation.`, "buy");
        after();
      }
      return;
    }
    if (key === "m") {
      const mgr = CR.bestManager();
      if (mgr && CR.hireManager(mgr)) {
        const def = CR.PRODUCERS.find(d => d.id === mgr);
        toast(`${def.icon}  ${def.name} manager hired.`, "ms");
        after();
      }
      return;
    }
    if (event.key === " " || event.key === "Enter") {
      // Space collects from the best producer you own: the early-game click loop
      // without hunting for a card.
      const owned = CR.PRODUCERS.filter(d => CR.state().producers[d.id].owned > 0
                                          && !CR.state().producers[d.id].manager);
      const pick = owned.length ? owned[owned.length - 1]
        : CR.PRODUCERS.filter(d => CR.state().producers[d.id].owned > 0).pop();
      if (pick) { doCollect(pick.id); after(); }
      if (event.preventDefault) event.preventDefault();
    }
  }

  /* ---- loop ------------------------------------------------------------- */

  let last = 0, sinceSave = 0, sinceCheck = 0;

  function frame(now) {
    requestAnimationFrame(frame);
    const dt = last ? Math.min(1.5, (now - last) / 1000) : 0;
    last = now;
    const panel = el("tab-game");
    const hidden = panel && panel.hidden;
    // The simulation still advances while the tab is hidden - it is an idle game -
    // but nothing is painted, so a background tab costs no layout work.
    if (dt > 0) {
      CR.tick(dt);
      sinceSave += dt;
      sinceCheck += dt;
      if (sinceSave > 4) { CR.save(); sinceSave = 0; }
      // Achievements are checked twice a second rather than per frame: 18 cheap
      // predicates is nothing, but nothing 60 times a second is still something.
      if (sinceCheck > 0.5) { announce(CR.checkAchievements()); sinceCheck = 0; }
    }
    if (!hidden) {
      for (const done of CR.takeFinished()) floatUnits(done.id, done.units);
      sweepFX(false);
      if (CR.isDirty()) buildAll();
      paint();
    }
  }

  function init() {
    if (!el("cr-producers")) return;      // markup absent: nothing to do
    const loaded = CR.load();
    if (!loaded) CR.applyStartPerks();
    const offline = loaded ? CR.offlineCatchUp() : 0;
    buildAll();
    recordEvent(loaded ? "Save restored; simulation resumed from persisted state." : "New farm started in manual mode.", loaded ? "ms" : "");
    paint();
    if (document.addEventListener) document.addEventListener("click", onClick);
    if (window.addEventListener) {
      window.addEventListener("keydown", onKey);
      window.addEventListener("beforeunload", () => CR.save());
    }
    if (offline > 0) {
      toast(`Welcome back \u2014 your managers banked ${fmt(offline)} produce while you were away.`, "ms");
    }
    announce(CR.checkAchievements());
    requestAnimationFrame(frame);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.CRUI = { fmt, secs, paint, buildAll, toast, adviceText, recordEvent };
})();
