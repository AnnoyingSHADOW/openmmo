"""Tutorial slides: a small Markdown subset -> HTML -> PNG (still) or MP4 (animated), drawn by a local
Chromium browser (Chrome / Brave / Edge in headless mode) so Khmer text shapes correctly and code gets
syntax colors.

Slide text format (what Claude writes and the editor shows):
    # Heading
    A short sentence.
    - bullet
    +++                      <- the parts after this line appear later (when the narration reaches a "|")
    - bullet
    ```python
    code                     <- code types itself line by line when its part appears
    +++                      <- inside a code block: the next lines type later
    more code
    ```
    ```output
    what the code prints
    ```
    ```diagram               <- an animated "how it works" diagram
    [browser] tabler:browser Browser           (icon: an open icon name, asset:<id> or an emoji)
    [server] tabler:server Server | [db] tabler:database Database   (one row per line; | = side by side)
    +++
    browser -> server: request                 (arrow draws itself, a glowing dot travels along it)
    +++
    server --> browser: web page               (--> dashed arrow, <-> two-way)
    ```
Inline `code` and **bold** are supported.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound

TEMPLATE_VERSION = "4"
CODE_STYLE = "github-dark"
OUTPUT_LANGS = {"output", "out", "result", "console", "terminal", "stdout"}
DIAGRAM_LANGS = {"diagram", "flow", "arch"}
BROWSER_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
# Windows: Chrome / Brave / Edge in Program Files or the user's AppData (Edge ships with Windows 10/11).
for _base in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                           os.environ.get("LOCALAPPDATA"))):
    BROWSER_CANDIDATES += [
        os.path.join(_base, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(_base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        os.path.join(_base, "Chromium", "Application", "chrome.exe"),
        os.path.join(_base, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
_render_slots = threading.Semaphore(2)  # at most 2 headless browsers at once

STEP_RE = re.compile(r"^\s*\+\+\+(?:\s+focus\s+(off|\d[\d,\s-]*))?\s*$", re.I)  # "+++" or "+++ focus 2-3"
QUIZ_COUNTDOWN = 5.0  # seconds of silence (with a countdown) before a quiz answer is revealed
NODE_RE = re.compile(r"^\[([A-Za-z0-9_-]{1,24})\]\s*(.*)$")
EDGE_RE = re.compile(r"^([A-Za-z0-9_-]{1,24})\s*(<-->|<->|-->|->)\s*([A-Za-z0-9_-]{1,24})\s*(?::\s*(.*))?$")
MAX_NODES, MAX_EDGES = 8, 12
# Diagram box icons: "tabler:server" (Tabler Icons, MIT) or "logos:python" (SVG Logos, CC0), or any other
# open icon set on Iconify (https://icon-sets.iconify.design), fetched once and cached in ICON_DIR.
# "asset:<id>" uses a picture from the app's asset library (e.g. your own logo). Emoji still work too.
ICON_RE = re.compile(r"^([a-z0-9]+(?:-[a-z0-9]+)*):([a-z0-9]+(?:-[a-z0-9]+)*)$")
ICONIFY_API = "https://api.iconify.design"
# "[logo] logos:java logos:spring-icon" on its own line: big logo tiles (e.g. above a title slide's heading)
LOGO_RE = re.compile(r"^[ \t]*\[logo\][ \t]*(.*)$", re.I | re.M)
ICON_DIR: Optional[Path] = None  # set by the app (data/icons)
_icon_lock = threading.Lock()

# Per-aspect layout: font sizes (px) and the space kept free at the bottom for burned-in captions.
LAYOUT = {
    "9:16": {"pad": 72, "top": 70, "h1": 74, "body": 42, "code": 36, "cap": 640, "cap_off": 160, "cover_h1": 104},
    "16:9": {"pad": 96, "top": 40, "h1": 62, "body": 36, "code": 31, "cap": 240, "cap_off": 80, "cover_h1": 96},
    "1:1": {"pad": 72, "top": 44, "h1": 60, "body": 36, "code": 30, "cap": 300, "cap_off": 100, "cover_h1": 88},
}


def find_browser() -> Optional[str]:
    env = os.environ.get("SLIDE_BROWSER")
    if env and os.access(env, os.X_OK):
        return env
    for p in BROWSER_CANDIDATES:
        if os.access(p, os.X_OK):
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser",
                 "microsoft-edge", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- #
# Markdown subset -> blocks (each with the part/step it appears in)
# --------------------------------------------------------------------------- #
def _inline(text: str) -> str:
    out, parts = [], re.split(r"(`[^`]+`)", text)
    for part in parts:
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            esc = html.escape(part)
            out.append(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc))
    return "".join(out)


def _split_icon(text: str) -> tuple[str, str]:
    """'tabler:server Web server' -> ('tabler:server', 'Web server'); '💻 Browser' -> ('💻', 'Browser').
    A leading icon name (set:name) or a token without letters or digits (an emoji) is the icon."""
    first, _, rest = text.partition(" ")
    if first and (ICON_RE.match(first) or (len(first) <= 8 and not any(ch.isalnum() for ch in first))):
        return first, rest.strip()
    return "", text.strip()


def _iconify_svg(prefix: str, name: str) -> Optional[str]:
    """The icon's SVG from the cache, else from the Iconify API (only the icon name is sent)."""
    folder = (ICON_DIR or Path(tempfile.gettempdir()) / "voiceclone-icons") / prefix
    path, missing = folder / f"{name}.svg", folder / f"{name}.missing"
    if path.exists():
        return path.read_text(encoding="utf-8")
    if missing.exists() and time.time() - missing.stat().st_mtime < 7 * 86400:
        return None
    with _icon_lock:
        if path.exists():
            return path.read_text(encoding="utf-8")
        try:
            req = urllib.request.Request(f"{ICONIFY_API}/{prefix}/{name}.svg", headers={"User-Agent": "VoiceCloneStudio"})
            with urllib.request.urlopen(req, timeout=8) as r:
                svg = r.read(400_000).decode("utf-8")
            if "<svg" not in svg[:300]:
                raise ValueError("not an SVG")
        except urllib.error.HTTPError as e:
            if e.code == 404:  # no such icon: don't ask again for a week
                folder.mkdir(parents=True, exist_ok=True)
                missing.touch()
            return None
        except Exception:  # noqa: BLE001  offline: the box shows its label only, and we try again next time
            return None
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(svg, encoding="utf-8")
        tmp.replace(path)
        return svg


SEARCH_SETS = ["logos", "devicon", "skill-icons", "vscode-icons", "tabler"]  # in this order of preference


def search_icons(query: str, limit: int = 24) -> list[str]:
    """Icon names for a word, real logos first (Iconify search API; only the search word is sent)."""
    url = (f"{ICONIFY_API}/search?query={urllib.parse.quote(query)}&prefixes={','.join(SEARCH_SETS)}&limit=96")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VoiceCloneStudio"})
        with urllib.request.urlopen(req, timeout=8) as r:
            icons = json.loads(r.read(500_000)).get("icons", [])
    except Exception:  # noqa: BLE001
        return []
    rank = {p: k for k, p in enumerate(SEARCH_SETS)}
    icons = [x for x in icons if isinstance(x, str) and ICON_RE.match(x)]
    return sorted(icons, key=lambda x: rank.get(x.split(":")[0], 99))[:limit]


def resolve_icon(spec: str) -> Optional[tuple[str, bool]]:
    """(data URI, one-color?) for an icon name or asset:<id>, or None if it can't be found.
    One-color icons are drawn in the slide's colors (and light up with the box); logos keep their colors."""
    if spec.startswith("asset:"):
        import assets  # late import: assets imports this module

        a = assets.get(spec[6:])
        return (_data_uri(a["path"]), False) if a else None
    m = ICON_RE.match(spec)
    if not m:
        return None
    svg = _iconify_svg(*m.groups())
    if not svg:
        return None
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode(), "currentColor" in svg


def icon_signature(md: str) -> tuple:
    """Which diagram icons were found (part of the slide's cache key, so a slide drawn offline is redrawn
    with its icons once they can be downloaded)."""
    blocks = parse(md)["blocks"]
    specs = [n["icon"] for b in blocks if b["kind"] == "diagram" for n in b["dia"]["nodes"].values()
             if n["icon"] and (ICON_RE.match(n["icon"]) or n["icon"].startswith("asset:"))]
    specs += [s for b in blocks if b["kind"] == "logos" for s in b["specs"]]
    return tuple((s, resolve_icon(s) is not None) for s in specs)


def _logos_html(specs: list[str], step: int) -> str:
    tiles = []
    for spec in specs:
        got = resolve_icon(spec)
        if not got:
            continue
        uri, mono = got
        inner = (f'<span class="m" style="-webkit-mask-image:url({uri});mask-image:url({uri})"></span>' if mono
                 else f'<img src="{uri}" alt="">')
        tiles.append(f'<div class="lg{" mono" if mono else ""}">{inner}</div>')
    if not tiles:
        return ""
    return f'<div class="logos" data-lstep="{step}">{"".join(tiles)}</div>'


