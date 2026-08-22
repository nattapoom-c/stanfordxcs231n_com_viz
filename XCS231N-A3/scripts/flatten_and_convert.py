#!/usr/bin/env python3
"""
flatten_and_convert.py — Build HTML content for a single XCS221 assignment.

Boilerplate for single-assignment repositories where the repo root IS the
assignment directory:
  - tex/main.tex    LaTeX source
  - points.json     (optional) question → points mapping
  - meta.json       (optional) due date and build flags
  - A#.bbl          (optional) pre-generated bibliography
  - A#.pdf          (optional) assignment PDF

Output: assignments/A#/index.html

Usage: python3 scripts/flatten_and_convert.py
"""

import json
import re
import shutil
import sys
import subprocess
import tempfile
from pathlib import Path
from html.parser import HTMLParser

REPO_ROOT = Path(__file__).parent.parent.resolve()
ASSIGNMENT_DIR = REPO_ROOT  # overridden in main() for CF mode
TEX_DIR = REPO_ROOT / "tex"
DOCS = REPO_ROOT / "site"

# Regex for 🐍...🐍 blocks in pytex files
PYTEX_RE = re.compile(r"(?s)🐍(.*?)🐍")

# Regex for \input{} and \include{} in LaTeX
INPUT_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")


# ─── Assignment detection ─────────────────────────────────────────────────────

def detect_assignment() -> tuple:
    """Read assignment ID (e.g. 'A1') and name from tex/main.tex.
    Falls back to repo directory name (e.g. 'XCS221-A1' → 'A1').
    Returns (assignment_id, assignment_name).
    """
    main_tex = TEX_DIR / "main.tex"
    if main_tex.exists():
        content = main_tex.read_text(encoding="utf-8", errors="replace")
        num_m = re.search(r'\\def\\assignmentnum\{([^}]+)\}', content)
        name_m = re.search(r'\\def\\assignmentname\{([^}]+)\}', content)
        num = num_m.group(1).strip() if num_m else None
        name = name_m.group(1).strip() if name_m else "Assignment"
        if num:
            return f"A{num}", name

    # Fallback: infer from repo directory name (e.g. XCS221-A3 → A3, XCS236-PS1 → PS1)
    m = re.search(r'(PS\d+|[Aa]\d+)', ASSIGNMENT_DIR.name)
    if m:
        token = m.group(1)
        # Normalize: PS1 stays PS1; A3 / a3 → A3
        if token.upper().startswith("PS"):
            return token.upper(), "Assignment"
        num = re.search(r'\d+', token).group()
        return f"A{num}", "Assignment"
    return "A1", "Assignment"


# ─── pytex processing ──────────────────────────────────────────────────────────

def process_pytex_file(pytex_path: Path, tex_dir: Path) -> Path:
    """
    Process a single .pytex file → .tex file.
    Returns the generated .tex path, or None on failure.
    """
    content = pytex_path.read_text(encoding="utf-8", errors="replace")

    tmp_pytex = tex_dir / f"_tmp_{pytex_path.stem}.pytex"
    out_tex = pytex_path.with_suffix(".tex")
    rel_tmp = tmp_pytex.relative_to(tex_dir)
    rel_out = out_tex.relative_to(tex_dir)

    try:
        tmp_pytex.write_text(content, encoding="utf-8")
        py2tex = tex_dir / "py2tex.py"
        result = subprocess.run(
            [sys.executable, str(py2tex), str(rel_tmp), str(rel_out)],
            cwd=tex_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return out_tex
        else:
            print(f"    WARNING: py2tex failed for {pytex_path.name}: {result.stderr[:300]}")
            return None
    except Exception as e:
        print(f"    WARNING: py2tex error for {pytex_path.name}: {e}")
        return None
    finally:
        if tmp_pytex.exists():
            tmp_pytex.unlink()


def process_all_pytex(tex_dir: Path) -> list:
    """Process all .pytex files in tex_dir tree. Returns list of generated .tex paths."""
    generated = []

    # Collect all pytex files, py-macros first (others may use \points{})
    all_pytex = sorted(tex_dir.rglob("*.pytex"))
    priority = sorted(all_pytex, key=lambda p: (
        0 if p.name == "py-macros.pytex" else
        1 if p.name == "00-outro.pytex" else
        2
    ))

    for pytex in priority:
        out = process_pytex_file(pytex, tex_dir)
        if out:
            generated.append(out)
            rel = pytex.relative_to(tex_dir)
            print(f"    processed {rel} → {out.name}")

    return generated


# ─── LaTeX flattener ───────────────────────────────────────────────────────────

def flatten_tex(file_path: Path, tex_dir: Path, visited: set = None) -> str:
    """
    Recursively inline \\input{} and \\include{} into a single string.
    """
    if visited is None:
        visited = set()

    resolved = None
    candidates = [file_path, file_path.with_suffix(".tex")]
    if not file_path.is_absolute():
        candidates += [tex_dir / file_path, (tex_dir / file_path).with_suffix(".tex")]
    for candidate in candidates:
        if candidate.exists():
            resolved = candidate
            break

    if resolved is None:
        return f"% [flatten: could not find {file_path.name}]\n"

    canon = resolved.resolve()
    if canon in visited:
        return f"% [flatten: circular {file_path.name}]\n"
    visited.add(canon)

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"% [flatten: read error {e}]\n"

    def replace_input(match):
        ref = match.group(1).strip()
        ref_path = resolved.parent / ref
        if not ref_path.exists() and not ref_path.with_suffix(".tex").exists():
            alt = tex_dir / ref
            if alt.exists() or alt.with_suffix(".tex").exists():
                ref_path = alt
        # Skip 00-instructions — submission instructions, not assignment content.
        if "00-instructions" in str(ref_path) and "00-instructions" not in str(resolved):
            return "% [skipped: submission instructions]\n"
        # Skip 00-outro — student answer template from submission.tex, not assignment content.
        if Path(ref).stem == "00-outro":
            return "% [skipped: student submission template]\n"
        # Skip the "Submitting your work" section (dir number varies: 03-submitting, 06-submitting, ...).
        if "-submitting" in ref:
            return "% [skipped: submitting your work section]\n"
        return flatten_tex(ref_path, tex_dir, visited)

    return INPUT_RE.sub(replace_input, content)


# ─── \points{} substitution ────────────────────────────────────────────────────

POINTS_RE = re.compile(r"\\points\{([^}]+)\}")


def build_points_map() -> dict:
    """
    Read points.json from repo root and return a mapping from
    question_id → {pts, is_written, is_extra_credit}.
    """
    points_file = ASSIGNMENT_DIR / "points.json"
    if not points_file.exists():
        return {}
    try:
        data = json.loads(points_file.read_text(encoding="utf-8"))
    except Exception:
        return {}

    totals: dict = {}
    for entry in data.values():
        qid = entry.get("question_id", "")
        pts = entry.get("points", 0)
        is_written = entry.get("is_written", False)
        is_extra_credit = entry.get("is_extra_credit", False)
        if qid:
            if qid not in totals:
                totals[qid] = {"pts": 0, "is_written": is_written, "is_extra_credit": is_extra_credit}
            totals[qid]["pts"] += pts
    return totals


def substitute_points(content: str, points_map: dict) -> str:
    """Replace \\points{X} with \\textbf{(Type — N pt(s))} using points_map."""
    def replacer(m):
        qid = m.group(1).strip()
        if qid in points_map:
            info = points_map[qid]
            pts = info["pts"]
            is_written = info["is_written"]
            is_extra_credit = info["is_extra_credit"]
            if pts == int(pts):
                pts_str = f"{int(pts)} pt" + ("s" if pts != 1 else "")
            else:
                pts_str = f"{pts:.1f} pts"
            if is_extra_credit:
                label = "EC, Written" if is_written else "EC, Coding"
            elif is_written:
                label = "Written"
            else:
                label = "Coding"
            return r"\textbf{(" + label + " \u2014 " + pts_str + r")}"
        return m.group(0)

    return POINTS_RE.sub(replacer, content)


# ─── HTML word count ───────────────────────────────────────────────────────────

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self._skip_tags = {"script", "style", "head"}
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._skip_tags and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            self.text_parts.append(data)


def count_words(html_path: Path) -> int:
    parser = TextExtractor()
    parser.feed(html_path.read_text(encoding="utf-8", errors="replace"))
    text = " ".join(parser.text_parts)
    return len(text.split())


# ─── \expect{} replacement ────────────────────────────────────────────────────

def extract_braced_arg(s, start):
    """Extract content of {}-delimited argument starting at index start."""
    assert s[start] == "{"
    depth = 0
    i = start
    while i < len(s):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start+1:i], i+1
        i += 1
    return s[start+1:], len(s)


