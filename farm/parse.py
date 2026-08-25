"""Parsers for Farm Friends plain-text tool output.

Every parser raises ParseDrift when it meets a line it does not recognize.
Failing loud is deliberate: the cycle halts before any mutating call rather
than acting on a half-understood game state.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class ParseDrift(ValueError):
    """Server text no longer matches the shapes this module knows."""


ANIMAL_RE = re.compile(
    r"^\s+\S+\s+(?P<name>.+?) the (?P<kind>chicken|pig|beehive|sheep|cow) "
    r"\(#(?P<id>\d+)\) is (?P<mood>[a-z ]+)\. "
    r"hunger (?P<hunger>\d+)/100, happiness (?P<happiness>\d+)/100(?P<rest>.*)$"
)
READY_RE = re.compile(r"x(\d+) ready to collect")
FIELD_RE = re.compile(r"^\s+\S+\s+(?P<crop>[a-z]+) plot \(#(?P<id>\d+)\): (?P<status>.+)$")
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
    ("wolves", re.compile(r"\b(?:wolf|wolves)\b", re.IGNORECASE)),
    ("sickness", re.compile(r"\b(?:sick|sickness|disease|illness|vet)\b", re.IGNORECASE)),
    ("storm", re.compile(r"\b(?:storm|stormy|repair|damaged?)\b", re.IGNORECASE)),
    ("spoilage", re.compile(r"\b(?:spoil|spoiled|spoilage|rotted?)\b", re.IGNORECASE)),
)
COIN_EVENT_RE = re.compile(r"(\d+) coins?", re.IGNORECASE)

SALEABLE = ("egg", "honey", "truffle", "milk", "wool", "pumpkin", "corn", "wheat")


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

    @property
    def feed(self) -> int:
        return self.inventory.get("feed", 0)

    @property
    def animal_count(self) -> int:
        return len(self.animals)

    @property
    def counts_by_kind(self) -> Dict[str, int]:
        out = {}
        for a in self.animals:
            out[a.kind] = out.get(a.kind, 0) + 1
        return out

    @property
    def max_hunger(self) -> int:
        return max([a.hunger for a in self.animals], default=0)

    @property
    def ready_units(self) -> int:
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
        if section == "animals":
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
    return farm


def parse_leaderboard(text: str) -> List[LeaderRow]:
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
    for line in text.split("\n"):
        if line.startswith("Total:"):
            return {m.group(2): int(m.group(1)) for m in TOTAL_RE.finditer(line)}
    low = text.lower()
    if not text.strip() or "nothing" in low or "no produce" in low or "empty" in low:
        return {}
    raise ParseDrift("collect output drift: %r" % text[:160])


def parse_sell(text: str) -> Dict[str, object]:
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
    m = BOUGHT_RE.search(text)
    if not m:
        raise ParseDrift("buy_feed output drift: %r" % text[:160])
    return {"qty": int(m.group(1)), "cost": int(m.group(2)), "coins_after": int(m.group(3))}


def parse_adopt(text: str) -> Dict[str, int]:
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
