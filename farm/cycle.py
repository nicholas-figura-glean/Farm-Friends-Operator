"""The deterministic run loop.

Design notes worth keeping:
- Production is per-animal, not a global tick, so the loop never tries to align
  to one. It collects every run and measures units/chicken/minute.
- list_farm grows ~65 bytes per animal, so it is read once per run and re-read
  only when something actually changed the state (feeding, an accepted trade)
  or on the verify cadence.
- Every mutating call is bracketed by an intent record so a crashed run can be
  reconciled rather than guessed at.
- Raw responses go to state/raw/latest for post-mortem, never to stdout.
"""

import contextlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import (
    analysis,
    compaction,
    growth,
    heal,
    ledger,
    mcp,
    novelty,
    parse,
    policy,
    progress,
    questions,
    rules,
)
from .mcp import Client, McpError, ToolError

STATE_DIR = "state"
RAW_DIR = os.path.join(STATE_DIR, "raw", "latest")
HISTORY = os.path.join(STATE_DIR, "history.ndjson")
INTENTS = os.path.join(STATE_DIR, "intents.ndjson")
META = os.path.join(STATE_DIR, "meta.json")
_INTENT_STATE = threading.local()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ensure_dirs() -> None:
    os.makedirs(RAW_DIR, exist_ok=True)


def load_meta() -> Dict[str, Any]:
    try:
        with open(META) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"run": 0, "tools": [], "declines": {}, "offers": {}}


def save_meta(meta: Dict[str, Any]) -> None:
    _ensure_dirs()
    tmp = META + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)
    os.replace(tmp, META)


def tail_history(n: int) -> List[Dict[str, Any]]:
    return compaction.read_rows(HISTORY, limit=n)


def last_history() -> Optional[Dict[str, Any]]:
    rows = tail_history(1)
    return rows[0] if rows else None


def append_history(row: Dict[str, Any]) -> None:
    _ensure_dirs()
    compaction.append_json(HISTORY, row)


def _raw(name: str, text: str) -> None:
    _ensure_dirs()
    with open(os.path.join(RAW_DIR, name + ".txt"), "w") as fh:
        fh.write(text)


def _intent(action: str, **detail: Any) -> None:
    _ensure_dirs()
    compaction.append_json(INTENTS, {"ts": utcnow(), "action": action, "detail": detail})
    # Self-tests and low-level unit calls exercise these helpers without an
    # execution identity. Keep the legacy intent fixture behavior, but never
    # contaminate the epistemic ledger with an unscoped pseudo-observation.
    if not ledger.current().get("actor"):
        return
    phase = "outcome" if action.endswith("_done") else (
        "started" if action.endswith("_start") else "intent"
    )
    base = action[:-5] if action.endswith("_done") else (
        action[:-6] if action.endswith("_start") else action
    )
    pending = getattr(_INTENT_STATE, "pending", None)
    if pending is None:
        pending = {}
        _INTENT_STATE.pending = pending
    if phase == "outcome":
        ledger.intervention(
            base,
            phase,
            detail,
            intervention_id=pending.pop(base, None),
        )
    else:
        pending[base] = ledger.intervention(base, phase, detail)


def _project(state: parse.Farm, actions: Dict[str, Any], coins: int) -> parse.Farm:
    """End-state projection from responses already received (skips a re-read).

    Every field is derived from a parsed server response, not a guess: adoptions
    from successful adopt calls, feed from the buy_feed response, sales from the
    sell responses. The next verify run reconciles it and flags any mismatch.
    """
    projected = parse.Farm(
        coins=coins,
        animals=list(state.animals),
        plots=list(state.plots),
        inventory=dict(state.inventory),
        trades=list(state.trades),
    )
    next_id = max([a.id for a in state.animals], default=0)
    for i in range(int(actions.get("adopted") or 0)):
        projected.animals.append(
            parse.Animal(
                id=next_id + 1 + i,
                name="projected",
                kind=rules.PRIMARY_KIND,
                mood="content",
                hunger=0,
                happiness=100,
                ready=0,
            )
        )
    for item in (actions.get("sold") or {}):
        projected.inventory.pop(item, None)
    if actions.get("feed_bought"):
        projected.inventory["feed"] = state.feed + int(actions["feed_bought"])
    for animal in projected.animals:
        animal.ready = 0
    return projected


def _is_funds_error(message: str) -> bool:
    """True when the server refused a purchase purely for lack of coins.

    Parallel adoption can race the coin balance by a call or two, and the server
    answers with 'That costs 10 coins and you have 8'. That is a normal stop
    condition, not a fault: it must not count as a failure, must not back off the
    global rate limiter, and must not raise an alert.
    """
    low = message.lower()
    return "costs" in low and "you have" in low