def replace_expect(content):
    result = []
    i = 0
    needle = r"\expect{"
    while i < len(content):
        idx = content.find(needle, i)
        if idx == -1:
            result.append(content[i:])
            break
        result.append(content[i:idx])
        arg, end = extract_braced_arg(content, idx + len(r"\expect"))
        result.append(r"\textbf{What we expect:} " + arg)
        i = end
    return "".join(result)


# ─── Arrow symbol substitution ────────────────────────────────────────────────

def substitute_text_commands(content):
    """Replace bare arrow commands outside math with Unicode equivalents."""
    arrows = [
        (r"\rightarrow", "\u2192"),
        (r"\to",         "\u2192"),
        (r"\leftarrow",  "\u2190"),
        (r"\Rightarrow", "\u21d2"),
        (r"\Leftarrow",  "\u21d0"),
        (r"\leftrightarrow", "\u2194"),
    ]
    MATH_RE = re.compile(
        r"(\$\$[\s\S]*?\$\$"
        r"|\$[^\$\n]+?\$"
        r"|\\begin\{(?:equation|align|eqnarray)\*?\}[\s\S]*?\\end\{(?:equation|align|eqnarray)\*?\}"
        r"|\\[\[\]])"
    )

    def replace_in_text(text):
        for latex_cmd, unicode_char in arrows:
            text = re.sub(
                re.escape(latex_cmd) + r"(?=[^a-zA-Z])",
                unicode_char,
                text
            )
        return text

    parts = MATH_RE.split(content)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            result.append(replace_in_text(part))
        else:
            result.append(part)
    return "".join(result)


# ─── Bibliography inlining ────────────────────────────────────────────────────

def inline_bibliography(content, assignment):
    """Inline pre-generated .bbl file (lives at repo root) into the LaTeX source."""
    bbl_path = ASSIGNMENT_DIR / f"{assignment}.bbl"
    if not bbl_path.exists():
        return content
    bbl_content = bbl_path.read_text(encoding="utf-8", errors="replace")
    content = re.sub(
        r"\\bibliographystyle\{[^}]*\}\s*\\bibliography\{[^}]*\}",
        lambda _: bbl_content,
        content
    )
    content = re.sub(
        r"\\bibliography\{[^}]*\}",
        lambda _: bbl_content,
        content
    )
    return content


# ─── Citation fix ──────────────────────────────────────────────────────────────

def fix_citations(html):
    """Find bibitem entries and replace empty citation spans with numbered links."""
    cite_keys_in_order = re.findall(r'data-cites="([^"]+)"', html)
    key_to_num = {}
    for key in cite_keys_in_order:
        for k in key.split():
            if k not in key_to_num:
                key_to_num[k] = len(key_to_num) + 1

    if not key_to_num:
        return html

    bib_match = re.search(r'(<div[^>]*class="[^"]*thebibliography[^"]*"[^>]*>)(.*?)(</div>)', html, re.DOTALL)
    if bib_match:
        bib_inner = bib_match.group(2)
        entries = list(key_to_num.items())

        raw_positions = [m.start() for m in re.finditer(r'<p>', bib_inner)]
        p_positions = []
        for pos in raw_positions:
            after = bib_inner[pos:]
            if re.match(r'<p>\s*<span>\s*\d+\s*</span>\s*</p>', after):
                continue
            p_positions.append(pos)
        offset = 0
        new_inner = bib_inner
        for i, (key, num) in enumerate(entries):
            if i < len(p_positions):
                pos = p_positions[i] + offset
                replacement = f'<p id="ref-{key}">'
                new_inner = new_inner[:pos] + replacement + new_inner[pos+3:]
                offset += len(replacement) - 3

        html = html[:bib_match.start(2)] + new_inner + html[bib_match.end(2):]

    def replace_cite(m):
        keys = m.group(1).split()
        nums = [key_to_num.get(k, '?') for k in keys]
        links = ', '.join(f'<a href="#ref-{k}">[{n}]</a>' for k, n in zip(keys, nums))
        return f'<sup class="citation">{links}</sup>'

    html = re.sub(
        r'<span[^>]*class="citation"[^>]*data-cites="([^"]+)"[^>]*>\s*</span>',
        replace_cite,
        html
    )
    html = re.sub(
        r'<span[^>]*data-cites="([^"]+)"[^>]*/>',
        replace_cite,
        html
    )
    return html


def fix_bibliography_entries(html):
    """Renumber all bibliography <p> entries sequentially with [N] labels."""
    def renumber_bib(m):
        bib_div = m.group(0)
        bib_div = re.sub(r'<p[^>]*>\s*(<span>[^<]*</span>\s*)?</p>\s*', '', bib_div)
        counter = [0]

        def number_entry(pm):
            counter[0] += 1
            n = counter[0]
            p_tag = pm.group(1)
            content = pm.group(2)
            p_id_match = re.search(r'id="([^"]+)"', p_tag)
            anchor_id = p_id_match.group(1) if p_id_match else f'ref-{n}'
            content = re.sub(r'^\s*<span>\d+</span>\s*', '', content)
            content = re.sub(r'^\s*<strong>\[\d+\]</strong>\s*', '', content)
            return f'<p id="{anchor_id}"><strong>[{n}]</strong> {content.strip()}</p>'

        bib_div = re.sub(r'(<p[^>]*>)(.*?)</p>', number_entry, bib_div, flags=re.DOTALL)
        return bib_div

    return re.sub(
        r'<div[^>]*class="[^"]*thebibliography[^"]*"[^>]*>.*?</div>',
        renumber_bib,
        html,
        flags=re.DOTALL
    )


def strip_duplicate_header(html):
    """Remove pandoc-generated title/due date that duplicates the injected header."""
    body_idx = html.find('<body>')
    ol_idx = html.find('<ol')
    if body_idx < 0 or ol_idx < 0:
        return html

    pre_content = html[body_idx:ol_idx]
    pre_content = re.sub(r'<p[^>]*>.*?XCS221 Assignment.*?</p>\s*', '', pre_content, flags=re.DOTALL)
    pre_content = re.sub(r'<hr\s*/?>\s*<p[^>]*>.*?<strong>Due.*?</p>\s*', '', pre_content, flags=re.DOTALL)
    pre_content = re.sub(r'<hr\s*/?>\s*', '', pre_content)

    return html[:body_idx] + pre_content + html[ol_idx:]