def _icon_html(icon: str) -> str:
    if not icon:
        return ""
    if not (ICON_RE.match(icon) or icon.startswith("asset:")):
        return f'<div class="ic">{html.escape(icon)}</div>'  # emoji
    got = resolve_icon(icon)
    if not got:
        return ""
    uri, mono = got
    if mono:
        return f'<div class="ic ico mono" style="-webkit-mask-image:url({uri});mask-image:url({uri})"></div>'
    return f'<div class="ic ico"><img src="{uri}" alt=""></div>'


def _parse_diagram(lines: list[str], step: int, auto_from: Optional[int]) -> tuple[dict, int, int]:
    """Nodes (in rows) and arrows. Returns (diagram, step counter after it, last auto step used).
    auto_from: when the slide has no +++ at all, every arrow becomes its own part, numbered from here."""
    nodes: dict[str, dict] = {}
    rows: list[list[str]] = []
    edges: list[dict] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if STEP_RE.match(line):
            step += 1
            continue
        if line.startswith("["):
            row = []
            for part in re.split(r"\s+\|\s+(?=\[)", line):
                m = NODE_RE.match(part.strip())
                if not m or len(nodes) >= MAX_NODES:
                    continue
                nid = m.group(1).lower()
                if nid in nodes:
                    continue
                icon, label = _split_icon(m.group(2).strip())
                nodes[nid] = {"id": nid, "icon": icon, "label": label or m.group(1), "step": step}
                row.append(nid)
            if row:
                rows.append(row)
            continue
        m = EDGE_RE.match(line)
        if m and len(edges) < MAX_EDGES:
            arrow = m.group(2)
            edges.append({"a": m.group(1).lower(), "b": m.group(3).lower(), "dashed": "--" in arrow,
                          "both": arrow.startswith("<"), "label": (m.group(4) or "").strip()[:60], "step": step})
    extra = []  # arrows naming boxes that were never defined: give them a plain box in a last row
    for e in edges:
        for nid in (e["a"], e["b"]):
            if nid not in nodes and len(nodes) < MAX_NODES:
                nodes[nid] = {"id": nid, "icon": "", "label": nid, "step": e["step"]}
                extra.append(nid)
    for k in range(0, len(extra), 3):
        rows.append(extra[k:k + 3])
    edges = [e for e in edges if e["a"] in nodes and e["b"] in nodes and e["a"] != e["b"]]
    last_auto = auto_from - 1 if auto_from is not None else -1
    if auto_from is not None:
        for k, e in enumerate(edges):
            e["step"] = auto_from + k
            last_auto = e["step"]
    for e in edges:  # a box is on screen at the latest when an arrow touches it
        for nid in (e["a"], e["b"]):
            nodes[nid]["step"] = min(nodes[nid]["step"], e["step"])
    return {"nodes": nodes, "rows": rows, "edges": edges}, step, last_auto


def _focus_lines(spec: Optional[str]) -> Optional[list[int]]:
    """'2-3, 5' -> [2, 3, 5]; 'off' -> []; None (a plain +++) -> None."""
    if spec is None:
        return None
    out: list[int] = []
    for part in re.split(r"[,\s]+", spec.strip().lower()):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            out += list(range(min(a, b), min(max(a, b), min(a, b) + 30) + 1))
    return sorted(set(out))


def _parse_quiz(lines: list[str]) -> dict:
    """```quiz block: an optional question line, then 2-4 options; '- [x]' (or a trailing '*') marks the answer."""
    question, options = [], []
    for raw in lines:
        line = raw.strip()
        m = re.match(r"^(?:[-*•]|[A-Da-d][.)]|\d+[.)])\s+(?:\[( |x|X)\]\s*)?(.+)$", line)
        if m and len(options) < 4:
            text, ok = m.group(2).strip(), (m.group(1) or "").lower() == "x"
            if text.endswith(" *") or text.endswith("(correct)"):
                text, ok = re.sub(r"\s*(\*|\(correct\))$", "", text), True
            options.append({"text": text[:80], "ok": ok})
        elif line and not STEP_RE.match(line):
            question.append(line)
    return {"question": " ".join(question)[:160], "options": options}


