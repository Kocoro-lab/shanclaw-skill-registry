# ShanClaw Skill Registry

Static catalog powering the **Skill Store** in [ShanClaw](https://github.com/Kocoro-lab/ShanClaw). The ShanClaw daemon fetches `index.json` from this repo's `main` branch and serves it over its local HTTP API so the client can browse, search, and one-click install skills from [ClawHub](https://clawhub.ai).

## What lives here

| File | Purpose |
|---|---|
| `allowlist.txt` | Hand-curated list of trusted skills (one `author/slug` per line) |
| `index.json` | Machine-generated catalog. **Do not edit by hand** — it's regenerated from the allowlist by the scraper |
| `scripts/scrape.py` | Daily scraper that reads `allowlist.txt`, fetches each ClawHub page, and builds `index.json` |
| `scripts/requirements.txt` | Python dependencies for the scraper |
| `.github/workflows/scrape.yml` | GitHub Action that runs the scraper daily and on allowlist changes |

## How it works

```
┌──────────────────┐    daily cron     ┌──────────────────┐    HTTP GET    ┌──────────────┐
│  allowlist.txt   │ ────────────────▶ │  scrape.py (CI)  │ ─────────────▶ │  clawhub.ai  │
└──────────────────┘                   └──────────────────┘                └──────────────┘
                                              │
                                              │ writes
                                              ▼
┌──────────────────┐    raw.github.com     ┌──────────────────┐
│  ShanClaw daemon │ ◀──────────────────── │    index.json    │
│  /skills/market* │        (1h cache)      │   (this repo)    │
└──────────────────┘                        └──────────────────┘
```

1. Maintainers add a trusted slug to `allowlist.txt`
2. A push to `allowlist.txt`, a daily cron at 04:00 UTC, or a manual `workflow_dispatch` triggers the scraper
3. The scraper fetches each ClawHub page via a real HTML parser (BeautifulSoup + lxml), extracts metadata from stable selectors (meta tags, license badges, stats icons, scan result rows), and writes a deterministic `index.json`
4. If the output differs from `HEAD`, the action commits it as a bot
5. The ShanClaw daemon's `MarketplaceClient` fetches `https://raw.githubusercontent.com/Kocoro-lab/shanclaw-skill-registry/main/index.json` with a 1-hour in-memory cache and a 1-minute stale cooldown after upstream failures

## Adding a new skill

1. Review the skill on ClawHub. Check its scan results, read `SKILL.md`, look at any shipped scripts.
2. Add one line to `allowlist.txt` in the form `<author>/<slug>` matching the ClawHub URL path
3. Open a PR
4. After merge, the push-triggered scraper run regenerates `index.json` and the daemon picks it up within the next cache refresh

**Security policy:** only add slugs you have personally reviewed. Each entry becomes part of the one-click install surface and can land arbitrary scripts and helper files into a user's `~/.shannon/skills/`. The scraper has no independent vetting — the allowlist IS the trust boundary.

## Removing a skill

Delete the line from `allowlist.txt` and open a PR. After merge, the scraper regenerates `index.json` without the entry. The daemon picks up the removal on the next cache refresh (~1 hour). Already-installed skills on user disks are NOT affected.

## Registry schema

See [the design document](https://github.com/Kocoro-lab/ShanClaw/blob/main/docs/superpowers/specs/2026-04-06-skill-marketplace-design.md) in the ShanClaw repo for the full schema and the daemon-side contract. Minimal shape:

```json
{
  "version": 1,
  "skills": [
    {
      "slug": "ontology",
      "name": "ontology",
      "description": "Typed knowledge graph for structured agent memory...",
      "author": "oswalpalash",
      "license": "MIT-0",
      "download_url": "https://wry-manatee-359.convex.site/api/v1/download?slug=ontology",
      "homepage": "https://clawhub.ai/oswalpalash/ontology",
      "downloads": 153000,
      "stars": 484,
      "version": "1.0.4",
      "security": {
        "virustotal": "benign",
        "openclaw": "benign"
      }
    }
  ]
}
```

Skills with a `download_url` field (and no `repo` field) are installed by the daemon via its zip-transport flow: HTTP GET → `archive/zip` → zip-slip/zip-bomb/symlink validation → stage → atomic rename into `~/.shannon/skills/<slug>/`.

Skills with a `repo` field (GitHub URL) are installed via the git-transport flow: `git clone --depth=1` → clean-stage copy → atomic rename.

Both transports run under a per-slug mutex and propagate request context so cancellation works end-to-end.

## Running the scraper locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r scripts/requirements.txt
.venv/bin/python scripts/scrape.py
```

The scraper is idempotent: running it twice against the same allowlist produces byte-identical `index.json`.

## License

MIT