def replace_pipe_inline(content):
    """Replace |...| inline code shorthand with \\texttt{...}."""
    result = []
    i = 0

    DELETE_MARKER = "\\lstDeleteShortInline|"
    RESTORE_MARKER = "\\lstMakeShortInline|"

    while i < len(content):
        del_idx = content.find(DELETE_MARKER, i)
        if del_idx == -1:
            result.append(_replace_pipes_in_segment(content[i:]))
            break

        result.append(_replace_pipes_in_segment(content[i:del_idx]))

        restore_idx = content.find(RESTORE_MARKER, del_idx + len(DELETE_MARKER))
        if restore_idx == -1:
            result.append(content[del_idx:])
            break

        result.append(content[del_idx:restore_idx + len(RESTORE_MARKER)])
        i = restore_idx + len(RESTORE_MARKER)

    return ''.join(result)


def _replace_pipes_in_segment(segment):
    """Replace |...| pairs with \\texttt{...} on a per-line basis."""
    lines = segment.split('\n')
    result = []
    for line in lines:
        if re.search(r'\\begin\{tabular\}', line):
            result.append(line)
        else:
            def replace_pipe_match(m):
                inner = m.group(1)
                inner = inner.replace('$', r'\$')
                return r'\texttt{' + inner + '}'
            # Use negative lookbehind to avoid matching \| (LaTeX norm bars in math mode)
            result.append(re.sub(r'(?<!\\)\|([^|\n]+?)(?<!\\)\|', replace_pipe_match, line))
    return '\n'.join(result)


MATH_SYMBOL_MAP = {
    r'\leq': '≤', r'\geq': '≥', r'\neq': '≠', r'\approx': '≈',
    r'\equiv': '≡', r'\sim': '~', r'\propto': '∝',
    r'\times': '×', r'\cdot': '·', r'\pm': '±', r'\mp': '∓',
    r'\div': '÷', r'\circ': '∘', r'\oplus': '⊕',
    r'\leftarrow': '←', r'\rightarrow': '→', r'\Leftarrow': '⇐',
    r'\Rightarrow': '⇒', r'\leftrightarrow': '↔', r'\to': '→',
    r'\in': '∈', r'\notin': '∉', r'\subset': '⊂', r'\subseteq': '⊆',
    r'\cup': '∪', r'\cap': '∩', r'\emptyset': '∅',
    r'\forall': '∀', r'\exists': '∃', r'\neg': '¬', r'\land': '∧',
    r'\lor': '∨',
    r'\infty': '∞', r'\partial': '∂', r'\nabla': '∇',
    r'\sum': 'Σ', r'\prod': 'Π', r'\int': '∫',
    r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\delta': 'δ',
    r'\epsilon': 'ε', r'\eta': 'η', r'\theta': 'θ', r'\lambda': 'λ',
    r'\mu': 'μ', r'\nu': 'ν', r'\pi': 'π', r'\rho': 'ρ',
    r'\sigma': 'σ', r'\tau': 'τ', r'\phi': 'φ', r'\psi': 'ψ',
    r'\omega': 'ω', r'\xi': 'ξ', r'\zeta': 'ζ',
    r'\Gamma': 'Γ', r'\Delta': 'Δ', r'\Theta': 'Θ', r'\Lambda': 'Λ',
    r'\Pi': 'Π', r'\Sigma': 'Σ', r'\Phi': 'Φ', r'\Psi': 'Ψ',
    r'\Omega': 'Ω',
}


def latex_to_readable(content):
    """Convert structural LaTeX expressions to readable plain text."""
    content = re.sub(r'^\s*\\\(|\\\)\s*$', '', content.strip())
    content = re.sub(r'^\s*\\\[|\\\]\s*$', '', content.strip())
    content = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', r'(\1)/(\2)', content)
    content = re.sub(r'\\sqrt\{([^{}]+)\}', r'sqrt(\1)', content)
    content = re.sub(r'\\math(?:bf|it|rm|cal|bb)\{([^{}]+)\}', r'\1', content)
    content = re.sub(r'\\text(?:bf|it|rm)?\{([^{}]+)\}', r'\1', content)
    content = re.sub(r'\\(?:hat|bar|tilde|vec|dot|ddot)\{([^{}]+)\}', r'\1', content)
    content = re.sub(r'\^\{([^{}]+)\}', r'^\1', content)
    content = re.sub(r'_\{([^{}]+)\}', r'_\1', content)
    content = re.sub(r'\\([A-Za-z]+)', r'\1', content)
    content = content.replace('{', '').replace('}', '')
    content = re.sub(r'\s+', ' ', content).strip()
    return content


def html_to_plaintext_for_textarea(inner_html):
    """Convert inner blockquote HTML to plain text preserving list structure."""
    text = inner_html

    def convert_math_span(m):
        content = m.group(1).strip()
        for cmd, sym in MATH_SYMBOL_MAP.items():
            content = content.replace(cmd, sym)
        return latex_to_readable(content)

    text = re.sub(
        r'<span[^>]*class="math inline"[^>]*>(.*?)</span>',
        convert_math_span, text, flags=re.DOTALL
    )
    text = re.sub(
        r'<span[^>]*class="math display"[^>]*>(.*?)</span>',
        lambda m: '\n' + convert_math_span(m) + '\n', text, flags=re.DOTALL
    )

    def replace_nested_ol(m):
        items = re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), re.DOTALL)
        result = []
        for i, item in enumerate(items):
            letter = chr(ord('a') + i)
            item_text = re.sub(r'<[^>]+>', '', item).strip()
            result.append(f"   {letter}. {item_text}")
        return '\n' + '\n'.join(result) + '\n'

    text = re.sub(r'<ol[^>]*>(.*?)</ol>', replace_nested_ol, text, flags=re.DOTALL)

    def replace_ul(m):
        items = re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), re.DOTALL)
        result = []
        for item in items:
            item_text = re.sub(r'<[^>]+>', '', item).strip()
            result.append(f"  \u2022 {item_text}")
        return '\n' + '\n'.join(result) + '\n'

    text = re.sub(r'<ul[^>]*>(.*?)</ul>', replace_ul, text, flags=re.DOTALL)

    def replace_ol(m):
        items = re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), re.DOTALL)
        result = []
        for i, item in enumerate(items, 1):
            item_text = re.sub(r'<[^>]+>', '', item).strip()
            result.append(f"{i}. {item_text}")
        return '\n' + '\n'.join(result) + '\n'

    text = re.sub(r'<ol[^>]*>(.*?)</ol>', replace_ol, text, flags=re.DOTALL)
    text = re.sub(
        r'<p[^>]*>(.*?)</p>',
        lambda m: re.sub(r'<[^>]+>', '', m.group(1)).strip() + '\n\n',
        text, flags=re.DOTALL
    )
    text = re.sub(r'<[^>]+>', '', text)

    import html as html_module
    text = html_module.unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