class Cycle(object):
    BACKSTOP_HUNGER = 45
    # The main loop already collects and sells, and uncollected produce is not
    # lost (it accumulates), so the backstop only guards against starvation.
    collect_when_idle = False

    def __init__(self, client: Client, dry_run: bool = False):
        self.c = client
        self.dry = dry_run
        self.meta = load_meta()
        self.prev = last_history()
        self.policy = policy.runtime_context()
        self.recon_findings: List[str] = []
        self.reentry: Optional[Dict[str, Any]] = None
        # Supervisor-set knobs. They can only throttle growth or add bounded
        # extra work, and they relax on their own once runs are clean again.
        self.knobs = heal.knobs()
        self.coins = 0
        # Start at the ceiling when the previous run was clean; otherwise resume
        # the backed-off rate. This way raising the ceiling in rules.py takes
        # effect immediately, while a server that is pushing back keeps its
        # concession until it stops erroring.
        #
        # "Clean" deliberately uses rules.transport_trouble rather than
        # `transport_errors > 0`. A single retry per run is normal at this call
        # volume, and treating it as unclean pinned the limiter at the 0.5/s
        # floor for many consecutive runs, throttling the whole loop.
        ceiling = rules.rate_ceiling(self.knobs)
        prev_clean = bool(self.prev) and not (
            self.prev.get("adopt_failures")
            or rules.transport_trouble(
                rules.core_transport_errors(
                    self.prev.get("transport_errors_by_tool"),
                    self.prev.get("transport_errors") or 0,
                ),
                self.prev.get("calls") or 0,
            )
        )
        stored = float(self.meta.get("call_rate", ceiling))
        rate = (
            ceiling
            if prev_clean
            else min(max(stored, rules.MIN_CALLS_PER_SECOND), ceiling)
        )
        mcp.LIMITER.set_rate(rate)
        self.actions: Dict[str, Any] = {
            "collected": {},
            "collect_ts": None,
            "revenue": 0,
            "sold": {},
            "feed_bought": 0,
            "adopted": 0,
            "adopt_requested": 0,
            "adopt_stopped": "none",
            "adopt_failures": 0,
            "collect_passes": 1,
            "fed": False,
            "harvested": False,
            "verified": False,
            "trades_sent": 0,
            "trades_accepted": 0,
            "trades_declined": 0,
            "trade_decisions": [],
            "trade_coin_outflow": 0,
            "trade_coin_outflow_blocked": 0,
            "risk_events": [],
            "risk_event_counts": {},
            "risk_event_signatures": [],
            "risk_charges": 0,
        }
        self.notes: List[str] = []
        # Growth policy for this run, decided in the plan step.
        self.growth: Dict[str, Any] = {}
        # Pre-action strategic uncertainty. Holds affect only the named domains;
        # routine collect/feed/sell work continues while research investigates.
        self.novelty: Dict[str, Any] = {
            "signals": [], "active_blocks": [], "blocked_domains": []
        }
        # Soft notes are operational commentary (rate easing, pacing) that belong
        # in the record but must not raise an alert or cost anyone tokens.
        self.notes_soft: List[str] = []
        self.phases: Dict[str, float] = {}

    # -- individual steps ---------------------------------------------------
    def read_state(self, tag: str) -> parse.Farm:
        text = self.c.call("list_farm")
        _raw("list_farm_" + tag, text)
        farm = parse.parse_farm(text)
        self.coins = farm.coins
        return farm

    def collect(self) -> None:
        """Drain matured produce with exactly one constant-time bulk call."""
        try:
            text = self.c.call("collect_produce")
        except McpError as exc:
            # A failed collection must not prevent feeding or risk checks. The
            # next cycle performs another single bulk drain.
            self.notes_soft.append(
                "collect_produce skipped after transport failure: %s" % str(exc)[:80]
            )
            self.actions["collect_passes"] = 0
            return
        _raw("collect", text)
        self.actions["collected"] = parse.parse_collect(text)
        self.actions["collect_passes"] = 1
        self.actions["collect_ts"] = utcnow()

    def read_risk_events(self) -> None:
        """Ingest and deduplicate the server's random daily-loss events."""
        try:
            text = self.c.call("farm_events", limit=100)
        except (McpError, ToolError) as exc:
            self.notes.append("farm_events unavailable: %s" % str(exc)[:100])
            return
        _raw("events", text)
        parsed_events = parse.parse_events(text)
        self.actions["risk_event_signatures"] = sorted(
            set(
                "risk:%s" % event.risk_kind
                if event.risk_kind
                else novelty.event_signature(event.text)
                for event in parsed_events
            )
        )
        events = [event for event in parsed_events if event.risk_kind]
        identities = list(self.meta.get("seen_risk_events") or [])[-500:]
        seen = set(identities)
        new_events = []
        for event in events:
            key = "%s|%s" % (event.time, event.text)
            if key in seen:
                continue
            seen.add(key)
            identities.append(key)
            new_events.append(
                {
                    "time": event.time,
                    "kind": event.risk_kind,
                    "text": event.text[:240],
                    "charged_coins": event.charged_coins,
                }
            )
        # The endpoint is a rolling window. Bound ordered local identities too.
        self.meta["seen_risk_events"] = identities[-500:]
        self.actions["risk_events"] = new_events
        counts: Dict[str, int] = {}
        for event in new_events:
            kind = str(event["kind"])
            counts[kind] = counts.get(kind, 0) + 1
        self.actions["risk_event_counts"] = counts
        self.actions["risk_charges"] = sum(
            int(event.get("charged_coins") or 0) for event in new_events
        )
        if new_events:
            self.notes_soft.append(
                "daily risk events: %s"
                % ", ".join("%s=%d" % item for item in sorted(counts.items()))
            )

    def harvest_if_needed(self, farm: parse.Farm) -> None:
        if not any(p.food_crop and p.harvestable for p in farm.plots):
            return
        _raw("harvest", self.c.call("harvest"))
        self.actions["harvested"] = True

    def ensure_feed_on_hand(self, farm: parse.Farm) -> parse.Farm:
        """Buy feed BEFORE feeding if the larder cannot cover a bulk feed.

        This exists because of the run 291-293 deadlock. The pipeline order was
        feed -> ... -> buy_feed, and feed_animals raises ToolError when feed is
        0. That error propagated out of run(), so the cycle aborted before ever
        reaching buy_feed: feed stayed 0 forever and every subsequent cycle and
        supervisor pass crashed on the same line. The loop could not heal itself
        because the remedy lived downstream of the crash.

        Coins were never the constraint - 3.5M were banked. So the fix is to buy
        first whenever the reserve cannot cover one bulk feed.
        """
        need = rules.feed_reserve_target(farm.animal_count, farm.committed_feed)
        if farm.feed >= need:
            return farm
        want = min(need - farm.feed, self.coins // rules.FEED_COST)
        if want <= 0:
            self.notes.append(
                "cannot pre-buy feed: %d feed on hand, %d coins" % (farm.feed, self.coins)
            )
            return farm
        self.buy_feed(want)
        return self.read_state("prefeed_topup")

    def feed_if_needed(self, farm: parse.Farm, run_no: int) -> parse.Farm:
        """Feed with one constant-time whole-herd operation and never fan out."""
        hungry_word = any("starving" in a.mood for a in farm.animals)
        since = run_no - int(self.meta.get("last_feed_run", -99))
        if not rules.should_feed(farm.max_hunger, hungry_word, since):
            return farm
        _intent("feed_animals", max_hunger=farm.max_hunger, runs_since_feed=since)
        try:
            _raw("feed", self.c.call("feed_animals", animal_id="all"))
        except (McpError, ToolError) as exc:
            # Do not issue a second feed in this cycle: the operation is bulk and
            # transport outcomes can be ambiguous. Reconcile from state instead.
            self.notes.append("bulk feed unconfirmed: %s" % str(exc)[:100])
            try:
                return self.read_state("postfeed_failed")
            except Exception as read_exc:  # noqa: BLE001
                self.notes_soft.append(
                    "post-feed re-read skipped: %s" % str(read_exc)[:100]
                )
                return farm
        _intent("feed_animals_done", max_hunger=farm.max_hunger)
        self.actions["fed"] = True
        self.meta["last_feed_run"] = run_no
        return self.read_state("postfeed")

    # "You only have 549033 eggs, not 549088." The server names the true count when it
    # rejects an oversell, which is the only reliable number available: our own figure
    # came from a read that is already stale by the time the sell lands.
    _SHORTFALL = re.compile(r"only have\s+([0-9][0-9,]*)\s+(\w+)", re.IGNORECASE)

    @classmethod
    def _oversold_actual(cls, message: str, item: str) -> Optional[int]:
        """The real quantity from an oversell rejection, if that is what this was.

        Returns None for any other error, so an unrelated failure is never silently
        retried as though it were a quantity mismatch.
        """
        match = cls._SHORTFALL.search(str(message))
        if not match:
            return None
        if match.group(2).lower().rstrip("s") != str(item).lower().rstrip("s"):
            return None
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None

    def sell_all(self, inventory: Dict[str, int]) -> None:
        """Sell the plan, tolerating the inventory moving underneath us.

        Two failures are fixed here, and the second is the serious one.

        The quantity sold comes from a `list_farm` read taken earlier in the cycle, and
        the farm keeps changing after it: produce spoils, trades ship goods out, and the
        expand agent runs concurrently on its own schedule. So the sell is occasionally a
        few dozen units above what is actually there -- ten times so far, always by
        between 18 and 71 units out of hundreds of thousands.

        The stale read is not really avoidable; re-reading immediately before the sell
        narrows the window rather than closing it, at the cost of another call. But an
        oversell rejection names the true quantity, so the honest response is to sell
        that instead.

        The serious failure: this raised out of the cycle and aborted the entire run.
        Twenty-two crashes are recorded, ten of them this. A run that has already fed the
        herd, collected produce and handled trades must not discard all of it because the
        last few dozen eggs of a 549,000-egg sale were not there. Anything still
        unsellable is now noted and skipped.
        """
        for item, qty in rules.sell_plan(inventory):
            _intent("sell", item=item, qty=qty)
            try:
                text = self.c.call("sell", item=item, qty=qty)
            except ToolError as exc:
                actual = self._oversold_actual(str(exc), item)
                if actual is None:
                    # Not a quantity problem. Record it and move on to the next item
                    # rather than losing the whole cycle over one sale.
                    self.notes.append("sell %s failed: %s" % (item, str(exc)[:110]))
                    _intent("sell_failed", item=item, qty=qty, reason=str(exc)[:110])
                    continue
                if actual <= 0:
                    self.notes_soft.append(
                        "sell %s skipped: inventory emptied between read and sell" % item)
                    continue
                self.notes_soft.append(
                    "sell %s adjusted %d -> %d: inventory moved between read and sell"
                    % (item, qty, actual))
                _intent("sell_retry", item=item, qty=actual, requested=qty)
                try:
                    text = self.c.call("sell", item=item, qty=actual)
                except ToolError as retry_exc:
                    # Two rejections in a row means something other than drift, so stop
                    # guessing at quantities and leave the item for the next cycle.
                    self.notes.append(
                        "sell %s failed after adjustment: %s"
                        % (item, str(retry_exc)[:100]))
                    _intent("sell_failed", item=item, qty=actual,
                            reason=str(retry_exc)[:100])
                    continue
                qty = actual
            _raw("sell_" + item, text)
            result = parse.parse_sell(text)
            _intent("sell_done", item=item, qty=qty, revenue=result["revenue"])
            self.actions["revenue"] += int(result["revenue"])
            self.actions["sold"][item] = int(result["qty"])
            self.coins = int(result["coins_after"])

    def domain_blocked(self, domain: str) -> bool:
        return domain in set(self.novelty.get("blocked_domains") or [])

    def assess_novelty(
        self,
        run_no: int,
        tools: List[str],
        farm: parse.Farm,
        board: List[parse.LeaderRow],
    ) -> Dict[str, Any]:
        rivals = [entry for entry in board if entry.name.strip().lower() != "nick"]
        snapshot = {
            "run": run_no,
            "tools": tools,
            "trades": [
                {
                    "id": trade.id,
                    "sender": trade.sender,
                    "recipient": trade.recipient,
                    "offer_item": trade.offer_item,
                    "offer_qty": trade.offer_qty,
                    "want_item": trade.want_item,
                    "want_qty": trade.want_qty,
                    "outgoing": trade.outgoing,
                }
                for trade in farm.trades
            ],
            "rival_herds": {entry.name: entry.animals for entry in rivals},
            "rival_coins": {entry.name: entry.coins for entry in rivals},
            "risk_kinds": sorted((self.actions.get("risk_event_counts") or {}).keys()),
            "event_signatures": self.actions.get("risk_event_signatures") or [],
        }
        novelty_state = dict(self.meta.get("novelty") or {})
        # Bootstrap tool comparison from the pre-sentinel metadata so the first
        # upgraded run can still fail closed on a capability change.
        novelty_state.setdefault("tools", self.meta.get("tools") or [])
        assessed = novelty.assess(
            snapshot,
            self.prev,
            state=novelty_state,
            question_rows=questions.load_all(),
        )
        self.meta["novelty"] = assessed.pop("state")
        self.novelty = assessed
        for resolved in self.novelty.get("resolved_blocks") or []:
            self.notes_soft.append(
                "novelty hold released: %s (%s)"
                % (resolved.get("class"), resolved.get("reason"))
            )
        if self.novelty.get("blocked_domains"):
            self.notes_soft.append(
                "novelty hold active for %s"
                % ",".join(self.novelty["blocked_domains"])
            )
        return self.novelty

    def handle_incoming_trades(self, farm: parse.Farm) -> None:
        blocked_coin_offers = 0
        # Evaluate a batch against running balances. Otherwise two offers can
        # each look safe against the same snapshot but cumulatively consume the
        # protected feed reserve.
        balances = dict(farm.inventory)
        balances["coin"] = farm.coins
        for trade in farm.incoming:
            available_qty = balances.get(trade.want_item, 0)
            if trade.want_item == "coin":
                protected_qty = rules.RISK_COIN_RESERVE
            else:
                protected_qty = (
                    rules.feed_reserve_target(farm.animal_count, farm.committed_feed)
                    if trade.want_item == "feed"
                    else 0
                )
            decision = rules.trade_decision(
                trade.offer_item,
                trade.offer_qty,
                trade.want_item,
                trade.want_qty,
                available_qty=available_qty,
                protected_qty=protected_qty,
            )
            if self.domain_blocked("trades"):
                decision["accept"] = False
                decision["reason"] = (
                    "trade domain held while novel activity is under evidence review"
                )
            accept = bool(decision["accept"])
            detail = {
                "trade_id": trade.id,
                "sender": trade.sender,
                "offer_item": trade.offer_item,
                "offer_qty": trade.offer_qty,
                "want_item": trade.want_item,
                "want_qty": trade.want_qty,
                **decision,
            }
            self.actions["trade_decisions"].append(detail)
            if trade.want_item == "coin":
                if accept:
                    # This should remain unreachable while the categorical gate in
                    # rules.trade_decision is enabled. Persist it as an invariant so
                    # monitoring catches any future policy regression immediately.
                    self.actions["trade_coin_outflow"] += trade.want_qty
                else:
                    blocked_coin_offers += 1
                    self.actions["trade_coin_outflow_blocked"] += trade.want_qty
            _intent("respond_to_trade", **detail)
            _raw(
                "respond_%d" % trade.id,
                self.c.call("respond_to_trade", trade_id=trade.id, accept=accept),
            )
            _intent("respond_to_trade_done", **detail)
            if accept:
                self.actions["trades_accepted"] += 1
                balances[trade.want_item] = available_qty - trade.want_qty
                balances[trade.offer_item] = (
                    balances.get(trade.offer_item, 0) + trade.offer_qty
                )
            else:
                self.actions["trades_declined"] += 1
                key = trade.sender.strip()
                self.meta.setdefault("declines", {})
                self.meta["declines"][key] = self.meta["declines"].get(key, 0) + 1
        if blocked_coin_offers:
            self.notes_soft.append(
                "trade guard blocked %d coin-outflow offer(s), preserving %d coins"
                % (blocked_coin_offers, self.actions["trade_coin_outflow_blocked"])
            )

    def maintain_offers(self, farm: parse.Farm) -> None:
        paused = [
            name
            for name, count in (self.meta.get("declines") or {}).items()
            if count >= rules.DECLINE_PAUSE_THRESHOLD
        ]
        for target in rules.offer_targets(farm.outgoing_recipients, paused):
            _intent("propose_trade", to=target)
            _raw(
                "propose_" + target.replace(" ", "_").replace(".", ""),
                self.c.call(
                    "propose_trade",
                    to=target,
                    offer_item="feed",
                    offer_qty=rules.OFFER_FEED_QTY,
                    want_item="coin",
                    want_qty=rules.OFFER_COIN_WANT,
                ),
            )
            _intent("propose_trade_done", to=target)
            self.actions["trades_sent"] += 1
            self.meta.setdefault("offers", {})[target] = utcnow()

    def buy_feed(self, qty: int) -> None:
        if qty <= 0:
            return
        _intent("buy_feed", qty=qty)
        text = self.c.call("buy_feed", qty=qty)
        _raw("buy_feed", text)
        result = parse.parse_buy_feed(text)
        _intent("buy_feed_done", qty=qty, cost=result["cost"])
        self.actions["feed_bought"] += int(result["qty"])
        self.coins = int(result["coins_after"])

    def adopt_chickens(self, n: int, deadline: float) -> None:
        """Adopt until the plan is met, the clock runs out, or the server balks.

        Parallel above a threshold because adoptions per run grow with revenue.
        Total pressure is bounded by the global rate limiter, not by the worker
        count, and the limiter is lowered for the rest of the run on any error.
        """
        if n <= 0:
            return
        workers = 1 if n < rules.ADOPT_PARALLEL_THRESHOLD else rules.adopt_worker_count(self.knobs)
        _intent("adopt_batch_start", count=n, workers=workers, rate=mcp.LIMITER.rate)

        state = {
            "claimed": 0,
            "done": 0,
            "stop": None,
            "call_seconds": 0.5,
            "wall_seconds": 0.5,
            "coins": self.coins,
        }
        lock = threading.Lock()
        worker_context = ledger.current()

        def worker(client: Client) -> None:
            if threading.current_thread() is not threading.main_thread():
                ledger.set_context(
                    **dict(worker_context, worker=threading.current_thread().name)
                )
            while True:
                with lock:
                    if state["stop"] is not None:
                        return
                    # Claim a slot before calling. Checking completions instead
                    # let every worker pass the check on the last chicken, so
                    # workers-1 extra calls went out and failed on funds.
                    if state["claimed"] >= n:
                        return
                    # Budget prediction must use WALL time - the limiter wait is
                    # real elapsed time even though it is not server strain.
                    if time.time() + state.get("wall_seconds", 0.5) > deadline:
                        state["stop"] = "budget"
                        return
                    state["claimed"] += 1
                started = time.time()
                try:
                    result = parse.parse_adopt(
                        client.call("adopt_animal", kind=rules.PRIMARY_KIND)
                    )
                except (ToolError, McpError, parse.ParseDrift) as exc:
                    message = str(exc)
                    with lock:
                        state["claimed"] -= 1
                        if _is_funds_error(message):
                            # Not a fault: we simply bought everything affordable.
                            state["stop"] = "funds"
                        else:
                            state["stop"] = "error"
                            self.actions["adopt_failures"] += 1
                            self.notes.append("adopt stopped early: %s" % message[:120])
                    return
                with lock:
                    state["done"] += 1
                    state["coins"] = min(state["coins"], int(result["coins_after"]))
                    # Server service time, not wall time. Wall time here includes
                    # the wait for a rate-limiter slot (~workers/rate), which made
                    # the ease-off below throttle on its own throttling. See
                    # Client.last_service_seconds.
                    observed = client.last_service_seconds or (time.time() - started)
                    state["call_seconds"] = (
                        0.7 * state["call_seconds"] + 0.3 * observed
                    )
                    state["wall_seconds"] = (
                        0.7 * state.get("wall_seconds", 0.5)
                        + 0.3 * (time.time() - started)
                    )

        if workers == 1:
            worker(self.c)
        else:
            clients = [self.c] + [
                Client(endpoint=self.c.endpoint) for _ in range(workers - 1)
            ]
            threads = [threading.Thread(target=worker, args=(cl,)) for cl in clients]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            for cl in clients[1:]:
                self.c.call_count += cl.call_count
                self.c.transport_errors += cl.transport_errors

        self.coins = state["coins"]
        stopped = state["stop"]
        rate = mcp.LIMITER.rate
        ceiling = rules.rate_ceiling(self.knobs)
        if stopped == "error":
            rate = max(rate / 2.0, rules.MIN_CALLS_PER_SECOND)
        elif state["call_seconds"] > rules.SLOW_CALL_SECONDS:
            # Courtesy: rising latency means the server is straining even if it
            # has not started refusing. Ease off before it has to.
            #
            # `call_seconds` is SERVER service time only. Timing the limiter wait
            # too made this branch fire on its own output: at 6 workers and 2.5/s
            # each call "took" 2.4s against a 1.2s threshold, so the rate was cut
            # every run, which lengthened the wait, which cut it again. Adoption
            # is the only thing that scores, so this ease-off has to be provoked
            # by the server and nothing else.
            rate = max(rate * 0.8, rules.MIN_CALLS_PER_SECOND)
            self.notes_soft.append(
                "eased rate to %.2f/s: mean adopt service %.2fs (wall %.2fs)"
                % (rate, state["call_seconds"], state.get("wall_seconds", 0.0))
            )
        elif stopped in (None, "funds") and state["done"] >= 20:
            # A clean, sizeable batch earns a little of the budget back.
            rate = min(rate * 1.1, ceiling)
        mcp.LIMITER.set_rate(min(rate, ceiling))
        self.meta["call_rate"] = round(mcp.LIMITER.rate, 2)
        self.meta["adopt_call_seconds"] = round(state["call_seconds"], 3)
        self.meta["adopt_wall_seconds"] = round(state.get("wall_seconds", 0.0), 3)
        _intent(
            "adopt_batch_done",
            requested=n,
            adopted=state["done"],
            stopped=stopped,
            rate=round(rate, 2),
        )
        self.actions["adopted"] = state["done"]
        self.actions["adopt_requested"] = n
        self.actions["adopt_stopped"] = stopped or "complete"

    # -- backstop -----------------------------------------------------------
    def backstop(self) -> Dict[str, Any]:
        """Cheap guard against starvation only. One or two calls."""
        started = time.time()
        farm = self.read_state("backstop")
        acted = []

        hungry_word = any("hungry" in a.mood or "starving" in a.mood for a in farm.animals)
        target = rules.feed_reserve_target(farm.animal_count, farm.committed_feed)

        # Buy BEFORE feeding, for the same reason as the main cycle: an empty
        # larder made feed_animals raise and killed the run before the purchase
        # below could ever happen (runs 291-293).
        if farm.feed < target:
            want = min(target - farm.feed, self.coins // rules.FEED_COST)
            if want > 0:
                self.buy_feed(want)
                acted.append("bought %d feed" % want)
                farm = self.read_state("backstop_topup")

        if farm.max_hunger >= self.BACKSTOP_HUNGER or hungry_word:
            _intent("backstop_feed", max_hunger=farm.max_hunger)
            try:
                _raw("backstop_feed", self.c.call("feed_animals", animal_id="all"))
                acted.append("fed")
            except ToolError as exc:
                if "out of feed" not in str(exc).lower():
                    raise
                acted.append("feed failed: empty larder")

        if farm.ready_units > 0 and self.collect_when_idle:
            self.collect()
            acted.append("collected %d" % sum(self.actions["collected"].values()))
            self.sell_all(self.read_state("backstop_mid").saleable)
            if self.actions["revenue"]:
                acted.append("sold %dc" % self.actions["revenue"])

        return {
            "ts": utcnow(),
            "animals": farm.animal_count,
            "max_hunger": farm.max_hunger,
            "feed": farm.feed,
            "reserve_target": target,
            "coins": self.coins,
            "ready_units": farm.ready_units,
            "acted": acted,
            "calls": self.c.call_count,
            "duration_s": round(time.time() - started, 1),
        }

    def _phase(self, name: str, started: float) -> None:
        """Record wall time per phase so the next bottleneck is measured."""
        self.phases[name] = round(time.time() - started, 1)

    @contextlib.contextmanager
    def _step(self, name: str):
        """Time a phase and publish live progress for the dashboard.

        Yields a dict; whatever the body puts in it is attached to the step as
        detail. Progress writes are best-effort inside farm/progress.py, so a
        monitoring problem can never fail a run, but a raising body is still
        recorded as a failed step before the exception propagates.
        """
        started = time.time()
        detail: Dict[str, Any] = {}
        if not self.dry:
            progress.start(name)
        with ledger.bind(step=name):
            try:
                yield detail
            except BaseException as exc:  # noqa: BLE001 - re-raised immediately
                if not self.dry:
                    progress.fail(name, "%s: %s" % (exc.__class__.__name__, exc))
                raise
        self._phase(name, started)
        if not self.dry:
            progress.done(name, seconds=self.phases.get(name), **detail)

    def _skip(self, name: str, note: str) -> None:
        if not self.dry:
            progress.skip(name, note)

    def _reentry_recon(self, run_no: int) -> Optional[Dict[str, Any]]:
        """Read standings before mutation after a blind window."""
        previous_ts = parse_ts((self.prev or {}).get("ts"))
        if not previous_ts:
            return None
        now = datetime.now(timezone.utc)
        minutes = (now - previous_ts).total_seconds() / 60.0
        if minutes <= rules.GAP_RECON_MINUTES:
            return None
        text = self.c.call("leaderboard")
        _raw("leaderboard_reentry", text)
        board = parse.parse_leaderboard(text)
        me = next((entry for entry in board if entry.name.strip().lower() == "nick"), None)
        rivals = [entry for entry in board if entry.name.strip().lower() != "nick"]
        observed = {
            "ts": utcnow(),
            "run": run_no,
            "rank": me.rank if me else None,
            "produce": me.produce if me else None,
            "animals": me.animals if me else None,
            "coins": me.coins if me else None,
            "rivals": {entry.name: entry.produce for entry in rivals},
            "rival_herds": {entry.name: entry.animals for entry in rivals},
            "rival_coins": {entry.name: entry.coins for entry in rivals},
        }
        history = tail_history(
            rules.RIVAL_WAKE_RECENT_INTERVALS + rules.RIVAL_WAKE_BASE_ROWS + 2
        )
        findings = rules.rival_wakes(history + [observed])
        self.recon_findings = [finding["alert"] for finding in findings]
        self.reentry = {
            "minutes": round(minutes, 2),
            "observed": observed,
            "findings": findings,
        }
        if not self.dry:
            ledger.blind_window(self.prev or {}, observed, minutes, self.recon_findings)
        return self.reentry

    # -- orchestration ------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        run_no = int(self.meta.get("run", 0)) + 1
        context = {
            "actor": "cycle",
            "run": run_no,
            "policy_id": self.policy.get("policy_id"),
            "claim_registry_version": self.policy.get("claim_registry_version"),
        }
        with ledger.bind(**context):
            if not self.dry:
                ledger.record(
                    "cycle.started",
                    {
                        "dry": False,
                        "previous_run": (self.prev or {}).get("run"),
                        "policy_compatible": self.policy.get("compatible"),
                        "policy_errors": self.policy.get("errors"),
                    },
                )
            try:
                return self._run_impl()
            except BaseException as exc:  # noqa: BLE001 - preserve the caller's failure path
                if not self.dry:
                    ledger.record(
                        "cycle.failed",
                        {"error_type": exc.__class__.__name__, "error": str(exc)[:500]},
                    )
                raise

    def _run_impl(self) -> Dict[str, Any]:
        started = time.time()
        deadline = started + rules.CYCLE_BUDGET_SECONDS
        run_no = int(self.meta.get("run", 0)) + 1
        if not self.dry:
            progress.begin(
                run_no, self.dry, rules.CYCLE_BUDGET_SECONDS, rules.CYCLE_HARD_TIMEOUT
            )
        with self._step("tools") as step:
            tools = self.c.tool_names()
            step["count"] = len(tools)

        previous_ts = parse_ts((self.prev or {}).get("ts"))
        gap_minutes = (
            (datetime.now(timezone.utc) - previous_ts).total_seconds() / 60.0
            if previous_ts else 0.0
        )
        if gap_minutes > rules.GAP_RECON_MINUTES:
            with self._step("recon") as step:
                recon = self._reentry_recon(run_no)
                step["gap_min"] = round(gap_minutes, 1)
                step["findings"] = len((recon or {}).get("findings") or [])
        else:
            self._skip("recon", "no blind window")

        if self.dry:
            state = self.read_state("dry")
            decision = growth.decide(
                state.animal_count, self.knobs, run_no, persist=False
            )
            self.growth = decision
            plan = rules.expansion_plan(
                state.coins,
                state.animal_count,
                state.feed,
                state.committed_feed,
                cap=decision["cap"],
            )
            board = parse.parse_leaderboard(self.c.call("leaderboard"))
            return self._finish(started, tools, state, board, state, plan)

        # Both herd-scale operations are now constant-time bulk calls. Collection
        # runs exactly once every cycle to minimize the inventory exposed to the
        # new spoilage event; there is no cadence, loop, or parallel fan-out.
        with self._step("collect") as step:
            self.collect()
            step["units"] = sum(self.actions["collected"].values())
            step["passes"] = self.actions.get("collect_passes")

        with self._step("read") as step:
            state = self.read_state("state")
            step["animals"] = state.animal_count
            step["coins"] = state.coins
            step["hunger"] = state.max_hunger
            step["ready"] = state.ready_units

        if any(p.food_crop and p.harvestable for p in state.plots):
            with self._step("harvest"):
                self.harvest_if_needed(state)
        else:
            self._skip("harvest", "no harvestable food crops")

        with self._step("feed") as step:
            # Top up first: feeding with an empty larder used to abort the whole
            # run before buy_feed could fix it (runs 291-293).
            state = self.ensure_feed_on_hand(state)
            state = self.feed_if_needed(state, run_no)
            step["fed"] = self.actions["fed"]
            step["hunger_after"] = state.max_hunger
            step["buffer_min"] = round(
                rules.feed_buffer_minutes(state.feed, state.animal_count), 1
            )

        # Observe the competitive and game surface before any strategic mutation.
        # These reads used to happen after adoption and offers, which meant the
        # system could notice a new regime only after acting inside it.
        with self._step("board") as step:
            board_text = self.c.call("leaderboard")
            _raw("leaderboard", board_text)
            board = parse.parse_leaderboard(board_text)
            me = next((r for r in board if r.name.strip().lower() == "nick"), None)
            step["rank"] = me.rank if me else None
            step["produce"] = me.produce if me else None

        with self._step("events") as step:
            self.read_risk_events()
            step["new"] = len(self.actions["risk_events"])
            step["counts"] = self.actions["risk_event_counts"]
            step["charges"] = self.actions["risk_charges"]

        with self._step("novelty") as step:
            assessed = self.assess_novelty(run_no, tools, state, board)
            step["signals"] = len(assessed.get("signals") or [])
            step["blocked"] = assessed.get("blocked_domains") or []

        if state.incoming:
            with self._step("trades") as step:
                self.handle_incoming_trades(state)
                step["accepted"] = self.actions["trades_accepted"]
                step["declined"] = self.actions["trades_declined"]
            if self.actions["trade_decisions"]:
                # Accept and decline both remove an open offer. Re-read so the
                # final row does not report a handled trade as still pending.
                state = self.read_state("posttrade")
        else:
            self._skip("trades", "no incoming offers")

        with self._step("sell") as step:
            self.sell_all(state.saleable)
            step["revenue"] = self.actions["revenue"]
            step["items"] = len(self.actions["sold"])

        with self._step("plan") as step:
            # Adoption is gated on measured evidence that a bigger herd still
            # produces more, not just on whether the cycle has time for it.
            decision = growth.decide(state.animal_count, self.knobs, run_no)
            self.growth = decision
            plan = rules.expansion_plan(
                self.coins,
                state.animal_count,
                state.feed,
                state.committed_feed,
                cap=decision["cap"],
            )
            if self.domain_blocked("adopt") and plan["adopt"] > 0:
                plan["adopt_before_novelty_hold"] = plan["adopt"]
                plan["adopt"] = 0
                plan["novelty_hold"] = "adopt"
            step["adopt"] = plan["adopt"]
            step["buy_feed"] = plan["buy_feed"]
            step["growth"] = "saturated" if decision["verdict"].get("saturated") else "growing"
            if decision["changed"]:
                self.notes_soft.append("growth policy changed: %s" % decision["reason"])
        # Adopt BEFORE topping up feed, then size the feed purchase to the number
        # actually adopted. The reverse order pre-committed feed for a planned
        # count that the wall-clock budget then cut short, so the reserve could
        # land under target whenever adoption stopped early. Coins for feed are
        # still reserved by expansion_plan, so buying after adopting is safe:
        # remaining coins always cover the (smaller) real requirement.
        if plan["adopt"] > 0:
            with self._step("adopt") as step:
                self.adopt_chickens(plan["adopt"], deadline)
                step["adopted"] = self.actions["adopted"]
                step["requested"] = self.actions["adopt_requested"]
                step["stopped"] = self.actions["adopt_stopped"]
                step["rate"] = self.meta.get("call_rate")
        else:
            self._skip("adopt", (self.growth.get("reason") or "nothing affordable")[:160])
        animals_now = state.animal_count + self.actions["adopted"]
        needed = rules.feed_reserve_target(animals_now, state.committed_feed) - state.feed
        want_feed = min(max(0, needed), self.coins)
        if want_feed > 0:
            with self._step("buy_feed") as step:
                self.buy_feed(want_feed)
                step["bought"] = self.actions["feed_bought"]
        else:
            self._skip("buy_feed", "reserve already met")

        offer_targets = rules.offer_targets(
            state.outgoing_recipients,
            [
                name
                for name, count in (self.meta.get("declines") or {}).items()
                if count >= rules.DECLINE_PAUSE_THRESHOLD
            ],
        )
        if self.domain_blocked("offers"):
            self._skip("offers", "novel activity hold")
        elif offer_targets:
            with self._step("offers") as step:
                self.maintain_offers(state)
                step["sent"] = self.actions["trades_sent"]
        else:
            self._skip("offers", "offer slots already full")

        must_verify = (
            run_no % rules.VERIFY_EVERY == 0
            or self.actions["adopt_failures"] > 0
            or self.c.transport_errors > 0
            # A buy response acknowledges the purchase before the preceding
            # bulk-feed debit is always visible to list_farm. Verify every
            # top-up so a delayed debit cannot leave the reserve short.
            or self.actions["feed_bought"] > 0
        )
        if must_verify:
            with self._step("verify") as step:
                final = self.read_state("final")
                expected = state.animal_count + self.actions["adopted"]
                step["animals"] = final.animal_count
                step["expected"] = expected
                # Only a SHORTFALL is a problem. The expansion agent
                # (deploy/com.nickfigura.farmfriends.expand.plist) adopts
                # concurrently and takes no lock, so a final count above the
                # cycle's own expectation is normal and must not raise an alert.
                if final.animal_count < expected:
                    animal_risk = any(
                        event.get("kind") in ("wolves", "sickness")
                        for event in self.actions["risk_events"]
                    )
                    note = "animal count %d below expected %d" % (
                        final.animal_count,
                        expected,
                    )
                    if animal_risk:
                        self.notes_soft.append(note + " (explained by daily risk event)")
                    else:
                        self.notes.append(note)
                elif final.animal_count > expected:
                    self.notes_soft.append(
                        "animal count %d above expected %d (concurrent expansion)"
                        % (final.animal_count, expected)
                    )
                if self.actions["feed_bought"] > 0:
                    target = rules.feed_reserve_target(
                        final.animal_count, final.committed_feed
                    )
                    shortfall = max(0, target - final.feed)
                    if shortfall > 0 and shortfall <= self.coins:
                        # The first purchase can race the debit from the bulk
                        # feed call. Reconcile once from a confirmed server
                        # listing; buy_feed is cumulative so the journal records
                        # the actual total purchased this run.
                        self.buy_feed(shortfall)
                        final = self.read_state("final_reconcile")
                        final_target = rules.feed_reserve_target(
                            final.animal_count, final.committed_feed
                        )
                        if final.feed < final_target:
                            remaining = final_target - final.feed
                            tolerance = max(
                                rules.FEED_RESERVE_TOLERANCE_MIN,
                                int(final_target * rules.FEED_RESERVE_TOLERANCE_FRACTION),
                            )
                            message = (
                                "feed reserve still short after reconciliation: %d/%d"
                                % (final.feed, final_target)
                            )
                            if remaining <= tolerance:
                                self.notes_soft.append(
                                    "%s (within %d tolerance)" % (message, tolerance)
                                )
                            else:
                                self.notes.append(message)
                self.actions["verified"] = True
        else:
            self._skip("verify", "projected; next verify on cadence")
            final = _project(state, self.actions, self.coins)

        with self._step("finish") as step:
            row = self._finish(started, tools, state, board, final, plan)
            step["run"] = row.get("run")
            step["animals"] = row.get("animals")
        return row

    def _finish(self, started, tools, state, board, final, plan) -> Dict[str, Any]:
        self.meta["run"] = int(self.meta.get("run", 0)) + (0 if self.dry else 1)
        prev_tools = self.meta.get("tools") or []
        tools_changed = bool(prev_tools) and prev_tools != tools
        self.meta["tools"] = tools

        me = next((r for r in board if r.name.strip().lower() == "nick"), None)
        rivals = [r for r in board if r.name.strip().lower() != "nick"]
        chickens = state.counts_by_kind.get("chicken", 0)
        collected = self.actions["collected"]
        units = sum(collected.values())

        # Throughput is measured collect-to-collect, because production is
        # per-animal and each run's own duration varies with herd size.
        prev_collect = parse_ts(
            (self.prev or {}).get("collect_ts") or (self.prev or {}).get("ts")
        )
        this_collect = parse_ts(self.actions.get("collect_ts")) or datetime.now(timezone.utc)
        interval = None
        if prev_collect:
            interval = (this_collect - prev_collect).total_seconds() / 60.0
        per_chicken_min = None
        if interval and interval > 0 and chickens:
            per_chicken_min = round(units / float(chickens) / interval, 4)

        zero_streak = 0
        if units == 0:
            zero_streak = int((self.prev or {}).get("zero_streak", 0)) + 1

        row = {
            "ts": utcnow(),
            "run": self.meta["run"],
            "dry": self.dry,
            "rank": me.rank if me else None,
            "produce": me.produce if me else None,
            "animals": final.animal_count,
            "by_kind": final.counts_by_kind,
            "coins": final.coins,
            "feed": final.feed,
            "max_hunger": final.max_hunger,
            "reserve_target": rules.feed_reserve_target(
                final.animal_count, final.committed_feed
            ),
            "collected": collected,
            "collect_ts": self.actions.get("collect_ts"),
            "collect_passes": self.actions.get("collect_passes"),
            "units_collected": units,
            "ready_units": final.ready_units,
            "interval_min": round(interval, 2) if interval else None,
            "units_per_chicken_min": per_chicken_min,
            "zero_streak": zero_streak,
            "revenue": self.actions["revenue"],
            "feed_share": (
                round(float(self.actions["feed_bought"]) / self.actions["revenue"], 3)
                if self.actions["revenue"]
                else None
            ),
            "feed_bought": self.actions["feed_bought"],
            "adopted": self.actions["adopted"],
            "adopt_requested": self.actions["adopt_requested"],
            "adopt_stopped": self.actions["adopt_stopped"],
            "adopt_failures": self.actions["adopt_failures"],
            "verified": self.actions["verified"],
            "fed": self.actions["fed"],
            "risk_events": self.actions["risk_events"],
            "risk_event_counts": self.actions["risk_event_counts"],
            "risk_event_signatures": self.actions["risk_event_signatures"],
            "risk_charges": self.actions["risk_charges"],
            "risk_coin_reserve": rules.RISK_COIN_RESERVE,
            "harvested": self.actions["harvested"],
            "trades_out": len([t for t in final.trades if t.outgoing]),
            "trades_in": len([t for t in final.trades if not t.outgoing]),
            "trades_sent": self.actions["trades_sent"],
            "trades_accepted": self.actions["trades_accepted"],
            "trades_declined": self.actions["trades_declined"],
            "trade_decisions": self.actions["trade_decisions"],
            "trade_coin_outflow": self.actions["trade_coin_outflow"],
            "trade_coin_outflow_blocked": self.actions["trade_coin_outflow_blocked"],
            "rivals": {r.name: r.produce for r in rivals},
            # Herd and coins for every rival, not just produce. The leaderboard
            # reported all three the whole time and we only ever read produce,
            # which is why "John is frozen at 56,061 animals on 76 coins" - the
            # single most decision-relevant fact in the game - was invisible to
            # the loop and had to be re-derived by hand from a raw response.
            #
            # Produce alone cannot distinguish the two cases that matter:
            #   rate up because the rival is ADOPTING  -> we must out-adopt them
            #   rate up because the rival got FED      -> their rate is capped
            # Herd separates them, and coins say whether they can sustain it.
            "rival_herds": {r.name: r.animals for r in rivals},
            "rival_coins": {r.name: r.coins for r in rivals},
            "leader": (
                max(board, key=lambda r: r.produce).name if board else None
            ),
            "plan": plan,
            "growth": {
                "saturated": bool((self.growth.get("verdict") or {}).get("saturated")),
                "cap": self.growth.get("cap"),
                "reason": self.growth.get("reason"),
                "recent_units_per_min": (self.growth.get("verdict") or {}).get(
                    "recent_units_per_min"
                ),
                "smaller_units_per_min": (self.growth.get("verdict") or {}).get(
                    "smaller_units_per_min"
                ),
            },
            "plan_inputs": {
                "coins": state.coins,
                "animals": state.animal_count,
                "feed": state.feed,
                "committed_feed": state.committed_feed,
            },
            "tools_changed": tools_changed,
            "call_rate": self.meta.get("call_rate", rules.MAX_CALLS_PER_SECOND),
            "knobs": dict(self.knobs or {}),
            "calls": self.c.call_count,
            "transport_errors": self.c.transport_errors,
            "transport_errors_by_tool": dict(self.c.transport_errors_by_tool),
            # Retries a lower call rate could actually have prevented. The
            # detectors and the healer use this, never the raw total.
            "transport_errors_core": rules.core_transport_errors(
                self.c.transport_errors_by_tool, self.c.transport_errors
            ),
            "duration_s": round(time.time() - started, 1),
            "notes": self.notes,
            "notes_soft": self.notes_soft,
            "phases": self.phases,
            "adopt_call_seconds": self.meta.get("adopt_call_seconds"),
            "policy_id": self.policy.get("policy_id"),
            "policy_compatible": self.policy.get("compatible"),
            "claim_registry_version": self.policy.get("claim_registry_version"),
            "claim_validated_runs": dict(self.policy.get("claim_validated_runs") or {}),
            "reentry": self.reentry,
            "recon_findings": list(self.recon_findings),
            "novelty": self.novelty,
        }
        row["decision_trace"] = policy.decision_trace(
            plan,
            row["plan_inputs"],
            growth=row["growth"],
            context=self.policy,
        )
        row["regimes"] = analysis.regime_labels(row, self.prev)
        save_meta(self.meta)
        return row
