#!/usr/bin/env python3
"""
Renders a GitHub contributions calendar as an animated SVG.

Pulls the real contribution data for USER, draws a flat calendar on the left
and the summary stats on the right, and writes assets/calendar.svg.

Runs on stdlib only, so the GitHub Action needs no pip install.

Usage:
    python scripts/calendar.py                 # uses USER below
    python scripts/calendar.py --user someone
    python scripts/calendar.py --demo          # fake data, for local preview
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

USER = "JacobBhatt12"
OUT = "assets/calendar.svg"

# ---------------------------------------------------------------- palette
LEVELS = ["#2D8C46", "#5A9E1A", "#86C232", "#BBF047"]   # L1 .. L4
EMPTY = "#8B949E"                                        # drawn at low opacity
ACCENT = "#BBF047"
FONT_STACK = "'IS','Inclusive Sans',system-ui,-apple-system,'Segoe UI',sans-serif"

HW, HH = 7.0, 4.0      # half width / half height of one isometric tile
MAX_BAR = 34.0         # tallest block, in px
MIN_BAR = 3.0          # shortest block for a day with any activity

EASE_POWER3 = "cubic-bezier(.165,.84,.44,1)"
EASE_BACK = "cubic-bezier(.34,1.56,.64,1)"

def _shade(c, f):
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(min(255, int(v * f)) for v in (r, g, b))


TOPS = LEVELS
LEFTS = [_shade(c, 0.62) for c in LEVELS]        # shaded side faces
RIGHTS = [_shade(c, 0.40) for c in LEVELS]


# ---------------------------------------------------------------- fetching
def fetch_graphql(user, token):
    """Preferred path. Needs any token with read access to public profiles."""
    query = """
    query($login:String!){
      user(login:$login){
        contributionsCollection{
          contributionCalendar{
            totalContributions
            weeks{ contributionDays{ date contributionCount } }
          }
        }
      }
    }"""
    body = json.dumps({"query": query, "variables": {"login": user}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-calendar",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)
    if "errors" in payload:
        raise RuntimeError(payload["errors"])
    cal = payload["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    days = []
    for week in cal["weeks"]:
        for d in week["contributionDays"]:
            days.append((datetime.strptime(d["date"], "%Y-%m-%d").date(),
                         int(d["contributionCount"])))
    return days


def fetch_html(user):
    """Fallback. The public contributions fragment needs no auth at all."""
    req = urllib.request.Request(
        f"https://github.com/users/{user}/contributions",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")

    # Counts live either on the cell itself or in a matching <tool-tip>.
    tips = {}
    for m in re.finditer(r'<tool-tip[^>]*\sfor="([^"]+)"[^>]*>(.*?)</tool-tip>', html, re.S):
        num = re.match(r"\s*(?:(\d[\d,]*)|No)\s+contribution", m.group(2))
        tips[m.group(1)] = int(num.group(1).replace(",", "")) if num and num.group(1) else 0

    days = []
    for m in re.finditer(r"<td[^>]*data-date=[^>]*>", html):
        cell = m.group(0)
        d = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', cell)
        if not d:
            continue
        count = re.search(r'data-count="(\d+)"', cell)
        if count:
            n = int(count.group(1))
        else:
            cid = re.search(r'id="([^"]+)"', cell)
            n = tips.get(cid.group(1), 0) if cid else 0
        days.append((datetime.strptime(d.group(1), "%Y-%m-%d").date(), n))

    if not days:
        raise RuntimeError("no contribution cells found in the response")
    return days


def fetch(user):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        try:
            return fetch_graphql(user, token)
        except Exception as e:                       # noqa: BLE001
            print(f"graphql failed ({e}), falling back to html", file=sys.stderr)
    return fetch_html(user)


def demo_days():
    import random
    random.seed(4)
    end = date.today()
    start = end - timedelta(days=364)
    start -= timedelta(days=(start.weekday() + 1) % 7)   # back to a Sunday
    out, d = [], start
    while d <= end:
        r = random.random()
        n = 0 if r < 0.42 else (1 if r < 0.68 else (3 if r < 0.85 else (8 if r < 0.96 else 22)))
        if d.weekday() >= 5 and random.random() < 0.5:
            n = max(0, n - 2)
        out.append((d, n))
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- stats
def summarise(days):
    counts = [n for _, n in days]
    total = sum(counts)
    best = run = 0
    for n in counts:
        run = run + 1 if n else 0
        best = max(best, run)

    current = 0
    for i in range(len(counts) - 1, -1, -1):
        if counts[i]:
            current += 1
        elif i == len(counts) - 1:
            continue                                  # today may not be logged yet
        else:
            break

    return {
        "total": total,
        "best": best,
        "current": current,
        "busiest": max(counts) if counts else 0,
        "average": round(total / len(counts), 2) if counts else 0,
    }


def level_of(n, busiest):
    if n <= 0:
        return 0
    if busiest <= 4:
        return min(4, n)
    step = busiest / 4
    return min(4, max(1, int((n + step - 1) // step)))


# ---------------------------------------------------------------- rendering
def load_font(name):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, f"is-{name}.b64")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def shade(hex_color, factor):
    """Darken a hex colour for the side faces of each block."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(min(255, int(c * factor)) for c in (r, g, b))


