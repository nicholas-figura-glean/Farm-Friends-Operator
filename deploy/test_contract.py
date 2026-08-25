#!/usr/bin/env python3
"""Contract capture and drift classification suite.

The contract watcher is allowed to trigger code rewrites, so its two failure
modes are both expensive:

  * a **false negative** lets a breaking server change reach the cycle, and the
    farm stops feeding until a human notices;
  * a **false positive** wakes the author agent, spends tokens, and risks
    publishing a patch for a change that never happened.

These tests pin both directions. Shape stability is checked against real captured
`state/raw/latest/` samples rather than synthetic strings, because the whole point
of the shape function is to be invariant to live game data.
"""

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from farm import contract

FAILURES = []
CHECKS = [0]


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def section(name):
    print("\n== %s" % name)


# -- fixtures ---------------------------------------------------------------

LIST_FARM_A = """\
🌾 Nick's Farm  🪙 79171475 coins

Animals:
  🐔 Pecky the chicken (#7) is delighted. hunger 0/100, happiness 100/100
  🐝 Gold Rush the beehive (#9) is delighted. hunger 0/100, happiness 100/100
  🐷 Truffle Shuffle the pig (#13) is delighted. hunger 0/100, happiness 100/100
"""

# Same server version, wildly different game state: more animals, different
# names, different moods, different hunger values, different coin balance.
LIST_FARM_B = """\
🌾 Nick's Farm  🪙 3 coins

Animals:
  🐔 Henrietta the chicken (#88214) is content. hunger 43/100, happiness 61/100
  🐔 Dot the chicken (#88215) is hungry. hunger 91/100, happiness 12/100
  🐷 Bacon Bit the pig (#90001) is content. hunger 12/100, happiness 88/100
  🐝 Buzzy the beehive (#90002) is delighted. hunger 0/100, happiness 100/100
  🐔 Clucky the chicken (#90003) is delighted. hunger 0/100, happiness 100/100
"""

# A genuine format change: hunger/happiness collapsed into one 'status' field.
LIST_FARM_CHANGED = """\
🌾 Nick's Farm  🪙 79171475 coins

Animals:
  🐔 Pecky the chicken (#7) status delighted [fed]
  🐝 Gold Rush the beehive (#9) status delighted [fed]
"""


def tools_fixture():
    """A miniature tools/list result shaped like the real server's."""
    return [
        {
            "name": "feed_animals",
            "description": "Feed animals on your farm.",
            "inputSchema": {
                "type": "object",
                "properties": {"animal_id": {"type": "string", "description": "id or 'all'"}},
                "required": [],
            },
        },
        {
            "name": "adopt_animal",
            "description": "Adopt a new animal.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["chicken", "cow", "pig", "beehive"]},
                    "qty": {"type": "integer"},
                },
                "required": ["kind"],
            },
        },
        {
            "name": "gift",
            "description": "Gift an item to another farmer.",
            "inputSchema": {
                "type": "object",
                "properties": {"to": {"type": "string"}, "item": {"type": "string"}, "qty": {"type": "integer"}},
                "required": ["to", "item", "qty"],
            },
        },
    ]


def snapshot(tools, shapes=None, rely=None):
    snap = {
        "ts": "2026-08-25T00:00:00Z",
        "tools": contract.normalize_tools(tools),
        "shapes": shapes or {},
        # feed_animals and adopt_animal are called by our code; gift is not.
        "reliance": rely
        if rely is not None
        else {
            "feed_animals": {"args": ["animal_id"], "sites": ["farm/cycle.py:357"]},
            "adopt_animal": {"args": ["kind", "qty"], "sites": ["farm/cycle.py:900"]},
        },
    }
    snap["fingerprint"] = contract.fingerprint(snap)
    return snap


def find(changes, kind, tool=None):
    for change in changes:
        if change["kind"] == kind and (tool is None or change["tool"] == tool):
            return change
    return None


# -- shape invariance -------------------------------------------------------

section("shape is invariant to game data")

shape_a = contract.shape_of(LIST_FARM_A)
shape_b = contract.shape_of(LIST_FARM_B)

