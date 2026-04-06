#!/usr/bin/env python3
"""Scrape ClawHub for allowlisted skills and emit index.json.

Reads allowlist.txt (one `<author>/<slug>` per line), fetches each skill's
public ClawHub page, extracts stable metadata using a real HTML parser,
and writes the aggregated catalog to index.json for the ShanClaw daemon
to consume via raw.githubusercontent.com.

Design rules (from ShanClaw's CLAUDE.md):
  - No fragile heuristics: uses BeautifulSoup + CSS selectors, not regex
    on raw HTML.
  - Per-entry failure isolation: an individual skill that fails to parse
    does NOT break the whole run. Falls back to partial metadata.
  - Atomic writes: writes index.json via a temp file + rename so a
    crashed run never leaves a truncated catalog.
  - Deterministic output: sorts skills alphabetically by slug and uses
    indented JSON so commit diffs are minimal.

Fields extracted per skill:
  - description: from <meta name="description"> (server-rendered, stable)
  - version: from <title> suffix or an inline version badge
  - license: from the License badge in the metadata section
  - downloads: from the stats row
  - stars: from the stats row
  - security: from the scan results panel (virustotal + openclaw verdicts)

Fields that come from the allowlist directly:
  - slug, name, author

Fields that are constructed from known URL patterns:
  - homepage: https://clawhub.ai/<author>/<slug>
  - download_url: https://wry-manatee-359.convex.site/api/v1/download?slug=<slug>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

REGISTRY_VERSION = 1
CLAWHUB_BASE = "https://clawhub.ai"
CONVEX_DOWNLOAD = "https://wry-manatee-359.convex.site/api/v1/download"
REQUEST_TIMEOUT = 30
USER_AGENT = "ShanClawSkillRegistry/1.0 (+https://github.com/Kocoro-lab/shanclaw-skill-registry)"


@dataclass
class SecurityScan:
    virustotal: str = ""
    openclaw: str = ""
    scanned_at: str = ""


@dataclass
class SkillEntry:
    slug: str
    name: str
    description: str
    author: str
    license: str = ""
    download_url: str = ""
    homepage: str = ""
    downloads: int = 0
    stars: int = 0
    version: str = ""
    security: SecurityScan = field(default_factory=SecurityScan)


# --- allowlist ---------------------------------------------------------------


def load_allowlist(path: Path) -> list[tuple[str, str]]:
    """Parse allowlist.txt. Returns list of (author, slug) tuples, in file order."""
    entries: list[tuple[str, str]] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "/" not in line:
            raise ValueError(f"{path}:{lineno}: expected `author/slug`, got {raw!r}")
        author, _, slug = line.partition("/")
        author = author.strip()
        slug = slug.strip()
        if not author or not slug:
            raise ValueError(f"{path}:{lineno}: empty author or slug in {raw!r}")
        entries.append((author, slug))
    return entries


# --- scraping ----------------------------------------------------------------


def fetch_page(author: str, slug: str, session: requests.Session) -> Optional[str]:
    """Fetch a skill page from ClawHub. Returns HTML text or None on failure."""
    url = f"{CLAWHUB_BASE}/{author}/{slug}"
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"WARN: {author}/{slug}: fetch failed: {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"WARN: {author}/{slug}: HTTP {resp.status_code}", file=sys.stderr)
        return None
    return resp.text


def extract_skill(author: str, slug: str, html: str) -> SkillEntry:
    """Parse a ClawHub page and build a SkillEntry. Individual field failures
    do not raise — the entry is always returned, possibly with empty fields.
    """
    soup = BeautifulSoup(html, "lxml")

    entry = SkillEntry(
        slug=slug,
        name=slug,
        description="",
        author=author,
        download_url=f"{CONVEX_DOWNLOAD}?slug={slug}",
        homepage=f"{CLAWHUB_BASE}/{author}/{slug}",
    )

    _safe("description", lambda: _extract_description(soup), lambda v: setattr(entry, "description", v))
    _safe("version", lambda: _extract_version(soup), lambda v: setattr(entry, "version", v))
    _safe("license", lambda: _extract_license(soup), lambda v: setattr(entry, "license", v))
    _safe("downloads", lambda: _extract_downloads(soup), lambda v: setattr(entry, "downloads", v))
    _safe("stars", lambda: _extract_stars(soup), lambda v: setattr(entry, "stars", v))
    _safe("security", lambda: _extract_security(soup), lambda v: setattr(entry, "security", v))

    return entry


def _safe(field_name: str, extractor, setter) -> None:
    """Call extractor; if it raises or returns None, log and skip the setter."""
    try:
        value = extractor()
    except Exception as e:
        print(f"WARN: extract {field_name}: {type(e).__name__}: {e}", file=sys.stderr)
        return
    if value is None:
        return
    setter(value)


def _extract_description(soup: BeautifulSoup) -> Optional[str]:
    """From <meta name='description'>. Server-rendered, most stable selector."""
    tag = soup.find("meta", attrs={"name": "description"})
    if not isinstance(tag, Tag):
        return None
    content = tag.get("content", "")
    if not isinstance(content, str):
        return None
    # ClawHub truncates long descriptions with "..." in the meta tag; keep it.
    return content.strip()


_VERSION_RE = re.compile(r"^v?(\d+\.\d+(?:\.\d+)?(?:[.\-+][\w\d]+)*)$")


def _extract_version(soup: BeautifulSoup) -> Optional[str]:
    """From a span containing just a version string like 'v1.0.4'.

    There's only one such badge per page (shown next to the skill title).
    We search every span and take the first match against a strict version
    regex so banner text or stat numbers can't trigger a false positive.
    """
    for span in soup.find_all("span"):
        text = span.get_text(strip=True)
        if not text:
            continue
        m = _VERSION_RE.match(text)
        if m:
            return m.group(1)
    return None


_LICENSES = (
    "MIT-0", "MIT", "Apache-2.0", "Apache 2.0", "BSD-3-Clause", "BSD-2-Clause",
    "BSD", "GPL-3.0", "GPLv3", "GPL-2.0", "GPLv2", "LGPL", "ISC", "MPL-2.0",
    "CC0", "CC-BY", "Unlicense", "WTFPL",
)


def _extract_license(soup: BeautifulSoup) -> Optional[str]:
    """License badge: a short span whose text matches a known SPDX identifier."""
    # Look for a span whose stripped text exactly equals one of the known
    # license IDs. Badges are short, so exact match is reliable and avoids
    # matching longer prose like "Uses the MIT License in general".
    for span in soup.find_all("span"):
        text = span.get_text(strip=True)
        if text in _LICENSES:
            return text
    return None


# Stats are rendered like "⭐ 484" for stars and "📦 153k" for downloads.
# We look for the aria-hidden lucide-star / lucide-package icons then read
# the sibling text in the same span. Uses structural traversal (parent span),
# NOT regex over the whole page, so layout changes to unrelated sections
# don't break extraction.
def _extract_downloads(soup: BeautifulSoup) -> Optional[int]:
    icon = soup.find("svg", class_="lucide-package")
    return _stat_near(icon)


def _extract_stars(soup: BeautifulSoup) -> Optional[int]:
    # Stars are labeled with a literal "⭐" emoji, not a lucide icon, so we
    # find spans whose text starts with the emoji.
    for span in soup.find_all("span"):
        text = span.get_text(strip=True)
        if text.startswith("⭐"):
            return _parse_stat_number(text.removeprefix("⭐").strip())
    return None


def _stat_near(icon: Optional[Tag]) -> Optional[int]:
    """Walk up to the enclosing span and pull the numeric stat text beside the icon."""
    if not isinstance(icon, Tag):
        return None
    span = icon.find_parent("span")
    if not isinstance(span, Tag):
        return None
    # Skip the icon itself when extracting text.
    text = span.get_text(strip=True)
    return _parse_stat_number(text)


_STAT_RE = re.compile(r"([\d.]+)\s*([kKmM]?)")


def _parse_stat_number(text: str) -> Optional[int]:
    """Parse '153k', '1.1k', '484', '2.5M' into an integer. Returns None on
    unparseable input so the caller keeps the field at its default.
    """
    m = _STAT_RE.search(text)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    suffix = m.group(2).lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def _extract_security(soup: BeautifulSoup) -> Optional[SecurityScan]:
    """Security scan results panel. Each scanner has its own row with a
    verdict span carrying a 'scan-status-*' class.
    """
    scan = SecurityScan()
    for row in soup.find_all("div", class_="scan-result-row"):
        if not isinstance(row, Tag):
            continue
        name_tag = row.find("span", class_="scan-result-scanner-name")
        status_tag = row.find("div", class_="scan-result-status")
        if not isinstance(name_tag, Tag) or not isinstance(status_tag, Tag):
            continue
        scanner = name_tag.get_text(strip=True).lower()
        verdict = status_tag.get_text(strip=True).lower()
        if scanner == "virustotal":
            scan.virustotal = verdict
        elif scanner == "openclaw":
            scan.openclaw = verdict
    if not scan.virustotal and not scan.openclaw:
        return None
    return scan


# --- main --------------------------------------------------------------------


def build_index(allowlist: list[tuple[str, str]], session: requests.Session) -> dict:
    skills: list[SkillEntry] = []
    for author, slug in allowlist:
        html = fetch_page(author, slug, session)
        if html is None:
            print(f"SKIP: {author}/{slug}: unreachable", file=sys.stderr)
            continue
        entry = extract_skill(author, slug, html)
        if not entry.description:
            print(f"WARN: {author}/{slug}: empty description", file=sys.stderr)
        skills.append(entry)

    # Sort deterministically so commit diffs are minimal.
    skills.sort(key=lambda s: s.slug)

    return {
        "version": REGISTRY_VERSION,
        "skills": [_entry_to_dict(s) for s in skills],
    }


def _entry_to_dict(entry: SkillEntry) -> dict:
    """Convert SkillEntry to a dict with omit-empty semantics matching the
    daemon's JSON decoder (so missing optional fields don't pollute output).
    """
    d = asdict(entry)
    # Drop empty optional fields to keep the JSON tight, but keep the core
    # fields (slug, name, description, author) even if empty.
    for optional in ("license", "version", "homepage"):
        if not d.get(optional):
            d.pop(optional, None)
    # Security block: drop if both scanners are empty.
    sec = d.get("security", {})
    if not sec.get("virustotal") and not sec.get("openclaw") and not sec.get("scanned_at"):
        d.pop("security", None)
    else:
        # Remove empty sub-fields for cleanliness.
        d["security"] = {k: v for k, v in sec.items() if v}
    # Zero stats are semantically "unknown"; keep them so sort-by-downloads
    # has a consistent key.
    return d


def write_index_atomic(index: dict, path: Path) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False, sort_keys=False) + "\n")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape ClawHub for allowlisted skills.")
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "allowlist.txt",
        help="Path to allowlist.txt",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "index.json",
        help="Path to write index.json",
    )
    args = parser.parse_args()

    if not args.allowlist.exists():
        print(f"ERROR: allowlist not found: {args.allowlist}", file=sys.stderr)
        return 1

    allowlist = load_allowlist(args.allowlist)
    if not allowlist:
        print("ERROR: allowlist is empty", file=sys.stderr)
        return 1

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    index = build_index(allowlist, session)

    if not index["skills"]:
        print("ERROR: no skills successfully fetched — refusing to overwrite index.json", file=sys.stderr)
        return 2

    write_index_atomic(index, args.output)
    print(f"OK: wrote {len(index['skills'])} skills to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