def render(days, stats):
    regular, semibold = load_font("regular"), load_font("semibold")
    face = "".join(
        f"@font-face{{font-family:'IS';font-style:normal;font-weight:{w};"
        f"src:url(data:font/woff2;base64,{b}) format('woff2')}}"
        for w, b in ((400, regular), (600, semibold)) if b
    )

    # group into week columns, each starting on a Sunday
    weeks, col = [], []
    for d, n in days:
        if d.weekday() == 6 and col:
            weeks.append(col)
            col = []
        col.append((d, n))
    if col:
        weeks.append(col)

    ox = 6 * HW + 12.0                 # shift the left-most diagonal into view
    oy = MAX_BAR + 14.0
    ncols = len(weeks)
    width = 830
    cal_right = ox + (ncols - 1) * HW + HW
    height = oy + (ncols - 1 + 6) * HH + HH + 18

    busiest = max(stats["busiest"], 1)

    groups = []
    for wi, week in enumerate(weeks):
        faces = []
        for d, n in week:
            row = (d.weekday() + 1) % 7
            cx = ox + (wi - row) * HW
            gy = oy + (wi + row) * HH          # where the block meets the ground
            lvl = level_of(n, busiest)

            if not lvl:                         # empty day: a flat tile
                faces.append(
                    f'<path d="M{cx:.1f} {gy - HH:.1f}l{HW} {HH}l{-HW} {HH}l{-HW} {-HH}z" '
                    f'fill="{EMPTY}" fill-opacity=".17"/>'
                )
                continue

            h = MIN_BAR + (n / busiest) ** 0.62 * (MAX_BAR - MIN_BAR)
            top = gy - h                        # centre line of the raised top face
            base = TOPS[lvl - 1]
            cls = ' class="hot"' if lvl == 4 else ""
            faces.append(
                # left face, right face, then the top so it sits over both
                f'<path d="M{cx - HW:.1f} {top:.1f}l{HW} {HH}l0 {h:.1f}l{-HW} {-HH}z" fill="{LEFTS[lvl - 1]}"/>'
                f'<path d="M{cx:.1f} {top + HH:.1f}l{HW} {-HH}l0 {h:.1f}l{-HW} {HH}z" fill="{RIGHTS[lvl - 1]}"/>'
                f'<path d="M{cx:.1f} {top - HH:.1f}l{HW} {HH}l{-HW} {HH}l{-HW} {-HH}z" fill="{base}"{cls}/>'
            )
        groups.append(
            f'<g class="col" style="animation-delay:{0.15 + wi * 0.013:.2f}s">{"".join(faces)}</g>'
        )

    body = ["".join(groups)]

    # ---- stats, to the right of the strip
    sx = 508.0
    body.append(f'<g class="s1"><text x="{sx}" y="66" class="big">{stats["total"]:,}</text>'
                f'<text x="{sx}" y="88" class="tiny">CONTRIBUTIONS THIS YEAR</text></g>')
    rows = [("Current streak", f'{stats["current"]} days'),
            ("Best streak", f'{stats["best"]} days'),
            ("Busiest day", f'{stats["busiest"]}'),
            ("Daily average", f'{stats["average"]}')]
    for i, (k, v) in enumerate(rows):
        y = 132 + i * 30
        body.append(
            f'<g class="s{i + 2}">'
            f'<text x="{sx}" y="{y}" class="label">{k}</text>'
            f'<text x="{sx + 280:.0f}" y="{y}" class="value" text-anchor="end">{v}</text>'
            f'<line x1="{sx}" y1="{y + 11}" x2="{sx + 280:.0f}" y2="{y + 11}" class="rule"/>'
            f"</g>"
        )

    # ---- legend
    ly = height - 26
    body.append(f'<text x="{sx}" y="{ly + 7:.0f}" class="tiny">LESS</text>')
    lx = sx + 42
    for i, c in enumerate(TOPS):
        x = lx + i * 20
        body.append(f'<path d="M{x:.0f} {ly:.0f}l{HW} {HH}l{-HW} {HH}l{-HW} {-HH}z" fill="{c}"/>')
    body.append(f'<text x="{lx + 4 * 20 + 4:.0f}" y="{ly + 7:.0f}" class="tiny">MORE</text>')

    css = f"""
:root{{--muted:#8B949E;--ink:#57606A;--accent:#5A9E1A}}
@media (prefers-color-scheme:dark){{:root{{--ink:#E6EDF3;--accent:{ACCENT}}}}}
text{{font-family:{FONT_STACK}}}
.tiny{{font-size:9.5px;letter-spacing:.8px;fill:var(--muted)}}
.label{{font-size:13px;fill:var(--muted)}}
.value{{font-size:13px;font-weight:600;fill:var(--ink)}}
.big{{font-size:40px;font-weight:600;fill:var(--accent)}}
.rule{{stroke:var(--muted);stroke-opacity:.2;stroke-width:1}}
.col{{opacity:0;animation:rise .75s {EASE_POWER3} forwards}}
@keyframes rise{{from{{opacity:0;transform:translateY(18px)}}to{{opacity:1;transform:none}}}}
.hot{{animation:glow 3.6s ease-in-out 1.6s infinite}}
@keyframes glow{{0%,100%{{opacity:1}}50%{{opacity:.6}}}}
.s1,.s2,.s3,.s4,.s5{{opacity:0;animation:slide .85s {EASE_POWER3} forwards}}
.s1{{animation-delay:.9s}}.s2{{animation-delay:1.05s}}.s3{{animation-delay:1.15s}}
.s4{{animation-delay:1.25s}}.s5{{animation-delay:1.35s}}
@keyframes slide{{from{{opacity:0;transform:translateX(10px)}}to{{opacity:1;transform:none}}}}
"""

    alt = (f'Isometric contributions calendar. {stats["total"]} contributions in the last year, '
           f'current streak {stats["current"]} days, best streak {stats["best"]} days, '
           f'busiest day {stats["busiest"]}, daily average {stats["average"]}.')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height:.0f}" '
        f'viewBox="0 0 {width} {height:.0f}" fill="none" role="img" aria-label="{alt}">\n'
        f"<style>{face}{css}</style>\n" + "".join(body) + "\n</svg>\n"
    )


# ---------------------------------------------------------------- entry
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=USER)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    days = demo_days() if args.demo else fetch(args.user)
    days.sort(key=lambda t: t[0])
    stats = summarise(days)
    svg = render(days, stats)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        f.write(svg)
    print(f"wrote {args.out}  ({len(days)} days, {stats['total']} contributions)")


if __name__ == "__main__":
    main()
