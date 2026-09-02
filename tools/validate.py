"""
validate.py — check that world.json is well-formed and internally consistent.

Run it:   python tools/validate.py
Exit 0 means the graph is clean; exit 1 means at least one problem was found.

Two kinds of checks happen here, and it helps to keep them separate in your head:

  1. STRUCTURAL  — "does every record have the right fields, of the right type?"
     Delegated to the JSON Schema in schema/world.schema.json via the `jsonschema`
     library. This is like a firewall's packet-format check: shape only.

  2. REFERENTIAL — "does every ID that points at something actually point at
     something that exists?" e.g. an edge whose `from` names a node we never
     defined, or a trace whose path doesn't chain. The schema can't express
     this, so we write it ourselves. This is the CMDB integrity check.

Security note: world.json is a file *we* author, so it isn't hostile input in the
way an API response is. But the same validator will later guard data the nightly
fetcher writes, so it is deliberately strict and never executes anything from the
data — it only reads and compares.
"""

from __future__ import annotations   # lets us write list[str] etc. on older Pythons

import json
import sys
from pathlib import Path             # Path objects beat string paths: no OS-specific slashes

from jsonschema import Draft202012Validator   # pinned in requirements.txt


# ---------- where things live ------------------------------------------------
# Path(__file__) is this script; .parent.parent walks up to the repo root, so the
# script works no matter which directory you run it from.
REPO_ROOT = Path(__file__).resolve().parent.parent
WORLD_PATH = REPO_ROOT / "world.json"
SCHEMA_PATH = REPO_ROOT / "schema" / "world.schema.json"


def load_json(path: Path) -> dict:
    """Read a JSON file into a Python dict. Fails loudly if the file is missing or malformed."""
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# ---------- check 1: structure ----------------------------------------------
def structural_errors(world: dict, schema: dict) -> list[str]:
    """Return a human-readable message for every place world.json violates the schema."""
    validator = Draft202012Validator(schema)
    messages: list[str] = []
    # iter_errors gives *all* problems at once instead of stopping at the first.
    for err in sorted(validator.iter_errors(world), key=lambda e: list(e.path)):
        # err.path is the location inside the document, e.g. ['edges', 3, 'why']
        where = "/".join(str(p) for p in err.path) or "(root)"
        messages.append(f"schema  {where}: {err.message}")
    return messages


# ---------- check 2: references ---------------------------------------------
def referential_errors(world: dict) -> list[str]:
    """Check that every ID reference resolves and that traces chain end-to-end."""
    messages: list[str] = []

    # Build lookup sets first. A set gives O(1) "is this in there?" checks and
    # is the natural way to say "the inventory of known IDs".
    layer_ids = {layer["id"] for layer in world["layers"]}
    node_ids = {node["id"] for node in world["nodes"]}
    source_ids = {src["id"] for src in world["sources"]}
    edges_by_id = {edge["id"]: edge for edge in world["edges"]}

    # Duplicate IDs are a silent killer: a dict would just keep the last one.
    for kind, records in (("layer", world["layers"]), ("node", world["nodes"]),
                          ("edge", world["edges"]), ("source", world["sources"]),
                          ("trace", world["traces"])):
        seen: set[str] = set()
        for rec in records:
            if rec["id"] in seen:
                messages.append(f"dupe    {kind} id '{rec['id']}' appears more than once")
            seen.add(rec["id"])

    # Every node must live in a real layer, and its metrics must cite real sources.
    for node in world["nodes"]:
        if node["layer"] not in layer_ids:
            messages.append(f"ref     node '{node['id']}' names unknown layer '{node['layer']}'")
        for metric in node.get("metrics", []):          # .get with default: metrics is optional
            if metric["source"] not in source_ids:
                messages.append(f"ref     metric '{metric['id']}' names unknown source '{metric['source']}'")

    # Every edge must connect two real nodes, cite a real source, and not loop on itself.
    for edge in world["edges"]:
        for end in ("from", "to"):
            if edge[end] not in node_ids:
                messages.append(f"ref     edge '{edge['id']}' {end}='{edge[end]}' is not a node")
        if edge["from"] == edge["to"]:
            messages.append(f"ref     edge '{edge['id']}' points at itself")
        if edge["source"] not in source_ids:
            messages.append(f"ref     edge '{edge['id']}' names unknown source '{edge['source']}'")
        # Rule from the architecture doc: a low-confidence number must show its reasoning.
        if edge["confidence"] == "low" and "range" not in edge:
            messages.append(f"rule    edge '{edge['id']}' is low confidence but gives no range")

    # A trace is a tree that grows outward from `you`: the first edge must start
    # at `you`, and every later edge must start from a node the trace has already
    # reached. (Reality branches — the sandwich needs water AND fertilizer — so a
    # strict single-file chain was the wrong model. This rule still forbids a
    # hop that floats free of the story.)
    for trace in world["traces"]:
        path = trace["path"]
        reached: set[str] = {"you"}
        for i, edge_id in enumerate(path):
            if edge_id not in edges_by_id:
                messages.append(f"ref     trace '{trace['id']}' step {i} names unknown edge '{edge_id}'")
                continue
            edge = edges_by_id[edge_id]
            if edge["from"] not in reached:
                messages.append(
                    f"chain   trace '{trace['id']}' step {i} ('{edge_id}') starts from "
                    f"'{edge['from']}', which the trace has not reached yet"
                )
            reached.add(edge["to"])

    return messages


# ---------- entry point ------------------------------------------------------
def main() -> int:
    """Run both checks, print findings, and return the process exit code."""
    world = load_json(WORLD_PATH)
    schema = load_json(SCHEMA_PATH)

    # Structure first: if the shape is wrong, referential checks may crash on
    # missing keys, so we stop early and report only the structural problems.
    problems = structural_errors(world, schema)
    if not problems:
        problems = referential_errors(world)

    if problems:
        print(f"world.json: {len(problems)} problem(s)")
        for line in problems:
            print("  " + line)
        return 1

    counts = {k: len(world[k]) for k in ("layers", "nodes", "edges", "sources", "traces")}
    print("world.json: OK  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":       # only runs when executed directly, not when imported
    sys.exit(main())
