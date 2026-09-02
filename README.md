# WorldConnect

A living map of how an everyday action — eating lunch, sending a message, taking a shower — propagates through the physical, economic, and political systems of the world, with a sourced number at every hop.

Three traces, 31 nodes, 35 edges, 35 sources, 7 live series. Zero servers, zero secrets, zero monthly cost.

## One-time setup (two clicks, then never again)

1. **Turn on GitHub Pages.** Repo → Settings → Pages → *Build and deployment* → Source: **Deploy from a branch** → Branch: **main**, folder **/ (root)** → Save. The site appears at `https://gurkhashieldforce.github.io/WorldConnect/` within a minute or two.
2. **Let the nightly job commit.** Repo → Settings → Actions → General → *Workflow permissions* → **Read and write permissions** → Save. (The workflow file already requests only `contents: write`; this setting is the repo-level ceiling that allows it.)

Optional: Actions tab → *Refresh live metrics* → **Run workflow** to fetch the first full series immediately instead of waiting for tonight.

## How it works

```
index.html               the site: trace walker + layered map. Plain HTML/CSS/JS, no framework.
world.json               the graph: layers, nodes, edges, sources, traces   (human-edited)
data/metrics.json        live series, rewritten nightly by the fetcher      (never edit by hand)
schema/world.schema.json what a valid world.json looks like
tools/validate.py        shape + referential-integrity checks for world.json
fetch/fetch.py           pulls keyless public CSVs (FRED, Our World in Data) into metrics.json
.github/workflows/       refresh.yml: nightly cron → validate → fetch → commit if changed
```

Visitor → GitHub Pages serves `index.html`, which reads `world.json` and `data/metrics.json`.
Nightly → GitHub Actions runs `fetch.py`, commits a new `metrics.json`, Pages redeploys.

If a publisher is down one night, the fetcher keeps yesterday's series for that metric and records the failure in `metrics.json`; the site footer says so. Degraded, never down.

## Working on the graph

```
pip install --require-hashes -r requirements.txt
python tools/validate.py          # run before every commit
python fetch/fetch.py --dry-run   # test the fetcher without writing
python -m http.server             # then open http://localhost:8000
```

To add a trace: add its nodes, edges and sources to `world.json`, list the edge ids in a new `traces` entry, run the validator, commit. The site picks it up with no other change. To add a live series: add a `metrics` entry with a `fetch` block to the node (see `grid-generation` for both FRED and OWID examples). New publishers must be added to `ALLOWED_HOSTS` in `fetch.py` on purpose.

## Rules of the model

- A node lives in exactly one layer. An edge may cross layers; a node may not.
- Every edge carries a quantity, a unit, a confidence, a scope (`us` or `global`), a source, and a `why` (the mechanism).
- Low-confidence numbers must give a `range`. Estimates are allowed but must say so (`license: "estimate"`).
- A trace grows outward from `you`: every hop must start from a node the trace has already reached.

## Security posture

- **No secrets anywhere.** Every data source is keyless. If a keyed source is ever unavoidable, it goes in GitHub Secrets and is read only inside the Action.
- **Least privilege.** The workflow token has `contents: write` on this repo and nothing else.
- **Pinned supply chain.** Actions are pinned to full commit SHAs; Python dependencies are pinned with hashes (`--require-hashes`).
- **Untrusted input.** `fetch.py` only connects to an explicit host allowlist, caps response size, times out, refuses off-host redirects, and parses strictly. Nothing from the network is ever executed.
- **Static site.** No backend, no database, no user input, no third-party scripts. All data is inserted into the page as text nodes, never as HTML.
- **Provenance.** Every number links to its source; every metric change is a git commit.

Architecture and backlog: the *Worldgraph Architecture* document (v0.3).
