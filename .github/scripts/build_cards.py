#!/usr/bin/env python3
"""
build_cards.py - Generate terminal-aesthetic GitHub stat SVG cards for the profile README.

Why this file exists
====================
The community-hosted `github-readme-stats.vercel.app` deployment is unreliable
(returns 503 DEPLOYMENT_PAUSED during extended outages). This script removes
that runtime dependency entirely: it fetches stats directly from the GitHub
GraphQL API and emits SVG cards in a terminal / neofetch aesthetic. The cards
are committed into the repository, so README rendering depends only on a
static file path -- no third-party service in the request path.

Architecture
============
1. ``fetch_data(token, user)`` - single GraphQL round-trip; returns the raw
   ``data.user`` payload (profile, lifetime totals, language byte aggregates,
   contribution calendar, top repos).
2. ``process_data(raw)`` - derives 30-day rollups, language percentages, the
   weekly heatmap matrix, total stars summed across owned repos, etc.
3. ``render_*(data)`` - pure SVG string builders. One function per card. No I/O.
4. ``main()`` - orchestrates fetch + render + write to ``./stats/``.

The script is intentionally stdlib-only (no ``requests``, no SVG libraries) so
it runs anywhere Python 3.9+ is available and inside a GitHub Action with no
package install step.

Cards produced
==============
- ``banner.svg``     : neofetch-style intro with ASCII initials + key stats.
- ``languages.svg``  : horizontal bar chart of top languages by byte size.
- ``activity.svg``   : last-30-day rollup + 30-week ASCII contribution heatmap.
- ``top-repos.svg``  : table of top original repos sorted by stars.

Run
===
    GITHUB_TOKEN=$(gh auth token) python3 .github/scripts/build_cards.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

USER = "BadRat-in"
"""GitHub login to fetch stats for. Hard-coded -- this script ships in one repo."""

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "stats"

# Terminal palette: GitHub-dark base, Tokyo-Night accents, purple matching the
# rest of the README. Hex values are kept here as named constants so the whole
# colour scheme can be tweaked in one place without touching individual cards.
BG = "#0d1117"
FG = "#c9d1d9"
DIM = "#6e7681"
LABEL = "#8b949e"
ACCENT = "#7c3aed"  # prompt arrows + headers (matches README badge colour)
BLUE = "#58a6ff"  # command names
ORANGE = "#f78166"  # numeric highlights
GREEN = "#3fb950"  # ok / running
YELLOW = "#d29922"
RED = "#ff7b72"

# Heatmap shade ramp: 0 -> dim background, deeper -> closer to accent.
HEATMAP_RAMP = ["#161b22", "#2d2440", "#4a3768", "#7c3aed", "#a78bfa"]

# Markup, data-format, and config "languages" that GitHub Linguist counts but
# that aren't really representative of programming output. Static-site assets
# (a vendored Bootstrap CSS, a copy-pasted template) routinely outweigh real
# source code in byte counts, which paints a misleading picture. The list is
# intentionally narrow -- only formats no one calls themselves a "developer" in.
EXCLUDED_LANGUAGES = {
    "HTML",
    "CSS",
    "SCSS",
    "Sass",
    "Less",
    "Stylus",
    "Vue",  # template portion inflates byte counts
    "Jupyter Notebook",  # embedded outputs balloon notebook size
    "Markdown",
    "TeX",
    "Roff",
    "XSLT",
    "Dockerfile",
    "Makefile",
    "Procfile",
    "Batchfile",
    "PowerShell",
    "Smarty",
    "Twig",
    "Handlebars",
}

# Wide font fallback chain. SF Mono / Cascadia / Consolas cover macOS / Windows;
# Liberation Mono / DejaVu cover Linux. Final ``monospace`` is the floor.
FONT = (
    "'SF Mono','Monaco','Cascadia Mono','JetBrains Mono','Fira Code',"
    "'Inconsolata','Roboto Mono','Consolas','Liberation Mono','DejaVu Sans Mono',"
    "'Courier New',monospace"
)

# Approximate character cell width for the font stack at font-size 13 -- used
# only for rough alignment of text columns. Real per-glyph widths vary slightly
# per OS; this is intentionally a best-effort number.
CELL = 7.8

# Single GraphQL query covering everything we render. Kept in one round-trip
# because GitHub charges all anonymous-rate-limited callers per request, not
# per node, and we want this to run on a cron without burning the quota.
GRAPHQL = """
query ($login: String!) {
  user(login: $login) {
    name login bio createdAt
    followers { totalCount }
    following { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER, isFork: false, orderBy: {field: STARGAZERS, direction: DESC}) {
      totalCount
      nodes {
        name description stargazerCount forkCount
        primaryLanguage { name color }
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
    # Separate count of forks we own (the main `repositories` field filters
    # them out with isFork:false). Lets the banner show "N (+M forks)".
    forkedRepos: repositories(ownerAffiliations: OWNER, isFork: true) {
      totalCount
    }
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


# -----------------------------------------------------------------------------
# Data layer
# -----------------------------------------------------------------------------


def fetch_data(token: str, user: str) -> dict[str, Any]:
    """
    Run the GraphQL query and return the ``data.user`` payload.

    Parameters
    ----------
    token : str
        A GitHub token with at least public-repo read scope. The default
        ``GITHUB_TOKEN`` provisioned by GitHub Actions is sufficient for
        public-facing profile stats.
    user : str
        The GitHub login to fetch (e.g. ``"BadRat-in"``).

    Returns
    -------
    dict
        Raw ``data.user`` subtree, structurally matching the ``GRAPHQL`` query.

    Raises
    ------
    SystemExit
        On HTTP error or any GraphQL ``errors`` field, with a useful message.
        The script is a one-shot CLI; failing loud is the right call.
    """
    body = json.dumps({"query": GRAPHQL, "variables": {"login": user}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"{user}-profile-cards",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub API HTTP {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"GitHub API unreachable: {e.reason}")
    if "errors" in payload:
        sys.exit(f"GitHub GraphQL errors: {payload['errors']}")
    return payload["data"]["user"]


def process_data(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Derive presentation-ready metrics from the raw GraphQL payload.

    This is where business logic lives: language byte aggregation, 30-day
    rollups, the heatmap matrix, total stars summed across owned repos. Keeping
    it separate from rendering means tests (if any) can exercise the math
    without touching SVG.

    Parameters
    ----------
    raw : dict
        Output of :func:`fetch_data`.

    Returns
    -------
    dict
        Flat dict of render-ready stats. Keys are documented inline below and
        consumed by the ``render_*`` functions.
    """
    repos = raw["repositories"]["nodes"]
    total_stars = sum(r["stargazerCount"] for r in repos)
    total_forks = sum(r["forkCount"] for r in repos)

    # Aggregate language byte sizes across all owned non-fork repos. We use byte
    # totals rather than repo counts so a single 50KB Rust crate doesn't outweigh
    # 5MB of TypeScript -- byte-weighted percentages reflect actual code mix.
    # Markup/data languages (see EXCLUDED_LANGUAGES) are dropped here so static
    # assets don't drown out real source code.
    lang_bytes: dict[str, int] = {}
    lang_colors: dict[str, str] = {}
    for r in repos:
        for edge in r["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in EXCLUDED_LANGUAGES:
                continue
            lang_bytes[name] = lang_bytes.get(name, 0) + edge["size"]
            # Some niche language entries return null colour; fall back to grey.
            lang_colors[name] = edge["node"]["color"] or "#8b949e"
    total_bytes = sum(lang_bytes.values()) or 1
    languages = sorted(
        (
            {
                "name": n,
                "bytes": b,
                "color": lang_colors[n],
                "pct": (b / total_bytes) * 100,
            }
            for n, b in lang_bytes.items()
        ),
        key=lambda x: -x["bytes"],
    )

    # Flatten the contribution calendar so we can slice by day count instead of
    # by week boundary. The calendar covers the last ~52 weeks ending today.
    days = []
    for week in raw["contributionsCollection"]["contributionCalendar"]["weeks"]:
        for d in week["contributionDays"]:
            days.append({"date": d["date"], "count": d["contributionCount"]})
    days.sort(key=lambda x: x["date"])

    last30 = days[-30:]
    last30_commits = sum(d["count"] for d in last30)
    last30_active = sum(1 for d in last30 if d["count"] > 0)
    best = max(last30, key=lambda d: d["count"]) if last30 else {"count": 0, "date": ""}

    # ~30-week heatmap: 7 rows (Mon..Sun) x N columns (oldest -> newest L -> R).
    # GitHub's API returns Sun-anchored weeks; we re-index by ``date.weekday()``
    # so the visual matches a Mon-first calendar (which reads more naturally for
    # most viewers than Sun-first). 30 weeks (~7 months) fills the 760px card
    # width without crowding.
    weeks_to_show = 30
    recent_weeks = raw["contributionsCollection"]["contributionCalendar"]["weeks"][-weeks_to_show:]
    heatmap = [[0] * len(recent_weeks) for _ in range(7)]
    for col, week in enumerate(recent_weeks):
        for d in week["contributionDays"]:
            iso = datetime.fromisoformat(d["date"])
            row = iso.weekday()  # Mon=0, Sun=6
            heatmap[row][col] = d["contributionCount"]
    max_day = max((c for row in heatmap for c in row), default=1) or 1

    # Years on GitHub: nice "uptime"-style number for the banner card.
    created = datetime.fromisoformat(raw["createdAt"].replace("Z", "+00:00"))
    years = (datetime.now(timezone.utc) - created).days / 365.25

    return {
        "login": raw["login"],
        "name": raw["name"] or raw["login"],
        "bio": raw["bio"] or "",
        "years": years,
        "followers": raw["followers"]["totalCount"],
        "following": raw["following"]["totalCount"],
        "total_repos": raw["repositories"]["totalCount"],
        "owned_forks": raw["forkedRepos"]["totalCount"],
        "total_stars": total_stars,
        "total_forks": total_forks,
        "total_prs": raw["pullRequests"]["totalCount"],
        "total_issues": raw["issues"]["totalCount"],
        "commits_year": raw["contributionsCollection"]["totalCommitContributions"],
        "contribs_year": raw["contributionsCollection"]["contributionCalendar"]["totalContributions"],
        "languages": languages,
        "last30_commits": last30_commits,
        "last30_active": last30_active,
        "best_day_count": best["count"],
        "best_day_date": best["date"],
        "heatmap": heatmap,
        "max_day": max_day,
        "top_repos": [
            {
                "name": r["name"],
                "stars": r["stargazerCount"],
                "language": (r["primaryLanguage"] or {}).get("name") or "—",
                "description": (r["description"] or "").strip(),
            }
            for r in repos[:5]
        ],
    }


# -----------------------------------------------------------------------------
# SVG helpers
# -----------------------------------------------------------------------------


def _xml_escape(s: str) -> str:
    """Minimal XML escape -- enough for SVG text nodes and attributes."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _truncate(s: str, n: int) -> str:
    """Truncate to ``n`` characters with an ellipsis if needed -- visual-only."""
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _shell_chrome(width: int, height: int, title: str) -> str:
    """
    Emit the common terminal-window chrome: rounded dark background, the macOS
    traffic-light dots, a centred title bar, and the shared <style> block.

    Returns the SVG opening up to (but not including) the content. The caller
    appends its own ``<text>``/``<rect>`` elements and the closing ``</svg>``.
    """
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{_xml_escape(title)}">
<style>
.bg{{fill:{BG}}}
.border{{fill:none;stroke:#30363d;stroke-width:1}}
text{{font-family:{FONT};font-size:13px;fill:{FG}}}
.dim{{fill:{DIM}}}
.label{{fill:{LABEL}}}
.acc{{fill:{ACCENT};font-weight:600}}
.blue{{fill:{BLUE}}}
.orange{{fill:{ORANGE};font-weight:600}}
.green{{fill:{GREEN}}}
.yellow{{fill:{YELLOW}}}
.red{{fill:{RED}}}
.small{{font-size:11px}}
.big{{font-size:15px;font-weight:600}}
.huge{{font-size:18px;font-weight:700;fill:{ACCENT}}}
.mono{{letter-spacing:0.5px}}
</style>
<rect class="bg" width="{width}" height="{height}" rx="10"/>
<rect class="border" width="{width}" height="{height}" rx="10"/>
<circle cx="18" cy="18" r="5.5" fill="#ff5f57"/>
<circle cx="36" cy="18" r="5.5" fill="#febc2e"/>
<circle cx="54" cy="18" r="5.5" fill="#28c840"/>
<text x="{width / 2}" y="22" text-anchor="middle" class="dim small">{_xml_escape(title)}</text>
"""


def _prompt_line(x: int, y: int, command: str, args: str = "") -> str:
    """Render a ``▸ command args`` shell-prompt line."""
    return (
        f'<text x="{x}" y="{y}">'
        f'<tspan class="acc">▸</tspan> '
        f'<tspan class="blue">{_xml_escape(command)}</tspan>'
        f"{' ' + _xml_escape(args) if args else ''}"
        f"</text>"
    )


def _rat_mascot(cx: int = 138, cy: int = 148) -> str:
    """
    Draw the BadRat mascot as vector primitives rather than text glyphs.

    Why vector, not ASCII: multi-line block-character art (the old ``BR`` logo)
    relies on the viewer's monospace font to align ``█▀▄`` cells on a grid.
    GitHub renders the SVG with its own font stack, so the cells drifted and the
    logo looked broken. Geometry has no such dependency -- circles and paths
    render identically everywhere -- so the mascot is crisp on any client.

    The figure is a front-facing sitting rat that occupies the neofetch "distro
    logo" slot: two round ears, a pear-shaped head/body, eyes with pupils, an
    orange nose, whiskers, two front paws, and a curling tail. It is drawn in the
    README's purple accent with faint fills so it reads on the dark terminal
    background. ``cx``/``cy`` set the centre of the head.

    Parameters
    ----------
    cx, cy:
        Pixel coordinates of the head centre within the 760x320 banner canvas.
        Defaults keep the whole figure inside the left logo column (x < ~255),
        clear of the key/value stats table that starts at x = 260.

    Returns
    -------
    str
        An SVG ``<g>`` fragment ready to splice into the banner card.
    """
    a = ACCENT  # outline + accent fills
    al = "#a78bfa"  # lighter accent for the inner ear
    parts: list[str] = [
        f'<g fill="none" stroke="{a}" stroke-width="2.5" '
        f'stroke-linecap="round" stroke-linejoin="round">',
        # Ears first -- the head path overlaps and "tucks" their lower half.
        f'<circle cx="{cx - 31}" cy="{cy - 46}" r="22" fill="{a}" fill-opacity="0.12"/>',
        f'<circle cx="{cx + 31}" cy="{cy - 46}" r="22" fill="{a}" fill-opacity="0.12"/>',
        f'<circle cx="{cx - 31}" cy="{cy - 43}" r="11" fill="{al}" '
        f'fill-opacity="0.35" stroke="none"/>',
        f'<circle cx="{cx + 31}" cy="{cy - 43}" r="11" fill="{al}" '
        f'fill-opacity="0.35" stroke="none"/>',
        # Curling tail off the lower-right haunch (drawn before the body so the
        # body outline sits cleanly on top of where it attaches).
        f'<path d="M {cx + 42} {cy + 66} C {cx + 84} {cy + 84} {cx + 110} {cy + 54} '
        f'{cx + 90} {cy + 26}" stroke-width="3"/>',
        # Head + body: one pear-shaped silhouette, widest at the haunches.
        f'<path d="M {cx} {cy - 42} '
        f'C {cx + 40} {cy - 42} {cx + 56} {cy - 12} {cx + 52} {cy + 18} '
        f'C {cx + 49} {cy + 52} {cx + 38} {cy + 80} {cx} {cy + 80} '
        f'C {cx - 38} {cy + 80} {cx - 49} {cy + 52} {cx - 52} {cy + 18} '
        f'C {cx - 56} {cy - 12} {cx - 40} {cy - 42} {cx} {cy - 42} Z" '
        f'fill="{a}" fill-opacity="0.10"/>',
        # Front paws.
        f'<ellipse cx="{cx - 16}" cy="{cy + 70}" rx="10" ry="6" '
        f'fill="{a}" fill-opacity="0.85" stroke="none"/>',
        f'<ellipse cx="{cx + 16}" cy="{cy + 70}" rx="10" ry="6" '
        f'fill="{a}" fill-opacity="0.85" stroke="none"/>',
    ]
    # Eyes: light "whites" with a dark pupil so the face reads at small sizes.
    for ex in (cx - 17, cx + 17):
        parts.append(f'<circle cx="{ex}" cy="{cy - 2}" r="7" fill="{FG}" stroke="none"/>')
        parts.append(f'<circle cx="{ex + 1}" cy="{cy - 1}" r="3" fill="{BG}" stroke="none"/>')
    # Nose (orange triangle) and a small smile.
    parts.append(
        f'<path d="M {cx - 7} {cy + 16} L {cx + 7} {cy + 16} L {cx} {cy + 25} Z" '
        f'fill="{ORANGE}" stroke="none"/>'
    )
    parts.append(
        f'<path d="M {cx - 8} {cy + 28} Q {cx} {cy + 35} {cx + 8} {cy + 28}" '
        f'stroke="{DIM}" stroke-width="1.5"/>'
    )
    # Whiskers: three per side, fanned from beside the nose.
    for ey in (cy + 12, cy + 20, cy + 28):
        parts.append(
            f'<line x1="{cx - 9}" y1="{cy + 20}" x2="{cx - 54}" y2="{ey}" '
            f'stroke="{DIM}" stroke-width="1"/>'
        )
        parts.append(
            f'<line x1="{cx + 9}" y1="{cy + 20}" x2="{cx + 54}" y2="{ey}" '
            f'stroke="{DIM}" stroke-width="1"/>'
        )
    parts.append("</g>")
    return "".join(parts)


def _kv(x: int, y: int, key: str, value: str, value_cls: str = "orange") -> str:
    """
    Render a neofetch-style key/value pair: left-aligned dim label, then value.

    The label uses fixed-width padding (12 chars) so values line up in a column
    even when keys have different lengths.
    """
    pad = max(0, 12 - len(key))
    return (
        f'<text x="{x}" y="{y}">'
        f'<tspan class="label">{_xml_escape(key)}</tspan>'
        f'{"&#160;" * pad}  '
        f'<tspan class="{value_cls}">{_xml_escape(value)}</tspan>'
        f"</text>"
    )


# -----------------------------------------------------------------------------
# Card renderers
# -----------------------------------------------------------------------------


def render_banner(d: dict[str, Any]) -> str:
    """
    Neofetch-style intro card: ASCII initials on the left, a column of key
    profile facts on the right. This is the "something cool" of the set --
    it does the same job as a normal hero header but in a developer-native
    terminal idiom.

    Layout: 760 x 320. Left third is the vector rat mascot (the neofetch
    "distro logo" slot), right two thirds is a 9-row key/value table covering
    the lifetime numbers.
    """
    w, h = 760, 320
    svg = [_shell_chrome(w, h, f"{d['login']} -- neofetch")]

    # Logo slot: the BadRat mascot, drawn as geometry so it stays crisp on any
    # client (see _rat_mascot for why this replaced the old block-letter "BR").
    rat_cx = 138
    svg.append(_rat_mascot(rat_cx, 148))

    # Caption centred beneath the mascot.
    svg.append(
        f'<text x="{rat_cx}" y="262" text-anchor="middle" class="dim small">'
        f"~/badrat-in</text>"
    )

    # Right column: stats. Vertical rhythm = 22px per row.
    col_x = 260
    row_y = 70
    rh = 24
    stack_summary = ", ".join(l["name"] for l in d["languages"][:4]) or "—"
    rows = [
        ("user", f"{d['name']}", "blue"),
        ("role", "Founder · RK Innovate", "orange"),
        ("uptime", f"{d['years']:.1f} years on GitHub", "green"),
        ("stack", stack_summary, "yellow"),
        ("repos", f"{d['total_repos']} (+{d['owned_forks']} forks)", "orange"),
        ("stars", f"★ {d['total_stars']}", "orange"),
        ("PRs", f"{d['total_prs']}", "orange"),
        ("issues", f"{d['total_issues']}", "orange"),
        ("commits/yr", f"{d['commits_year']}", "orange"),
    ]
    for i, (k, v, cls) in enumerate(rows):
        svg.append(_kv(col_x, row_y + i * rh, k, v, cls))

    svg.append("</svg>")
    return "".join(svg)


def render_languages(d: dict[str, Any]) -> str:
    """
    Top languages by byte count, drawn as horizontal progress bars.

    Each row: ``<lang>  <████░░░░░░░░>  <pct>%``. The bar fill colour comes
    from GitHub's per-language colour (the same one that powers the language
    dots on repo pages), keeping the card visually consistent with GitHub's
    own UI.
    """
    w, h = 760, 300
    svg = [_shell_chrome(w, h, "languages")]
    svg.append(_prompt_line(20, 56, "langstat", "--owner --by=bytes"))

    top = d["languages"][:6]
    if not top:
        svg.append(
            '<text x="20" y="120" class="dim">No language data available.</text></svg>'
        )
        return "".join(svg)

    bar_x = 150
    pct_w = 90  # px reserved on the right for the percentage label
    bar_max = w - bar_x - pct_w - 20  # bar track stretches to fill the card
    y0 = 95
    row_h = 32
    pct_x = bar_x + bar_max + 18

    for i, lang in enumerate(top):
        y = y0 + i * row_h
        name = _truncate(lang["name"], 14)
        pct = lang["pct"]
        fill_w = max(2, int(bar_max * pct / 100))

        # Language name (left-aligned).
        svg.append(f'<text x="20" y="{y}" class="blue">{_xml_escape(name)}</text>')

        # Background track + coloured fill. Rounded corners give the bar a
        # softer feel than a square block, which reads better against the
        # rounded card edges.
        svg.append(
            f'<rect x="{bar_x}" y="{y - 11}" width="{bar_max}" height="14" '
            f'rx="3" fill="#21262d"/>'
        )
        svg.append(
            f'<rect x="{bar_x}" y="{y - 11}" width="{fill_w}" height="14" '
            f'rx="3" fill="{lang["color"]}"/>'
        )

        # Percentage on the right.
        svg.append(
            f'<text x="{pct_x}" y="{y}" class="orange">{pct:.1f}%</text>'
        )

    svg.append("</svg>")
    return "".join(svg)


def render_activity(d: dict[str, Any]) -> str:
    """
    Last-30-day rollup + 30-week ASCII contribution heatmap.

    Top half: three big numbers (commits, active days, best day). Bottom half:
    a Mon..Sun x N-week grid of small squares coloured by contribution count
    -- a ``git log --graph``-flavoured take on the standard contribution heat
    grid.
    """
    w, h = 760, 372
    svg = [_shell_chrome(w, h, "activity")]
    svg.append(_prompt_line(20, 56, "contrib", "--since=30d"))

    # Top stats row -- three boxed numbers with labels under them.
    boxes = [
        ("commits", str(d["last30_commits"]), ORANGE),
        ("active days", f"{d['last30_active']} / 30", GREEN),
        ("best day", f"{d['best_day_count']}", ACCENT),
    ]
    box_w = (w - 80) // 3
    box_y = 80
    box_h = 70
    for i, (label, value, colour) in enumerate(boxes):
        x = 20 + i * (box_w + 10)
        # Subtle box outline -- using a fill rather than stroke for a softer
        # look that mimics terminal block elements rather than UI borders.
        svg.append(
            f'<rect x="{x}" y="{box_y}" width="{box_w}" height="{box_h}" '
            f'rx="6" fill="#161b22"/>'
        )
        svg.append(
            f'<text x="{x + box_w / 2}" y="{box_y + 32}" text-anchor="middle" '
            f'class="big" fill="{colour}">{_xml_escape(value)}</text>'
        )
        svg.append(
            f'<text x="{x + box_w / 2}" y="{box_y + 56}" text-anchor="middle" '
            f'class="label small">{_xml_escape(label)}</text>'
        )

    # Grid data first, so the section header can report the real week span.
    heat = d["heatmap"]
    max_day = max(1, d["max_day"])
    weeks = len(heat[0]) if heat and heat[0] else 0

    # Heatmap section header.
    svg.append(_prompt_line(20, 190, "heatmap", f"--weeks={weeks}"))

    # 7 x N grid. Cell ~16px square, 4px gap. The row labels (Mon/Wed/Fri)
    # sit to the left, matching GitHub's own contribution graph idiom.
    cell = 16
    gap = 4
    grid_x = 60
    grid_y = 210
    row_labels = ["Mon", "", "Wed", "", "Fri", "", ""]

    for row in range(7):
        if row_labels[row]:
            svg.append(
                f'<text x="{grid_x - 10}" y="{grid_y + row * (cell + gap) + 12}" '
                f'text-anchor="end" class="dim small">{row_labels[row]}</text>'
            )
        for col in range(weeks):
            count = heat[row][col]
            # Bucket the count into 5 shades. The thresholds are chosen so a
            # near-empty grid still shows some variation rather than flat dim.
            if count == 0:
                shade = HEATMAP_RAMP[0]
            else:
                ratio = count / max_day
                if ratio < 0.25:
                    shade = HEATMAP_RAMP[1]
                elif ratio < 0.5:
                    shade = HEATMAP_RAMP[2]
                elif ratio < 0.8:
                    shade = HEATMAP_RAMP[3]
                else:
                    shade = HEATMAP_RAMP[4]
            x = grid_x + col * (cell + gap)
            y = grid_y + row * (cell + gap)
            svg.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
                f'rx="3" fill="{shade}"/>'
            )

    svg.append("</svg>")
    return "".join(svg)


def render_top_repos(d: dict[str, Any]) -> str:
    """
    Top original (non-fork) repos sorted by stars, drawn as a terminal table.

    Each row shows: ``name`` (blue), ``★ stars`` (orange), ``language`` (dim),
    plus a short truncated description below. We cap at 4 entries -- past that,
    the card stops being scan-friendly and starts being a wall of text.
    """
    w, h = 760, 340
    svg = [_shell_chrome(w, h, "top-repos")]
    svg.append(_prompt_line(20, 56, "gh repo list", "--sort=stars --limit=4"))

    repos = d["top_repos"][:4]
    if not repos:
        svg.append(
            '<text x="20" y="120" class="dim">No repositories to display.</text></svg>'
        )
        return "".join(svg)

    row_h = 64
    y0 = 90
    for i, r in enumerate(repos):
        y = y0 + i * row_h
        name = _truncate(r["name"], 30)
        lang = r["language"]
        stars = r["stars"]
        desc = _truncate(r["description"], 92) or "(no description)"

        # Row separator above each entry except the first -- mimics a divided
        # table without adding visual noise of a full grid.
        if i > 0:
            svg.append(
                f'<line x1="20" y1="{y - 16}" x2="{w - 20}" y2="{y - 16}" '
                f'stroke="#21262d" stroke-width="1"/>'
            )

        svg.append(
            f'<text x="20" y="{y}" class="blue big">{_xml_escape(name)}</text>'
        )
        svg.append(
            f'<text x="{w - 20}" y="{y}" text-anchor="end" class="orange">'
            f"★ {stars}</text>"
        )
        svg.append(
            f'<text x="20" y="{y + 22}" class="dim small">{_xml_escape(desc)}</text>'
        )
        svg.append(
            f'<text x="{w - 20}" y="{y + 22}" text-anchor="end" '
            f'class="yellow small">{_xml_escape(lang)}</text>'
        )

    svg.append("</svg>")
    return "".join(svg)


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------


def main() -> int:
    """Fetch, process, render, write. Returns shell exit code."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN env var is required. Try: GITHUB_TOKEN=$(gh auth token)")

    raw = fetch_data(token, USER)
    data = process_data(raw)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cards = {
        "banner.svg": render_banner(data),
        "languages.svg": render_languages(data),
        "activity.svg": render_activity(data),
        "top-repos.svg": render_top_repos(data),
    }
    for name, svg in cards.items():
        path = OUT_DIR / name
        path.write_text(svg, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}  ({len(svg):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