def parse(md: str) -> dict:
    """Split slide Markdown into a heading and blocks (paragraphs, bullets, code/output, diagrams).
    Every element knows its part ("step"): 0 = on screen from the start, k = appears at the k-th '|'."""
    heading, heading_step, heading_at, blocks = "", 0, 0, []
    lines = (md or "").replace("\r\n", "\n").split("\n")
    explicit = any(STEP_RE.match(ln) for ln in lines)
    step, auto_next = 0, 1
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^\s*```\s*([\w+#.-]*)\s*$", line)
        if STEP_RE.match(line):
            step += 1
            focus = _focus_lines(STEP_RE.match(line).group(1))
            if focus is not None:  # spotlight lines of the code block above
                last = next((b for b in reversed(blocks) if b["kind"] == "code" and b["lang"].lower() not in OUTPUT_LANGS),
                            None)
                if last:
                    last["focus"].append({"step": step, "lines": focus})
        elif fence:
            lang, buf = fence.group(1), []
            i += 1
            while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            if lang.lower() in DIAGRAM_LANGS:
                start = step
                dia, step, last = _parse_diagram(buf, step, None if explicit else auto_next)
                auto_next = max(auto_next, last + 1)
                if dia["nodes"]:
                    blocks.append({"kind": "diagram", "step": start, "dia": dia})
            elif lang.lower() == "quiz":
                quiz = _parse_quiz(buf)
                if quiz["options"]:  # options appear in this part; the answer is revealed in the next one
                    blocks.append({"kind": "quiz", "step": step, "reveal": step + 1, **quiz})
                    step += 1
            else:
                start, code_lines, focus_list = step, [], []
                for ln in buf:
                    m = STEP_RE.match(ln)
                    if m:
                        step += 1
                        focus = _focus_lines(m.group(1))
                        if focus is not None:
                            focus_list.append({"step": step, "lines": focus})
                    else:
                        code_lines.append((ln, step))
                while code_lines and not code_lines[-1][0].strip():
                    code_lines.pop()
                blocks.append({"kind": "code", "lang": lang, "lines": code_lines, "step": start, "focus": focus_list})
        elif LOGO_RE.match(line):
            specs = [x for x in LOGO_RE.match(line).group(1).split() if ICON_RE.match(x) or x.startswith("asset:")]
            if specs:
                blocks.append({"kind": "logos", "specs": specs[:4], "step": step})
        elif re.match(r"^\s*#{1,3}\s+", line):
            text = re.sub(r"^\s*#{1,3}\s+", "", line).strip()
            if not heading:
                heading, heading_step, heading_at = text, step, len(blocks)
            else:
                blocks.append({"kind": "p", "text": text, "step": step})
        elif re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", line):
            blocks.append({"kind": "li", "text": re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line).strip(), "step": step})
        elif line.strip():
            blocks.append({"kind": "p", "text": line.strip(), "step": step})
        i += 1
    steps = heading_step if heading else 0
    for b in blocks:
        steps = max(steps, b["step"])
        if b["kind"] == "code":
            steps = max([steps] + [k for _, k in b["lines"]] + [f["step"] for f in b["focus"]])
        elif b["kind"] == "quiz":
            steps = max(steps, b["reveal"])
        elif b["kind"] == "diagram":
            dia = b["dia"]
            steps = max([steps] + [n["step"] for n in dia["nodes"].values()] + [e["step"] for e in dia["edges"]])
    return {"heading": heading, "heading_step": heading_step, "heading_at": heading_at, "blocks": blocks,
            "steps": steps}


def count_steps(md: str) -> int:
    """How many times something new appears on this slide (0 = a still slide)."""
    return parse(md)["steps"]


def quiz_reveal_steps(md: str) -> set[int]:
    """The parts that reveal a quiz answer (the narration pauses before them for the countdown)."""
    return {b["reveal"] for b in parse(md)["blocks"] if b["kind"] == "quiz"}


def has_logos(md: str) -> bool:
    """A slide with logo tiles is animated too (the logos pop in)."""
    return any(b["kind"] == "logos" for b in parse(md)["blocks"])


# --------------------------------------------------------------------------- #
# Blocks -> HTML
# --------------------------------------------------------------------------- #
def _rv(step: int, cls: str = "") -> str:
    """class + data-step attributes for an element that appears in a later part."""
    names = " ".join(x for x in (cls, "rv" if step else "") if x)
    return (f' class="{names}"' if names else "") + (f' data-step="{step}"' if step else "")


def _quiz_html(q: dict, step: int, reveal: int) -> str:
    """Options appear in their part; a countdown runs during the pause; then the answer lights up green.
    Without animation (the still preview) it shows the revealed answer."""
    opts = "".join(
        f'<div class="opt{" ok" if o["ok"] else ""}"><div class="in"><span class="key">{"ABCD"[k]}</span>'
        f'<span class="otx">{_inline(o["text"])}</span><span class="mark">{"✓" if o["ok"] else ""}</span></div></div>'
        for k, o in enumerate(q["options"]))
    question = f'<p class="qq">{_inline(q["question"])}</p>' if q["question"] else ""
    timer = ('<div class="timer"><svg viewBox="0 0 120 120"><circle class="bg" cx="60" cy="60" r="52"/>'
             '<circle class="fg" cx="60" cy="60" r="52" pathLength="100"/></svg><span class="num">5</span></div>')
    return (f'<div class="quiz" data-qstep="{step}" data-reveal="{reveal}">{question}<div class="opts">{opts}</div>'
            f"{timer}</div>")


def _code_block(lang: str, lines: list[tuple[str, int]], km: bool, step: int, focus: Optional[list] = None) -> str:
    if lang.lower() in OUTPUT_LANGS:
        label = "លទ្ធផល · Output" if km else "Output"
        body = "".join(f'<span{_rv(k if k > step else 0, "ol")}>{html.escape(t) or "&#8203;"}</span>'
                       for t, k in lines)
        return (f'<div{_rv(step, "out")}><div class="bar"><span class="label">▸ {label}</span></div>'
                f"<pre>{body}</pre></div>")
    texts = [t for t, _ in lines]
    try:
        lexer = get_lexer_by_name(lang, stripnl=False, ensurenl=True) if lang else TextLexer(stripnl=False)
    except ClassNotFound:
        lexer = TextLexer(stripnl=False)
    hl = highlight("\n".join(texts), lexer, HtmlFormatter(nowrap=True, noclasses=True, style=CODE_STYLE)).split("\n")
    if len(hl) < len(texts):  # unexpected lexer output: fall back to plain text
        hl = [html.escape(t) for t in texts]
    rows, numbered = [], len(lines) >= 2  # line numbers, so the narrator can say "look at line 2"
    for n, ((text, k), line_html) in enumerate(zip(lines, hl), 1):
        attrs = f' class="ln" data-step="{k}"' if k else ' class="ln"'
        no = f'<span class="no">{n}</span>' if numbered else ""
        rows.append(f'<span{attrs}>{no}<span class="tx">{line_html or "&#8203;"}</span></span>')
    label = html.escape(lang or "code")
    data = f" data-focus='{html.escape(json.dumps(focus), quote=True)}'" if focus else ""
    return (f'<div{_rv(step, "code")}{data}><div class="bar"><i></i><i></i><i></i><span class="label">{label}</span></div>'
            f"<pre>{''.join(rows)}</pre></div>")


def _diagram_html(dia: dict, geo: dict) -> str:
    """Boxes are placed in % of the diagram area; the browser draws the arrows once the fonts are in."""
    rows = dia["rows"]
    n_rows, widest = len(rows), max(len(r) for r in rows)
    cw, body = geo["width"], geo["body"]
    placed = []
    if geo["vertical"]:  # rows top -> bottom; boxes in a row spread to the edges so arrow labels fit between
        node_w = 360 if widest == 1 else min(300, (cw - (widest - 1) * 170) / widest)
        node_h, gap, pad = 206, (230 if n_rows <= 3 else 180), 14
        height = n_rows * node_h + (n_rows - 1) * gap + 2 * pad
        for i, row in enumerate(rows):
            for j, nid in enumerate(row):
                x = 50.0 if len(row) == 1 else (node_w / 2 + j * (cw - node_w) / (len(row) - 1)) / cw * 100
                placed.append((nid, x, (pad + i * (node_h + gap) + node_h / 2) / height * 100))
    else:  # rows become columns, left -> right
        col = cw / n_rows
        node_w = min(300, col * 0.6)
        node_h, gap, pad = 182, 140, 60
        height = widest * node_h + (widest - 1) * gap + 2 * pad
        for i, row in enumerate(rows):
            top = (height - (len(row) * node_h + (len(row) - 1) * gap)) / 2
            for j, nid in enumerate(row):
                placed.append((nid, (i + 0.5) / n_rows * 100, (top + j * (node_h + gap) + node_h / 2) / height * 100))
    parts = []
    for nid, x, y in placed:
        n = dia["nodes"][nid]
        icon = _icon_html(n["icon"])
        step = f' data-step="{n["step"]}"' if n["step"] else ""
        parts.append(f'<div class="node{"" if icon else " noicon"}" data-id="{nid}"{step} '
                     f'style="left:{x:.3f}%;top:{y:.3f}%">{icon}<div class="lb">{_inline(n["label"])}</div></div>')
    edges = [{"a": e["a"], "b": e["b"], "dashed": e["dashed"], "both": e["both"], "step": e["step"]}
             for e in dia["edges"]]
    labels = "".join(f'<div class="elabel" data-e="{k}">{_inline(e["label"])}</div>'
                     for k, e in enumerate(dia["edges"]) if e["label"])
    style = (f"height:calc(var(--s) * {height:.0f}px);--nw:{node_w:.0f}px;--nh:{node_h}px;"
             f"--nf:{round(body * 0.8)}px;--lf:{round(body * 0.68)}px")
    return (f'<div class="dia{" v" if geo["vertical"] else ""}" style="{style}" '
            f"data-edges='{html.escape(json.dumps(edges), quote=True)}'>{''.join(parts)}{labels}</div>")


def _blocks_html(blocks: list, km: bool, geo: dict) -> str:
    out, items = [], []

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li{_rv(k)}>{_inline(t)}</li>" for t, k in items) + "</ul>")
            items.clear()

    for b in blocks:
        if b["kind"] == "li":
            items.append((b["text"], b["step"]))
            continue
        flush()
        if b["kind"] == "p":
            out.append(f"<p{_rv(b['step'])}>{_inline(b['text'])}</p>")
        elif b["kind"] == "code":
            out.append(_code_block(b["lang"], b["lines"], km, b["step"], b.get("focus")))
        elif b["kind"] == "quiz":
            out.append(_quiz_html(b, b["step"], b["reveal"]))
        elif b["kind"] == "diagram":
            out.append(_diagram_html(b["dia"], geo))
        elif b["kind"] == "logos":
            out.append(_logos_html(b["specs"], b["step"]))
    flush()
    return "\n".join(out)


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    if path.suffix.lower() in (".ttf", ".otf"):
        mime = "font/ttf" if path.suffix.lower() == ".ttf" else "font/otf"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


# Runs inside the slide page. Lays the slide out (shrinks it to fit above the captions, draws diagram
# arrows), then exposes __setup(times, dur) and __frame(t) so a frame at any time t can be drawn exactly.
# Without __setup the page simply shows the finished slide (that is the still preview).
MOTION_JS = r"""
(function () {
  const root = document.documentElement, c = document.getElementById("c");
  const css = (k) => getComputedStyle(root).getPropertyValue(k).trim();
  const S = () => parseFloat(root.style.getPropertyValue("--s")) || 1;
  function overflowing() {  // measured from the blocks themselves: scrollHeight counts tall Khmer glyph boxes
    let top = Infinity, bottom = -Infinity;
    for (const k of c.children) { const r = k.getBoundingClientRect(); top = Math.min(top, r.top); bottom = Math.max(bottom, r.bottom); }
    return bottom - top > c.clientHeight + 2;
  }
  function fit() {
    let s = 1;
    root.style.setProperty("--s", "1");
    while (overflowing() && s > 0.45) { s -= 0.04; root.style.setProperty("--s", s.toFixed(2)); }
  }
  const hex = (h) => (/^#[0-9a-f]{6}$/i.test(h) ? [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) : [255, 210, 31]);
  const ACC = hex(css("--accent")), MUTED = [176, 188, 214], BORDER = [92, 104, 136], ICON = [226, 232, 248];
  const rgb = (a) => `rgb(${a.map(Math.round).join(",")})`;
  const mix = (a, b, k) => a.map((v, i) => v + (b[i] - v) * k);
  const clamp = (x) => Math.max(0, Math.min(1, x));
  const easeOut = (x) => 1 - Math.pow(1 - x, 3);
  const easeInOut = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
  const back = (x) => 1 + 2.70158 * Math.pow(x - 1, 3) + 1.70158 * Math.pow(x - 1, 2);
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
  let DIAS = [];

  function rayExit(o, d, r, pad) {  // where a ray from inside box r leaves it (box grown by pad)
    const hw = r.w / 2 + pad, hh = r.h / 2 + pad;
    let t = Infinity;
    if (Math.abs(d.x) > 1e-6) t = Math.min(t, ((d.x > 0 ? r.cx + hw : r.cx - hw) - o.x) / d.x);
    if (Math.abs(d.y) > 1e-6) t = Math.min(t, ((d.y > 0 ? r.cy + hh : r.cy - hh) - o.y) / d.y);
    if (!isFinite(t) || t < 0) t = 0;
    return { x: o.x + d.x * t, y: o.y + d.y * t };
  }
  function segHits(a, b, r, pad) {  // does the segment a-b cross box r (grown by pad)?
    const x0 = r.cx - r.w / 2 - pad, x1 = r.cx + r.w / 2 + pad, y0 = r.cy - r.h / 2 - pad, y1 = r.cy + r.h / 2 + pad;
    const dx = b.x - a.x, dy = b.y - a.y;
    let t0 = 0, t1 = 1;
    for (const [p, q] of [[-dx, a.x - x0], [dx, x1 - a.x], [-dy, a.y - y0], [dy, y1 - a.y]]) {
      if (p === 0) { if (q < 0) return false; continue; }
      const t = q / p;
      if (p < 0) { if (t > t1) return false; if (t > t0) t0 = t; } else { if (t < t0) return false; if (t < t1) t1 = t; }
    }
    return t0 <= t1;
  }
  const unit = (v) => { const l = Math.hypot(v.x, v.y) || 1; return { x: v.x / l, y: v.y / l }; };

  function layoutDiagram(d, idx) {
    const s = S();
    d.querySelectorAll("svg.wires").forEach((x) => x.remove());
    const W = d.clientWidth, H = d.clientHeight, r0 = d.getBoundingClientRect();
    const box = {}, nodes = [];
    d.querySelectorAll(".node").forEach((el) => {
      el.style.transform = "translate(-50%,-50%)"; el.style.opacity = ""; el.style.borderColor = ""; el.style.boxShadow = "";
      const r = el.getBoundingClientRect();
      const n = { el, ico: el.querySelector(".ico.mono"), id: el.dataset.id, step: +(el.dataset.step || 0), cx: r.left - r0.left + r.width / 2,
                  cy: r.top - r0.top + r.height / 2, w: r.width, h: r.height, arr: [] };
      box[n.id] = n; nodes.push(n);
    });
    const edges = JSON.parse(d.dataset.edges || "[]");
    const svg = mk("svg", { class: "wires", width: W, height: H, viewBox: `0 0 ${W} ${H}` });
    const defs = mk("defs", {});
    svg.appendChild(defs);
    d.insertBefore(svg, d.firstChild);
    const groups = {};
    edges.forEach((e, k) => { const key = [e.a, e.b].sort().join("|"); (groups[key] = groups[key] || []).push(k); });
    const sw = Math.max(3, 5.5 * s), out = [];
    edges.forEach((e, k) => {
      const A = box[e.a], B = box[e.b], lab = d.querySelector(`.elabel[data-e="${k}"]`);
      if (!A || !B || A === B) { if (lab) lab.style.display = "none"; return; }
      const grp = groups[[e.a, e.b].sort().join("|")];
      const off = grp.length > 1 ? (grp.indexOf(k) - (grp.length - 1) / 2) * 34 * s : 0;
      const P = e.a < e.b ? A : B, Q = e.a < e.b ? B : A;
      const N = unit({ x: -(Q.cy - P.cy), y: Q.cx - P.cx });  // same for both directions, so pairs sit side by side
      const a = { x: A.cx + N.x * off, y: A.cy + N.y * off }, b = { x: B.cx + N.x * off, y: B.cy + N.y * off };
      let ctrl = null;
      const obst = nodes.filter((o) => o !== A && o !== B && segHits(a, b, o, 12 * s));
      if (obst.length) {  // another box is in the way: bend around it
        const u = unit({ x: b.x - a.x, y: b.y - a.y }), n = { x: -u.y, y: u.x };
        const o = obst[0], cr = u.x * (o.cy - a.y) - u.y * (o.cx - a.x);
        const side = Math.abs(cr) < 1 ? (off < 0 ? -1 : 1) * (n.x * N.x + n.y * N.y < 0 ? -1 : 1) : (cr > 0 ? -1 : 1);
        const reach = Math.abs(o.w / 2 * n.x) + Math.abs(o.h / 2 * n.y) + 34 * s;
        ctrl = { x: (a.x + b.x) / 2 + n.x * side * reach * 2, y: (a.y + b.y) / 2 + n.y * side * reach * 2 };
      }
      const pa = rayExit(a, unit({ x: (ctrl || b).x - a.x, y: (ctrl || b).y - a.y }), A, 10 * s);
      const pb = rayExit(b, unit({ x: (ctrl || a).x - b.x, y: (ctrl || a).y - b.y }), B, 12 * s);
      const dpath = ctrl ? `M${pa.x},${pa.y} Q${ctrl.x},${ctrl.y} ${pb.x},${pb.y}` : `M${pa.x},${pa.y} L${pb.x},${pb.y}`;
      const id = `m${idx}_${k}`;
      const mask = mk("mask", { id, maskUnits: "userSpaceOnUse", x: -W, y: -H, width: 3 * W, height: 3 * H });
      const mp = mk("path", { d: dpath, stroke: "#fff", "stroke-width": sw * 4, fill: "none" });
      mask.appendChild(mp); defs.appendChild(mask);
      const line = mk("path", { d: dpath, stroke: rgb(MUTED), "stroke-width": sw, fill: "none", "stroke-linecap": "round",
                                mask: `url(#${id})` });
      if (e.dashed) line.setAttribute("stroke-dasharray", `${(16 * s).toFixed(1)} ${(13 * s).toFixed(1)}`);
      svg.appendChild(line);
      const len = line.getTotalLength();
      mp.setAttribute("stroke-dasharray", `${len + 4} ${len + 4}`);
      mp.setAttribute("stroke-dashoffset", "0");
      const head = (at, from) => {
        const tip = line.getPointAtLength(at), prev = line.getPointAtLength(from);
        const u = unit({ x: tip.x - prev.x, y: tip.y - prev.y }), hl = 24 * s, hw = 13 * s;
        const bx = tip.x - u.x * hl, by = tip.y - u.y * hl;
        const poly = mk("polygon", { points: `${tip.x},${tip.y} ${bx - u.y * hw},${by + u.x * hw} ${bx + u.y * hw},${by - u.x * hw}`,
                                     fill: rgb(MUTED) });
        svg.appendChild(poly);
        return poly;
      };
      const heads = [head(len, Math.max(0, len - 2))];
      if (e.both) heads.push(head(0, Math.min(len, 2)));
      const packet = mk("circle", { r: (12 * s).toFixed(1), cx: pa.x, cy: pa.y, class: "packet", fill: rgb(ACC) });
      packet.style.opacity = 0;
      svg.appendChild(packet);
      if (lab) {  // label beside the middle of the arrow: above horizontal arrows, right of vertical ones
        const mid = line.getPointAtLength(len / 2), m1 = line.getPointAtLength(Math.max(0, len / 2 - 2)),
              m2 = line.getPointAtLength(Math.min(len, len / 2 + 2));
        const t = unit({ x: m2.x - m1.x, y: m2.y - m1.y });
        let sv = { x: -t.y, y: t.x };
        const flip = () => (sv = { x: -sv.x, y: -sv.y });
        if (ctrl) { const cx = ctrl.x - (pa.x + pb.x) / 2, cy = ctrl.y - (pa.y + pb.y) / 2; if (cx * sv.x + cy * sv.y < 0) flip(); }
        else if (off) { if ((N.x * sv.x + N.y * sv.y) * off < 0) flip(); }
        else if (Math.abs(t.x) >= Math.abs(t.y)) { if (sv.y > 0) flip(); }
        else if (sv.x < 0) flip();
        const horizontal = Math.abs(t.x) >= Math.abs(t.y);
        lab.style.maxWidth = (horizontal && !ctrl ? Math.max(170 * s, Math.abs(pb.x - pa.x) - 16 * s) : W * 0.42) + "px";
        lab.style.left = "0px"; lab.style.top = "0px"; lab.style.transform = "translate(-50%,-50%)"; lab.style.opacity = "";
        const lw = lab.offsetWidth, lh = lab.offsetHeight, gap = 14 * s;
        const ext = gap + Math.abs(sv.x) * lw / 2 + Math.abs(sv.y) * lh / 2;
        let x = mid.x + sv.x * ext;
        const y = mid.y + sv.y * ext;
        x = Math.max(lw / 2, Math.min(W - lw / 2, x));
        lab.style.left = x + "px"; lab.style.top = y + "px";
      }
      out.push({ e, A, B, line, mp, len, heads, packet, lab });
    });
    DIAS.push({ nodes, edges: out });
  }
  function layout() {
    fit();
    DIAS = [];
    document.querySelectorAll(".dia").forEach((d, i) => layoutDiagram(d, i));
  }

  // ---- animation ----
  let T = [0], DUR = 0, ITEMS = [], BUSY = [], lastBusy = true, CW = 20, FBOX = [], EV = [];
  const at = (k) => (k <= 0 ? 0 : T[k] !== undefined ? T[k] : 1e9);
  const nextAt = (k) => { for (let j = k + 1; j < T.length; j++) if (T[j] > at(k) + 0.01) return T[j]; return Infinity; };
  const sfx = (t, k, d) => { if (t >= 0 && t < DUR) EV.push(d ? { t, k, d } : { t, k }); };
  window.__sfx = () => EV;  // sound effects for this slide: [{t, k(ind), d(uration)}]
  window.__setup = function (times, dur) {
    T = [0].concat(times); DUR = dur; ITEMS = []; BUSY = []; lastBusy = true; FBOX = []; EV = [];
    document.querySelectorAll(".rv[data-step]").forEach((el) => {
      const a = at(+el.dataset.step) + 0.05;
      ITEMS.push({ kind: "rv", el, a }); BUSY.push([a, a + 0.55]); sfx(a, "pop");
    });
    document.querySelectorAll(".quiz").forEach((q) => {  // options slide in, countdown, then the answer lights up
      const a = at(+q.dataset.qstep), b = at(+q.dataset.reveal), cd0 = b - QUIZ_CD;
      const qq = q.querySelector(".qq");
      if (qq) ITEMS.push({ kind: "rv", el: qq, a: a + 0.05 });
      q.querySelectorAll(".opt").forEach((el, j) => {
        const t0 = a + 0.15 + 0.13 * j;
        ITEMS.push({ kind: "rv", el, a: t0 }); sfx(t0, "pop");
      });
      ITEMS.push({ kind: "quiz", el: q, timer: q.querySelector(".timer"), fg: q.querySelector(".timer .fg"),
                   num: q.querySelector(".timer .num"), ok: q.querySelector(".opt.ok"), b, cd0 });
      q.style.setProperty("--r", "0");
      BUSY.push([a, a + 1.2], [cd0, b + 0.7]);
      for (let n = Math.ceil(QUIZ_CD); n >= 1; n--) if (b - n >= cd0 - 0.01) sfx(b - n, n === 1 ? "tick2" : "tick");
      sfx(Math.max(cd0, b - QUIZ_CD), "tick");
      sfx(b, q.querySelector(".opt.ok") ? "correct" : "pop");
    });
    document.querySelectorAll(".code[data-focus]").forEach((box) => {  // spotlight on lines: dim the rest, zoom in
      const lines = [...box.querySelectorAll(".ln")], cr = c.getBoundingClientRect(), br = box.getBoundingClientRect();
      const items = JSON.parse(box.dataset.focus).filter((f) => f.lines.length).map((f) => {
        const set = new Set(f.lines), hit = lines.filter((_, i) => set.has(i + 1));
        const ys = hit.map((l) => { const r = l.getBoundingClientRect(); return r.top + r.height / 2 - cr.top; });
        const F = { set, a: at(f.step) + 0.05, until: nextAt(f.step), w: 0,
                    ox: br.left + br.width / 2 - cr.left, oy: ys.length ? ys.reduce((x, y) => x + y, 0) / ys.length : br.height / 2 };
        BUSY.push([F.a, F.a + 0.5]); if (isFinite(F.until)) BUSY.push([F.until, F.until + 0.5]);
        sfx(F.a, "focus");
        return F;
      });
      if (items.length) FBOX.push({ lines, items });
    });
    document.querySelectorAll(".code").forEach((box) => {
      const bstep = +(box.dataset.step || 0), groups = {};
      box.querySelectorAll(".ln[data-step]").forEach((l) => { const k = +l.dataset.step; (groups[k] = groups[k] || []).push(l); });
      for (const ks in groups) {
        const k = +ks, ls = groups[k];
        let t = at(k) + (k === bstep ? 0.45 : 0.12);
        const chars = ls.reduce((n, l) => n + l.textContent.trim().length, 0) || 1;
        const room = Math.max(0.8, (Math.min(nextAt(k), DUR - 0.8) - t) * 0.8);
        const cps = Math.max(20, chars / room);
        ls.forEach((l) => {
          const tx = l.querySelector(".tx"), n = (tx || l).textContent.replace(/\s+$/, "").length;
          const d = n ? Math.max(0.15, n / cps) : 0.06;
          const lr = l.getBoundingClientRect(), tr = tx ? tx.getBoundingClientRect() : lr;
          ITEMS.push({ kind: "type", el: l, a: t, b: t + d, w: tr.width, ox: tr.left - lr.left });
          BUSY.push([t, t + d + 1.3]);
          if (n) sfx(t, "type", d);
          t += d + 0.05;
        });
      }
    });
    document.querySelectorAll(".logos").forEach((row) => {  // logo tiles pop in, one after another
      const k = +(row.dataset.lstep || 0);
      row.querySelectorAll(".lg").forEach((el, j) => {
        const a = at(k) + (k ? 0.05 : 0.3) + 0.16 * j;
        ITEMS.push({ kind: "pop", el, a }); BUSY.push([a, a + 0.7]); sfx(a, "pop2");
      });
    });
    CW = (parseFloat(getComputedStyle(document.querySelector("pre") || document.body).fontSize) || 30) * 0.62;
    DIAS.forEach((D) => {
      const order = {};
      D.nodes.forEach((n) => {
        const k = n.step, j = order[k] = (order[k] === undefined ? 0 : order[k] + 1);
        n.a = at(k) + (k === 0 ? 0.25 : 0.05) + 0.13 * j; n.arr = [];
        BUSY.push([n.a, n.a + 0.5]); sfx(n.a, "pop");
      });
      const count = {};
      D.edges.forEach((E) => {
        const k = E.e.step, j = count[k] = (count[k] === undefined ? 0 : count[k] + 1);
        E.a = Math.max(at(k) + 0.05, E.A.a + 0.4, E.B.a + 0.4) + j * 1.25;
        E.p0 = E.a + 0.25; E.p1 = E.a + 1.25; E.q0 = E.a + 1.4; E.q1 = E.a + 2.4;
        E.dim = Math.max(nextAt(k), (E.e.both ? E.q1 : E.p1) + 0.2);
        E.B.arr.push(E.p1); if (E.e.both) E.A.arr.push(E.q1);
        sfx(E.p0, "whoosh", E.p1 - E.p0); sfx(E.p1, "blip");
        if (E.e.both) { sfx(E.q0, "whoosh", E.q1 - E.q0); sfx(E.q1, "blip"); }
        BUSY.push([E.a, (E.e.both ? E.q1 : E.p1) + 0.1]);
        if (isFinite(E.dim)) BUSY.push([E.dim, E.dim + 0.5]);
      });
      D.nodes.forEach((n) => n.arr.forEach((ta) => BUSY.push([ta, ta + 1.7])));
    });
    if (document.querySelector(".bgimg")) BUSY.push([0, DUR + 1]);
  };
  window.__frame = function (t) {
    const s = S();
    for (const it of ITEMS) {
      if (it.kind === "rv") {
        const p = clamp((t - it.a) / 0.5), e = easeOut(p);
        it.el.style.opacity = p >= 1 ? "" : e.toFixed(3);
        it.el.style.transform = p >= 1 ? "" : `translateY(${((1 - e) * 26 * s).toFixed(1)}px)`;
      } else if (it.kind === "pop") {
        const p = clamp((t - it.a) / 0.6);
        it.el.style.opacity = p >= 1 ? "" : clamp(p * 2.5).toFixed(3);
        it.el.style.transform = p >= 1 ? "" : `scale(${(0.35 + 0.65 * back(p)).toFixed(4)}) rotate(${((1 - easeOut(p)) * -8).toFixed(2)}deg)`;
      } else if (it.kind === "quiz") {
        const r = clamp((t - it.b) / 0.45), left = it.b - t;
        it.el.style.setProperty("--r", easeOut(r).toFixed(3));
        it.timer.style.opacity = (clamp((t - it.cd0) / 0.3) * (1 - clamp((t - it.b) / 0.3))).toFixed(3);
        it.fg.style.strokeDashoffset = ((1 - clamp(left / QUIZ_CD)) * 100).toFixed(2);
        it.num.textContent = left > 0 ? String(Math.ceil(left - 1e-6)) : "";
        const beat = left > 0 && t >= it.cd0 ? 1 - (left - Math.floor(left)) : 0;  // little pulse on each second
        it.timer.style.transform = `scale(${(1 + 0.07 * Math.max(0, 1 - beat * 4)).toFixed(4)})`;
        if (it.ok) it.ok.style.transform = r > 0 && r < 1 ? `scale(${(1 + 0.05 * Math.sin(Math.PI * r)).toFixed(4)})` : "";
      } else {
        const el = it.el;
        if (t < it.a) { el.style.visibility = "hidden"; el.classList.remove("typing"); el.style.clipPath = ""; }
        else if (t < it.b) {
          const x = it.ox + it.w * (t - it.a) / (it.b - it.a);
          el.style.visibility = ""; el.classList.add("typing");
          el.style.clipPath = `inset(-6px calc(100% - ${(x + CW).toFixed(1)}px) -6px -6px)`;
          el.style.setProperty("--cx", x.toFixed(1) + "px");
        } else { el.style.visibility = ""; el.classList.remove("typing"); el.style.clipPath = ""; }
        const hl = t < it.a ? 0 : t < it.b ? 1 : clamp(1 - (t - it.b) / 1.2);
        el.style.background = hl > 0 ? `rgba(${ACC.join(",")},${(0.15 * hl).toFixed(3)})` : "";
      }
    }
    for (const D of DIAS) {
      for (const n of D.nodes) {
        const p = clamp((t - n.a) / 0.45);
        let sc = p >= 1 ? 1 : 0.6 + 0.4 * back(p), glow = 0;
        for (const ta of n.arr) {
          const u = t - ta;
          if (u >= 0 && u < 0.6) sc *= 1 + 0.08 * Math.sin(Math.PI * u / 0.6);
          if (u >= 0 && u < 1.7) glow = Math.max(glow, 1 - u / 1.7);
        }
        n.el.style.opacity = p >= 1 ? "" : clamp(p * 1.8).toFixed(3);
        n.el.style.transform = `translate(-50%,-50%) scale(${sc.toFixed(4)})`;
        n.el.style.borderColor = glow > 0 ? rgb(mix(BORDER, ACC, glow)) : "";
        if (n.ico) n.ico.style.background = glow > 0 ? rgb(mix(ICON, ACC, glow)) : "";
        n.el.style.boxShadow = glow > 0 ? `0 0 ${(50 * glow * s).toFixed(1)}px rgba(${ACC.join(",")},${(0.6 * glow).toFixed(3)})` : "";
      }
      for (const E of D.edges) {
        const p = clamp((t - E.a) / 0.6), dim = isFinite(E.dim) ? clamp((t - E.dim) / 0.45) : 0;
        E.mp.setAttribute("stroke-dashoffset", ((1 - easeInOut(p)) * (E.len + 4)).toFixed(2));
        const col = rgb(mix(ACC, MUTED, dim));
        E.line.setAttribute("stroke", col);
        E.heads.forEach((h, j) => { h.setAttribute("fill", col); h.style.opacity = j === 0 ? clamp((p - 0.85) / 0.15) : clamp(p * 6); });
        let pos = null;
        if (t >= E.p0 && t <= E.p1) pos = easeInOut((t - E.p0) / (E.p1 - E.p0));
        else if (E.e.both && t >= E.q0 && t <= E.q1) pos = 1 - easeInOut((t - E.q0) / (E.q1 - E.q0));
        if (pos === null) E.packet.style.opacity = 0;
        else {
          const pt = E.line.getPointAtLength(pos * E.len);
          E.packet.setAttribute("cx", pt.x.toFixed(1)); E.packet.setAttribute("cy", pt.y.toFixed(1)); E.packet.style.opacity = 1;
        }
        if (E.lab) {
          const q = clamp((t - E.a - 0.35) / 0.4);
          E.lab.style.opacity = (easeOut(q) * (1 - 0.3 * dim)).toFixed(3);
          E.lab.style.transform = `translate(-50%,-50%) scale(${(0.88 + 0.12 * easeOut(q)).toFixed(4)})`;
        }
      }
    }
    let zoom = null;  // spotlight: the focused lines glow, the rest dim, and the slide zooms in a little
    for (const B of FBOX) {
      let wMax = 0;
      for (const F of B.items) { F.w = easeInOut(clamp((t - F.a) / 0.45) * (1 - clamp((t - F.until) / 0.45))); wMax = Math.max(wMax, F.w); if (!zoom || F.w > zoom.w) zoom = F; }
      B.lines.forEach((el, i) => {
        let hw = 0;
        for (const F of B.items) if (F.set.has(i + 1)) hw = Math.max(hw, F.w);
        el.style.opacity = wMax > 0 ? (1 - 0.62 * (wMax - hw)).toFixed(3) : "";
        if (hw > 0) {
          el.style.background = `rgba(${ACC.join(",")},${(0.17 * hw).toFixed(3)})`;
          el.style.boxShadow = `inset ${(7 * s).toFixed(1)}px 0 0 rgba(${ACC.join(",")},${hw.toFixed(3)})`;
          el._f = true;
        } else if (el._f) { el.style.background = ""; el.style.boxShadow = ""; el._f = false; }
      });
    }
    c.style.transform = zoom && zoom.w > 0 ? `scale(${(1 + 0.07 * zoom.w).toFixed(4)})` : "";
    if (zoom && zoom.w > 0) c.style.transformOrigin = `${zoom.ox.toFixed(1)}px ${zoom.oy.toFixed(1)}px`;
    const bg = document.querySelector(".bgimg");
    if (bg && DUR) bg.style.transform = `scale(${(1 + 0.04 * Math.min(1, t / DUR)).toFixed(5)})`;
    const now = BUSY.some(([a, b]) => t >= a - 0.001 && t <= b + 0.04), busy = now || lastBusy;
    lastBusy = now;
    return busy;
  };
  layout();
  window.__ready = new Promise((resolve) => {
    const fonts = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
    fonts.then(() => setTimeout(() => { layout(); resolve(true); }, 0));
  });
})();
"""


def build_html(md: str, *, layout: str, size: tuple[int, int], aspect: str, accent: str, heading_font: str,
               font_files: list[Path], lesson: str, step: str, km: bool, captions: bool,
               background: Optional[Path] = None, embed_fonts: bool = False,
               autoplay: Optional[tuple[list[float], float]] = None) -> str:
    """embed_fonts: put the fonts inside the page (for showing it in the app's browser tab).
    autoplay: (times, duration) to loop the animation in a normal browser (the preview player)."""
    w, h = size
    L = LAYOUT.get(aspect, LAYOUT["9:16"])
    cap = L["cap"] if captions else L["cap_off"]
    data = parse(md)
    faces = "\n".join(
        f'@font-face {{ font-family: "{f.stem.split("-")[0]}"; '
        f'src: url("{_data_uri(f) if embed_fonts else f.resolve().as_uri()}"); }}'
        for f in font_files)
    head_stack = f'"{heading_font}", "Koulen", "Battambang", "Khmer Sangam MN", "Khmer UI", sans-serif'
    body_stack = ('"Battambang", "Hanuman", "Khmer Sangam MN", "Khmer MN", "Khmer UI", "Noto Sans Khmer", '
                  '"Helvetica Neue", "Segoe UI", Arial, sans-serif')
    mono_stack = ('"SF Mono", Menlo, Monaco, Consolas, "Cascadia Mono", "Courier New", "Khmer Sangam MN", "Khmer UI", '
                  'monospace')
    cover = layout == "cover"
    bg = ""
    if cover and background and background.exists():
        bg = (f'<img class="bgimg" src="{_data_uri(background)}" alt="">'
              '<div class="shade"></div>')
    heading = _inline(data["heading"]) if data["heading"] else ""
    geo = {"width": w - 2 * L["pad"], "body": L["body"], "vertical": aspect == "9:16"}
    split = data["heading_at"] if data["heading"] else 0  # e.g. a [logo] line above the heading
    before = _blocks_html(data["blocks"][:split], km, geo)
    body = _blocks_html(data["blocks"][split:], km, geo)
    accent = accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent or "") else "#FFD21F"
    play = ""
    if autoplay:
        times, dur = autoplay
        play = (f"<script>window.__ready.then(() => {{ __setup({json.dumps([round(x, 3) for x in times])}, {dur:.3f});"
                f" const t0 = performance.now(), loop = (now) => {{ const t = ((now - t0) / 1000) % ({dur:.3f} + 1.5);"
                f" __frame(Math.min(t, {dur:.3f})); requestAnimationFrame(loop); }}; requestAnimationFrame(loop); }});"
                "</script>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
{faces}
:root {{ --s: 1; --accent: {accent}; --lg: {round(L['cover_h1'] * 1.75) if cover else round(L['h1'] * 1.5)}px; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; }}
body {{ background: radial-gradient(120% 80% at 0% 0%, #1d2a52 0%, #0e1530 45%, #090d1c 100%); color: #f4f6fb;
  font-family: {body_stack}; -webkit-font-smoothing: antialiased; position: relative; }}
.bgimg {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; transform-origin: 50% 50%; }}
.shade {{ position: absolute; inset: 0; background: linear-gradient(180deg, rgba(5,8,20,.55) 0%, rgba(5,8,20,.25) 35%,
  rgba(5,8,20,.80) 70%, rgba(5,8,20,.92) 100%); }}
.top {{ position: absolute; top: {L['top']}px; left: {L['pad']}px; right: {L['pad']}px; display: flex; gap: 24px;
  align-items: center; font-size: {round(L['body'] * .62)}px; color: #b7c0d6; letter-spacing: .02em; z-index: 2; }}
.top .lesson {{ flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.top .step {{ background: rgba(255,255,255,.10); border-radius: 999px; padding: 6px 18px; color: #fff; }}
.top .dot {{ width: 14px; height: 14px; border-radius: 50%; background: var(--accent); flex: none; }}
.content {{ position: absolute; left: {L['pad']}px; right: {L['pad']}px; top: {L['top'] + round(L['body'] * 1.6)}px;
  bottom: {cap}px; display: flex; flex-direction: column; justify-content: {'flex-end' if cover else 'center'};
  gap: calc(var(--s) * {round(L['body'] * .75)}px); z-index: 2; }}
h1 {{ font-family: {head_stack}; font-weight: 400; margin: 0; color: #fff; line-height: 1.3;
  font-size: calc(var(--s) * {L['cover_h1'] if cover else L['h1']}px); text-shadow: {'0 4px 24px rgba(0,0,0,.6)' if cover else 'none'}; }}
h1::after {{ content: ""; display: block; width: calc(var(--s) * 120px); height: calc(var(--s) * 9px);
  background: var(--accent); border-radius: 6px; margin-top: calc(var(--s) * 18px); }}
p {{ margin: 0; font-size: calc(var(--s) * {L['body']}px); line-height: 1.6; color: #dfe5f2; }}
ul {{ margin: 0; padding: 0; list-style: none; display: grid; gap: calc(var(--s) * {round(L['body'] * .45)}px); }}
li {{ position: relative; padding-left: calc(var(--s) * {round(L['body'] * 1.2)}px); font-size: calc(var(--s) * {L['body']}px);
  line-height: 1.55; }}
li::before {{ content: ""; position: absolute; left: calc(var(--s) * 6px); top: .55em; width: calc(var(--s) * {round(L['body'] * .42)}px);
  height: calc(var(--s) * {round(L['body'] * .42)}px); border-radius: 50%; background: var(--accent); }}
b {{ color: var(--accent); font-weight: 700; }}
code {{ font-family: {mono_stack}; font-size: .86em; background: rgba(255,255,255,.12); border-radius: 10px; padding: .08em .35em; }}
.code, .out {{ border-radius: calc(var(--s) * 26px); overflow: hidden; box-shadow: 0 18px 50px rgba(0,0,0,.45);
  border: 1px solid rgba(255,255,255,.08); flex: none; }}
.code {{ background: #0d1117; }}
.out {{ background: #05070a; }}
.bar {{ display: flex; align-items: center; gap: calc(var(--s) * 12px); padding: calc(var(--s) * 18px) calc(var(--s) * 28px) 0; }}
.bar i {{ width: calc(var(--s) * 18px); height: calc(var(--s) * 18px); border-radius: 50%; background: #ff5f57; }}
.bar i:nth-child(2) {{ background: #febc2e; }}
.bar i:nth-child(3) {{ background: #28c840; }}
.bar .label {{ margin-left: auto; font-size: calc(var(--s) * {round(L['code'] * .62)}px); color: #8b95a7;
  font-family: {mono_stack}; text-transform: lowercase; }}
.out .bar .label {{ margin-left: 0; color: #7ee787; text-transform: none; font-family: {body_stack}; }}
pre {{ margin: 0; padding: calc(var(--s) * 20px) calc(var(--s) * 32px) calc(var(--s) * 28px); font-family: {mono_stack};
  font-size: calc(var(--s) * {L['code']}px); line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; color: #e6edf3; }}
.out pre {{ color: #b8f5c0; }}
.ln, .ol {{ display: block; position: relative; min-height: 1.5em; border-radius: 6px; }}
.ln.typing::after {{ content: ""; position: absolute; left: var(--cx); top: .18em; width: .58em; height: 1.15em;
  background: var(--accent); border-radius: 2px; }}
.ln .no {{ display: inline-block; width: 2ch; margin-right: 1.2ch; text-align: right; color: #4d5870; }}
.content {{ transform-origin: 50% 50%; }}
.quiz {{ display: grid; gap: calc(var(--s) * 22px); flex: none; --r: 1; }}
.quiz .qq {{ font-weight: 700; color: #fff; }}
.quiz .opts {{ display: grid; gap: calc(var(--s) * 18px); grid-template-columns: {'1fr' if aspect == '9:16' else '1fr 1fr'}; }}
.quiz .opt {{ position: relative; border-radius: calc(var(--s) * 24px); }}
.quiz .opt::before {{ content: ""; position: absolute; inset: 0; border-radius: inherit; background: #16a34a; opacity: 0; }}
.quiz .opt.ok::before {{ opacity: var(--r); box-shadow: 0 0 calc(var(--s) * 40px) rgba(34,197,94,calc(.55 * var(--r))); }}
.quiz .in {{ position: relative; display: flex; align-items: center; gap: calc(var(--s) * 22px);
  padding: calc(var(--s) * 20px) calc(var(--s) * 26px); border-radius: inherit; background: rgba(255,255,255,.07);
  border: calc(var(--s) * 3px) solid rgba(255,255,255,.14); font-size: calc(var(--s) * {L['body']}px); line-height: 1.35; }}
.quiz .opt.ok .in {{ border-color: color-mix(in srgb, rgb(134,239,172) calc(var(--r) * 100%), rgba(255,255,255,.14));
  background: rgba(255,255,255,calc(.07 * (1 - var(--r)))); }}
.quiz .opt:not(.ok) .in {{ opacity: calc(1 - .62 * var(--r)); }}
.quiz .key {{ flex: none; width: calc(var(--s) * {round(L['body'] * 1.45)}px); height: calc(var(--s) * {round(L['body'] * 1.45)}px);
  border-radius: 50%; display: grid; place-items: center; background: var(--accent); color: #111; font-weight: 800;
  font-size: calc(var(--s) * {round(L['body'] * .8)}px); }}
.quiz .opt.ok .key {{ background: color-mix(in srgb, #fff calc(var(--r) * 100%), var(--accent));
  color: color-mix(in srgb, #15803d calc(var(--r) * 100%), #111); }}
.quiz .otx {{ flex: 1; }}
.quiz .mark {{ flex: none; font-weight: 900; color: #fff; font-size: calc(var(--s) * {round(L['body'] * 1.1)}px);
  opacity: var(--r); transform: scale(calc(.4 + .6 * var(--r))); }}
.quiz .timer {{ position: relative; justify-self: center; width: calc(var(--s) * 132px); height: calc(var(--s) * 132px); opacity: 0; }}
.quiz .timer svg {{ width: 100%; height: 100%; transform: rotate(-90deg); }}
.quiz .timer circle {{ fill: rgba(9,13,28,.85); stroke-width: 9; }}
.quiz .timer .bg {{ stroke: rgba(255,255,255,.15); }}
.quiz .timer .fg {{ fill: none; stroke: var(--accent); stroke-linecap: round; stroke-dasharray: 100 100; stroke-dashoffset: 0; }}
.quiz .timer .num {{ position: absolute; inset: 0; display: grid; place-items: center; color: #fff; font-weight: 800;
  font-family: "SF Pro Rounded", "Helvetica Neue", "Segoe UI", Arial, sans-serif; font-size: calc(var(--s) * 58px); }}
.dia {{ position: relative; width: 100%; flex: none; }}
.dia .wires {{ position: absolute; left: 0; top: 0; overflow: visible; }}
.dia .packet {{ filter: drop-shadow(0 0 10px var(--accent)) drop-shadow(0 0 4px #fff); }}
.node {{ position: absolute; transform: translate(-50%,-50%); width: calc(var(--s) * var(--nw)); height: calc(var(--s) * var(--nh));
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: calc(var(--s) * 6px);
  padding: calc(var(--s) * 12px) calc(var(--s) * 14px); text-align: center; border-radius: calc(var(--s) * 30px);
  background: linear-gradient(180deg, rgba(40,52,92,.96), rgba(22,30,58,.96)); border: calc(var(--s) * 3px) solid rgb(92,104,136);
  box-shadow: 0 14px 40px rgba(0,0,0,.35); z-index: 1; }}
.node .ic {{ font-size: calc(var(--s) * 60px); line-height: 1.1; font-family: "Apple Color Emoji", "Segoe UI Emoji", sans-serif; }}
.node .ico {{ width: calc(var(--s) * 70px); height: calc(var(--s) * 70px); flex: none; }}
.node .ico.mono {{ background: rgb(226,232,248); -webkit-mask-size: contain; mask-size: contain; -webkit-mask-repeat: no-repeat;
  mask-repeat: no-repeat; -webkit-mask-position: center; mask-position: center; }}
.node .ico:not(.mono) {{ width: calc(var(--s) * 72px); height: calc(var(--s) * 72px); padding: calc(var(--s) * 10px);
  background: #fff; border-radius: calc(var(--s) * 20px); box-shadow: 0 6px 18px rgba(0,0,0,.3); }}
.node .ico img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
.logos {{ display: flex; flex-wrap: wrap; gap: calc(var(--s) * var(--lg) * .16); flex: none; }}
.logos .lg {{ width: calc(var(--s) * var(--lg)); height: calc(var(--s) * var(--lg)); display: block; flex: none;
  background: #fff; border-radius: calc(var(--s) * var(--lg) * .24);
  box-shadow: 0 18px 44px rgba(0,0,0,.45), 0 0 0 calc(var(--s) * 4px) rgba(255,255,255,.18); }}
.logos .lg img, .logos .lg .m {{ display: block; width: 68%; height: 68%; margin: 16%; object-fit: contain; }}
.logos .lg .m {{ background: #18213f; -webkit-mask-size: contain; mask-size: contain;
  -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat; -webkit-mask-position: center; mask-position: center; }}
.node .lb {{ font-size: calc(var(--s) * var(--nf)); line-height: 1.3; color: #fff; display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }}
.node.noicon .lb {{ font-size: calc(var(--s) * var(--nf) * 1.1); -webkit-line-clamp: 3; }}
.elabel {{ position: absolute; left: 0; top: 0; transform: translate(-50%,-50%); width: max-content; z-index: 2;
  font-size: calc(var(--s) * var(--lf)); line-height: 1.35; color: #fff; text-align: center;
  background: rgba(9,13,28,.92); border: calc(var(--s) * 2px) solid rgba(255,255,255,.16);
  border-radius: calc(var(--s) * 18px); padding: calc(var(--s) * 6px) calc(var(--s) * 16px); }}
</style></head><body>
{bg}
<div class="top"><span class="dot"></span><span class="lesson">{html.escape(lesson)}</span><span class="step">{html.escape(step)}</span></div>
<div class="content" id="c">{before}{f'<h1{_rv(data["heading_step"])}>{heading}</h1>' if heading else ''}{body}</div>
<script>const QUIZ_CD = {QUIZ_COUNTDOWN - 0.2:.2f};{MOTION_JS}</script>
{play}
</body></html>"""


def slide_key(*parts) -> str:
    return hashlib.sha1(repr((TEMPLATE_VERSION,) + parts).encode()).hexdigest()[:10]


def _browser_cmd(browser: str, profile: Path, size: tuple[int, int]) -> list[str]:
    w, h = size
    return [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
            "--allow-file-access-from-files", f"--user-data-dir={profile}", f"--window-size={w},{h}",
            "--force-device-scale-factor=1"]


def render_png(html_text: str, size: tuple[int, int], out: Path, browser: Optional[str] = None,
               transparent: bool = False) -> Path:
    browser = browser or find_browser()
    if not browser:
        raise RuntimeError("Drawing tutorial slides needs Google Chrome (or Brave, Edge or Chromium) installed.")
    # ignore_cleanup_errors: on Windows the browser can hold its profile files for a moment after exiting
    with _render_slots, tempfile.TemporaryDirectory(prefix="slide-", ignore_cleanup_errors=True) as td:
        page = Path(td) / "slide.html"
        page.write_text(html_text, encoding="utf-8")
        shot = Path(td) / "shot.png"
        cmd = _browser_cmd(browser, Path(td) / "profile", size) + [
            "--virtual-time-budget=4000", f"--screenshot={shot}", page.as_uri()]
        if transparent:  # e.g. SVG logos: keep the background see-through
            cmd.insert(1, "--default-background-color=00000000")
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        if not shot.exists() or shot.stat().st_size == 0:
            raise RuntimeError(f"The browser could not draw the slide: {(proc.stderr or '').strip()[-300:]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.png")
        shutil.move(str(shot), tmp)
        tmp.replace(out)
    return out


# --------------------------------------------------------------------------- #
# Animated slides: drive the page frame by frame over the DevTools protocol
# --------------------------------------------------------------------------- #
class _Devtools:
    def __init__(self, ws) -> None:
        self.ws, self.n, self.events = ws, 0, []

    def _recv(self, timeout: float) -> dict:
        return json.loads(self.ws.recv(timeout=timeout))

    def call(self, method: str, params: Optional[dict] = None, session: Optional[str] = None,
             timeout: float = 60) -> dict:
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        while True:
            r = self._recv(timeout)
            if r.get("id") == self.n:
                if "error" in r:
                    raise RuntimeError(f"{method}: {r['error'].get('message')}")
                return r.get("result", {})
            if "method" in r:
                self.events.append(r)

    def wait_event(self, name: str, timeout: float) -> dict:
        deadline = time.time() + timeout
        while True:
            for k, ev in enumerate(self.events):
                if ev.get("method") == name:
                    del self.events[k]
                    return ev
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"The browser did not finish loading the slide ({name}).")
            r = self._recv(left)
            if "method" in r:
                self.events.append(r)