def fix_llmprompt_html(html):
    """Wrap LLMPROMPTSTART...LLMPROMPTEND markers in a textarea copy widget."""
    def make_textarea_widget(inner_html):
        plain_text = html_to_plaintext_for_textarea(inner_html)
        import html as html_module
        escaped = html_module.escape(plain_text)
        lines = plain_text.count('\n') + 2
        rows = max(lines, 8)
        return (
            '<div class="llmprompt-box">\n'
            '<div class="llmprompt-header">\U0001f4cb Prompt Template '
            '<button class="copy-btn" onclick="'
            "var t=this.closest('.llmprompt-box').querySelector('textarea');"
            "t.select();document.execCommand('copy');"
            "this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)"
            '">Copy</button></div>\n'
            f'<textarea class="llmprompt-text" readonly rows="{rows}">{escaped}</textarea>\n'
            '</div>'
        )

    def wrap1(m):
        return make_textarea_widget(m.group(1))

    html = re.sub(
        r'<p[^>]*>\s*LLMPROMPTSTART\s*</p>\s*(.*?)\s*<p[^>]*>\s*LLMPROMPTEND\s*</p>',
        wrap1,
        html, flags=re.DOTALL
    )
    html = re.sub(
        r'LLMPROMPTSTART\s*</p>\s*(.*?)\s*<p[^>]*>\s*LLMPROMPTEND\s*</p>',
        wrap1,
        html, flags=re.DOTALL
    )
    html = html.replace('LLMPROMPTSTART', '').replace('LLMPROMPTEND', '')
    return html


def fix_answer_hints(html: str) -> str:
    """Convert \\begin{answer} blockquotes (marked with XCS-ANSWER-HINT) to styled divs.

    pandoc renders \\textbf{XCS-ANSWER-HINT} as <strong>XCS-ANSWER-HINT</strong> inline
    inside a paragraph, not as a standalone <p>.  We strip the marker wherever it
    appears and wrap the blockquote in a div.answer-hint.  Empty divs are removed.
    """
    def replace_hint_block(m):
        inner = m.group(1)
        # Strip the inline marker text (and any immediately following space/nbsp)
        inner = re.sub(
            r'<strong>\s*XCS-ANSWER-HINT\s*</strong>\s*',
            '',
            inner,
        )
        inner = inner.strip()
        if not inner:
            return ''  # drop completely empty answer blocks
        return f'<div class="answer-hint">\n{inner}\n</div>'

    return re.sub(
        r'<blockquote>\s*(.*?XCS-ANSWER-HINT.*?)\s*</blockquote>',
        replace_hint_block,
        html,
        flags=re.DOTALL,
    )


def mark_sub_question_lists(html: str) -> str:
    """Add class="sub-questions" to <ol> blocks whose items carry a points annotation.

    substitute_points() converts \\points{Na} → \\textbf{(Written — N pts)} before
    pandoc runs, so each sub-question <li> contains <strong>(... pts)</strong>.
    We use that as a reliable structural marker, independent of pandoc's nesting.

    For section-less assignments (PS1-style, no <h1 data-number="...">), the outermost
    <ol> IS the top-level question list and must remain a plain numbered list (1., 2., 3.)
    rather than being tagged as sub-questions (a., b., c.).
    """
    # Detect whether this document uses \section{}-based headings (PS2/PS3/PS4).
    # PS1 has no <h1 data-number="..."> — its questions live in a top-level enumerate.
    has_sections = bool(re.search(r'<h1[^>]+data-number', html))

    # Process from innermost <ol> outward (non-greedy finds shortest match first).
    def _tag(m):
        inner = m.group(1)
        # Check if any <li> in this block has a points annotation
        if re.search(r'<strong>\([^<]*\d+\s*pts?\b', inner, re.IGNORECASE):
            return f'<ol class="sub-questions">{inner}</ol>'
        return m.group(0)

    # Iterate to handle nested <ol> (each pass tags the innermost remaining ones)
    prev = None
    while prev != html:
        prev = html
        html = re.sub(r'<ol>(.*?)</ol>', _tag, html, flags=re.DOTALL)

    # For section-less assignments (e.g. PS1): revert the first (outermost) tagged
    # <ol> back to plain <ol> so it renders as 1., 2., 3. not a., b., c.
    if not has_sections:
        html = html.replace('<ol class="sub-questions">', '<ol>', 1)

    return html


def fix_wrapfigure(html):
    """Remove stray position/width <span> artifacts from wrapfigure divs."""
    def clean_wrapfig(m):
        inner = m.group(1)
        inner = re.sub(r'^\s*(<span>[^<]*</span>\s*)+', '', inner)
        return f'<figure class="wrapfigure">{inner.strip()}</figure>'
    return re.sub(
        r'<div class="wrapfigure">\s*<p>(.*?)</p>\s*</div>',
        clean_wrapfig, html, flags=re.DOTALL
    )


def fix_subfigure_caption(html: str) -> str:
    """Remove duplicate <figcaption> pandoc adds to outer <figure> containing subfigures.

    pandoc copies the last subfigure caption to the enclosing figure element.
    Strip it when the outer figure already has inner <figure> children.
    """
    return re.sub(
        r'(<figure[^>]*>(?:\s*<figure>.*?</figure>\s*)+)\s*<figcaption>[^<]*</figcaption>(\s*</figure>)',
        r'\1\2',
        html,
        flags=re.DOTALL,
    )


def fix_pipe_code_html(html):
    """Replace remaining |...| pipe pairs in HTML text with <code>...</code>.

    Uses negative lookbehind to avoid matching \\| (LaTeX norm bars) inside
    math mode spans.
    """
    return re.sub(r'(?<!\\)\|([^|<>\n]+?)(?<!\\)\|', r'<code>\1</code>', html)


def fix_code_quotes(html):
    """Fix backtick-quote patterns and Unicode smart quotes in code blocks."""
    def fix_pre_block(m):
        code = m.group(1)
        code = code.replace('`', "'")
        code = code.replace('\u2019', "'")
        code = code.replace('\u2018', "'")
        code = code.replace('\u201c', '"')
        code = code.replace('\u201d', '"')
        return f'<pre><code>{code}</code></pre>'

    def fix_inline_code(m):
        code = m.group(1)
        code = code.replace('`', "'")
        code = code.replace('\u2019', "'")
        code = code.replace('\u2018', "'")
        code = code.replace('\u201c', '"')
        code = code.replace('\u201d', '"')
        return f'<code>{code}</code>'

    html = re.sub(r'<pre><code>(.*?)</code></pre>', fix_pre_block, html, flags=re.DOTALL)
    html = re.sub(r'<code>(.*?)</code>', fix_inline_code, html, flags=re.DOTALL)
    return html


def clean_latex_artifacts(html):
    """Remove LaTeX command artifacts that leaked into HTML text."""
    html = re.sub(r'\\hline\b', '', html)
    html = re.sub(r'\\newline\b', '<br>', html)
    for env in ['enumerate', 'itemize', 'listing', 'document', 'figure']:
        html = html.replace(f'\\begin{{{env}}}', '')
        html = html.replace(f'\\begin{{{env}*}}', '')
        html = html.replace(f'\\end{{{env}}}', '')
        html = html.replace(f'\\end{{{env}*}}', '')
    html = re.sub(r'\\item\s+', '', html)
    return html


def convert_lstlisting(content):
    """Convert \\begin{lstlisting}...\\end{lstlisting} to verbatim for pandoc."""
    def replace_listing(m):
        options_str = m.group(1) or ''
        code = m.group(2).strip()

        if 'mathescape' in options_str:
            DOLLAR = '\x00DOLLAR\x00'
            code = re.sub(r'\$\\\$\$', DOLLAR, code)
            code = code.replace(r'$\leftarrow$', '\u2190')
            code = code.replace(r'$\rightarrow$', '\u2192')
            code = code.replace(r'$\Rightarrow$', '\u21d2')
            code = code.replace(r'$\Leftarrow$', '\u21d0')
            code = code.replace(r'$\to$', '\u2192')
            code = re.sub(r'\$([^$\n]+)\$', r'\1', code)
            code = code.replace(DOLLAR, '$')

        return '\\begin{verbatim}\n' + code + '\n\\end{verbatim}'

    return re.sub(
        r'\\begin\{lstlisting\}(\[[^\]]*\])?(.*?)\\end\{lstlisting\}',
        replace_listing,
        content,
        flags=re.DOTALL
    )


