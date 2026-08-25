/* Coop Rush UI smoke test - drives the real render/input code in JavaScriptCore
 * against a hand-rolled DOM stub. There is no node and no browser automation on
 * this machine, so this is the only way to prove the UI wires up rather than
 * merely parses: it builds cards from the real markup ids, clicks real buttons
 * through the real delegated handler, and checks the numbers that come back.
 *
 *     osascript -l JavaScript game/test_ui.js     (from the project root)
 */
ObjC.import('Foundation');
function slurp(p) { return $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, null).js; }

var out = [], fails = 0;
function ok(c, label, detail) { out.push((c ? "  ok   " : "  FAIL ") + label + (detail ? "  [" + detail + "]" : "")); if (!c) fails++; }

/* ---- DOM stub: enough of the API that the UI uses, and no more ---- */
function Node(tag) {
  this.tagName = (tag || "div").toUpperCase();
  this.children = []; this.dataset = {}; this.style = {}; this.classes = {};
  this._html = ""; this._text = ""; this.hidden = false; this.disabled = false;
}
Node.prototype.appendChild = function (n) { this.children.push(n); n.parent = this; return n; };
Node.prototype.removeChild = function (n) {
  var i = this.children.indexOf(n);
  if (i < 0) throw new Error("removeChild: not a child");
  this.children.splice(i, 1);
  return n;
};
Node.prototype.querySelector = function (sel) { return (this.querySelectorAll(sel) || [])[0] || null; };
Node.prototype.querySelectorAll = function (sel) {
  // Only the selector shapes the UI actually uses.
  var all = collect(this), m;
  if ((m = sel.match(/^\[data-f="([^"]+)"\]$/))) return all.filter(function (n) { return n.dataset.f === m[1]; });
  if ((m = sel.match(/^\.cr-prod\[data-id="([^"]+)"\]$/))) return all.filter(function (n) { return n.classes["cr-prod"] && n.dataset.id === m[1]; });
  if ((m = sel.match(/^button\.(\w+)$/))) return all.filter(function (n) { return n.tagName === "BUTTON" && n.classes[m[1]]; });
  if ((m = sel.match(/^#(\S+) button$/))) { var host = document.getElementById(m[1]); return host ? collect(host).filter(function (n) { return n.tagName === "BUTTON"; }) : []; }
  if ((m = sel.match(/^#(\S+) \.cr-up:not\(\.owned\)$/))) { var h2 = document.getElementById(m[1]); return h2 ? collect(h2).filter(function (n) { return n.classes["cr-up"] && !n.classes.owned; }) : []; }
  return [];
};
Object.defineProperty(Node.prototype, "innerHTML", {
  get: function () { return this._html; },
  // Parsing is not the point of this stub: build nodes from the tags we emit.
  set: function (html) { this._html = html; this.children = []; parseInto(this, html); },
});
Object.defineProperty(Node.prototype, "textContent", {
  get: function () { return this._text; }, set: function (v) { this._text = String(v); },
});
Node.prototype.classList = null;
function classListFor(node) {
  return {
    add: function (c) { node.classes[c] = true; },
    remove: function (c) { delete node.classes[c]; },
    toggle: function (c, on) { if (on === undefined) on = !node.classes[c]; if (on) node.classes[c] = true; else delete node.classes[c]; },
    contains: function (c) { return !!node.classes[c]; },
  };
}
function collect(node, acc) {
  acc = acc || [];
  for (var i = 0; i < node.children.length; i++) { acc.push(node.children[i]); collect(node.children[i], acc); }
  return acc;
}
// Tag scanner that tracks nesting properly. The first version ignored closing
// tags and guessed with a depth cap, which put [data-f] nodes outside their card
// and made paint() throw on a null ref - a stub bug that looked like a UI bug.
var VOID = { br: 1, hr: 1, img: 1, input: 1, meta: 1, link: 1 };
function parseInto(host, html) {
  var re = /<(\/?)(\w+)([^>]*?)(\/?)>/g, m, stack = [host];
  while ((m = re.exec(html))) {
    var closing = m[1] === "/", tag = m[2].toLowerCase(), attrs = m[3], selfClose = m[4] === "/";
    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    var node = new Node(tag);
    node.classList = classListFor(node);
    var cls = /class="([^"]*)"/.exec(attrs);
    if (cls) cls[1].split(/\s+/).forEach(function (c) { if (c) node.classes[c] = true; });
    var id = /id="([^"]*)"/.exec(attrs);
    if (id) { node.id = id[1]; registry[id[1]] = node; }
    var da, dre = /data-([\w-]+)="([^"]*)"/g;
    while ((da = dre.exec(attrs))) {
      node.dataset[da[1].replace(/-(\w)/g, function (_, c) { return c.toUpperCase(); })] = da[2];
    }
    if (/(^|\s)disabled(\s|=|$)/.test(attrs)) node.disabled = true;
    stack[stack.length - 1].appendChild(node);
    if (!selfClose && !VOID[tag]) stack.push(node);
  }
}
var registry = {};
var listeners = {};
var document = {
  readyState: "complete",
  getElementById: function (id) { return registry[id] || null; },
  querySelectorAll: function (sel) { return Node.prototype.querySelectorAll.call(root, sel); },
  addEventListener: function (type, fn) { listeners[type] = fn; },
  // Effects (floating numbers, egg sparks) are appended nodes rather than an
  // innerHTML rebuild, because rebuilding restarts every in-flight animation.
  createElement: function (tag) { var n = new Node(tag); n.classList = classListFor(n); return n; },
};
var root = new Node("body"); root.classList = classListFor(root);
var window = { addEventListener: function (type, fn) { listeners["w:" + type] = fn; } };
var store = {};
var localStorage = { getItem: function (k) { return k in store ? store[k] : null; }, setItem: function (k, v) { store[k] = v; } };
var rafQueue = [];
function requestAnimationFrame(fn) { rafQueue.push(fn); }
function setTimeout() {}
function confirm() { return true; }

/* ---- build the DOM from the REAL markup fragment ---- */
var markup = slurp("game/coop_rush.html");
root.innerHTML = markup;
ok(markup.indexOf("the real one plateaued") < 0
   && markup.indexOf("short collection cohort appeared capped") >= 0
   && markup.indexOf("later superseded") >= 0,
   "plateau copy identifies a superseded run-46 collection proxy");
ok(markup.indexOf("The economy is deliberately abstract") >= 0
   && markup.indexOf("not a model of its full economy") >= 0,
   "economy copy distinguishes game units from the full real-farm economy");
ok(markup.indexOf("over the observed range; it does not prove unlimited scaling") >= 0
   && markup.indexOf("run-50 collection cohort") >= 0,
   "scaling and species claims are scoped to the current evidence");
ok(!!document.getElementById("cr-producers"), "markup provides the producer host");
ok(!!document.getElementById("cr-rebuild"), "markup provides the rebuild button");
ok(!!document.getElementById("cr-oneoff") && !!document.getElementById("cr-perks"), "markup provides both upgrade panels");

/* ---- load engine + UI with the stubs ---- */
var engine = slurp("game/coop_rush.js");
var ui = slurp("game/coop_rush_ui.js");
var CR = new Function("localStorage", engine + "\n;return CR;")(localStorage);
new Function("document", "window", "localStorage", "requestAnimationFrame", "setTimeout", "confirm", "CR",
             ui + "\n;return window.CRUI;");
var CRUI = new Function("document", "window", "localStorage", "requestAnimationFrame", "setTimeout", "confirm", "CR",
             ui + "\n;return window.CRUI;")(document, window, localStorage, requestAnimationFrame, setTimeout, confirm, CR);

ok(!!CRUI && typeof CRUI.fmt === "function", "UI initialised and exported its helpers");
ok(CRUI.fmt(1234) === "1.23K" && CRUI.fmt(1e9) === "1.00B", "number formatting", CRUI.fmt(1234) + " / " + CRUI.fmt(1e9));
ok(CRUI.fmt(Infinity) === "\u221E", "runaway numbers show as infinity, not 0");
ok(document.getElementById("cr-producers").children.length > 0, "producer cards were rendered",
   collect(document.getElementById("cr-producers")).filter(function (n) { return n.classes["cr-prod"]; }).length + " cards");

/* ---- drive the real click handler ---- */
var click = listeners["click"];
ok(typeof click === "function", "UI registered a delegated click handler");
function clickOn(node) { click({ target: { closest: function () { return node; } } }); }

var s = CR.state();
s.coins = 100000;
var card = document.getElementById("cr-producers").querySelector('.cr-prod[data-id="coop"]');
var buyBtn = card.querySelector("button.buy");
var ownedBefore = s.producers.coop.owned;
clickOn(buyBtn);
ok(CR.state().producers.coop.owned > ownedBefore, "clicking Buy adds producers",
   ownedBefore + " -> " + CR.state().producers.coop.owned);

var mgrBtn = card.querySelector("button.mgr");
clickOn(mgrBtn);
ok(CR.state().producers.coop.manager === true, "clicking Manager hires one");
ok(CR.unitsPerSec() > 0, "hiring a manager starts idle income", CRUI.fmt(CR.unitsPerSec()) + "/s");
CRUI.paint();
var expectedVsRun46 = CRUI.fmt(CR.unitsPerSec() / CR.RUN46_COLLECTION_PER_SEC) + "x";
ok(document.getElementById("cr-vs-real").textContent === expectedVsRun46,
   "HUD compares per-second output with the 1,550/min run-46 proxy",
   document.getElementById("cr-vs-real").textContent + " vs " + expectedVsRun46);

var modeBtn = document.querySelectorAll("#cr-buymode button").filter(function (b) { return b.dataset.mode === "100"; })[0];
clickOn(modeBtn);
ok(CR.state().buyMode === 100, "buy-mode buttons switch the amount", String(CR.state().buyMode));

var upBtn = document.querySelectorAll("#cr-oneoff .cr-up:not(.owned)")[0];
CR.state().coins = 1e9;
var upId = upBtn.dataset.id;
clickOn(upBtn);
ok(CR.state().oneoff[upId] === true, "clicking a one-off upgrade buys it", upId);

CR.state().lifetime = 4e6;
CRUI.paint();
ok(document.getElementById("cr-gain").textContent === "24", "rebuild panel shows the heirlooms on offer",
   document.getElementById("cr-gain").textContent);
ok(document.getElementById("cr-rebuild").disabled === false, "rebuild button enables past the gate");
clickOn(document.getElementById("cr-rebuild"));
ok(CR.state().rebuilds === 1 && CR.state().heirlooms === 24, "clicking Rebuild prestiges",
   CR.state().heirlooms + " heirlooms");

CRUI.paint();
ok(document.getElementById("cr-heirlooms").textContent === "24", "HUD reflects the new heirlooms");
ok(document.getElementById("cr-rebuild").disabled === true, "rebuild disables again after resetting");
ok(/Rebuild at 1\.00M produce/.test(document.getElementById("cr-rebuild").textContent),
   "locked button states the real gate", document.getElementById("cr-rebuild").textContent);

/* ---- keyboard ---- */
var keydown = listeners["w:keydown"];
ok(typeof keydown === "function", "keyboard handler registered");
function press(key) { keydown({ key: key, target: { tagName: "BODY" }, preventDefault: function () {} }); }
press("4");
ok(CR.state().buyMode === "max", "key 4 selects Max buy");

/* ---- the advisor, and the two keys that play it ---- */
CR.wipe();
CR.state().coins = 1e7;
CR.setBuyMode(1);
CRUI.buildAll();
CRUI.paint();
var advice = document.getElementById("cr-advice").textContent;
ok(advice.length > 10, "the advisor states a next purchase", advice);
ok(/press [BM]/.test(advice), "and names the key that does it", advice);
ok(CR.bestManager() === "coop", "a coop you own and cannot automate is the first advice");
press("m");
ok(CR.state().producers.coop.manager === true, "M hires the advised manager");
var ownedBeforeB = CR.state().producers.coop.owned;
press("b");
ok(CR.state().producers.coop.owned > ownedBeforeB || CR.state().producers.pig.owned > 0,
   "B buys the advised producer",
   "coops " + CR.state().producers.coop.owned + ", pigs " + CR.state().producers.pig.owned);
CRUI.paint();
var bestButtons = collect(document.getElementById("cr-producers"))
  .filter(function (n) { return n.tagName === "BUTTON" && n.classes.best; });
ok(bestButtons.length === 1, "exactly one button is marked as the best value", bestButtons.length + " marked");

/* ---- achievements ---- */
var achHost = document.getElementById("cr-achievements");
ok(!!achHost, "markup provides the achievements panel");
var achCards = collect(achHost).filter(function (n) { return n.classes["cr-ach"]; });
ok(achCards.length === CR.ACHIEVEMENTS.length, "every achievement is listed, locked ones included",
   achCards.length + " of " + CR.ACHIEVEMENTS.length);
ok(achCards.filter(function (n) { return n.classes.won; }).length > 0,
   "the ones already earned are marked as won");
ok(/^\d+ \/ \d+$/.test(document.getElementById("cr-ach-count").textContent),
   "the records panel counts them", document.getElementById("cr-ach-count").textContent);
ok(document.getElementById("cr-ach-bonus").textContent !== "x1.00",
   "and shows the bonus they are worth", document.getElementById("cr-ach-bonus").textContent);

/* ---- toasts stack rather than overwrite ---- */
var toastHost = document.getElementById("cr-toast");
var beforeToasts = collect(toastHost).filter(function (n) { return n.classes["cr-toast-item"]; }).length;
CRUI.toast("one"); CRUI.toast("two"); CRUI.toast("three");
var items = collect(toastHost).filter(function (n) { return n.classes["cr-toast-item"]; });
ok(items.length === Math.min(4, beforeToasts + 3),
   "three toasts show three toasts, not just the last one",
   beforeToasts + " + 3 -> " + items.length + " (queue caps at 4)");
ok(items.filter(function (n) { return n._text === "three" || /three/.test(n.parent && n.parent._html || ""); }).length > 0
   || /three/.test(toastHost._html), "the newest toast is present", toastHost._html.slice(-60));
ok(toastHost.classes.show === true, "the toast container is revealed while it holds something");

/* ---- floating numbers are appended, then swept by the frame loop ---- */
CR.wipe();
CR.state().coins = 1e6;
CR.buy("coop", 4);
CRUI.buildAll();
CRUI.paint();
var coopCard = document.getElementById("cr-producers").querySelector('.cr-prod[data-id="coop"]');
var fx = coopCard.querySelector('[data-f="fx"]');
ok(!!fx, "each card carries an effects layer");
var iconNode = collect(coopCard).filter(function (n) { return n.classes["cr-prod-icon"]; })[0];
clickOn(iconNode);
ok(fx.children.length > 0, "hand-collecting spawns effect nodes", fx.children.length + " nodes");
var sparks = fx.children.filter(function (n) { return n.classes["cr-spark"]; }).length;
ok(sparks === 6, "an egg burst is six sparks, capped so a held click cannot grow the DOM", String(sparks));
// Without a sweep the page leaks one node per click forever, which on an idle
// game means thousands. The frame loop owns that, not setTimeout.
var spawned = fx.children.length;
var raf = rafQueue.pop();
ok(typeof raf === "function", "the frame loop is scheduled");
var realNow = Date.now;
Date.now = function () { return realNow() + 5000; };   // 5s later: everything expired
raf(16);
Date.now = realNow;
ok(fx.children.length < spawned, "expired effects are removed again",
   spawned + " -> " + fx.children.length);

out.push("");
out.push(fails === 0 ? "UI SMOKE TEST PASSED" : fails + " FAILURES");
out.join("\n");
