"""UnvibeCode PR Brief — prototype.

Turns an existing UnvibeCode review (Business Workflow Map + Complete Repository
Context) into a short, branded pull-request comment:

  * which business workflows a diff touches,
  * which connected code depends on the changed symbols but was NOT changed,
  * which tests are linked to the changed code.

Changes are matched to the review by *symbol name* (e.g. ``Config.from_object``),
not by line number, so one cached review can be reused across many later PRs.

usage:
  python unvibecode_brief.py --repo PATH --results PATH --base REV --head REV [--pr-url URL]

Python sources are mapped to symbols with the standard ``ast`` module; other
languages fall back to file-level matching.
"""
from __future__ import annotations

import argparse
import ast
import collections
import html
import io
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

BRAND = "UnvibeCode"
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# --------------------------------------------------------------------------- review loading
@dataclass
class Reference:
    name: str           # e.g. "Config.from_object" or "Config"
    file: str
    start: int
    end: int

    @property
    def is_class_level(self) -> bool:
        return "." not in self.name and self.name[:1].isupper()


@dataclass
class Workflow:
    wid: str
    title: str
    area: str
    summary: str
    refs: list[Reference] = field(default_factory=list)


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def load_workflows(results: Path) -> list[Workflow]:
    page = next(results.rglob("business-workflows.html")).read_text(encoding="utf-8")
    workflows: list[Workflow] = []
    for group in re.split(r'<section class="workflow-group"', page)[1:]:
        area = _text(re.search(r"<h2>(.*?)</h2>", group, re.S).group(1))
        for card in re.split(r'<article class="workflow-card"', group)[1:]:
            wid = re.search(r'data-workflow-id="([^"]+)"', card).group(1)
            title = _text(re.search(r'class="workflow-title">(.*?)</h3>', card, re.S).group(1))
            summary_m = re.search(r'class="workflow-summary"><h3>.*?</h3><p>(.*?)</p>', card, re.S)
            wf = Workflow(wid, title, area, _text(summary_m.group(1)) if summary_m else "")
            for name, loc in re.findall(
                r'class="implementation-item"><strong>(.*?)</strong>\s*'
                r'<span class="implementation-location">(.*?)</span>', card, re.S):
                m = re.match(r"(.+?) · lines (\d+)\D+(\d+)", _text(loc))
                if m:
                    wf.refs.append(Reference(_text(name).removesuffix("()"), m.group(1),
                                             int(m.group(2)), int(m.group(3))))
            workflows.append(wf)
    return workflows


def load_context(results: Path):
    bundle = next(results.rglob("*-repository-context.zip"))
    with zipfile.ZipFile(bundle) as z:
        read = lambda n: [json.loads(l) for l in io.TextIOWrapper(z.open(n), encoding="utf-8") if l.strip()]
        chunks = {c["chunk_id"]: c for c in read("chunks.jsonl")}
        connections = read("connections.jsonl")
    return chunks, connections


# --------------------------------------------------------------------------- diff → symbols
def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def changed_lines(repo: Path, base: str, head: str) -> dict[str, dict[str, set[int]]]:
    """{path: {"old": lines removed/changed in base, "new": lines added/changed in head}}"""
    out: dict[str, dict[str, set[int]]] = {}
    path = None
    for line in git(repo, "diff", "-U0", "--no-color", "--find-renames", base, head).splitlines():
        if line.startswith("+++ "):
            path = None if line.endswith("/dev/null") else line[6:]
            if path:
                out.setdefault(path, {"old": set(), "new": set()})
        elif line.startswith("--- ") and line.endswith("/dev/null"):
            path = None
        elif (m := HUNK.match(line)) and path:
            o, oc, n, nc = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            out[path]["old"].update(range(o, o + oc))
            out[path]["new"].update(range(n, n + nc) if nc else {n})
    return out