# ─── Environment transforms ────────────────────────────────────────────────────

def transform_environments(content: str) -> str:
    """Replace custom LaTeX environments with pandoc-friendly equivalents."""
    content = content.replace(
        "\\begin{llmprompt}",
        "\n\nLLMPROMPTSTART\n\n\\textbf{Prompt Template:}\n\n"
    )
    content = content.replace(
        "\\end{llmprompt}",
        "\n\nLLMPROMPTEND\n\n"
    )
    return content


# ─── Header injection (inlined from inject_points.py) ─────────────────────────

def read_due_date() -> str:
    """Read due date from meta.json at repo root."""
    meta_file = ASSIGNMENT_DIR / "meta.json"
    if not meta_file.exists():
        return ""
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        return data.get("due_date", "")
    except Exception:
        return ""


def compute_badge_text(points_map: dict) -> str:
    """Return a human-readable points badge string from a points map."""
    if not points_map:
        return ""
    total_pts = sum(v["pts"] for v in points_map.values() if not v["is_extra_credit"])
    ec_pts = sum(v["pts"] for v in points_map.values() if v["is_extra_credit"])

    def fmt(p):
        return str(int(p)) if p == int(p) else f"{p:.1f}"

    if total_pts == 0 and ec_pts > 0:
        return f"{fmt(ec_pts)} EC pts"
    elif ec_pts > 0:
        return f"{fmt(total_pts)} pts + {fmt(ec_pts)} EC"
    return f"{fmt(total_pts)} pts"