check(
    "same server version yields identical templates",
    set(shape_a["templates"]) == set(shape_b["templates"]),
    "\n    A=%s\n    B=%s" % (shape_a["templates"], shape_b["templates"]),
)
check(
    "mood and species variation does not change the skeleton",
    set(contract.shape_of(LIST_FARM_A)["templates"])
    == set(contract.shape_of(LIST_FARM_A.replace("delighted", "starving").replace("chicken", "alpaca"))["templates"]),
)
check(
    "animal names never enter the vocabulary",
    not ({"pecky", "henrietta", "dot", "clucky", "buzzy", "bacon", "nick"} & set(shape_a["vocabulary"]) | 
         {"pecky", "henrietta", "dot", "clucky", "buzzy", "bacon", "nick"} & set(shape_b["vocabulary"])),
    "A=%s B=%s" % (shape_a["vocabulary"], shape_b["vocabulary"]),
)
check(
    "schema words are retained",
    {"hunger", "happiness"} <= set(shape_a["vocabulary"]),
    str(shape_a["vocabulary"]),
)
check(
    "numeric field labels are extracted",
    {"hunger", "happiness"} <= set(shape_a["numeric_labels"]),
    str(shape_a["numeric_labels"]),
)
check(
    "numeric labels survive mood variation",
    set(shape_a["numeric_labels"]) == set(shape_b["numeric_labels"]),
    "A=%s B=%s" % (shape_a["numeric_labels"], shape_b["numeric_labels"]),
)
check(
    "a renamed numeric field is caught",
    "fullness" in contract.shape_of(LIST_FARM_A.replace("hunger", "fullness"))["numeric_labels"],
)
check(
    "numbers are collapsed but their framing survives",
    any("(#)" in tpl for tpl in shape_a["templates"]),
    str(shape_a["templates"]),
)
check(
    "a real format change is detected",
    set(contract.shape_of(LIST_FARM_CHANGED)["templates"]) != set(shape_a["templates"]),
)

# The same invariance, against whatever the live cycle actually captured.
section("shape invariance on real captured samples")
real = []
if os.path.isdir(contract.RAW_DIR):
    for entry in sorted(os.listdir(contract.RAW_DIR)):
        if entry.startswith("list_farm") and entry.endswith(".txt"):
            path = os.path.join(contract.RAW_DIR, entry)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    real.append((entry, contract._head(handle, 400)))
            except OSError:
                pass
real = [(n, t) for n, t in real if t.strip()][:6]
if len(real) >= 2:
    base_name, base_text = real[0]
    base = contract.shape_of(base_text)
    compared = 0
    for name, text in real[1:]:
        other = contract.shape_of(text)
        # Different call sites capture different slices of the same farm; the
        # animal line format must still agree.
        animal_base = {t for t in base["templates"] if t.count("<w+>") >= 2 and "#" in t}
        animal_other = {t for t in other["templates"] if t.count("<w+>") >= 2 and "#" in t}
        if not animal_base or not animal_other:
            continue
        compared += 1
        check(
            "%s vs %s agree on animal line format" % (base_name, name),
            bool(animal_base & animal_other),
            "\n    %s\n    %s" % (sorted(animal_base)[:2], sorted(animal_other)[:2]),
        )
    # A silently empty section would hide a broken extractor behind a green run.
    check("real samples were actually compared", compared > 0, "no comparable pairs found")
else:
    print("  skip real-sample comparison (need 2+ non-empty list_farm dumps)")


# -- severity depends on whether we call the tool ---------------------------

section("severity is judged against our own call sites")

base = snapshot(tools_fixture())

# A new required argument on a tool we call, versus one we do not.
breaking_tools = copy.deepcopy(tools_fixture())
breaking_tools[0]["inputSchema"]["required"] = ["animal_id"]
changes = contract.diff(base, snapshot(breaking_tools))
hit = find(changes, "required_arg_added", "feed_animals")
check("new required arg on a called tool is breaking", hit and hit["severity"] == "breaking", str(hit))
check("breaking change carries the call sites to fix", hit and hit["sites"], str(hit))

unused_tools = copy.deepcopy(tools_fixture())
unused_tools[2]["inputSchema"]["required"] = ["to", "item", "qty", "message"]
unused_tools[2]["inputSchema"]["properties"]["message"] = {"type": "string"}
changes = contract.diff(base, snapshot(unused_tools))
hit = find(changes, "required_arg_added", "gift")
check("same change on an uncalled tool is not breaking", hit and hit["severity"] == "additive", str(hit))


section("renames are detected as mechanical fixes")

renamed = copy.deepcopy(tools_fixture())
renamed[0]["inputSchema"]["properties"] = {"id": {"type": "string"}}
changes = contract.diff(base, snapshot(renamed))
hit = find(changes, "arg_removed", "feed_animals")
check("dropped arg we pass is breaking", hit and hit["severity"] == "breaking", str(hit))
check(
    "the replacement argument is named as a rename candidate",
    hit and hit["detail"].get("rename_candidate") == "id",
    str(hit and hit["detail"]),
)
check(
    "a rename is not double-reported as an unrelated addition",
    find(changes, "arg_added", "feed_animals") is None,
    str(changes),
)


section("tool inventory changes")

removed = [t for t in tools_fixture() if t["name"] != "feed_animals"]
hit = find(contract.diff(base, snapshot(removed)), "tool_removed", "feed_animals")
check("losing a tool we depend on is breaking", hit and hit["severity"] == "breaking", str(hit))

added = tools_fixture() + [
    {
        "name": "auction",
        "description": "Bid on livestock at auction.",
        "inputSchema": {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]},
    }
]
hit = find(contract.diff(base, snapshot(added)), "tool_added", "auction")
check("a brand new tool is an opportunity, not a break", hit and hit["severity"] == "opportunity", str(hit))


section("enum and type drift")