def enclosing_symbols(source: str, lines: set[int]) -> set[str]:
    """Innermost def/class qualified names that contain any of ``lines``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + child.name
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                span = set(range(start, child.end_lineno + 1))
                if span & lines:
                    inner_before = len(found)
                    visit(child, q + ".")
                    if len(found) == inner_before:
                        found.add(q)
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def changed_symbols(repo: Path, base: str, head: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for path, sides in changed_lines(repo, base, head).items():
        syms: set[str] = set()
        if path.endswith(".py"):
            for rev, side in ((head, "new"), (base, "old")):
                try:
                    src = git(repo, "show", f"{rev}:{path}")
                except subprocess.CalledProcessError:
                    continue
                syms |= enclosing_symbols(src, sides[side])
        result[path] = syms or {"<module>"}
    return result


# --------------------------------------------------------------------------- matching
def match_workflows(workflows, changes):
    hits = []
    for wf in workflows:
        direct, via_class = [], []
        for ref in wf.refs:
            for sym in changes.get(ref.file, ()):
                if sym == ref.name or sym.startswith(ref.name + ".") and not ref.is_class_level:
                    direct.append((ref, sym))
                elif sym.startswith(ref.name + ".") or ref.name.startswith(sym + "."):
                    via_class.append((ref, sym))
        direct_syms = {s for _, s in direct}
        via_class = [(r, s) for r, s in via_class if s not in direct_syms]
        if direct or via_class:
            hits.append((wf, direct, via_class))
    hits.sort(key=lambda h: (-len(h[1]), -len(h[2])))
    return hits


def blast_radius(chunks, connections, changes):
    """Files that link INTO the changed symbols but are not part of the diff."""
    changed_files = set(changes)
    top_level = {f: {s.split(".")[0] for s in syms} for f, syms in changes.items()}
    dependents = collections.defaultdict(set)
    for c in connections:
        tgt = c["target_file"]
        if tgt not in changed_files or c["source_file"] in changed_files:
            continue
        tgt_chunk = chunks.get(c["target_chunk"], {})
        qn = (tgt_chunk.get("qualified_name") or "")
        names = top_level[tgt]
        if "<module>" in changes[tgt] or c["symbol"] in names or qn.split(".")[0] in names:
            dependents[c["source_file"]].add(c["symbol"] or qn or "module")
    prod = {f: s for f, s in dependents.items() if not f.startswith(("tests/", "test/")) and "/tests/" not in f}
    tests = {f: s for f, s in dependents.items() if f not in prod}
    rank = lambda d: sorted(d.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    # real test modules first, fixture apps after
    is_test_module = lambda f: bool(re.search(r"(^|/)test_[^/]*\.py$|\.(spec|test)\.[jt]sx?$", f))
    ranked_tests = sorted(tests.items(), key=lambda kv: (not is_test_module(kv[0]), -len(kv[1]), kv[0]))
    return rank(prod), ranked_tests


# --------------------------------------------------------------------------- rendering
def render(repo_name, base, head, changes, wf_hits, prod, tests, pr_url=None, report_url=None):
    n_sym = sum(len(s - {"<module>"}) for s in changes.values())
    out = [f"### 🧭 {BRAND} PR Brief — `{repo_name}`",
           f"<sub>{base[:8]}…{head[:8]} · {len(changes)} files · {n_sym} changed symbols"
           f"{' · ' + pr_url if pr_url else ''}</sub>", ""]
    if wf_hits:
        out.append(f"**Business workflows touched ({len(wf_hits)})**")
        for wf, direct, via in wf_hits:
            tag = "🔴 direct" if direct else "🟡 inside a referenced class"
            syms = sorted({s for _, s in direct} or {s for _, s in via})
            out.append(f"- {tag} — **{wf.title}** _({wf.area})_  ")
            out.append(f"  changed: {', '.join(f'`{s}`' for s in syms[:6])}")
    else:
        out.append("**Business workflows touched:** none of the mapped workflows — "
                   "this change sits outside the reviewed business paths.")
    out.append("")
    if prod:
        out.append(f"**Connected code NOT changed in this PR** — check these still hold ({len(prod)})")
        for f, syms in prod[:8]:
            out.append(f"- `{f}` — uses {', '.join(f'`{s}`' for s in sorted(syms)[:4])}")
        out.append("")
    if tests:
        out.append(f"**Linked tests to run** ({len(tests)}): " + ", ".join(f"`{f}`" for f, _ in tests[:8]))
        out.append("")
    out.append("<details><summary>Changed symbols</summary>\n")
    for f, syms in sorted(changes.items()):
        out.append(f"- `{f}`: {', '.join(f'`{s}`' for s in sorted(syms))}")
    out.append("\n</details>\n")
    link = f"[Open the full map in {BRAND}]({report_url})" if report_url else f"Full map: run `unvibecode review`"
    out.append(f"<sub>Generated by **{BRAND}** from a cached review · {link} · "
               f"👍/👎 this brief to tell us if it helped</sub>")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--results", required=True, type=Path, help="UnvibeCode results folder")
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--pr-url")
    ap.add_argument("--report-url")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    workflows = load_workflows(a.results)
    chunks, connections = load_context(a.results)
    changes = changed_symbols(a.repo, a.base, a.head)
    hits = match_workflows(workflows, changes)
    prod, tests = blast_radius(chunks, connections, changes)
    if a.json:
        print(json.dumps({
            "changes": {k: sorted(v) for k, v in changes.items()},
            "workflows": [{"title": w.title, "direct": [s for _, s in d], "via_class": [s for _, s in v]}
                          for w, d, v in hits],
            "connected_not_changed": [{"file": f, "symbols": sorted(s)} for f, s in prod],
            "linked_tests": [f for f, _ in tests]}, indent=2))
    else:
        print(render(a.repo.resolve().name, git(a.repo, "rev-parse", a.base).strip(),
                     git(a.repo, "rev-parse", a.head).strip(), changes, hits, prod, tests,
                     a.pr_url, a.report_url))


if __name__ == "__main__":
    sys.exit(main())
