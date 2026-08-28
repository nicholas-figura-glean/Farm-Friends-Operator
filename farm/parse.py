"""Parsers for Farm Friends plain-text tool output.

Every parser raises ParseDrift when it meets a line it does not recognize.
Failing loud is deliberate: the cycle halts before any mutating call rather
than acting on a half-understood game state.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import format_compat


class ParseDrift(ValueError):
    """Server text no longer matches the shapes this module knows."""


def _normalize(tool: str, text: str) -> str:
    """Apply the narrow compatibility adapter, then enforce parser bounds."""
    source = str(text or "")
    try:
        normalized = format_compat.normalize(tool, source)
    except Exception as exc:  # noqa: BLE001 - adapter failures are parser drift
        raise ParseDrift("%s compatibility adapter failed: %s" % (tool, str(exc)[:160]))
    if not isinstance(normalized, str):
        raise ParseDrift("%s compatibility adapter returned non-text" % tool)
    # A format translation may add bounded canonical labels, never materialize a
    # summarized 288k-animal response or turn a tiny payload into memory pressure.
    if len(normalized) > len(source) + 1_000_000:
        raise ParseDrift("%s compatibility adapter expanded response beyond bound" % tool)
    return normalized


ANIMAL_RE = re.compile(
    r"^\s+\S+\s+(?P<name>.+?) the (?P<kind>chicken|pig|beehive|sheep|cow) "
    r"\(#(?P<id>\d+)\) is (?P<mood>[a-z ]+)\. "
    r"hunger (?P<hunger>\d+)/100, happiness (?P<happiness>\d+)/100(?P<rest>.*)$"
)
READY_RE = re.compile(r"x(\d+) ready to collect")
ANIMAL_SUMMARY_HEADER_RE = re.compile(
    r"^Animals \((?P<total>\d+) total [^)]*summari[sz]ing by kind\):$",
    re.IGNORECASE,
)
ANIMAL_KIND_SUMMARY_RE = re.compile(
    r"^\s*\S+\s+(?P<kind>chicken|pig|beehive|sheep|cow): (?P<count>\d+)"
    r"(?:, (?P<ready>\d+) (?P<item>egg|truffle|honey|wool|milk) ready to collect)?$",
    re.IGNORECASE,
)
FIELD_RE = re.compile(r"^\s+\S+\s+(?P<crop>[a-z]+) plot \(#(?P<id>\d+)\): (?P<status>.+)$")
FIELD_SUMMARY_HEADER_RE = re.compile(
    r"^Fields \((?P<total>\d+) plots? [^)]*summari[sz]ing by kind\):$",
    re.IGNORECASE,
)
FIELD_KIND_SUMMARY_RE = re.compile(
    r"^\s*\S+\s+(?P<crop>[a-z]+): (?P<status>.+)$",
    re.IGNORECASE,
)
INVENTORY_RE = re.compile(r"([a-z]+) x(\d+)")
TRADE_RE = re.compile(
    r"^\s*#(?P<id>\d+): (?P<sender>.+?) offers (?P<offer_qty>\d+) (?P<offer_item>[a-z]+) "
    r"to (?P<recipient>.+?) for (?P<want_qty>\d+) (?P<want_item>[a-z]+)"
)
COINS_RE = re.compile(r"(\d+) coins?")
LEADER_RE = re.compile(
    r"^(?:\S+|\d+\.)\s+(?P<name>.+?): (?P<produce>\d+) produce, "
    r"(?P<animals>\d+) animals?, (?P<coins>\d+) coins?"
)
TOTAL_RE = re.compile(r"(\d+) ([a-z]+)")
SOLD_RE = re.compile(r"Sold (\d+) ([a-z]+) for (\d+) coins?\. You now have (\d+) coins?")
BOUGHT_RE = re.compile(r"Bought (\d+) feed for (\d+) coins?\. (\d+) coins? left")
ADOPT_RE = re.compile(r"Cost (\d+) coins?, (\d+) left\. Animal id #(\d+)")
EVENT_RE = re.compile(r"^(?P<time>\d{2}:\d{2}) UTC\s+(?P<text>.*)$")
PRODUCE_EVENT_RE = re.compile(r"laid|produced|made honey|found a truffle|gave milk|grew wool")
TWO_RE = re.compile(r"\bTWO\b")
RISK_EVENT_PATTERNS = (
    # Aliens abduct animals outright, so this is the only risk kind that removes
    # production capacity permanently rather than charging coins for it. It is
    # listed first because `risk_kind` returns the first pattern that matches and
    # an abduction report can also mention repair or vet costs.
    #
    # Detection only. The server exposes `call_fbi` to end an active invasion, but
    # it costs 80% of current gold -- roughly 81M coins at the time of writing --
    # and we have no measurement of the abduction rate, so there is no way yet to
    # know whether paying beats absorbing the loss. POSTMORTEM-run291 and run377 are
    # both about plausible interventions that cost more than they saved; spending
    # four fifths of the treasury on an unmeasured threat would be a third. The
    # abduction counts this makes visible are what a later evidence-gated decision
    # will be built from.
    ("aliens", re.compile(r"\b(?:alien|aliens|abduct(?:ed|ing|ion)?|ufo|flying saucer|invasion|tractor beam|beamed up)\b", re.IGNORECASE)),
    ("wolves", re.compile(r"\b(?:wolf|wolves)\b", re.IGNORECASE)),
    ("sickness", re.compile(r"\b(?:sick|sickness|disease|illness|vet)\b", re.IGNORECASE)),
    ("storm", re.compile(r"\b(?:storm|stormy|repair|damaged?)\b", re.IGNORECASE)),
    ("spoilage", re.compile(r"\b(?:spoil|spoiled|spoilage|rotted?)\b", re.IGNORECASE)),
)
COIN_EVENT_RE = re.compile(r"(\d+) coins?", re.IGNORECASE)

SALEABLE = ("egg", "honey", "truffle", "milk", "wool", "pumpkin", "corn", "wheat")
KIND_PRODUCE = {
    "chicken": "egg",
    "beehive": "honey",
    "cow": "milk",
    "pig": "truffle",
    "sheep": "wool",
}


@dataclass
class Animal:
    id: int
    name: str
    kind: str
    mood: str
    hunger: int
    happiness: int
    ready: int = 0


@dataclass
class Trade:
    id: int
    sender: str
    recipient: str
    offer_item: str
    offer_qty: int
    want_item: str
    want_qty: int

    @property
    def outgoing(self) -> bool:
        return self.sender.strip().lower() == "nick"


@dataclass
class Plot:
    id: int
    crop: str
    status: str

    @property
    def harvestable(self) -> bool:
        low = self.status.lower()
        return "ready" in low or "harvest" in low

    @property
    def food_crop(self) -> bool:
        return self.crop != "wildflowers"


@dataclass
class Farm:
    coins: int
    animals: List[Animal] = field(default_factory=list)
    plots: List[Plot] = field(default_factory=list)
    inventory: Dict[str, int] = field(default_factory=dict)
    trades: List[Trade] = field(default_factory=list)
    # Large farms are returned as authoritative totals plus representative rows.
    # Keep the samples for hunger/mood inspection without pretending their length
    # is the herd size or allocating hundreds of thousands of synthetic objects.
    animal_total: Optional[int] = None
    animal_counts: Dict[str, int] = field(default_factory=dict)
    summary_ready: Dict[str, int] = field(default_factory=dict)
    plot_total: Optional[int] = None

    @property
    def feed(self) -> int:
        return self.inventory.get("feed", 0)

    @property
    def animal_count(self) -> int:
        return int(self.animal_total) if self.animal_total is not None else len(self.animals)

    @property
    def counts_by_kind(self) -> Dict[str, int]:
        if self.animal_counts:
            return dict(self.animal_counts)
        out = {}
        for a in self.animals:
            out[a.kind] = out.get(a.kind, 0) + 1
        return out

    @property
    def animals_summarized(self) -> bool:
        return self.animal_total is not None

    @property
    def max_hunger(self) -> int:
        return max([a.hunger for a in self.animals], default=0)

    @property
    def ready_units(self) -> int:
        if self.summary_ready:
            return sum(self.summary_ready.values())
        return sum(a.ready for a in self.animals)

    @property
    def saleable(self) -> Dict[str, int]:
        return {k: v for k, v in self.inventory.items() if k in SALEABLE and v > 0}

    @property
    def committed_feed(self) -> int:
        return sum(t.offer_qty for t in self.trades if t.outgoing and t.offer_item == "feed")

    @property
    def incoming(self) -> List[Trade]:
        return [t for t in self.trades if not t.outgoing]

    @property
    def outgoing_recipients(self) -> List[str]:
        return [t.recipient for t in self.trades if t.outgoing]


@dataclass
class LeaderRow:
    rank: int
    name: str
    produce: int
    animals: int
    coins: int


def parse_farm(text: str) -> Farm:
    text = _normalize("list_farm", text)
    lines = text.split("\n")
    if "Farm" not in lines[0]:
        raise ParseDrift("list_farm header changed: %r" % text[:80])
    header = COINS_RE.search(lines[0])
    if not header:
        raise ParseDrift("no coin count in list_farm header: %r" % lines[0][:80])
    farm = Farm(coins=int(header.group(1)))
    section = None
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        animal_summary = ANIMAL_SUMMARY_HEADER_RE.match(stripped)
        if animal_summary:
            farm.animal_total = int(animal_summary.group("total"))
            section = "animal counts"
            continue
        field_summary = FIELD_SUMMARY_HEADER_RE.match(stripped)
        if field_summary:
            farm.plot_total = int(field_summary.group("total"))
            section = "field counts"
            continue
        if stripped.lower() == "a few of them up close:":
            section = "animal samples"
            continue
        if stripped.startswith("Barn inventory:"):
            section = None
            farm.inventory = {
                m.group(1): int(m.group(2))
                for m in INVENTORY_RE.finditer(stripped.split(":", 1)[1])
            }
            continue
        if stripped.endswith(":") and not stripped.startswith("#"):
            section = stripped[:-1].lower()
            continue
        if section == "animal counts":
            m = ANIMAL_KIND_SUMMARY_RE.match(line)
            if not m:
                raise ParseDrift("animal summary line drift: %r" % stripped[:120])
            kind = m.group("kind").lower()
            if kind in farm.animal_counts:
                raise ParseDrift("duplicate animal summary kind: %s" % kind)
            farm.animal_counts[kind] = int(m.group("count"))
            if m.group("ready"):
                item = str(m.group("item") or "").lower()
                if KIND_PRODUCE.get(kind) != item:
                    raise ParseDrift(
                        "animal summary produce mismatch: %s cannot produce %s"
                        % (kind, item)
                    )
                farm.summary_ready[item] = int(m.group("ready"))
        elif section in ("animals", "animal samples"):
            m = ANIMAL_RE.match(line)
            if not m:
                raise ParseDrift("animal line drift: %r" % stripped[:120])
            ready = READY_RE.search(m.group("rest") or "")
            farm.animals.append(
                Animal(
                    id=int(m.group("id")),
                    name=m.group("name"),
                    kind=m.group("kind"),
                    mood=m.group("mood"),
                    hunger=int(m.group("hunger")),
                    happiness=int(m.group("happiness")),
                    ready=int(ready.group(1)) if ready else 0,
                )
            )
        elif section == "field counts":
            m = FIELD_KIND_SUMMARY_RE.match(line)
            if not m:
                raise ParseDrift("field summary line drift: %r" % stripped[:120])
            farm.plots.append(
                Plot(id=0, crop=m.group("crop").lower(), status=m.group("status"))
            )
        elif section == "fields":
            m = FIELD_RE.match(line)
            if not m:
                raise ParseDrift("field line drift: %r" % stripped[:120])
            farm.plots.append(
                Plot(id=int(m.group("id")), crop=m.group("crop"), status=m.group("status"))
            )
        elif section == "open trades":
            m = TRADE_RE.match(line)
            if not m:
                if stripped.lower() in ("(none)", "none"):
                    continue
                raise ParseDrift("trade line drift: %r" % stripped[:120])
            farm.trades.append(
                Trade(
                    id=int(m.group("id")),
                    sender=m.group("sender"),
                    recipient=m.group("recipient"),
                    offer_item=m.group("offer_item"),
                    offer_qty=int(m.group("offer_qty")),
                    want_item=m.group("want_item"),
                    want_qty=int(m.group("want_qty")),
                )
            )
    if not farm.animals:
        raise ParseDrift("no animals parsed from list_farm")
    if farm.animal_total is not None:
        counted = sum(farm.animal_counts.values())
        if counted != farm.animal_total:
            raise ParseDrift(
                "animal summary total mismatch: declared %d, counted %d"
                % (farm.animal_total, counted)
            )
        if len(farm.animals) > farm.animal_total:
            raise ParseDrift("animal sample exceeds declared total")
    return farm


def parse_leaderboard(text: str) -> List[LeaderRow]:
    text = _normalize("leaderboard", text)
    rows = []
    for line in text.split("\n")[1:]:
        if not line.strip():
            continue
        m = LEADER_RE.match(line.strip())
        if not m:
            raise ParseDrift("leaderboard line drift: %r" % line.strip()[:120])
        rows.append(
            LeaderRow(
                rank=len(rows) + 1,
                name=m.group("name"),
                produce=int(m.group("produce")),
                animals=int(m.group("animals")),
                coins=int(m.group("coins")),
            )
        )
    if not rows:
        raise ParseDrift("empty leaderboard")
    return rows


def parse_collect(text: str) -> Dict[str, int]:
    """Return {item: qty} collected. Empty dict when nothing was ready."""
    text = _normalize("collect_produce", text)
    for line in text.split("\n"):
        if line.startswith("Total:"):
            return {m.group(2): int(m.group(1)) for m in TOTAL_RE.finditer(line)}
    low = text.lower()
    if not text.strip() or "nothing" in low or "no produce" in low or "empty" in low:
        return {}
    raise ParseDrift("collect output drift: %r" % text[:160])


def parse_sell(text: str) -> Dict[str, object]:
    text = _normalize("sell", text)
    m = SOLD_RE.search(text)
    if not m:
        raise ParseDrift("sell output drift: %r" % text[:160])
    return {
        "qty": int(m.group(1)),
        "item": m.group(2),
        "revenue": int(m.group(3)),
        "coins_after": int(m.group(4)),
    }


def parse_buy_feed(text: str) -> Dict[str, int]:
    text = _normalize("buy_feed", text)
    m = BOUGHT_RE.search(text)
    if not m:
        raise ParseDrift("buy_feed output drift: %r" % text[:160])
    return {"qty": int(m.group(1)), "cost": int(m.group(2)), "coins_after": int(m.group(3))}


def parse_adopt(text: str) -> Dict[str, int]:
    text = _normalize("adopt_animal", text)
    m = ADOPT_RE.search(text)
    if not m:
        raise ParseDrift("adopt output drift: %r" % text[:160])
    return {
        "cost": int(m.group(1)),
        "coins_after": int(m.group(2)),
        "animal_id": int(m.group(3)),
    }


@dataclass
class Event:
    time: str
    text: str

    @property
    def is_production(self) -> bool:
        return bool(PRODUCE_EVENT_RE.search(self.text))

    @property
    def risk_kind(self) -> Optional[str]:
        """Normalized daily-loss category, if this is a risk event."""
        for kind, pattern in RISK_EVENT_PATTERNS:
            if pattern.search(self.text):
                return kind
        return None

    @property
    def charged_coins(self) -> int:
        """Best-effort coin charge visible in a risk event."""
        if self.risk_kind is None:
            return 0
        amounts = [int(match.group(1)) for match in COIN_EVENT_RE.finditer(self.text)]
        return max(amounts, default=0)

    @property
    def units(self) -> int:
        if not self.is_production:
            return 0
        return 2 if TWO_RE.search(self.text) else 1


def parse_events(text: str) -> List[Event]:
    text = _normalize("farm_events", text)
    events = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        m = EVENT_RE.match(line.strip())
        if m:
            events.append(Event(time=m.group("time"), text=m.group("text")))
    return events


def latest_tick(events: List[Event]) -> Optional[str]:
    for ev in events:
        if ev.is_production:
            return ev.time
    return None


def tick_units(events: List[Event], tick: Optional[str]) -> Dict[str, int]:
    """Units produced within one tick timestamp, keyed by animal kind."""
    if tick is None:
        return {}
    out = {}
    for ev in events:
        if ev.time != tick or not ev.is_production:
            continue
        if "laid" in ev.text:
            kind = "chicken"
        elif "honey" in ev.text:
            kind = "beehive"
        else:
            kind = "other"
        out[kind] = out.get(kind, 0) + ev.units
    return out
