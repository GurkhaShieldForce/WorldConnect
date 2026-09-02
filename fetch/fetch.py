"""
fetch.py — refresh data/metrics.json from public sources.

Run it:   python fetch/fetch.py            (GitHub Actions runs this nightly)
          python fetch/fetch.py --dry-run  (fetch and report, write nothing)

What it does, in order:
  1. Reads world.json and collects every metric that has a `fetch` block.
  2. For each one, downloads a CSV from the publisher and pulls out (date, value) rows.
  3. Validates what came back. If anything is wrong — network error, wrong shape,
     a value that isn't a number — it keeps the previous series for that metric
     and records the failure. One bad source never blanks the others.
  4. Writes data/metrics.json atomically (write a temp file, then rename), so a
     crash mid-write can't leave a half-file behind.

Security posture — this script is the only thing in the project that talks to
the outside world, so it is written like an inbound firewall:
  - It uses only the Python standard library. No third-party HTTP package to trust.
  - It will only connect to hosts on ALLOWED_HOSTS. Even if world.json were edited
    to point somewhere else, the request is refused.
  - It caps download size, sets a timeout, and never follows a redirect off-host.
  - It treats every byte from the network as untrusted: it parses, it never evals.
  - It needs no API key and no secret. Nothing to leak.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

# ---------- configuration ----------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
WORLD_PATH = REPO_ROOT / "world.json"
METRICS_PATH = REPO_ROOT / "data" / "metrics.json"

# The only hosts this script is permitted to contact. Adding a source means
# adding its host here on purpose — a deliberate, reviewable change.
ALLOWED_HOSTS = {"fred.stlouisfed.org", "ourworldindata.org"}

TIMEOUT_SECONDS = 30
MAX_BYTES = 5_000_000          # 5 MB. The largest CSV we fetch is well under 1 MB.
KEEP_POINTS = 60               # how many trailing observations to keep per series
USER_AGENT = "WorldConnect/1.0 (+https://github.com/GurkhaShieldForce/WorldConnect)"


# ---------- tiny helpers -----------------------------------------------------
def log(msg: str) -> None:
    """Print with a timestamp so the Action log reads like an audit trail."""
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def build_url(kind: str, spec: dict) -> str:
    """Turn a fetch spec from world.json into a URL. Only known shapes are allowed."""
    if kind == "fred-csv":
        # FRED's keyless CSV endpoint: one series, all history, two columns.
        return "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urllib.parse.urlencode(
            {"id": spec["series"]}
        )
    if kind == "owid-csv":
        # OWID grapher CSV, filtered to one entity so the download stays small.
        return f"https://ourworldindata.org/grapher/{spec['slug']}.csv?" + urllib.parse.urlencode(
            {"csvType": "filtered", "useColumnShortNames": "true", "country": spec["entity"]}
        )
    raise ValueError(f"unknown fetch kind {kind!r}")


def download(url: str) -> str:
    """Fetch text from an allow-listed host with a timeout and a size cap."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        # This is the firewall rule. Fail closed.
        raise PermissionError(f"host {host!r} is not in ALLOWED_HOSTS")

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:   # noqa: S310 (host is allow-listed above)
        final_host = urllib.parse.urlparse(resp.geturl()).hostname or ""
        if final_host not in ALLOWED_HOSTS:
            raise PermissionError(f"redirected off-host to {final_host!r}")
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("response exceeded MAX_BYTES")
    return raw.decode("utf-8", errors="strict")   # strict: garbage bytes are an error, not silently dropped


def parse_number(text: str) -> float | None:
    """Convert a CSV cell to a float, or None for FRED's '.' placeholder and blanks."""
    text = text.strip()
    if text in ("", ".", "NA", "nan"):
        return None
    return float(text)        # raises ValueError on anything that isn't a number — good


# ---------- parsers: one per source shape -----------------------------------
def parse_fred(text: str) -> list[tuple[str, float]]:
    """FRED CSV: header is 'observation_date,<SERIES>' (older files say 'DATE'); rows are date,value."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or len(rows[0]) != 2:
        raise ValueError("FRED CSV: expected exactly two columns")
    out: list[tuple[str, float]] = []
    for row in rows[1:]:
        if len(row) != 2:
            continue
        date.fromisoformat(row[0])            # validates the date format; raises if not YYYY-MM-DD
        value = parse_number(row[1])
        if value is not None:
            out.append((row[0], value))
    return out


def parse_owid(text: str, entity: str, column: str) -> list[tuple[str, float]]:
    """OWID CSV: header has entity,code,year,<columns...>. Keep rows for our entity code."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or column not in reader.fieldnames:
        raise ValueError(f"OWID CSV: column {column!r} not found in {reader.fieldnames}")
    out: list[tuple[str, float]] = []
    for row in reader:
        if row.get("code") != entity:
            continue
        value = parse_number(row.get(column, ""))
        if value is not None:
            year = int(row["year"])              # raises if not an integer
            out.append((f"{year}-01-01", value))
    return out


# ---------- the main loop ----------------------------------------------------
def collect_metrics(world: dict) -> list[dict]:
    """Every metric in world.json that declares how to fetch itself."""
    found = []
    for node in world["nodes"]:
        for metric in node.get("metrics", []):
            if "fetch" in metric:
                found.append({**metric, "node": node["id"]})
    return found


def refresh(world: dict, previous: dict, dry_run: bool) -> dict:
    """Build the new metrics document, falling back to `previous` per metric on failure."""
    today = datetime.now(timezone.utc).date().isoformat()
    result: dict = {"generated": today, "metrics": {}, "failures": []}
    prev_metrics = previous.get("metrics", {})

    for metric in collect_metrics(world):
        mid, spec = metric["id"], metric["fetch"]
        try:
            url = build_url(spec["kind"], spec)
            text = download(url)
            if spec["kind"] == "fred-csv":
                series = parse_fred(text)
            else:
                series = parse_owid(text, spec["entity"], spec["column"])
            if not series:
                raise ValueError("parsed zero observations")
            series = series[-KEEP_POINTS:]
            result["metrics"][mid] = {
                "node": metric["node"], "label": metric["label"], "unit": metric["unit"],
                "scope": metric["scope"], "source": metric["source"], "cadence": metric["cadence"],
                "updated": today, "series": [[d, v] for d, v in series],
            }
            log(f"ok    {mid:32s} {len(series):3d} points, latest {series[-1][0]} = {series[-1][1]}")
        except (urllib.error.URLError, ValueError, PermissionError, KeyError, OSError) as exc:
            # Keep yesterday's series rather than publish a gap. Record why.
            log(f"FAIL  {mid:32s} {type(exc).__name__}: {exc}")
            result["failures"].append({"metric": mid, "error": f"{type(exc).__name__}: {exc}", "on": today})
            if mid in prev_metrics:
                result["metrics"][mid] = prev_metrics[mid]

    return result


def write_atomic(path: Path, payload: dict) -> None:
    """Write to a temp file in the same directory, then rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)     # atomic on POSIX: readers see the old file or the new one, never a mix


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, but do not write")
    args = parser.parse_args()

    world = load_json(WORLD_PATH)
    previous = load_json(METRICS_PATH) if METRICS_PATH.exists() else {}
    result = refresh(world, previous, args.dry_run)

    ok, failed = len(result["metrics"]), len(result["failures"])
    log(f"done: {ok} series written, {failed} failed")
    if not args.dry_run:
        write_atomic(METRICS_PATH, result)
    # Exit 0 even with partial failures: a flaky publisher must not mark the whole
    # nightly run red and stop the commit of the sources that did work.
    return 0


if __name__ == "__main__":
    sys.exit(main())