def build_instructions_details(tex_dir: Path, out_dir: Path) -> str:
    """
    Flatten the 00-instructions content and return a <details> HTML block.
    Pandoc is called without --standalone so the output is already an HTML fragment.
    Images in 00-instructions/ are copied to out_dir/figures/ and paths are fixed.
    Returns an empty string if 00-instructions is absent or pandoc fails.
    """
    instr_dir = tex_dir / "00-instructions"
    if not instr_dir.exists():
        return ""

    # After process_all_pytex(), 00-main.tex exists (generated from 00-main.pytex)
    instr_main = instr_dir / "00-main.tex"
    if not instr_main.exists():
        return ""

    # Flatten instructions. Skip logic doesn't suppress sub-files here because
    # resolved IS inside 00-instructions, so "not in resolved" is False for them.
    instr_content = flatten_tex(instr_main, tex_dir)

    # Strip any code fences that appear inside instruction templates
    instr_content = re.sub(
        r'[^\n]*%[ \t]*###[ \t]*START CODE HERE[ \t]*###[^\n]*\n'
        r'[^\n]*%[ \t]*###[ \t]*END CODE HERE[ \t]*###[^\n]*\n?',
        '',
        instr_content,
        flags=re.DOTALL,
    )

    # Convert |...| pipe pairs to \texttt{} before pandoc (same as main content step 2f)
    # so that multi-line wrapping by pandoc doesn't split the delimiters.
    instr_content = replace_pipe_inline(instr_content)

    # Wrap in a minimal standalone document so packages are available
    wrapped = (
        "\\documentclass{article}\n"
        "\\usepackage{hyperref}\n"
        "\\usepackage{verbatim}\n"
        "\\usepackage{listings}\n"
        "\\begin{document}\n"
        + instr_content + "\n"
        "\\end{document}\n"
    )

    flat_path = Path(tempfile.gettempdir()) / "_xcs_instructions_flat.tex"
    out_path  = Path(tempfile.gettempdir()) / "_xcs_instructions.html"
    flat_path.write_text(wrapped, encoding="utf-8")

    resource_path = f"{tex_dir}:{instr_dir}"
    # No --standalone: pandoc outputs a raw HTML fragment (no <html>/<body> wrapper)
    pandoc_cmd = [
        "pandoc", str(flat_path),
        "-f", "latex", "-t", "html5",
        f"--resource-path={resource_path}",
        "-o", str(out_path),
    ]
    try:
        subprocess.run(pandoc_cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return ""

    if not out_path.exists() or out_path.stat().st_size < 50:
        return ""

    body_content = out_path.read_text(encoding="utf-8").strip()
    if not body_content:
        return ""

    # Copy images from 00-instructions/ to out_dir/figures/ and fix src paths
    figures_dir = out_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    for ext in ["*.png", "*.jpg", "*.jpeg", "*.svg", "*.gif"]:
        for img in instr_dir.glob(ext):
            dst = figures_dir / img.name
            if dst.exists():
                dst.chmod(0o644)
            shutil.copy2(str(img), str(dst))
    # Rewrite any local image src (bare filename or path-prefixed) to figures/filename.
    # Skips absolute URLs (http/https/data).
    body_content = re.sub(
        r'src="(?!https?://)(?:[^"]+/)?([^/"]+\.(?:png|jpg|jpeg|svg|gif))"',
        r'src="figures/\1"',
        body_content,
        flags=re.IGNORECASE,
    )
    # Convert |...| pipe pairs to <code>...</code>
    body_content = fix_pipe_code_html(body_content)

    return (
        '\n<details class="instructions-section" id="instructions">\n'
        '  <summary>Instructions &amp; Submission</summary>\n'
        '  <div class="instructions-body">\n'
        + body_content + '\n'
        '  </div>\n'
        '</details>\n'
    )


def _parse_latex_macros(tex_dir: Path) -> str:
    """
    Parse \\newcommand definitions from macros.tex and return a JS snippet
    suitable for inserting inside MathJax's tex.macros object literal.

    Handles:
      \\newcommand\\name{def}
      \\newcommand{\\name}{def}
      \\newcommand{\\name}[n]{def}

    \\ensuremath{...} wrappers are stripped (MathJax is always in math mode).
    Backslashes are doubled so they survive the JS string.
    Returns '' if macros.tex is absent or empty.
    """
    macros_file = tex_dir / "macros.tex"
    if not macros_file.exists():
        return ""

    def _braced(text, start):
        """Return (content, pos_after) for the next {...} block at/after start."""
        i = text.find('{', start)
        if i == -1:
            return None, -1
        depth, j = 0, i
        while j < len(text):
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    return text[i + 1:j], j + 1
            j += 1
        return None, -1

    content = macros_file.read_text(encoding="utf-8")
    js_entries = {}

    for m in re.finditer(r'\\(?:re)?newcommand\s*', content):
        pos = m.end()
        if pos >= len(content):
            continue

        # Extract command name (with or without surrounding braces)
        if content[pos] == '{':
            name_raw, pos = _braced(content, pos)
            if name_raw is None:
                continue
            name = name_raw.lstrip('\\').strip()
        elif content[pos] == '\\':
            nm = re.match(r'\\(\w+)', content[pos:])
            if not nm:
                continue
            name = nm.group(1)
            pos += nm.end()
        else:
            continue

        # Skip whitespace
        pos += len(content[pos:]) - len(content[pos:].lstrip())

        # Optional [numargs]
        nargs = 0
        if pos < len(content) and content[pos] == '[':
            end = content.find(']', pos)
            if end != -1:
                try:
                    nargs = int(content[pos + 1:end])
                except ValueError:
                    pass
                pos = end + 1

        # Skip whitespace
        pos += len(content[pos:]) - len(content[pos:].lstrip())

        # Extract definition body
        defn, _ = _braced(content, pos)
        if defn is None:
            continue
        defn = defn.strip()

        # Strip \ensuremath{...} wrapper
        defn = re.sub(r'\\ensuremath\{(.*?)\}', r'\1', defn, flags=re.DOTALL).strip()

        # Double backslashes for JS string representation
        defn_js = defn.replace('\\', '\\\\')

        if nargs == 0:
            js_entries[name] = f'"{defn_js}"'
        else:
            js_entries[name] = f'["{defn_js}", {nargs}]'

    if not js_entries:
        return ""

    return ',\n        '.join(f'{k}: {v}' for k, v in js_entries.items())


def _parse_assignment_title(tex_dir: Path) -> str:
    """
    Read main.tex and resolve \\assignmenttitle to a plain-text string.

    Collects all \\def\\macroname{value} entries (last definition wins),
    expands \\assignmentnum / \\assignmentname inside \\assignmenttitle,
    then strips remaining LaTeX commands and braces to produce clean text.
    Returns '' if main.tex is absent or \\assignmenttitle is not defined.
    """
    main_tex = tex_dir / "main.tex"
    if not main_tex.exists():
        return ""

    text = main_tex.read_text(encoding="utf-8")

    # Collect \def\macroname{...} – last definition of each macro wins
    defs: dict = {}
    for m in re.finditer(r'\\def\\(\w+)\{', text):
        macro_name = m.group(1)
        start = m.end()
        depth, i = 1, start
        while i < len(text) and depth:
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
            i += 1
        defs[macro_name] = text[start:i - 1]

    title = defs.get("assignmenttitle", "")
    if not title:
        return ""

    # Expand simple peer macros (\assignmentnum, \assignmentname, etc.)
    for macro_name, value in defs.items():
        title = title.replace(f"\\{macro_name}", value)

    # Remove {\bf ...} blocks entirely (carries the "(Solutions)" label in solution titles)
    while True:
        m = re.search(r'\{\\bf\b', title)
        if not m:
            break
        start = m.start()
        depth, i = 1, start + 1
        while i < len(title) and depth:
            if title[i] == '{':
                depth += 1
            elif title[i] == '}':
                depth -= 1
            i += 1
        title = title[:start] + title[i:]

    # Strip remaining LaTeX commands and grouping braces, then normalize whitespace
    title = re.sub(r'\\[a-zA-Z]+', ' ', title)
    title = re.sub(r'[{}]', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def inject_header(html_content: str, assignment: str, name: str,
                  instructions_html: str = "", tex_dir: Path = None) -> str:
    """
    Post-process generated HTML:
      1. Replace pandoc's <head> with a clean one linking to ../assets/style.css
      2. Strip stale injection artifacts from prior builds
      3. Inject assignment header (title + due date + points badge)
    """
    # 1. Clean up stale sections from prior builds
    for pattern in [
        r'<(?:div|section)[^>]*class="points-breakdown"[^>]*>.*?</(?:div|section)>',
        r'<(?:div|section)[^>]*class="env-setup"[^>]*>.*?</(?:div|section)>',
        r'<div[^>]*class="env-option"[^>]*>.*?</div>',
        r'<section[^>]*>\s*</section>',
        r'<div[^>]*class="assignment-header"[^>]*>.*?</div>',
        r'<div[^>]*class="info-banner"[^>]*>.*?</div>',
    ]:
        html_content = re.sub(pattern, '', html_content, flags=re.DOTALL)

    # 2. Replace pandoc's <head> with a clean stylesheet-linked one
    # Resolve the assignment title from main.tex (\assignmenttitle), falling back
    # to the caller-supplied assignment/name strings.
    _tex_title = (_parse_assignment_title(tex_dir) if tex_dir else "") or f"{assignment}: {name}"
    # Build MathJax macros: always include \bm, then add any from macros.tex
    _bm_entry = 'bm: ["\\\\boldsymbol{#1}", 1],\n        ensuremath: ["#1", 1]'
    _custom = _parse_latex_macros(tex_dir) if tex_dir else ""
    _all_macros = (_bm_entry + ',\n        ' + _custom) if _custom else _bm_entry
    clean_head = f"""<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_tex_title}</title>
  <link rel="stylesheet" href="assets/style.css">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
  <script>
  MathJax = {{
    tex: {{
      macros: {{
        {_all_macros}
      }}
    }}
  }};
  </script>
  <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
  <script>hljs.highlightAll();</script>
</head>"""
    html_content = re.sub(r'<head>.*?</head>', lambda _: clean_head, html_content, flags=re.DOTALL)

    # 3. Build and inject the assignment header
    due = read_due_date()
    points_map = build_points_map()
    badge = compute_badge_text(points_map)

    due_line = f'\n  <p class="meta">Due: {due}</p>' if due else ''
    badge_line = f'\n  <span class="badge-points">{badge}</span>' if badge else ''

    # Link to the original assignment PDF on GitHub. Course code (e.g. XCS231N) comes
    # from \assignmenttitle in main.tex; repo pattern is scpd-proed/{course}-{assignment}.
    course = re.search(r'XCS\d+[A-Z]*', _tex_title)
    pdf_line = ''
    if course:
        pdf_url = f'https://github.com/scpd-proed/{course.group()}-{assignment}/blob/main/{assignment}.pdf'
        pdf_line = f'\n  <p class="meta"><a href="{pdf_url}">Original assignment PDF</a></p>'

    header_html = (
        f'\n<div class="assignment-header">\n'
        f'  <h1>{_tex_title}</h1>{due_line}{badge_line}{pdf_line}\n'
        f'</div>\n'
    )

    html_content = html_content.replace(
        '<body>',
        '<body>' + header_html + instructions_html,
        1,
    )
    return html_content


# ─── Minimal CSS bootstrap ────────────────────────────────────────────────────

MINIMAL_CSS = """\
/* XCS221 assignment page — minimal stylesheet */
*, *::before, *::after { box-sizing: border-box; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 16px; line-height: 1.6;
  max-width: 900px; margin: 0 auto; padding: 1.5rem;
  color: #1a1a2e;
}

.assignment-header {
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  color: white; padding: 1.5rem 2rem; border-radius: 8px; margin-bottom: 2rem;
}
.assignment-header h1 { margin: 0; font-size: 1.6rem; color: white; }
.assignment-header .meta { margin: 0.3rem 0 0; color: #ccc; font-size: 0.95rem; }
.assignment-header .meta a { color: #8ec9ff; }
.badge-points {
  display: inline-block; background: #e8f4f8; color: #1a1a2e;
  border-radius: 12px; padding: 0.2rem 0.7rem; font-size: 0.85rem;
  margin-top: 0.4rem;
}

h1, h2, h3 { color: #1a1a2e; margin-top: 1.5rem; }

/* Sub-question lists: tagged by mark_sub_question_lists() post-processor */
ol.sub-questions { list-style-type: lower-alpha; }
/* Fallback structural selectors for edge cases */
ol ol, section ol { list-style-type: lower-alpha; }

/* Section numbers from --number-sections: add a period after the counter */
.header-section-number::after { content: "."; }

a { color: #0066cc; }
code { background: #f5f5f5; padding: 0.1em 0.35em; border-radius: 3px; font-size: 0.9em; }
pre { background: #f5f5f5; padding: 1rem; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #ccc; margin-left: 0; padding-left: 1rem; color: #555; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border: 1px solid #ddd; padding: 0.5rem 0.75rem; text-align: left; }
th { background: #f0f0f0; }
figure { text-align: center; margin: 1rem 0; }
figure img { max-width: 100%; height: auto; }
figcaption { color: #666; font-size: 0.9rem; margin-top: 0.4rem; }

/* Answer hint boxes (from \begin{answer}...\end{answer}) */
.answer-hint {
  background: #fffbec; border-left: 4px solid #e6a817;
  padding: 0.75rem 1rem 0.75rem 1.1rem; border-radius: 0 6px 6px 0;
  margin: 0.75rem 0; color: #4a3000;
}
.answer-hint p:last-child { margin-bottom: 0; }

/* Instructions collapsible block */
.instructions-section {
  border: 1px solid #c5d8e8; border-radius: 6px;
  margin-bottom: 1.5rem; overflow: hidden;
}
.instructions-section > summary {
  background: #deeaf5; padding: 0.55rem 1rem; cursor: pointer;
  font-weight: 600; font-size: 0.92rem; list-style: none;
  display: flex; align-items: center; gap: 0.5rem;
  user-select: none;
}
.instructions-section > summary::before { content: "▶"; font-size: 0.7rem; }
.instructions-section[open] > summary::before { content: "▼"; }
.instructions-section > summary:hover { background: #c8dcee; }
.instructions-body { padding: 0.75rem 1.25rem; }
.instructions-body h1, .instructions-body h2 { font-size: 1.1rem; margin-top: 1rem; }

/* LLM prompt copy widget */
.llmprompt-box {
  border: 1px solid #cce5ff; border-radius: 6px;
  background: #f0f7ff; margin: 1rem 0; overflow: hidden;
}
.llmprompt-header {
  background: #cce5ff; padding: 0.4rem 0.8rem; font-weight: 600;
  display: flex; justify-content: space-between; align-items: center;
}
.copy-btn {
  background: #0066cc; color: white; border: none; border-radius: 4px;
  padding: 0.2rem 0.6rem; cursor: pointer; font-size: 0.85rem;
}
.copy-btn:hover { background: #0052a3; }
.llmprompt-text { width: 100%; padding: 0.75rem; border: none; background: #f0f7ff;
  font-family: monospace; font-size: 0.9rem; resize: vertical; }
"""


def ensure_assets(docs: Path):
    """Write (or update) style.css in docs/assets/."""
    assets = docs / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    css_path = assets / "style.css"
    css_path.write_text(MINIMAL_CSS, encoding="utf-8")
    print(f"  Wrote {css_path.relative_to(REPO_ROOT)}")


# ─── Main conversion ───────────────────────────────────────────────────────────

def convert_assignment(assignment: str, name: str) -> bool:
    """Convert tex/main.tex → assignments/A#/index.html. Returns True on success."""
    print(f"\n── {assignment}: {name} ──")
    tex_dir = TEX_DIR
    out_html = DOCS / "index.html"

    if not tex_dir.exists():
        print(f"  ERROR: {tex_dir} not found")
        return False

    # 1. Process all .pytex → .tex
    print("  Processing .pytex files ...")
    generated_tex = process_all_pytex(tex_dir)

    # 1b. Generate instructions <details> block (while pytex .tex files still exist)
    print("  Building instructions block ...")
    instructions_html = build_instructions_details(tex_dir, out_html.parent)
    if instructions_html:
        print("  Instructions block: OK")
    else:
        print("  Instructions block: not found or skipped")

    # 2. Flatten main.tex following \input{} chains
    print("  Flattening \\input{} chains ...")
    main_tex = tex_dir / "main.tex"
    if not main_tex.exists():
        print(f"  ERROR: main.tex not found in {tex_dir}")
        return False

    flat_content = flatten_tex(main_tex, tex_dir)
    print(f"  Flattened: {len(flat_content):,} chars")

    # 2b. Substitute \points{X} with visible text
    points_map = build_points_map()
    if points_map:
        flat_content = substitute_points(flat_content, points_map)
        print(f"  Substituted \\points{{}} for {len(points_map)} question ids")

    # 2c. Replace \expect{...} with bold "What we expect:" prefix
    flat_content = replace_expect(flat_content)

    # 2d. Transform custom environments (llmprompt → quote block)
    flat_content = transform_environments(flat_content)

    # 2e. Convert lstlisting environments to verbatim for pandoc
    flat_content = convert_lstlisting(flat_content)

    # 2f. Replace |...| pipe shorthand with \texttt{}
    flat_content = replace_pipe_inline(flat_content)

    # 2g. Inline bibliography (.bbl lives at repo root)
    flat_content = inline_bibliography(flat_content, assignment)

    # 2h. Wrap bare arrow commands in math mode
    flat_content = substitute_text_commands(flat_content)

    # 2i. Strip student-submission code fences — remove the entire block (markers +
    #     any code/content between them) so nothing leaks into the HTML output.
    flat_content = re.sub(
        r'[^\n]*%[ \t]*###[ \t]*START CODE HERE[ \t]*###[^\n]*\n'
        r'[^\n]*%[ \t]*###[ \t]*END CODE HERE[ \t]*###[^\n]*\n?',
        '',
        flat_content,
        flags=re.DOTALL,
    )

    # 2j. Convert \begin{answer}...\end{answer} to styled blockquotes.
    #     A unique text marker lets post-processing distinguish them from other <blockquote>s.
    flat_content = re.sub(
        r'\\begin\{answer\}',
        r'\\begin{quote}\\textbf{XCS-ANSWER-HINT}',
        flat_content,
    )
    flat_content = flat_content.replace(r'\end{answer}', r'\end{quote}')

    # 2k. Strip enumitem options from \begin{enumerate}[...] — pandoc's LaTeX reader
    #     fails to parse advanced options like [label=(\alph*)] and silently drops the
    #     entire block. Removing the option leaves a plain \begin{enumerate} which
    #     pandoc handles correctly; lettering is applied via CSS post-processing.
    flat_content = re.sub(r'\\begin\{enumerate\}\[[^\]]*\]', r'\\begin{enumerate}', flat_content)

    # 2l. Convert \text{\texttt{...}} → \texttt{...} — MathJax supports \texttt
    #     directly in math mode but not as a nested command inside \text{}.
    #     Strip the outer \text{} wrapper so the code label renders correctly.
    flat_content = re.sub(r'\\text\{\\texttt\{([^}]+)\}\}', r'\\texttt{\1}', flat_content)

    # 2m. Inside \texttt{...} and \text{...} arguments, replace \_ with plain _ .
    #     MathJax processes both commands' content in text mode where _ is already
    #     a literal character; \_ can appear as a visible backslash in some
    #     MathJax configurations.
    flat_content = re.sub(
        r'\\texttt\{([^}]*)\}',
        lambda m: '\\texttt{' + m.group(1).replace('\\_', '_') + '}',
        flat_content,
    )
    flat_content = re.sub(
        r'\\text\{([^}]*)\}',
        lambda m: '\\text{' + m.group(1).replace('\\_', '_') + '}',
        flat_content,
    )

    # 3. Write flattened content to temp file
    flat_path = Path(tempfile.gettempdir()) / f"{assignment}_flat.tex"
    flat_path.write_text(flat_content, encoding="utf-8")

    # 4. Run pandoc on the flattened file
    print("  Running pandoc ...")
    out_html.parent.mkdir(parents=True, exist_ok=True)

    images_dir = tex_dir / "images"
    figures_dir = tex_dir / "figures"
    resource_path = str(tex_dir)
    if images_dir.exists():
        resource_path += f":{images_dir}"
    if figures_dir.exists():
        resource_path += f":{figures_dir}"
    for subdir in tex_dir.iterdir():
        if subdir.is_dir() and subdir.name not in ('00-instructions', 'shared_tex'):
            resource_path += f":{subdir}"

    pandoc_cmd = [
        "pandoc", str(flat_path),
        "-f", "latex",
        "-t", "html5",
        "--standalone",
        "--mathjax",
        "--number-sections",
        f"--resource-path={resource_path}",
        "-o", str(out_html),
    ]

    success = False
    try:
        result = subprocess.run(
            pandoc_cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            print(f"  pandoc OK → {out_html.relative_to(REPO_ROOT)}")
            success = True
        else:
            stderr_lines = result.stderr.splitlines()[:15]
            print(f"  WARNING: pandoc returned {result.returncode}:")
            for line in stderr_lines:
                print(f"    {line}")
            success = out_html.exists() and out_html.stat().st_size > 500
            if success:
                print(f"  (output exists despite errors — proceeding)")
    except subprocess.TimeoutExpired:
        print(f"  ERROR: pandoc timed out for {assignment}")
    except Exception as e:
        print(f"  ERROR: pandoc exception: {e}")

    # 4b. Copy image directories to output
    if success:
        for imgdir_name in ["images", "figures"]:
            src = tex_dir / imgdir_name
            dst = out_html.parent / "figures"
            if src.exists():
                shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
                print(f"  Copied {imgdir_name}/ → {dst.relative_to(REPO_ROOT)}")
        for ext in ["*.png", "*.jpg", "*.jpeg", "*.svg"]:
            for img in tex_dir.glob(ext):
                shutil.copy2(str(img), str(out_html.parent / img.name))
        for subdir in tex_dir.iterdir():
            if subdir.is_dir() and subdir.name not in ('00-instructions', 'shared_tex'):
                for ext in ['*.png', '*.jpg', '*.jpeg', '*.svg', '*.gif']:
                    for img in subdir.glob(ext):
                        dst_subdir = out_html.parent / subdir.name
                        dst_subdir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(img), str(dst_subdir / img.name))
                        print(f"  Copied {subdir.name}/{img.name}")

    # 4c. Fix image paths:
    #   - images/foo → figures/foo  (normalise source directory name)
    #   - figures/foo (no ext) → figures/foo.png (add missing extension)
    if success and out_html.exists():
        html_content = out_html.read_text(encoding="utf-8")
        # Normalise images/ → figures/
        html_content = re.sub(r'src="images/([^"]+)"', r'src="figures/\1"', html_content)

        # Add missing file-extension to bare image src attributes that lack one.
        # Guards skip: URLs, paths that already have any extension (image or otherwise).
        _img_exts = [".png", ".jpg", ".jpeg", ".svg", ".gif"]

        def _add_ext(m):
            src = m.group(1)
            # Skip URLs and paths with any extension
            if src.startswith(("http://", "https://", "data:", "//")):
                return m.group(0)
            if Path(src).suffix:  # already has an extension
                return m.group(0)
            # Try to locate a matching file next to the output HTML
            for ext in _img_exts:
                candidate = out_html.parent / (src + ext)
                if candidate.exists():
                    return f'src="{src}{ext}"'
            return m.group(0)  # give up; leave unchanged

        html_content = re.sub(r'src="([^"]+)"', _add_ext, html_content)
        out_html.write_text(html_content, encoding="utf-8")

    # 4d. Post-process HTML: citations, llmprompt, code fixes, latex artifacts
    if success and out_html.exists():
        html_content = out_html.read_text(encoding="utf-8")
        html_content = strip_duplicate_header(html_content)
        html_content = fix_citations(html_content)
        html_content = re.sub(
            r'(<div[^>]*class="[^"]*thebibliography[^"]*")',
            r'<h2>References</h2>\n\1',
            html_content
        )
        html_content = fix_bibliography_entries(html_content)
        html_content = fix_answer_hints(html_content)
        html_content = fix_llmprompt_html(html_content)
        html_content = fix_pipe_code_html(html_content)
        html_content = fix_code_quotes(html_content)
        html_content = clean_latex_artifacts(html_content)
        html_content = mark_sub_question_lists(html_content)
        html_content = fix_wrapfigure(html_content)
        html_content = fix_subfigure_caption(html_content)
        html_content = re.sub(r'<p>\s*\|\s*</p>', '', html_content)
        out_html.write_text(html_content, encoding="utf-8")

    # 4e. Inject assignment header and clean <head> (inlined inject_points logic)
    if success and out_html.exists():
        html_content = out_html.read_text(encoding="utf-8")
        html_content = inject_header(html_content, assignment, name,
                                     instructions_html=instructions_html,
                                     tex_dir=tex_dir)
        out_html.write_text(html_content, encoding="utf-8")
        print(f"  Injected header for {assignment}")

    # 4f. Copy PDF to assignments/A#/
    if success:
        pdf_src = ASSIGNMENT_DIR / f"{assignment}.pdf"
        if pdf_src.exists():
            shutil.copy2(str(pdf_src), str(out_html.parent / f"{assignment}.pdf"))
            print(f"  Copied {assignment}.pdf")
        else:
            print(f"  (no {assignment}.pdf found at repo root — skipping)")

    # 5. Clean up generated .tex files from pytex processing
    for f in generated_tex:
        try:
            f.unlink()
        except Exception:
            pass

    if flat_path.exists():
        flat_path.unlink()

    return success


# ─── Validation ───────────────────────────────────────────────────────────────

MIN_WORDS = 200


def validate(assignment: str) -> bool:
    html = DOCS / "index.html"
    print("\n── Validation ──")
    if not html.exists():
        print(f"  FAIL: {html} does not exist")
        return False
    words = count_words(html)
    status = "PASS" if words >= MIN_WORDS else "FAIL"
    flag = " ✓" if status == "PASS" else " ✗"
    print(f"  {assignment}: {words} words — {status}{flag}")
    return words >= MIN_WORDS


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    global ASSIGNMENT_DIR, TEX_DIR, DOCS

    import argparse
    parser = argparse.ArgumentParser(description="Build HTML for XCS assignment")
    parser.add_argument(
        "--assignment", "-a", metavar="PSX",
        help="CF mode: use PSX/ subdir for tex, points.json, etc. (e.g. --assignment PS1)"
    )
    args = parser.parse_args()

    if args.assignment:
        # CF repo mode: assignment sources live in a subdirectory
        ASSIGNMENT_DIR = REPO_ROOT / args.assignment
        TEX_DIR = ASSIGNMENT_DIR / "tex"
        DOCS = REPO_ROOT / "site" / args.assignment
        if not ASSIGNMENT_DIR.exists():
            print(f"ERROR: {ASSIGNMENT_DIR} not found")
            sys.exit(1)
        _, name = detect_assignment()        # read name from PSX/tex/main.tex
        assignment = args.assignment         # use "PS1" as the canonical ID
    else:
        # Student repo mode: assignment is at repo root (unchanged behavior)
        assignment, name = detect_assignment()

    print("==> flatten_and_convert.py — XCS221 single-assignment build")
    print(f"    Assignment: {assignment} ({name})")
    print(f"    Repo root:  {REPO_ROOT}")

    # Check pandoc
    try:
        result = subprocess.run(["pandoc", "--version"], capture_output=True, text=True)
        version_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
        print(f"    {version_line}")
    except FileNotFoundError:
        print("ERROR: pandoc not found. Install with: brew install pandoc  (macOS)")
        sys.exit(1)

    ensure_assets(DOCS)

    ok = convert_assignment(assignment, name)

    all_pass = validate(assignment)

    print(f"\n==> {'Success' if ok else 'FAILED'}: {assignment}")

    if not all_pass:
        print("\nERROR: Output page has < 200 words of visible text")
        sys.exit(1)
    else:
        print(f"\nOutput: {(DOCS / 'index.html').relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()