enum_lost = copy.deepcopy(tools_fixture())
enum_lost[1]["inputSchema"]["properties"]["kind"]["enum"] = ["chicken", "cow"]
hit = find(contract.diff(base, snapshot(enum_lost)), "enum_values_removed", "adopt_animal")
check("removed enum values on a called tool are breaking", hit and hit["severity"] == "breaking", str(hit))
check("the lost values are enumerated", hit and set(hit["detail"]["removed"]) == {"beehive", "pig"}, str(hit))

enum_gained = copy.deepcopy(tools_fixture())
enum_gained[1]["inputSchema"]["properties"]["kind"]["enum"] = ["chicken", "cow", "pig", "beehive", "alpaca"]
hit = find(contract.diff(base, snapshot(enum_gained)), "enum_values_added", "adopt_animal")
check("new enum values are an opportunity", hit and hit["severity"] == "opportunity", str(hit))

type_changed = copy.deepcopy(tools_fixture())
type_changed[1]["inputSchema"]["properties"]["qty"]["type"] = "string"
hit = find(contract.diff(base, snapshot(type_changed)), "arg_type_changed", "adopt_animal")
check("a type change on an arg we pass is breaking", hit and hit["severity"] == "breaking", str(hit))


section("cosmetic changes stay cosmetic")

reworded = copy.deepcopy(tools_fixture())
reworded[0]["description"] = "Feed the animals living on your farm."
changes = contract.diff(base, snapshot(reworded))
hit = find(changes, "description_changed", "feed_animals")
check("rewording a description is cosmetic", hit and hit["severity"] == "cosmetic", str(hit))
check("rewording produces no breaking change", not [c for c in changes if c["severity"] == "breaking"], str(changes))


section("no change means no diff")

check("identical snapshots produce no changes", contract.diff(base, snapshot(tools_fixture())) == [])
check(
    "identical contracts share a fingerprint",
    snapshot(tools_fixture())["fingerprint"] == base["fingerprint"],
)
check(
    "a changed contract changes the fingerprint",
    snapshot(breaking_tools)["fingerprint"] != base["fingerprint"],
)
check("a first scan with no baseline reports nothing", contract.diff(None, base) == [])


# -- confirmation gating ----------------------------------------------------

section("shape drift needs confirmation, schema drift does not")

shape_before = {"list_farm": contract.shape_of(LIST_FARM_A)}
shape_after = {"list_farm": contract.shape_of(LIST_FARM_CHANGED)}
shape_changes = contract.diff(
    snapshot(tools_fixture(), shapes=shape_before),
    snapshot(tools_fixture(), shapes=shape_after),
)
check("a response format change is reported", any(c["kind"].startswith("response_") for c in shape_changes), str(shape_changes))

actionable, streaks = contract.confirm(shape_changes, {})
check("shape drift is not actionable on first sighting", actionable == [], str(actionable))
actionable, streaks = contract.confirm(shape_changes, streaks)
check("shape drift becomes actionable on the second", len(actionable) == len(shape_changes), str(actionable))

schema_changes = contract.diff(base, snapshot(breaking_tools))
actionable, _ = contract.confirm(schema_changes, {})
check("schema drift is actionable immediately", len(actionable) == len(schema_changes), str(actionable))

check(
    "change ids are stable across scans",
    [c["id"] for c in contract.diff(base, snapshot(breaking_tools))]
    == [c["id"] for c in contract.diff(base, snapshot(breaking_tools))],
)


# -- reliance ---------------------------------------------------------------

section("reliance is read from our own source")

rely = contract.reliance(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
check("feed_animals is detected as called", "feed_animals" in rely, str(sorted(rely)))
check("its animal_id argument is detected", "animal_id" in (rely.get("feed_animals") or {}).get("args", []), str(rely.get("feed_animals")))
check("call sites are recorded with line numbers", any(":" in s for s in (rely.get("feed_animals") or {}).get("sites", [])))
check("tools we never call are absent", "gift" not in rely and "visit_farm" not in rely, str(sorted(rely)))


# -- persistence ------------------------------------------------------------

section("baseline persistence")

import tempfile

tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "contract.json")
check("a missing baseline reads as None", contract.load_baseline(path) is None)
contract.save_baseline(base, path)
loaded = contract.load_baseline(path)
check("a saved baseline round-trips", loaded and loaded["fingerprint"] == base["fingerprint"])
check("no temp file is left behind", not os.path.exists(path + ".tmp"))
open(path, "w").write("{not json")
check("a corrupt baseline reads as None rather than raising", contract.load_baseline(path) is None)

hist = os.path.join(tmp, "contract.ndjson")
contract.record_scan({"ts": "t1", "fingerprint": "a"}, hist)
contract.record_scan({"ts": "t2", "fingerprint": "b"}, hist)
check("scan history appends", len(contract.history(10, hist)) == 2)
open(hist, "a").write("garbage\n")
check("corrupt history rows are skipped", len(contract.history(10, hist)) == 2)


print("\n%d checks, %d failures" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for name in FAILURES:
        print("  failed: %s" % name)
    sys.exit(1)
print("contract suite passed")