def render_motion(html_text: str, size: tuple[int, int], out: Path, times: list[float], dur: float, fps: int,
                  browser: Optional[str] = None, cancel: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable[[float], None]] = None) -> Path:
    """Film the slide's animation: every frame is drawn by the page's __frame(t) and captured, then encoded
    to an MP4 of `dur` seconds. Frames where nothing moves are reused instead of captured again.
    The sound-effect cues of the animation are saved next to it (same name, .json)."""
    from websockets.sync.client import connect

    browser = browser or find_browser()
    if not browser:
        raise RuntimeError("Drawing tutorial slides needs Google Chrome (or Brave, Edge or Chromium) installed.")
    w, h = size
    n_frames = max(1, int(round(dur * fps)) + 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_suffix(".tmp.mp4")
    with _render_slots, tempfile.TemporaryDirectory(prefix="motion-", ignore_cleanup_errors=True) as td:
        page = Path(td) / "slide.html"
        page.write_text(html_text, encoding="utf-8")
        profile = Path(td) / "profile"
        proc = subprocess.Popen(_browser_cmd(browser, profile, size) + ["--remote-debugging-port=0", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ff = None
        try:
            port_file, deadline = profile / "DevToolsActivePort", time.time() + 30
            while not (port_file.exists() and len(port_file.read_text().split()) >= 2):
                if proc.poll() is not None or time.time() > deadline:
                    raise RuntimeError("The browser did not start for the slide animation.")
                time.sleep(0.05)
            port, path = port_file.read_text().split()[:2]
            with connect(f"ws://127.0.0.1:{port}{path}", max_size=None, open_timeout=15) as ws:
                dt = _Devtools(ws)
                target = dt.call("Target.createTarget", {"url": "about:blank"})["targetId"]
                sid = dt.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
                dt.call("Emulation.setDeviceMetricsOverride",
                        {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False}, sid)
                dt.call("Page.enable", {}, sid)
                dt.call("Page.navigate", {"url": page.as_uri()}, sid)
                dt.wait_event("Page.loadEventFired", 45)
                dt.events.clear()
                setup = (f"window.__ready.then(() => {{ __setup({json.dumps([round(x, 3) for x in times])}, {dur:.3f});"
                         " return true; })")
                r = dt.call("Runtime.evaluate", {"expression": setup, "awaitPromise": True, "returnByValue": True}, sid)
                if r.get("exceptionDetails"):
                    raise RuntimeError(f"Slide animation script failed: {r['exceptionDetails'].get('text')}")
                events = dt.call("Runtime.evaluate", {"expression": "JSON.stringify(__sfx())", "returnByValue": True},
                                 sid).get("result", {}).get("value") or "[]"
                ff = subprocess.Popen(
                    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps),
                     "-c:v", "mjpeg", "-i", "-", "-vf", f"scale={w}:{h},format=yuv420p", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "14", "-r", str(fps), "-movflags", "+faststart", str(tmp_out)],
                    stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                last = None
                for f in range(n_frames):
                    if cancel and f % 15 == 0 and cancel():
                        raise RuntimeError("Cancelled")
                    busy = dt.call("Runtime.evaluate", {"expression": f"__frame({f / fps:.4f})", "returnByValue": True},
                                   sid).get("result", {}).get("value")
                    if busy or last is None:
                        shot = dt.call("Page.captureScreenshot",
                                       {"format": "jpeg", "quality": 94, "optimizeForSpeed": True}, sid)
                        last = base64.b64decode(shot["data"])
                    ff.stdin.write(last)
                    if progress and f % 30 == 0:
                        progress(f / n_frames)
                ff.stdin.close()
                err = ff.stderr.read().decode(errors="replace")
                if ff.wait() != 0 or not tmp_out.exists():
                    raise RuntimeError(f"Encoding the slide animation failed: {err.strip()[-300:]}")
                out.with_suffix(".json").write_text(events, encoding="utf-8")  # sound effects (see sfx.py)
                tmp_out.replace(out)
        finally:
            if ff and ff.poll() is None:
                ff.kill()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            tmp_out.unlink(missing_ok=True)
    return out
