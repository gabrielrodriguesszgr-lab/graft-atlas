#!/usr/bin/env python3
"""graft - code intelligence local, adaptativa e sem dependencias, para agentes de IA.

Responde "onde esta X", "quem chama X", "o que quebra se eu mudar X" com poucos
tokens, em vez de o agente ler arquivos inteiros.

Principios (ver references/audit.md):
  * 100% local: sem rede, sem telemetria, sem alterar configuracao global/agentes.
    Unico escrito no projeto: .graft/ (com .gitignore proprio). Projetos minusculos
    nem isso: o indice vive em memoria.
  * Adaptativa: tiers tiny / standard / large escolhidos pelo tamanho do projeto.
  * Honesta: toda aresta tem confianca (EXACT > INFER > AMBIG); respostas fracas
    dizem "weak" e mandam cair para grep.
  * Sempre fresca: cada comando revalida o indice por mtime/size (incremental).
  * Orcada: toda saida respeita --budget (tokens aprox.) e explica como refinar.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import builtins
import collections
import concurrent.futures as cf
import fnmatch
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import warnings
from pathlib import Path, PurePosixPath

VERSION = "1.0.0"
SCHEMA = 4
MAX_FILE_BYTES = 1_000_000
TINY_FILES, TINY_BYTES = 25, 200_000
LARGE_FILES, LARGE_BYTES = 1500, 25_000_000
PARALLEL_MIN = 120  # arquivos a parsear para valer abrir processos

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".env",
    "dist", "build", "out", "target", ".next", ".nuxt", ".cache", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "coverage", ".idea", ".vscode", ".graft", "graphify-out",
    ".codegraph", "vendor", "bower_components", ".gradle", "Pods", ".terraform", "site-packages",
}
IGNORE_FILES = ("*.min.js", "*.min.css", "*.map", "*.d.ts", "*.pb.go", "*_pb2.py", "*_pb2_grpc.py",
                "*.generated.*", "*.bundle.js", "*.chunk.js")

LANG = {
    ".py": "py", ".pyw": "py",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts",
    ".go": "go", ".java": "java", ".kt": "kt", ".kts": "kt", ".scala": "scala", ".cs": "cs",
    ".rs": "rs", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".php": "php", ".rb": "rb", ".swift": "swift",
}
JSLIKE = {"js", "ts"}
BRACE_LANGS = {"js", "ts", "go", "java", "kt", "scala", "cs", "rs", "c", "cpp", "php", "swift"}
SAME_DIR_LANGS = {"go": 3, "java": 2, "kt": 2, "cs": 2, "swift": 2, "c": 2, "cpp": 2, "scala": 2, "php": 2}

CONF_NAME = {3: "EXACT", 2: "INFER", 1: "AMBIG"}
try:
    STDLIB = set(sys.stdlib_module_names)  # py>=3.10
except AttributeError:  # pragma: no cover
    STDLIB = {"os", "sys", "re", "json", "typing", "pathlib", "collections", "functools", "itertools", "math",
              "time", "datetime", "subprocess", "logging", "argparse", "unittest", "ast", "io", "abc"}
BUILTINS = set(dir(builtins))

STOP = set("""a an the of in on to for from by with and or is are was were be been it its this that these those
how what where which who when why does do did can could should would will shall i you we they my your our their as at
into out about over under between via per using use used find show list get give tell explain implemented implement
implementation work works working code function functions method methods class classes file files project repo
codebase handle handles handled there here if then than so such
como onde qual quais que de do da dos das em no na nos nas para por um uma uns umas o os as e sao foi ser esta
funciona implementado implementada codigo funcao arquivo""".split())

COMMON_ATTRS = set("""get set add append extend pop remove clear update items keys values copy join split strip lstrip
rstrip replace format encode decode read write close open seek flush send recv start stop run call apply bind map
filter reduce find index count sort reverse insert push shift then catch finally next init new delete create put post
head toString valueOf length size name id type value data error info debug warn log emit on off once trigger render
exists load save dump dumps loads lower upper title startswith endswith add_argument main setdefault discard
iter min max sum any all enumerate zip range len str int float bool list dict tuple""".split())

KEYWORDS = set("""if for while switch catch return function typeof sizeof await async else do with throw delete void in of
instanceof match when using lock foreach elif unless and or not defer go select case assert super this self cls fn func def
class new yield print println printf require import from export extends implements interface struct enum trait impl
namespace package try finally except raise lambda int float double char long short byte bool string var let const static
public private protected final abstract override virtual operator template typename decltype alignof""".split())

DECL_PRE = re.compile(r"(?:function|func|fn|def|class|interface|struct|enum|trait|impl|type|namespace|package)\s+$")


# --------------------------------------------------------------------------- util
def est_tokens(s: str) -> int:
    return max(1, (len(s) + 3) // 4)


def split_ident(s: str):
    return [p.lower() for p in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", s)]


_IRREGULAR = {"built": "build", "written": "writ", "wrote": "writ", "ran": "run", "made": "mak", "chosen": "choos",
              "found": "find", "sent": "send", "kept": "keep", "held": "hold"}


def stem(t: str) -> str:
    if t in _IRREGULAR:
        return _IRREGULAR[t]
    for suf in ("ing", "ed", "es", "s", "e"):
        if len(t) > len(suf) + 2 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def is_test_path(p: str) -> bool:
    low = p.lower()
    base = low.rsplit("/", 1)[-1]
    return bool(re.search(r"(^|/)(tests?|__tests__|specs?|e2e)(/|$)", low)) or bool(
        re.match(r"(test_.*\.py|.*_test\.(py|go|rs)|.*\.(test|spec)\.[jt]sx?|.*test\.java|.*tests?\.cs|.*_spec\.rb)$", base))


def first_line(s: str, n: int = 110) -> str:
    for ln in (s or "").strip().splitlines():
        ln = ln.strip().strip("*/#! ").strip()
        if ln:
            return ln[:n]
    return ""


def shorten(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------- parsing: python
def _py_sig(node) -> str:
    kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "..."
    ret = ""
    if node.returns is not None:
        try:
            ret = " -> " + ast.unparse(node.returns)
        except Exception:
            pass
    s = f"{kw} {node.name}({args}){ret}"
    if len(s) > 140:
        names = [a.arg for a in node.args.posonlyargs + node.args.args]
        if node.args.vararg:
            names.append("*" + node.args.vararg.arg)
        names += [a.arg for a in node.args.kwonlyargs]
        if node.args.kwarg:
            names.append("**" + node.args.kwarg.arg)
        s = f"{kw} {node.name}({', '.join(names)}){ret}"
    return shorten(s, 160)


def _iter_nodes(nodes, skip_defs):
    stack = list(reversed(nodes))
    while stack:
        n = stack.pop()
        yield n
        for c in reversed(list(ast.iter_child_nodes(n))):
            if skip_defs and isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(c)


def _base_of(v):
    if isinstance(v, ast.Name):
        return v.id
    if isinstance(v, ast.Attribute):
        b = _base_of(v.value)
        return f"{b}.{v.attr}" if b else None
    if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "super":
        return "super"
    return None


def parse_python(text: str, path: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(text)
    syms, raws, imports = [], [], []
    nlines = text.count("\n") + 1

    # imports (qualquer profundidade: inclui imports preguicosos dentro de funcoes)
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imports.append({"a": a.asname or a.name.split(".")[0], "k": "mod",
                                "m": a.name if a.asname else a.name.split(".")[0], "l": 0})
                if not a.asname and "." in a.name:
                    imports.append({"a": a.name, "k": "mod", "m": a.name, "l": 0})
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "*":
                    imports.append({"a": "*", "k": "star", "m": n.module or "", "n": "*", "l": n.level})
                else:
                    imports.append({"a": a.asname or a.name, "k": "from", "m": n.module or "", "n": a.name, "l": n.level})
    aliases = {e["a"] for e in imports}
    top_names = set()
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_names.add(st.name)

    def scan(nodes, owner, skip_defs, exclude=None):
        nodes = list(nodes)
        stores = set()
        for sub in _iter_nodes(nodes, skip_defs):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
                stores.add(sub.id)
            elif isinstance(sub, ast.arg):
                stores.add(sub.arg)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                stores.add(sub.name)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and sub is not exclude \
                    and not skip_defs:
                stores.add(sub.name)
        call_funcs = set()
        seen = set()
        for sub in _iter_nodes(nodes, skip_defs):
            if isinstance(sub, ast.Call):
                call_funcs.add(id(sub.func))
        for sub in _iter_nodes(nodes, skip_defs):
            rec = None
            ln = getattr(sub, "lineno", 0)
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    if f.id not in stores and (f.id not in BUILTINS or f.id in top_names or f.id in aliases):
                        rec = ("call", f.id, None)
                elif isinstance(f, ast.Attribute):
                    rec = ("call", f.attr, _base_of(f.value))
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and id(sub) not in call_funcs:
                if sub.id not in stores and (sub.id in aliases or sub.id in top_names):
                    rec = ("ref", sub.id, None)
            elif isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load) and id(sub) not in call_funcs:
                b = _base_of(sub.value)
                if b and (b in ("self", "cls") or b.split(".")[0] in aliases):
                    rec = ("ref", sub.attr, b)
            if rec and (owner,) + rec not in seen:
                seen.add((owner,) + rec)
                raws.append((owner,) + rec + (ln,))

    def add_sym(name, qname, kind, node, parent, sig):
        syms.append({"name": name, "qname": qname, "kind": kind, "line": node.lineno,
                     "end": getattr(node, "end_lineno", node.lineno) or node.lineno,
                     "sig": sig, "doc": first_line(ast.get_docstring(node) or "") if kind != "const" else "",
                     "parent": parent})

    module_stmts = []

    def visit(body, parent):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{parent}.{st.name}" if parent else st.name
                add_sym(st.name, q, "method" if parent else "function", st, parent, _py_sig(st))
                scan([st], q, False, exclude=st)
            elif isinstance(st, ast.ClassDef):
                q = f"{parent}.{st.name}" if parent else st.name
                bases = []
                for b in st.bases:
                    try:
                        bases.append(ast.unparse(b))
                    except Exception:
                        pass
                add_sym(st.name, q, "class", st, parent, shorten(f"class {st.name}({', '.join(bases)})" if bases else f"class {st.name}", 160))
                for b in st.bases:
                    bb = _base_of(b)
                    if bb:
                        head, _, tail = bb.rpartition(".")
                        raws.append((q, "inherit", tail or head, head or None, st.lineno))
                scan(list(st.decorator_list) + [s for s in st.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))],
                     q, True)
                visit(st.body, q)
            elif isinstance(st, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.While, ast.AsyncFor)) or \
                    type(st).__name__ == "TryStar":
                module_stmts.append(st) if not parent else None
                for fld in ("body", "orelse", "finalbody"):
                    visit(getattr(st, fld, []) or [], parent)
                for h in getattr(st, "handlers", []) or []:
                    visit(h.body, parent)
            else:
                if not parent:
                    module_stmts.append(st)
                    targets = []
                    if isinstance(st, ast.Assign):
                        targets = [t for t in st.targets if isinstance(t, ast.Name)]
                    elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                        targets = [st.target]
                    for t in targets:
                        if t.id.isupper() and len(t.id) > 2:
                            add_sym(t.id, t.id, "const", st, None, f"{t.id} = ...")

    visit(tree.body, None)
    scan(module_stmts, "<module>", True)
    entry = bool(re.search(r"""if\s+__name__\s*==\s*['"]__main__['"]""", text))
    return {"syms": syms, "raws": raws, "imports": imports, "entry": entry, "nlines": nlines}


# --------------------------------------------------------------------------- parsing: generic
_STR_BRACE = re.compile(
    r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.[^'\n]*|[^'\\\n])'|`[^`]*`""", re.S)
_STR_JS = re.compile(r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`""", re.S)
_STR_PHP = re.compile(r"""//[^\n]*|#[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'""", re.S)
_STR_RB = re.compile(r"""#[^\n]*|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'""", re.S)


def _blank(m):
    return re.sub(r"[^\n]", " ", m.group(0))


def strip_noise(text: str, lang: str) -> str:
    rx = {"js": _STR_JS, "ts": _STR_JS, "php": _STR_PHP, "rb": _STR_RB}.get(lang, _STR_BRACE)
    return rx.sub(_blank, text)


_MODS_JS = r"(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*"
_MODS_JV = r"(?:(?:public|private|protected|static|final|abstract|override|virtual|async|synchronized|internal|open|suspend|native|default|sealed|partial|readonly|unsafe|extern)\s+)*"
# (kind, regex). kinds: class (container), type, func (funcao/metodo conforme contexto), method (so dentro de container), impl (container sem simbolo), arrow
GEN_PATTERNS = {
    "js": [
        ("class", r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<n>[A-Za-z_$][\w$]*)"),
        ("func", r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<n>[A-Za-z_$][\w$]*)\s*[<(]"),
        ("arrow", r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+(?P<n>[A-Za-z_$][\w$]*)\s*(?::\s*[^=\n]+)?=\s*(?:async\s+)?(?:function\b|\([^)\n]*\)\s*(?::\s*[^=\n]+)?=>|[A-Za-z_$][\w$]*\s*=>)"),
        ("type", r"^[ \t]*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+(?P<n>[A-Za-z_$][\w$]*)"),
        ("method", r"^[ \t]+" + _MODS_JS + r"(?P<n>[A-Za-z_$#][\w$]*)\s*(?:<[^>\n]*>)?\("),
    ],
    "go": [
        ("func", r"^func\s+(?:\((?P<recv>[^)]*)\)\s*)?(?P<n>[A-Za-z_]\w*)\s*(?:\[[^\]\n]*\])?\("),
        ("class", r"^type\s+(?P<n>[A-Za-z_]\w*)\s+(?:struct|interface)\b"),
    ],
    "java": [
        ("class", r"^[ \t]*" + _MODS_JV + r"(?:class|interface|enum|record|object|struct|trait)\s+(?P<n>[A-Za-z_]\w*)"),
        ("method", r"^[ \t]+" + _MODS_JV + r"(?:[\w<>\[\],.?*&:]+\s+)+(?P<n>[A-Za-z_]\w*)\s*\("),
    ],
    "kt": [
        ("class", r"^[ \t]*" + _MODS_JV + r"(?:class|interface|object|enum\s+class|data\s+class)\s+(?P<n>[A-Za-z_]\w*)"),
        ("func", r"^[ \t]*" + _MODS_JV + r"fun\s+(?:<[^>]+>\s*)?(?:[\w.<>?]+\.)?(?P<n>[A-Za-z_]\w*)\s*\("),
    ],
    "rs": [
        ("func", r"^[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+(?P<n>[A-Za-z_]\w*)"),
        ("class", r"^[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+(?P<n>[A-Za-z_]\w*)"),
        ("impl", r"^[ \t]*impl(?:<[^>]*>)?\s+(?:[\w:<>,\s]+?\s+for\s+)?(?P<n>[A-Za-z_]\w*)"),
    ],
    "php": [
        ("class", r"^[ \t]*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+(?P<n>[A-Za-z_]\w*)"),
        ("func", r"^[ \t]*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+&?(?P<n>[A-Za-z_]\w*)\s*\("),
    ],
    "rb": [
        ("class", r"^[ \t]*(?:class|module)\s+(?P<n>[A-Z]\w*(?:::\w+)*)"),
        ("func", r"^[ \t]*def\s+(?:self\.)?(?P<n>[A-Za-z_]\w*[?!=]?)"),
    ],
    "swift": [
        ("class", r"^[ \t]*" + _MODS_JV + r"(?:class|struct|enum|protocol|extension|actor)\s+(?P<n>[A-Za-z_]\w*)"),
        ("func", r"^[ \t]*(?:(?:public|private|internal|open|fileprivate|static|class|final|override|mutating)\s+)*func\s+(?P<n>[A-Za-z_]\w*)"),
    ],
    "cpp": [
        ("class", r"^[ \t]*(?:class|struct|enum(?:\s+class)?|namespace)\s+(?P<n>[A-Za-z_]\w*)"),
        ("func", r"^(?!\s*(?:if|for|while|switch|return|else|do|typedef|using|namespace|template)\b)[A-Za-z_][\w \t\*&:<>,~]*?[ \t\*&]+(?P<n>[A-Za-z_~][\w:~]*)\s*\("),
    ],
}
GEN_PATTERNS["ts"] = GEN_PATTERNS["js"]
GEN_PATTERNS["cs"] = GEN_PATTERNS["java"]
GEN_PATTERNS["scala"] = GEN_PATTERNS["java"]
GEN_PATTERNS["c"] = GEN_PATTERNS["cpp"]
_COMPILED = {k: [(kind, re.compile(rx, re.M)) for kind, rx in v] for k, v in GEN_PATTERNS.items()}
NOT_METHOD = {"if", "for", "while", "switch", "catch", "function", "return", "else", "do", "with", "super", "await",
              "new", "throw", "typeof", "delete", "case", "using", "lock", "foreach", "when", "sizeof", "synchronized"}
NOT_TYPE_LEAD = {"return", "new", "throw", "else", "case", "await", "yield", "in", "is", "as", "typeof", "delete",
                 "goto", "import", "using", "package", "extends", "implements"}

_CALL_RX = re.compile(r"(?:(?P<b>[A-Za-z_$][\w$]*)\s*(?:\?\.|\.|::|->)\s*)?(?P<n>[A-Za-z_$][\w$]*)\s*(?:<[^<>()\n]*>)?\s*\(")
_TYPE_RX = re.compile(r"\b(?P<n>[A-Z][a-z0-9_$][\w$]*)\b")
_JS_IMPORT = re.compile(
    r"""import\s+(?:type\s+)?(?P<d>[A-Za-z_$][\w$]*)?\s*,?\s*(?:\{(?P<named>[^}]*)\}|\*\s*as\s+(?P<ns>[A-Za-z_$][\w$]*))?\s*from\s*['"](?P<s>[^'"]+)['"]""", re.S)
_JS_REEXPORT = re.compile(
    r"""export\s+(?:type\s+)?(?:\{(?P<named>[^}]*)\}|\*(?:\s*as\s+(?P<ns>[A-Za-z_$][\w$]*))?)\s*from\s*['"](?P<s>[^'"]+)['"]""", re.S)
_JS_REQUIRE = re.compile(
    r"""(?:const|let|var)\s+(?:\{(?P<named>[^}]*)\}|(?P<ns>[A-Za-z_$][\w$]*))\s*=\s*require\(\s*['"](?P<s>[^'"]+)['"]\s*\)""")


def _named_list(named: str):
    out = []
    for part in (named or "").split(","):
        part = re.sub(r"^\s*type\s+", "", part.strip())
        if not part:
            continue
        if re.search(r"\bas\b", part):
            o, a = re.split(r"\s+as\s+", part, maxsplit=1)
        else:
            o = a = part
        out.append((o.strip(), a.strip()))
    return out


def parse_js_imports(text: str):
    ents = []
    for m in _JS_IMPORT.finditer(text):
        s = m.group("s")
        if m.group("d"):
            ents.append({"a": m.group("d"), "k": "js", "s": s, "n": "default"})
        for o, a in _named_list(m.group("named")):
            ents.append({"a": a, "k": "js", "s": s, "n": o})
        if m.group("ns"):
            ents.append({"a": m.group("ns"), "k": "js", "s": s, "n": "*"})
    for m in _JS_REEXPORT.finditer(text):
        s = m.group("s")
        if m.group("named"):
            for o, a in _named_list(m.group("named")):
                ents.append({"a": a, "k": "js", "s": s, "n": o, "re": 1})
        else:
            ents.append({"a": m.group("ns") or "*", "k": "jsstar" if not m.group("ns") else "js", "s": s,
                         "n": "*", "re": 1})
    for m in _JS_REQUIRE.finditer(text):
        s = m.group("s")
        if m.group("named"):
            for o, a in _named_list(m.group("named").replace(":", " as ")):
                ents.append({"a": a, "k": "js", "s": s, "n": o})
        elif m.group("ns"):
            ents.append({"a": m.group("ns"), "k": "js", "s": s, "n": "*"})
    return ents


def parse_generic(text: str, lang: str):
    clean = strip_noise(text, lang)
    orig_lines = text.split("\n")
    nlines = len(orig_lines)
    nl = [i for i, ch in enumerate(clean) if ch == "\n"]

    def line_of(off):
        return bisect.bisect_left(nl, off) + 1

    def off_of_line(ln):
        return 0 if ln <= 1 else nl[ln - 2] + 1

    brace, paren = {}, {}
    if lang in BRACE_LANGS:
        sb, sp = [], []
        for i, ch in enumerate(clean):
            if ch == "{":
                sb.append(i)
            elif ch == "}":
                if sb:
                    brace[sb.pop()] = i
            elif ch == "(":
                sp.append(i)
            elif ch == ")":
                if sp:
                    paren[sp.pop()] = i

    found = []
    for kind, rx in _COMPILED.get(lang, []):
        for m in rx.finditer(clean):
            found.append((m.start("n"), m.start(), kind, m.group("n"), m))
    found.sort(key=lambda x: (x[1], x[0]))

    def body_range(start, name_end, kind):
        """retorna (body_start_off, end_off, sig_end_off)"""
        if lang == "rb":
            ln0 = line_of(start)
            base_indent = len(orig_lines[ln0 - 1]) - len(orig_lines[ln0 - 1].lstrip())
            end_ln = ln0
            for j in range(ln0, nlines):
                ln = orig_lines[j]
                if ln.strip() == "":
                    continue
                ind = len(ln) - len(ln.lstrip())
                if ind <= base_indent:
                    if ln.strip().startswith("end"):
                        end_ln = j + 1
                    break
                end_ln = j + 1
            return off_of_line(ln0 + 1), off_of_line(end_ln + 1) - 1 if end_ln < nlines else len(clean), off_of_line(ln0 + 1)
        pos = name_end
        if kind in ("func", "method", "arrow"):
            p = clean.find("(", name_end - 1, name_end + 400) if kind != "arrow" else -1
            if p != -1 and p in paren:
                pos = paren[p] + 1
            if kind == "arrow":
                a = clean.find("=>", name_end)
                if a != -1 and a - name_end < 300:
                    pos = a + 2
                    j = pos
                    while j < len(clean) and clean[j] in " \t\n":
                        j += 1
                    if j < len(clean) and clean[j] == "{" and j in brace:
                        return j, brace[j], j
                    e = clean.find(";", j)
                    nlp = clean.find("\n", j)
                    cands = [x for x in (e, nlp) if x != -1]
                    end = min(cands) if cands else len(clean)
                    return j, end, j
                p2 = clean.find("{", name_end, name_end + 600)
                if p2 != -1 and p2 in brace:
                    return p2, brace[p2], p2
        j = pos
        lim = min(len(clean), pos + 1500)
        while j < lim and clean[j] not in "{;":
            j += 1
        if j < lim and clean[j] == "{" and j in brace:
            return j, brace[j], j
        eol = clean.find("\n", pos)
        eol = len(clean) if eol == -1 else eol
        return None, min(max(j, pos), eol if clean[min(j, len(clean) - 1)] != ";" else j), min(j, eol)

    defs = []  # dicts com start/end offsets
    for name_off, start, kind, name, m in found:
        d = {"name": name, "kind": kind, "start": start, "name_off": name_off, "m": m}
        if kind == "method":
            if name in NOT_METHOD:
                continue
            if lang in ("java", "cs", "scala"):
                lead = clean[m.start(): name_off].split()
                lead = [w for w in lead if w not in ("public", "private", "protected", "static", "final", "abstract",
                                                      "override", "virtual", "async", "synchronized", "internal",
                                                      "open", "suspend", "native", "default", "sealed", "partial",
                                                      "readonly", "unsafe", "extern")]
                if not lead or lead[0] in NOT_TYPE_LEAD:
                    continue
        if kind == "func" and lang == "cpp" and name in NOT_METHOD:
            continue
        bs, end, sigend = body_range(start, m.end("n"), kind if kind != "type" else "class")
        d.update({"bs": bs, "end": end, "sigend": sigend})
        # metodos/funcoes C-like exigem corpo
        if kind in ("method",) or (kind == "func" and lang in ("cpp", "java")):
            if bs is None:
                continue
        defs.append(d)

    containers = [d for d in defs if d["kind"] in ("class", "impl") and d["bs"] is not None]
    for d in containers:
        d["cstart"], d["cend"] = d["bs"], d["end"]

    def container_of(off):
        best = None
        for c in containers:
            if c["cstart"] < off < c["cend"] and (best is None or c["cstart"] > best["cstart"]):
                best = c
        return best

    syms, owners = [], []
    funcs = []
    for d in sorted(defs, key=lambda x: x["start"]):
        kind = d["kind"]
        c = container_of(d["start"])
        # aninhado dentro de funcao/metodo: ignora (chamadas ficam com o dono)
        if any(f["start"] < d["start"] < f["end"] for f in funcs if f["bs"] is not None):
            continue
        if kind == "method" and c is None:
            continue
        if kind == "impl":
            continue
        parent = None
        if c is not None:
            parent = c["name"] if not (c["kind"] == "class" and c["name"] is None) else None
        sk = kind
        if kind in ("func", "arrow", "method"):
            sk = "method" if parent else "function"
            if lang == "go" and d["m"].groupdict().get("recv"):
                rt = re.findall(r"[A-Za-z_]\w*", d["m"].group("recv"))
                parent = rt[-1] if rt else None
                sk = "method" if parent else "function"
        elif kind == "type":
            sk = "type"
        else:
            sk = "class"
        q = f"{parent}.{d['name']}" if parent else d["name"]
        ln0 = line_of(d["start"])
        ln1 = line_of(max(d["end"], d["start"]))
        sig = shorten(clean[d["start"]: d["sigend"] if d["sigend"] and d["sigend"] > d["start"] else d["m"].end()], 160)
        # docstring: comentarios imediatamente acima
        doc = ""
        j = ln0 - 2
        while j >= 0 and orig_lines[j].strip().startswith("@"):
            j -= 1
        block = []
        while j >= 0 and re.match(r"\s*(//|/\*|\*|#|--)", orig_lines[j]):
            block.append(orig_lines[j])
            j -= 1
        if block:
            doc = first_line("\n".join(reversed(block)))
        sym = {"name": d["name"], "qname": q, "kind": sk, "line": ln0, "end": ln1, "sig": sig, "doc": doc, "parent": parent}
        syms.append(sym)
        d2 = dict(d, sym=sym)
        owners.append(d2)
        if kind in ("func", "arrow", "method"):
            funcs.append(d)

    raws, seen = [], set()
    for d in owners:
        sym = d["sym"]
        owner = sym["qname"]
        body_start = d["bs"] if d["bs"] is not None else d["sigend"]
        seg_start, seg_end = (body_start or d["start"]), d["end"]
        body = list(clean[seg_start: seg_end + 1])
        if d["kind"] in ("class", "impl"):  # mascara filhos
            for o in owners:
                if o is not d and seg_start < o["start"] < seg_end:
                    for i in range(max(o["start"], seg_start) - seg_start, min(o["end"] + 1, seg_end + 1) - seg_start):
                        if body[i] != "\n":
                            body[i] = " "
        body = "".join(body)
        header = clean[d["start"]: (d["bs"] if d["bs"] is not None else d["sigend"]) or d["start"]]
        for mm in _TYPE_RX.finditer(header[len(d["name"]):] if d["name"] in header else header):
            n = mm.group("n")
            if n not in KEYWORDS and n != d["name"]:
                kk = "inherit" if d["kind"] in ("class", "impl") else "ref"
                key = (owner, kk, n, None)
                if key not in seen:
                    seen.add(key)
                    raws.append((owner, kk, n, None, sym["line"]))
        for mm in _CALL_RX.finditer(body):
            n, b = mm.group("n"), mm.group("b")
            if n in KEYWORDS:
                continue
            pre = body[max(0, mm.start() - 20): mm.start()]
            if DECL_PRE.search(pre):
                continue
            key = (owner, "call", n, b)
            if key not in seen:
                seen.add(key)
                raws.append((owner, "call", n, b, line_of(seg_start + mm.start("n"))))
        for mm in _TYPE_RX.finditer(body):
            n = mm.group("n")
            if n in KEYWORDS:
                continue
            key = (owner, "ref", n, None)
            if key not in seen and (owner, "call", n, None) not in seen:
                pre = body[max(0, mm.start() - 20): mm.start()]
                if DECL_PRE.search(pre):
                    continue
                seen.add(key)
                raws.append((owner, "ref", n, None, line_of(seg_start + mm.start("n"))))
    # codigo de nivel de modulo (JS: chamadas fora de funcoes/classes)
    if lang in JSLIKE or lang in ("rb", "php"):
        mask = list(clean)
        for o in owners:
            for i in range(o["start"], min(o["end"] + 1, len(mask))):
                if mask[i] != "\n":
                    mask[i] = " "
        mod = "".join(mask)
        for mm in _CALL_RX.finditer(mod):
            n, b = mm.group("n"), mm.group("b")
            if n in KEYWORDS or n == "require":
                continue
            key = ("<module>", "call", n, b)
            if key not in seen:
                seen.add(key)
                raws.append(("<module>", "call", n, b, line_of(mm.start("n"))))
    imports = parse_js_imports(text) if lang in JSLIKE else []
    entry = bool(re.search(r"(?m)^(?:func main\(|fn main\(|public static void main|if __name__)", text))
    return {"syms": syms, "raws": raws, "imports": imports, "entry": entry, "nlines": nlines}


def parse_file(job):
    """Executado (possivelmente em outro processo). job=(abs_path, rel, lang)"""
    abs_path, rel, lang = job
    res = {"path": rel, "lang": lang, "err": None, "syms": [], "raws": [], "imports": [], "entry": False, "nlines": 0}
    try:
        with open(abs_path, "rb") as fh:
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        if lang == "py":
            out = parse_python(text, rel)
        else:
            out = parse_generic(text, lang)
        res.update(out)
    except SyntaxError as e:
        res["err"] = f"SyntaxError line {e.lineno}"
        try:  # fallback minimo: nada de simbolos, mas o arquivo continua indexado
            res["nlines"] = text.count("\n") + 1
        except Exception:
            pass
    except Exception as e:  # noqa
        res["err"] = f"{type(e).__name__}: {str(e)[:80]}"
    return res


# --------------------------------------------------------------------------- filesystem / tiers
def find_root(start: Path) -> Path:
    p = start.resolve()
    for cand in [p] + list(p.parents):
        if (cand / ".graft").is_dir() or (cand / ".git").exists():
            return cand
    return p


def list_files(root: Path):
    paths = None
    if (root / ".git").exists():
        try:
            r = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                               capture_output=True, timeout=120)
            if r.returncode == 0:
                paths = [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
        except Exception:
            paths = None
    if paths is None:
        paths = []
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in IGNORE_DIRS and not d.startswith(".git")]
            rel = os.path.relpath(dp, root).replace("\\", "/")
            for fn in fns:
                paths.append(fn if rel == "." else f"{rel}/{fn}")
    out = []
    for rel in paths:
        parts = rel.split("/")
        if any(p in IGNORE_DIRS for p in parts[:-1]):
            continue
        ext = os.path.splitext(parts[-1])[1].lower()
        lang = LANG.get(ext)
        if not lang:
            continue
        if any(fnmatch.fnmatch(parts[-1], pat) for pat in IGNORE_FILES):
            continue
        try:
            st = os.stat(root / rel)
        except OSError:
            continue
        if st.st_size > MAX_FILE_BYTES or st.st_size == 0:
            continue
        out.append((rel, lang, st.st_size, st.st_mtime_ns))
    return out


def decide_tier(n_files: int, n_bytes: int, forced: str | None):
    if forced in ("tiny", "standard", "large"):
        return forced
    if n_files <= TINY_FILES and n_bytes <= TINY_BYTES:
        return "tiny"
    if n_files > LARGE_FILES or n_bytes > LARGE_BYTES:
        return "large"
    return "standard"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, lang TEXT, mtime_ns INTEGER, size INTEGER,
  is_test INTEGER, nsym INTEGER, entry INTEGER, err TEXT, imports TEXT, nlines INTEGER);
CREATE TABLE IF NOT EXISTS symbols(id INTEGER PRIMARY KEY, fid INTEGER, name TEXT, qname TEXT, kind TEXT, line INTEGER,
  end_line INTEGER, sig TEXT, doc TEXT, parent TEXT, pr REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_sym_fid ON symbols(fid);
CREATE INDEX IF NOT EXISTS ix_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS ix_sym_qname ON symbols(qname);
CREATE TABLE IF NOT EXISTS raw(fid INTEGER, owner TEXT, kind TEXT, name TEXT, base TEXT, line INTEGER);
CREATE INDEX IF NOT EXISTS ix_raw_fid ON raw(fid);
CREATE TABLE IF NOT EXISTS edges(src INTEGER, dst INTEGER, kind TEXT, conf INTEGER, line INTEGER, fid INTEGER);
CREATE INDEX IF NOT EXISTS ix_e_src ON edges(src);
CREATE INDEX IF NOT EXISTS ix_e_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS tok(t TEXT, sid INTEGER, f INTEGER);
CREATE INDEX IF NOT EXISTS ix_tok_t ON tok(t);
CREATE INDEX IF NOT EXISTS ix_tok_sid ON tok(sid);
"""


class Ctx:
    def __init__(self, root: Path, forced_tier=None, quiet=True):
        self.root = root
        self.forced = forced_tier or os.environ.get("GRAFT_TIER")
        self.conn = None
        self.tier = None
        self.dbpath = root / ".graft" / "index.db"
        self.stats = {}
        self.persist = False

    # -- conexao
    def open(self, files):
        total = sum(f[2] for f in files)
        self.tier = decide_tier(len(files), total, self.forced)
        self.persist = self.dbpath.exists() or self.tier != "tiny"
        if self.persist:
            self.dbpath.parent.mkdir(exist_ok=True)
            gi = self.dbpath.parent / ".gitignore"
            if not gi.exists():
                gi.write_text("*\n", encoding="utf-8")
            self.conn = sqlite3.connect(str(self.dbpath))
        else:
            self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA journal_mode=WAL" if self.persist else "PRAGMA journal_mode=MEMORY")
        self.conn.execute("PRAGMA synchronous=OFF")
        ver = None
        try:
            ver = self.conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        except sqlite3.OperationalError:
            pass
        if ver is None or ver[0] != str(SCHEMA):
            for t in ("meta", "files", "symbols", "raw", "edges", "tok"):
                self.conn.execute(f"DROP TABLE IF EXISTS {t}")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES('schema',?)", (str(SCHEMA),))
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES('tier',?)", (self.tier,))
        self.conn.commit()

    # -- indexacao incremental
    def refresh(self):
        t0 = time.time()
        files = list_files(self.root)
        self.open(files)
        c = self.conn
        have = {r[1]: (r[0], r[2], r[3]) for r in c.execute("SELECT id,path,mtime_ns,size FROM files")}
        cur = {f[0]: f for f in files}
        removed = [p for p in have if p not in cur]
        changed = [f for f in files if f[0] not in have or have[f[0]][1] != f[3] or have[f[0]][2] != f[2]]
        for p in removed:
            self._delete_file(have[p][0])
        n_parse = len(changed)
        if changed:
            jobs = [(str(self.root / f[0]), f[0], f[1]) for f in changed]
            results = None
            if self.tier == "large" or len(jobs) >= PARALLEL_MIN:
                try:
                    with cf.ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
                        results = list(ex.map(parse_file, jobs, chunksize=16))
                except Exception:
                    results = None
            if results is None:
                results = [parse_file(j) for j in jobs]
            meta = {f[0]: f for f in changed}
            for res in results:
                f = meta[res["path"]]
                if res["path"] in have:
                    self._delete_file(have[res["path"]][0])
                self._insert_file(f, res)
            c.commit()
        if changed or removed or c.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0 and \
                c.execute("SELECT COUNT(*) FROM raw").fetchone()[0] > 0:
            relink(c)
        self.stats = {"files": len(files), "parsed": n_parse, "removed": len(removed), "secs": time.time() - t0}
        return self.stats

    def _delete_file(self, fid):
        c = self.conn
        c.execute("DELETE FROM tok WHERE sid IN (SELECT id FROM symbols WHERE fid=?)", (fid,))
        c.execute("DELETE FROM symbols WHERE fid=?", (fid,))
        c.execute("DELETE FROM raw WHERE fid=?", (fid,))
        c.execute("DELETE FROM files WHERE id=?", (fid,))

    def _insert_file(self, f, res):
        c = self.conn
        rel, lang, size, mtime = f
        is_test = 1 if is_test_path(rel) else 0
        base = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        entry = 1 if (res["entry"] or base in ("main", "__main__", "index", "app", "cli", "server")) else 0
        cur = c.execute("INSERT INTO files(path,lang,mtime_ns,size,is_test,nsym,entry,err,imports,nlines) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (rel, lang, mtime, size, is_test, len(res["syms"]), entry, res["err"],
                         json.dumps(res["imports"]), res.get("nlines", 0)))
        fid = cur.lastrowid
        path_toks = []
        for part in rel.rsplit(".", 1)[0].split("/"):
            path_toks += split_ident(part)
        syms = [{"name": rel.rsplit("/", 1)[-1], "qname": "<module>", "kind": "module", "line": 1,
                 "end": res.get("nlines", 1), "sig": rel, "doc": "", "parent": None}] + res["syms"]
        toks = []
        for s in syms:
            cur = c.execute("INSERT INTO symbols(fid,name,qname,kind,line,end_line,sig,doc,parent) VALUES(?,?,?,?,?,?,?,?,?)",
                            (fid, s["name"], s["qname"], s["kind"], s["line"], s["end"], s["sig"], s["doc"], s["parent"]))
            sid = cur.lastrowid
            seen = set()

            def add(t, w):
                if t and (t, w) not in seen and len(t) > 1:
                    seen.add((t, w))
                    toks.append((t, sid, w))
            nt = split_ident(s["name"])
            add(s["name"].lower(), 0)
            for t in nt:
                add(t, 0)
                add(stem(t), 0)
            if s["parent"]:
                for t in split_ident(s["parent"]):
                    add(t, 1)
                    add(stem(t), 1)
            for t in path_toks:
                add(t, 1)
                add(stem(t), 1)
            for w in re.findall(r"[A-Za-z]{3,}", s["doc"] or ""):
                for t in split_ident(w):
                    add(t, 2)
                    add(stem(t), 2)
            if s["kind"] in ("function", "method", "class"):
                for w in re.findall(r"[A-Za-z_]{4,}", s["sig"].split("(", 1)[-1]):
                    for t in split_ident(w):
                        add(t, 3)
        c.executemany("INSERT INTO tok VALUES(?,?,?)", toks)
        c.executemany("INSERT INTO raw VALUES(?,?,?,?,?,?)", [(fid, r[0], r[1], r[2], r[3], r[4]) for r in res["raws"]])


# --------------------------------------------------------------------------- resolucao (linking)
class Resolver:
    def __init__(self, conn):
        c = conn
        self.fpath, self.lang, self.istest = {}, {}, {}
        self.imports = {}
        for fid, path, lang, imps, it in c.execute("SELECT id,path,lang,imports,is_test FROM files"):
            self.fpath[fid], self.lang[fid], self.istest[fid] = path, lang, it
            self.imports[fid] = json.loads(imps or "[]")
        self.pathid = {p: f for f, p in self.fpath.items()}
        self.alias = {fid: collections.defaultdict(list) for fid in self.fpath}
        for fid, ents in self.imports.items():
            for e in ents:
                self.alias[fid][e["a"]].append(e)
        self.sym = {}  # sid -> (fid, name, qname, kind, parent)
        self.top = collections.defaultdict(lambda: collections.defaultdict(list))  # fid -> name -> [sid]
        self.cls_methods = collections.defaultdict(lambda: collections.defaultdict(list))  # (fid,cls) -> name -> [sid]
        self.by_top = collections.defaultdict(list)
        self.by_method = collections.defaultdict(list)
        self.mod_sid = {}
        self.classes = collections.defaultdict(list)  # (fid, qname) -> sid
        for sid, fid, name, qname, kind, parent in c.execute("SELECT id,fid,name,qname,kind,parent FROM symbols"):
            self.sym[sid] = (fid, name, qname, kind, parent)
            if kind == "module":
                self.mod_sid[fid] = sid
            elif kind == "method" or (parent and kind != "class"):
                self.cls_methods[(fid, parent)][name].append(sid)
                self.by_method[name].append(sid)
            elif kind in ("function", "class", "const", "type"):
                self.top[fid][name].append(sid)
                self.by_top[name].append(sid)
                if kind == "class":
                    self.classes[(fid, qname)].append(sid)
            if kind == "class" and parent:
                self.classes[(fid, qname)].append(sid)
        # indice de modulos python por sufixo
        self.pysuffix = collections.defaultdict(list)
        for fid, p in self.fpath.items():
            if self.lang[fid] == "py":
                comps = p[:-3].split("/") if p.endswith(".py") else p.split("/")
                if comps and comps[-1] == "__init__":
                    comps = comps[:-1]
                for k in range(len(comps)):
                    self.pysuffix[".".join(comps[k:])].append(fid)
        self.inherits = collections.defaultdict(list)  # (fid, cls) -> [(name, base)]
        self._memo_chain = {}
        self.dir_of = {fid: p.rsplit("/", 1)[0] if "/" in p else "" for fid, p in self.fpath.items()}

    # -- modulos
    def py_mod(self, dotted, level, fpath):
        if level:
            base = PurePosixPath(fpath).parent
            for _ in range(level - 1):
                base = base.parent
            parts = [p for p in dotted.split(".") if p] if dotted else []
            cand = str(base.joinpath(*parts)).replace("\\", "/") if parts else str(base).replace("\\", "/")
            cand = "" if cand == "." else cand
            for c in ((cand + ".py") if cand else None, (cand + "/__init__.py") if cand else "__init__.py"):
                if c and c in self.pathid:
                    return [self.pathid[c]], True
            return [], True
        f = self.pysuffix.get(dotted)
        if f:
            return list(f), True
        parts = dotted.split(".")
        if parts[0] in STDLIB:
            return [], True
        for i in range(1, len(parts)):
            f = self.pysuffix.get(".".join(parts[i:]))
            if f and len(f) == 1:
                return list(f), False
        return [], True

    def js_mod(self, spec, fpath):
        exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
        cands = []
        if spec.startswith("."):
            base = PurePosixPath(fpath).parent
            joined = os.path.normpath(str(base / spec)).replace("\\", "/")
            cands.append(joined)
            if re.search(r"\.(m|c)?js$", joined):
                cands.append(re.sub(r"\.(m|c)?js$", "", joined))
            for c in list(cands):
                for e in exts:
                    cands.append(c + e)
                    cands.append(c + "/index" + e)
            for c in cands:
                if c in self.pathid:
                    return [self.pathid[c]], True
            return [], True
        s = re.sub(r"^(@/|~/|\.\./)+", "", spec)
        if "/" not in s and not spec.startswith(("@/", "~/")):
            return [], True  # pacote externo
        s = s.lstrip("@")
        hits = [fid for p, fid in self.pathid.items()
                if self.lang[fid] in JSLIKE and re.sub(r"\.[a-z]+$", "", p).endswith(s)
                or any(p.endswith(s + e) or p.endswith(s + "/index" + e) for e in exts)]
        hits = list(dict.fromkeys(hits))
        return (hits, False) if len(hits) == 1 else ([], True)

    def find_in(self, fid, name, depth=0):
        d = self.top[fid].get(name)
        if d:
            return list(d)
        if depth < 3:
            for e in self.imports.get(fid, []):
                if e["k"] == "from" and e["a"] == name:
                    fids, _ = self.py_mod(e["m"], e["l"], self.fpath[fid])
                    for f2 in fids:
                        r = self.find_in(f2, e["n"], depth + 1)
                        if r:
                            return r
                elif e["k"] == "js" and e.get("re") and e["a"] == name:
                    fids, _ = self.js_mod(e["s"], self.fpath[fid])
                    for f2 in fids:
                        r = self.find_in(f2, e["n"], depth + 1)
                        if r:
                            return r
                elif e["k"] == "jsstar":
                    fids, _ = self.js_mod(e["s"], self.fpath[fid])
                    for f2 in fids:
                        r = self.find_in(f2, name, depth + 1)
                        if r:
                            return r
                elif e["k"] == "star":
                    fids, _ = self.py_mod(e["m"], e["l"], self.fpath[fid])
                    for f2 in fids:
                        r = self.find_in(f2, name, depth + 1)
                        if r:
                            return r
        return []

    def import_target(self, fid, e, name):
        """alias (bare) -> [(sid, conf)]"""
        lang = self.lang[fid]
        res = []
        if e["k"] == "from":
            fids, exact = self.py_mod(e["m"], e["l"], self.fpath[fid])
            for f2 in fids:
                for s in self.find_in(f2, e["n"]):
                    res.append((s, 3 if exact else 2))
        elif e["k"] == "js" and lang in JSLIKE:
            fids, exact = self.js_mod(e["s"], self.fpath[fid])
            for f2 in fids:
                if e["n"] == "*":
                    continue
                tgt = self.find_in(f2, e["n"] if e["n"] != "default" else e["a"])
                if not tgt and e["n"] == "default":
                    tops = [s for d in self.top[f2].values() for s in d if self.sym[s][3] in ("class", "function")]
                    tgt = tops if len(tops) == 1 else []
                for s in tgt:
                    res.append((s, 3 if exact else 2))
        return res

    def class_chain(self, fid, cls, depth=0):
        key = (fid, cls)
        if key in self._memo_chain:
            return self._memo_chain[key]
        self._memo_chain[key] = []
        out = []
        if depth < 4:
            for (name, base) in self.inherits.get(key, []):
                for sid, conf in self.res(fid, None, "ref", name, base, None):
                    sf, sn, sq, sk, sp = self.sym[sid]
                    if sk == "class":
                        out.append((sf, sq))
                        out.extend(self.class_chain(sf, sq, depth + 1))
        self._memo_chain[key] = out
        return out

    def pick_global(self, fid, name, methods):
        cands = self.by_method.get(name, []) if methods else self.by_top.get(name, [])
        if methods and name in COMMON_ATTRS:
            return []
        cands = [s for s in cands if self.sym[s][3] != "const"] or []
        if not cands:
            return []
        if len(cands) == 1:
            return [(cands[0], 2)]
        same = [s for s in cands if self.dir_of[self.sym[s][0]] == self.dir_of[fid]]
        if len(same) == 1:
            return [(same[0], 2)]
        if len(cands) <= 3:
            return [(s, 1) for s in cands]
        return []

    def res(self, fid, owner_cls, kind, name, base, owner_qname):
        lang = self.lang[fid]
        alias = self.alias[fid]
        out = []
        if base is None:
            for e in alias.get(name, []):
                out += self.import_target(fid, e, name)
            if out:
                return out
            if owner_cls:  # chamada/ref implicita no escopo da classe (Java, Kotlin, Rust...)
                if lang not in ("py",):
                    for s in self.cls_methods[(fid, owner_cls)].get(name, []):
                        out.append((s, 3))
                    if out:
                        return out
            d = self.top[fid].get(name)
            if d:
                return [(s, 3) for s in d]
            if lang in SAME_DIR_LANGS:
                same = [s for s in self.by_top.get(name, []) if self.dir_of[self.sym[s][0]] == self.dir_of[fid]]
                if len(same) == 1:
                    return [(same[0], SAME_DIR_LANGS[lang])]
            return self.pick_global(fid, name, False)
        head = base.split(".")[0]
        if base in ("self", "cls", "this", "super") or (lang == "rs" and base == "Self"):
            if owner_cls:
                chain = [(fid, owner_cls)] if base != "super" else []
                chain += self.class_chain(fid, owner_cls)
                for i, (cf_, cq) in enumerate(chain):
                    hit = self.cls_methods[(cf_, cq)].get(name, [])
                    if hit:
                        return [(s, 3 if i == 0 or True else 2) for s in hit]
                    ct = self.top[cf_].get(name) if lang not in ("py",) else None
            return self.pick_global(fid, name, True)
        # alias de modulo / namespace / classe importada
        for key in (base, head):
            for e in alias.get(key, []):
                if e["k"] == "mod":
                    dotted = e["m"] + (base[len(key):] if key == head and key != base and e["a"] == head else "")
                    fids, exact = self.py_mod(dotted, 0, self.fpath[fid])
                    for f2 in fids:
                        for s in self.find_in(f2, name):
                            out.append((s, 3 if exact else 2))
                elif e["k"] == "from":
                    fids, exact = self.py_mod(((e["m"] + ".") if e["m"] else "") + e["n"], e["l"], self.fpath[fid]) \
                        if not e["l"] else self.py_mod(e["n"] if not e["m"] else e["m"] + "." + e["n"], e["l"], self.fpath[fid])
                    for f2 in fids:
                        for s in self.find_in(f2, name):
                            out.append((s, 3 if exact else 2))
                    if not out:  # classe importada: Cls.metodo
                        for s, conf in self.import_target(fid, e, key):
                            sf, sn, sq, sk, sp = self.sym[s]
                            if sk == "class":
                                for m_ in self.cls_methods[(sf, sq)].get(name, []):
                                    out.append((m_, conf))
                elif e["k"] == "js":
                    fids, exact = self.js_mod(e["s"], self.fpath[fid])
                    for f2 in fids:
                        if e["n"] == "*" or e["n"] == "default":
                            for s in self.find_in(f2, name):
                                out.append((s, 3 if exact else 2))
                        for s, conf in self.import_target(fid, e, key):
                            sf, sn, sq, sk, sp = self.sym[s]
                            if sk == "class":
                                for m_ in self.cls_methods[(sf, sq)].get(name, []):
                                    out.append((m_, conf))
                if out:
                    return out
        # classe local: Cls.metodo
        for (cf_, cq), sids in self.classes.items():
            pass
        if (fid, head) in self.classes and base == head:
            hit = self.cls_methods[(fid, head)].get(name, [])
            if hit:
                return [(s, 3) for s in hit]
        return self.pick_global(fid, name, True)


def relink(conn):
    c = conn
    c.execute("DELETE FROM edges")
    R = Resolver(c)
    for fid, owner, kind, name, base in c.execute("SELECT fid,owner,kind,name,base FROM raw WHERE kind='inherit'"):
        R.inherits[(fid, owner)].append((name, base))
    owner_sid = {}
    for sid, (fid, name, qname, kind, parent) in R.sym.items():
        owner_sid[(fid, qname)] = sid
    edges = {}
    rows = c.execute("SELECT fid,owner,kind,name,base,line FROM raw").fetchall()
    for fid, owner, kind, name, base, line in rows:
        src = R.mod_sid.get(fid) if owner == "<module>" else owner_sid.get((fid, owner))
        if src is None:
            continue
        cls = None
        if owner != "<module>":
            fsym = R.sym[src]
            cls = fsym[4] if fsym[3] == "method" else (owner if fsym[3] == "class" else fsym[4])
        for dst, conf in R.res(fid, cls, kind, name, base, owner):
            if dst == src:
                continue
            k = (src, dst, kind)
            old = edges.get(k)
            if old is None:
                edges[k] = [conf, line, fid]
            else:
                old[0] = max(old[0], conf)
                old[1] = min(old[1], line)
    c.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?)", [(k[0], k[1], k[2], v[0], v[1], v[2]) for k, v in edges.items()])
    c.commit()
    compute_pagerank(c)


def compute_pagerank(conn, personal: dict | None = None, store=True, iters=40):
    c = conn
    ids = [r[0] for r in c.execute("SELECT id FROM symbols")]
    n = len(ids)
    if n == 0:
        return {}
    idx = {s: i for i, s in enumerate(ids)}
    W = {3: 1.0, 2: 0.5, 1: 0.2}
    out_w = [0.0] * n
    E = []
    for s, t, conf, kind in c.execute("SELECT src,dst,conf,kind FROM edges"):
        if s in idx and t in idx:
            w = W.get(conf, 0.2) * (1.0 if kind == "call" else 0.7)
            E.append((idx[s], idx[t], w))
            out_w[idx[s]] += w
    if personal:
        tot = sum(personal.values()) or 1.0
        base = [0.0] * n
        for s, w in personal.items():
            if s in idx:
                base[idx[s]] = w / tot
        if sum(base) == 0:
            base = [1.0 / n] * n
    else:
        base = [1.0 / n] * n
    d = 0.85
    pr = list(base)
    for _ in range(iters):
        dangling = sum(pr[i] for i in range(n) if out_w[i] == 0)
        new = [(1 - d) * base[i] + d * dangling * base[i] for i in range(n)]
        for s, t, w in E:
            new[t] += d * pr[s] * w / out_w[s]
        delta = sum(abs(new[i] - pr[i]) for i in range(n))
        pr = new
        if delta < 1e-7:
            break
    mx = max(pr) or 1.0
    norm = {ids[i]: pr[i] / mx for i in range(n)}
    if store:
        c.executemany("UPDATE symbols SET pr=? WHERE id=?", [(v, k) for k, v in norm.items()])
        c.commit()
    return norm


# --------------------------------------------------------------------------- saida / orcamento
class Out:
    def __init__(self, budget: int):
        self.budget = budget
        self.lines = []
        self.used = 0
        self.truncated = 0
        self.files = set()

    def add(self, line: str, files=(), force=False):
        t = est_tokens(line) + 1
        if not force and self.used + t > self.budget:
            self.truncated += 1
            return False
        self.lines.append(line)
        self.used += t
        self.files.update(files)
        return True

    def text(self, hint: str = ""):
        res = list(self.lines)
        if self.truncated:
            res.append(f"[+{self.truncated} omitted by --budget {self.budget}; {hint or 'raise --budget, add --scope DIR, or refine the query'}]")
        return "\n".join(res)


def sym_row(c, sid):
    return c.execute("SELECT s.id,s.name,s.qname,s.kind,s.line,s.end_line,s.sig,s.doc,s.parent,s.pr,f.path,f.is_test,f.id "
                     "FROM symbols s JOIN files f ON f.id=s.fid WHERE s.id=?", (sid,)).fetchone()


def label(r, with_loc=True):
    sid, name, qname, kind, line, end, sig, doc, parent, pr, path, is_test, fid = r
    if kind == "module":
        return f"{path} [module-level]"
    return f"{qname}  {path}:{line}" if with_loc else qname


def in_scope(path, scopes):
    if not scopes:
        return True
    return any(path == s or path.startswith(s.rstrip("/") + "/") or fnmatch.fnmatch(path, s) for s in scopes)


def parse_scopes(s):
    return [x.strip().replace("\\", "/") for x in (s or "").split(",") if x.strip()]


def read_lines(root: Path, rel: str, a: int, b: int):
    try:
        with open(root / rel, "r", encoding="utf-8", errors="replace", newline="") as fh:
            lines = fh.read().replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return lines[a - 1: b]
    except OSError:
        return []


# --------------------------------------------------------------------------- resolucao de simbolo (CLI)
def lookup(c, spec, scopes=None):
    spec = spec.strip().replace("\\", "/")
    rows = []
    m = re.match(r"^(.*?):(\d+)$", spec)
    if m and "/" in m.group(1) or (m and "." in m.group(1)):
        path, ln = m.group(1), int(m.group(2))
        for r in c.execute("SELECT s.id FROM symbols s JOIN files f ON f.id=s.fid WHERE f.path LIKE ? AND s.line<=? AND s.end_line>=? "
                           "AND s.kind!='module' ORDER BY (s.end_line-s.line) ASC LIMIT 1", ("%" + path, ln, ln)):
            rows.append(r[0])
        return rows
    path_part, sym = (None, spec)
    if ":" in spec:
        path_part, sym = spec.rsplit(":", 1)
    base_q = "SELECT s.id FROM symbols s JOIN files f ON f.id=s.fid WHERE s.kind!='module' AND "
    args, where = [], ""
    if path_part:
        where = "f.path LIKE ? AND "
        args.append("%" + path_part)
    ids = [r[0] for r in c.execute(base_q + where + "s.qname=?", args + [sym])]
    if not ids:
        ids = [r[0] for r in c.execute(base_q + where + "s.name=?", args + [sym])]
    if not ids and not path_part:  # sufixo Classe.metodo
        ids = [r[0] for r in c.execute(base_q + "s.qname LIKE ?", ["%." + sym])]
    if not ids:  # sem parenteses: build() -> build
        sym2 = sym.rstrip("()")
        if sym2 != sym:
            ids = [r[0] for r in c.execute(base_q + where + "s.name=?", args + [sym2])]
    if scopes:
        ids = [i for i in ids if in_scope(sym_row(c, i)[10], scopes)]
    # ordena por pagerank (mais central primeiro), codigo antes de teste
    ids.sort(key=lambda i: (sym_row(c, i)[11], -sym_row(c, i)[9]))
    return ids


# --------------------------------------------------------------------------- comandos
def cmd_index(ctx, a):
    st = ctx.stats
    c = ctx.conn
    nsym = c.execute("SELECT COUNT(*) FROM symbols WHERE kind!='module'").fetchone()[0]
    ne = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    where = ".graft/index.db" if ctx.persist else "memory (tiny project, nothing written)"
    return (f"tier={ctx.tier} files={st['files']} symbols={nsym} edges={ne} reparsed={st['parsed']} "
            f"removed={st['removed']} in {st['secs']:.1f}s -> {where}")


def cmd_map(ctx, a):
    c = ctx.conn
    scopes = parse_scopes(a.scope)
    default = {"tiny": 3000, "standard": 1800, "large": 1400}[ctx.tier]
    out = Out(a.budget or default)
    pers = None
    if a.focus:
        pers = {}
        for f in parse_scopes(a.focus):
            for r in c.execute("SELECT s.id FROM symbols s JOIN files f ON f.id=s.fid WHERE f.path LIKE ? OR s.qname=? OR s.name=?",
                               ("%" + f + "%", f, f)):
                pers[r[0]] = 1.0
        pr = compute_pagerank(c, pers or None, store=False)
    else:
        pr = None
    langs = collections.Counter(r[0] for r in c.execute("SELECT lang FROM files"))
    nsym = c.execute("SELECT COUNT(*) FROM symbols WHERE kind!='module'").fetchone()[0]
    out.add(f"# graft map — tier={ctx.tier}, {sum(langs.values())} files ({', '.join(f'{k}:{v}' for k, v in langs.most_common(4))}), {nsym} symbols" +
            (f", focus={a.focus}" if a.focus else ""), force=True)
    frows = c.execute("SELECT f.id,f.path,f.entry,f.is_test,f.nsym,f.lang FROM files f").fetchall()
    frows = [r for r in frows if in_scope(r[1], scopes)]
    fscore = {}
    sym_by_file = collections.defaultdict(list)
    for sid, fid, name, qname, kind, line, sig, pr0, parent in c.execute(
            "SELECT id,fid,name,qname,kind,line,sig,pr,parent FROM symbols WHERE kind!='module'"):
        sym_by_file[fid].append((pr[sid] if pr else pr0, sid, qname, kind, line, sig, parent))
    for r in frows:
        sy = sym_by_file.get(r[0], [])
        fscore[r[0]] = sum(x[0] for x in sy) * (0.4 if r[3] else 1.0) + (0.5 if r[2] else 0)
    frows.sort(key=lambda r: -fscore[r[0]])
    if ctx.tier == "large" and not scopes:  # nivel diretorio primeiro
        dcount = collections.defaultdict(lambda: [0, 0.0])
        for r in frows:
            d = r[1].rsplit("/", 1)[0] if "/" in r[1] else "."
            d = "/".join(d.split("/")[:2])
            dcount[d][0] += 1
            dcount[d][1] += fscore[r[0]]
        out.add("## directories (by centrality) — use --scope DIR to zoom in")
        for d, (n, sc) in sorted(dcount.items(), key=lambda x: -x[1][1])[:25]:
            out.add(f"  {d}/  {n} files")
    show_all = ctx.tier == "tiny" and not a.focus
    out.add("## files (most central first)" if not show_all else "## all files")
    for r in frows:
        fid, path, entry, is_test, ns, lang = r
        sy = sorted(sym_by_file.get(fid, []), key=lambda x: -x[0] if not show_all else x[4])
        if not sy and not show_all:
            continue
        head = f"{path}{' *entry' if entry else ''}{' (test)' if is_test else ''}"
        if not out.add(head, files=[path]):
            break
        limit = len(sy) if show_all else (8 if ctx.tier != "large" else 5)
        picked = sy[:limit]
        picked.sort(key=lambda x: x[4])
        for pr_, sid, qname, kind, line, sig, parent in picked:
            if kind in ("class", "function", "method", "type") or show_all:
                brief = re.sub(r"^(async def|def|class)\s+", "", sig)
                if not out.add(f"  {line}: {shorten(brief, 90)}"):
                    break
        if len(sy) > limit:
            out.add(f"  … +{len(sy) - limit} more (graft find / show {path})")
    return out.text("zoom with --scope DIR or --focus FILE|SYMBOL, or raise --budget"), out.files


def _score(c, question, scopes, want_test=False):
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
    toks = []
    for w in raw:
        parts = split_ident(w)
        low = w.lower()
        cand = [low] + parts if len(parts) > 1 else parts or [low]
        for t in cand:
            if t in STOP or len(t) < 2:
                continue
            toks.append(t)
    toks = list(dict.fromkeys(toks))
    qset = list(dict.fromkeys(toks + [stem(t) for t in toks if stem(t) != t]))
    if not qset:
        return [], toks, {}
    N = c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] or 1
    ph = ",".join("?" * len(qset))
    df = dict(c.execute(f"SELECT t,COUNT(DISTINCT sid) FROM tok WHERE t IN ({ph}) GROUP BY t", qset).fetchall())
    W = {0: 3.0, 1: 1.4, 2: 1.0, 3: 0.6}
    per = collections.defaultdict(dict)
    for t, sid, f in c.execute(f"SELECT t,sid,f FROM tok WHERE t IN ({ph})", qset):
        idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
        sc = idf * W[f]
        key = stem(t) if stem(t) in toks or t in toks else t
        # agrupa token e seu stem como o mesmo termo
        term = t if t in toks else next((x for x in toks if stem(x) == t), t)
        if sc > per[sid].get(term, 0):
            per[sid][term] = sc
    scored = []
    for sid, d in per.items():
        r = sym_row(c, sid)
        if not in_scope(r[10], scopes):
            continue
        base = sum(d.values())
        cover = len(d) / max(1, len(toks))
        s = base * (0.5 + cover) ** 1.5
        s *= {"class": 1.0, "function": 1.0, "method": 0.95, "type": 0.9, "const": 0.6, "module": 0.55}.get(r[3], 0.8)
        if r[11] and not want_test:
            s *= 0.6
        s *= 1 + 0.35 * r[9]
        if r[1].lower() in toks or r[2].lower() in toks:
            s *= 1.6
        scored.append((s, sid, cover))
    scored.sort(key=lambda x: -x[0])
    return scored, toks, df


def fmt_find(r, rank=None):
    sid, name, qname, kind, line, end, sig, doc, parent, pr, path, is_test, fid = r
    if kind == "module":
        return f"{path}  [file]"
    s = shorten(sig, 110) if kind != "const" else sig
    d = f"  # {doc}" if doc else ""
    return f"{path}:{line}  {s}{d}"


def cmd_find(ctx, a):
    c = ctx.conn
    scopes = parse_scopes(a.scope)
    out = Out(a.budget or 1200)
    q = " ".join(a.query)
    exact = lookup(c, q, scopes) if re.fullmatch(r"[\w.:/()\\-]+", q) else []
    scored, toks, _ = _score(c, q, scopes, want_test="test" in toks_of(q))
    seen, n = set(), 0
    results = []
    for sid in exact[:a.limit]:
        results.append(sid)
        seen.add(sid)
    for s, sid, cov in scored:
        if sid in seen:
            continue
        r = sym_row(c, sid)
        if a.kind and r[3] != a.kind:
            continue
        if r[3] == "module" and not a.kind and cov < 1:
            continue
        results.append(sid)
        seen.add(sid)
        if len(results) >= a.limit:
            break
    if a.kind:
        results = [i for i in results if sym_row(c, i)[3] == a.kind]
    if not results:
        return "no match. Try fewer/different words, an identifier, or grep for literals (strings, config keys).", set()
    for sid in results:
        r = sym_row(c, sid)
        out.add(fmt_find(r), files=[r[10]])
    return out.text("raise --limit/--budget or narrow with --scope/--kind"), out.files


def toks_of(q):
    return [w.lower() for w in re.findall(r"[A-Za-z]+", q)]


def callers_of(c, sid, kinds=("call", "ref", "inherit")):
    ph = ",".join("?" * len(kinds))
    return c.execute(f"SELECT src,conf,line,kind FROM edges WHERE dst=? AND kind IN ({ph}) ORDER BY conf DESC, line", (sid, *kinds)).fetchall()


def callees_of(c, sid, kinds=("call", "ref", "inherit")):
    ph = ",".join("?" * len(kinds))
    return c.execute(f"SELECT dst,conf,line,kind FROM edges WHERE src=? AND kind IN ({ph}) ORDER BY conf DESC, line", (sid, *kinds)).fetchall()


def _short(c, sid):
    r = sym_row(c, sid)
    return label(r, with_loc=False) if r[3] != "module" else f"{r[10]}[module]"


def cmd_ask(ctx, a):
    c = ctx.conn
    scopes = parse_scopes(a.scope)
    q = " ".join(a.question)
    out = Out(a.budget or 2200)
    scored, toks, df = _score(c, q, scopes, want_test="test" in toks_of(q))
    scored = [x for x in scored if sym_row(c, x[1])[3] != "module" or x[2] >= 0.99][:12]
    if not scored:
        return ("match: none — no indexed symbol matches. The answer may live in strings/config/docs: use grep with "
                "concrete keywords (" + ", ".join(toks[:5]) + ").", set())
    top = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0
    strong = top[2] >= 0.66 and top[0] >= 1.25 * second
    medium = top[2] >= 0.5
    quality = "strong" if strong else "medium" if medium else "weak"
    out.add(f"match: {quality} (terms: {', '.join(toks[:8])})", force=True)
    if quality == "weak":
        out.add("hint: weak match — also try grep for literals; candidates below are best guesses.", force=True)
    out.add("## candidates")
    for s, sid, cov in scored[:8]:
        r = sym_row(c, sid)
        out.add(fmt_find(r), files=[r[10]])
    nshow = 1 if strong else 2 if medium else 0
    if a.code:
        nshow = a.code
    if nshow:
        out.add("## code (top matches)")
    shown = 0
    for s, sid, cov in scored:
        if shown >= nshow:
            break
        r = sym_row(c, sid)
        if r[3] in ("module",):
            continue
        sid_, name, qname, kind, line, end, sig, doc, parent, pr, path, is_test, fid = r
        # funcoes longas: so o cabecalho (assinatura+docstring+inicio); o agente pede o resto com `show`
        cap = a.lines or (end - line + 1 if end - line + 1 <= 60 else 22)
        lines = read_lines(ctx.root, path, line, min(end, line + cap - 1))
        callers = [x for x in callers_of(c, sid) if x[1] >= 2][:5]
        callees = [x for x in callees_of(c, sid) if x[1] >= 2][:6]
        out.add(f"--- {qname}  {path}:{line}-{end}", files=[path])
        for i, ln in enumerate(lines):
            if not out.add(f"{line + i:>5}  {ln}"):
                break
        if end - line + 1 > cap:
            out.add(f"      … {end - line + 1 - cap} more lines — graft show {qname} --lines {end - line + 1}")
        if callers:
            out.add("  called by: " + ", ".join(dict.fromkeys(_short(c, x[0]) for x in callers)))
        if callees:
            out.add("  calls: " + ", ".join(dict.fromkeys(_short(c, x[0]) for x in callees)))
        shown += 1
    return out.text("raise --budget, or use graft show <symbol> for one body"), out.files


def cmd_show(ctx, a):
    c = ctx.conn
    scopes = parse_scopes(a.scope)
    out = Out(a.budget or 2500)
    spec = a.symbol
    # arquivo -> outline
    frow = c.execute("SELECT id,path,lang,nlines,is_test FROM files WHERE path=? OR path LIKE ?", (spec, "%/" + spec)).fetchall()
    if frow and not re.search(r"[:()]", spec) and len(frow) == 1:
        fid, path, lang, nl, it = frow[0]
        out.add(f"{path}  ({lang}, {nl} lines){' [test]' if it else ''}", files=[path])
        for sid, qname, kind, line, end, sig, doc in c.execute(
                "SELECT id,qname,kind,line,end_line,sig,doc FROM symbols WHERE fid=? AND kind!='module' ORDER BY line", (fid,)):
            out.add(f"  {line}-{end}: {shorten(sig, 100)}" + (f"  # {doc}" if doc else ""))
        deps = c.execute("SELECT DISTINCT f.path FROM edges e JOIN symbols s ON s.id=e.dst JOIN symbols s2 ON s2.id=e.src "
                         "JOIN files f ON f.id=s2.fid WHERE s.fid=? AND s2.fid!=?", (fid, fid)).fetchall()
        if deps:
            out.add("  used by files: " + ", ".join(d[0] for d in deps[:12]) + (f" (+{len(deps) - 12})" if len(deps) > 12 else ""))
        return out.text("raise --budget"), out.files
    ids = lookup(c, spec, scopes)
    if not ids:
        return f"symbol not found: {spec}. Try: graft find {spec}", set()
    sid = ids[0]
    r = sym_row(c, sid)
    _, name, qname, kind, line, end, sig, doc, parent, pr, path, is_test, fid = r
    cap = a.lines or 80
    out.add(f"{qname}  {path}:{line}-{end}  ({kind})", files=[path], force=True)
    if len(ids) > 1:
        others = [label(sym_row(c, i)) for i in ids[1:6]]
        out.add("also matches: " + " | ".join(others) + "  (disambiguate with FILE:NAME)", force=True)
    lines = read_lines(ctx.root, path, line, min(end, line + cap - 1))
    for i, ln in enumerate(lines):
        if not out.add(f"{line + i:>5}  {ln}"):
            break
    if end - line + 1 > cap:
        out.add(f"      … {end - line + 1 - cap} more lines — rerun with --lines {end - line + 1}")
    cs = callers_of(c, sid)
    ce = callees_of(c, sid)
    if cs:
        out.add(f"## callers ({len(cs)})")
        for src, conf, ln, k in cs[:a.max_edges]:
            out.add(f"  {'' if conf == 3 else '~' if conf == 2 else '?'}{_short(c, src)}  {sym_row(c, src)[10]}:{ln} [{k}]")
        if len(cs) > a.max_edges:
            out.add(f"  … +{len(cs) - a.max_edges} more: graft callers {qname}")
    if ce:
        out.add(f"## calls ({len(ce)})")
        for dst, conf, ln, k in ce[:a.max_edges]:
            rr = sym_row(c, dst)
            out.add(f"  {'' if conf == 3 else '~' if conf == 2 else '?'}{_short(c, dst)}  {rr[10]}:{rr[4]}")
        if len(ce) > a.max_edges:
            out.add(f"  … +{len(ce) - a.max_edges} more: graft callees {qname}")
    return out.text("raise --budget/--lines"), out.files


def _edge_cmd(ctx, a, direction):
    c = ctx.conn
    scopes = parse_scopes(a.scope)
    out = Out(a.budget or 1500)
    ids = lookup(c, a.symbol, scopes)
    if not ids:
        return f"symbol not found: {a.symbol}. Try: graft find {a.symbol}", set()
    if len(ids) > 1 and not a.all:
        out.add(f"note: {len(ids)} symbols match '{a.symbol}'; showing the most central. Others: " +
                " | ".join(label(sym_row(c, i)) for i in ids[1:4]), force=True)
        ids = ids[:1]
    for sid in ids:
        r = sym_row(c, sid)
        rows = callers_of(c, sid) if direction == "in" else callees_of(c, sid)
        title = "callers of" if direction == "in" else "callees of"
        out.add(f"{title} {r[2]}  {r[10]}:{r[4]}  — {len(rows)} (EXACT / ~INFER / ?AMBIG)", force=True, files=[r[10]])
        by = collections.OrderedDict()
        for other, conf, ln, k in rows:
            if conf < a.min_conf:
                continue
            rr = sym_row(c, other)
            mark = "" if conf == 3 else "~" if conf == 2 else "?"
            if direction == "in":
                by.setdefault(rr[10], []).append(f"{mark}{rr[2] if rr[3] != 'module' else '[module]'}:{ln}" + ("" if k == "call" else f"({k})"))
            else:
                by.setdefault(rr[10], []).append(f"{mark}{rr[2]}:{rr[4]}")
        for path, items in by.items():
            out.add(f"  {path}: " + ", ".join(items), files=[path])
        hidden = sum(1 for x in rows if x[1] < a.min_conf)
        if hidden:
            out.add(f"  (+{hidden} low-confidence hidden; --min-conf 1 to show)")
    return out.text(), out.files


def cmd_callers(ctx, a):
    return _edge_cmd(ctx, a, "in")


def cmd_callees(ctx, a):
    return _edge_cmd(ctx, a, "out")


def impact_set(c, seeds, depth, min_conf, kinds=("call", "ref", "inherit")):
    ph = ",".join("?" * len(kinds))
    seen = {s: 0 for s in seeds}
    frontier = list(seeds)
    for d in range(1, depth + 1):
        nxt = []
        for s in frontier:
            for (src,) in c.execute(f"SELECT src FROM edges WHERE dst=? AND conf>=? AND kind IN ({ph})", (s, min_conf, *kinds)):
                if src not in seen:
                    seen[src] = d
                    nxt.append(src)
        frontier = nxt
        if not frontier:
            break
    return seen


def render_impact(c, seen, seeds, out, show_tests=True):
    by_depth = collections.defaultdict(lambda: collections.defaultdict(list))
    tests = collections.defaultdict(list)
    for sid, d in seen.items():
        if sid in seeds:
            continue
        r = sym_row(c, sid)
        if r[11]:
            tests[r[10]].append(r[2] if r[3] != "module" else "[module]")
            continue
        by_depth[d][r[10]].append(r[2] if r[3] != "module" else "[module]")
    total = sum(len(v) for d in by_depth.values() for v in d.values())
    out.add(f"impacted (non-test): {total}  |  test files touching it: {len(tests)}", force=True)
    for d in sorted(by_depth):
        n = sum(len(v) for v in by_depth[d].values())
        out.add(f"## depth {d} — {n} ({'direct callers/refs' if d == 1 else 'transitive'})")
        for path, items in sorted(by_depth[d].items(), key=lambda x: -len(x[1])):
            out.add(f"  {path}: " + ", ".join(items[:8]) + (f" (+{len(items) - 8})" if len(items) > 8 else ""), files=[path])
    if show_tests and tests:
        out.add("## tests to run")
        for path, items in sorted(tests.items()):
            out.add(f"  {path}  ({len(items)} refs)", files=[path])
    return total, len(tests)


def cmd_impact(ctx, a):
    c = ctx.conn
    scopes = parse_scopes(a.scope)
    out = Out(a.budget or 1800)
    ids = lookup(c, a.symbol, scopes)
    if not ids:
        return f"symbol not found: {a.symbol}. Try: graft find {a.symbol}", set()
    if len(ids) > 1 and not a.all:
        out.add(f"note: {len(ids)} symbols match; analysing the most central ({label(sym_row(c, ids[0]))}). Others: " +
                " | ".join(label(sym_row(c, i)) for i in ids[1:4]), force=True)
        ids = ids[:1]
    seen = impact_set(c, ids, a.depth, a.min_conf)
    r = sym_row(c, ids[0])
    out.add(f"impact of changing {r[2]}  {r[10]}:{r[4]}  (depth<={a.depth}, conf>={CONF_NAME[a.min_conf]})", force=True, files=[r[10]])
    render_impact(c, seen, set(ids), out)
    return out.text("--depth N, --min-conf 3 for certainty only, or raise --budget"), out.files


def cmd_path(ctx, a):
    c = ctx.conn
    A = lookup(c, a.a)
    B = set(lookup(c, a.b))
    out = Out(a.budget or 800)
    if not A or not B:
        return f"symbol not found: {a.a if not A else a.b}", set()
    prev = {}
    frontier = list(A[:5])
    seen = set(frontier)
    found = None
    for _ in range(a.depth):
        nxt = []
        for s in frontier:
            for dst, conf, ln, k in callees_of(c, s):
                if conf < a.min_conf or dst in seen:
                    continue
                prev[dst] = (s, conf, k)
                seen.add(dst)
                if dst in B:
                    found = dst
                    break
                nxt.append(dst)
            if found:
                break
        if found or not nxt:
            break
        frontier = nxt
    if not found:
        return f"no forward path (depth<={a.depth}, conf>={CONF_NAME[a.min_conf]}). Try the reverse direction or --min-conf 1.", set()
    chain = [found]
    while chain[-1] in prev:
        chain.append(prev[chain[-1]][0])
    chain.reverse()
    out.add(f"path ({len(chain) - 1} hops):", force=True)
    for i, sid in enumerate(chain):
        r = sym_row(c, sid)
        edge = ""
        if i:
            _, conf, k = prev[sid]
            edge = f"  <-[{k},{CONF_NAME[conf]}]"
        out.add(f"  {r[2]}  {r[10]}:{r[4]}{edge}", files=[r[10]])
    return out.text(), out.files


def git(root, *args):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def cmd_changes(ctx, a):
    c = ctx.conn
    out = Out(a.budget or 2000)
    if not (ctx.root / ".git").exists():
        return "not a git repository: use `graft impact <symbol>` instead.", set()
    rc, diff = git(ctx.root, "diff", "-U0", a.base)
    if rc != 0:
        return f"git diff failed against {a.base!r}.", set()
    rc, untracked = git(ctx.root, "ls-files", "--others", "--exclude-standard")
    changed = collections.defaultdict(list)
    cur = None
    for ln in diff.splitlines():
        if ln.startswith("+++ b/"):
            cur = ln[6:]
        elif ln.startswith("@@") and cur:
            m = re.search(r"\+(\d+)(?:,(\d+))?", ln)
            s, n = int(m.group(1)), int(m.group(2) or 1)
            changed[cur].append((s, s + max(n, 1) - 1))
    for p in untracked.split():
        changed[p].append((1, 10 ** 9))
    seeds = set()
    names = []
    for path, ranges in changed.items():
        for sid, qname, line, end, kind in c.execute(
                "SELECT s.id,s.qname,s.line,s.end_line,s.kind FROM symbols s JOIN files f ON f.id=s.fid WHERE f.path=? AND s.kind!='module'", (path,)):
            if any(not (e < line or s > end) for s, e in ranges):
                seeds.add(sid)
                names.append(f"{qname} ({path}:{line})")
    if not changed:
        return f"no changes vs {a.base}.", set()
    out.add(f"changed files: {len(changed)} | changed symbols: {len(seeds)}", force=True)
    for nme in names[:15]:
        out.add("  • " + nme)
    if len(names) > 15:
        out.add(f"  … +{len(names) - 15}")
    if seeds:
        seen = impact_set(c, list(seeds), a.depth, a.min_conf)
        render_impact(c, seen, seeds, out)
    else:
        out.add("no indexed symbol overlaps the diff (docs/config?). Nothing to propagate.")
    return out.text("--depth N or raise --budget"), out.files


def cmd_stats(ctx, a):
    c = ctx.conn
    nsym = c.execute("SELECT COUNT(*) FROM symbols WHERE kind!='module'").fetchone()[0]
    ne = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conf = dict(c.execute("SELECT conf,COUNT(*) FROM edges GROUP BY conf").fetchall())
    size_src = sum(r[0] for r in c.execute("SELECT size FROM files"))
    db = ctx.dbpath.stat().st_size if ctx.persist and ctx.dbpath.exists() else 0
    lines = [f"tier={ctx.tier}  files={ctx.stats['files']}  symbols={nsym}  edges={ne} "
             f"(EXACT {conf.get(3, 0)}, INFER {conf.get(2, 0)}, AMBIG {conf.get(1, 0)})",
             f"source={size_src / 1e6:.2f}MB  index={'memory' if not db else f'{db / 1e6:.2f}MB'}"]
    led = ctx.root / ".graft" / "ledger.jsonl"
    if led.exists():
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines() if x.strip()]
        out_t = sum(r["out"] for r in rows)
        naive = sum(r["naive"] for r in rows)
        lines.append(f"ledger (local, estimated): {len(rows)} calls, returned {out_t:,} tokens vs ~{naive:,} tokens "
                     f"to read the referenced files whole -> ~{100 * (1 - out_t / max(1, naive)):.0f}% less "
                     "(upper bound: an agent would not always read whole files)")
    else:
        lines.append("ledger: empty (created on first use in non-tiny projects)")
    return "\n".join(lines), set()


def cmd_doctor(ctx, a):
    c = ctx.conn
    L = [f"graft {VERSION} — tier={ctx.tier}"]
    for lang, n, zero, errs in c.execute("SELECT lang,COUNT(*),SUM(nsym=0),SUM(err IS NOT NULL) FROM files GROUP BY lang"):
        mode = "AST (precise)" if lang == "py" else "heuristic (regex + import resolution)" if lang in JSLIKE else "heuristic (regex, name-based)"
        L.append(f"  {lang:6} {n:5} files  no-symbols={zero or 0}  parse-errors={errs or 0}  [{mode}]")
    errs = c.execute("SELECT path,err FROM files WHERE err IS NOT NULL LIMIT 5").fetchall()
    for p, e in errs:
        L.append(f"  ! {p}: {e}")
    conf = dict(c.execute("SELECT conf,COUNT(*) FROM edges GROUP BY conf").fetchall())
    tot = sum(conf.values()) or 1
    L.append(f"  edges: EXACT {100 * conf.get(3, 0) // tot}% / INFER {100 * conf.get(2, 0) // tot}% / AMBIG {100 * conf.get(1, 0) // tot}%")
    nraw = c.execute("SELECT COUNT(*) FROM raw WHERE kind IN ('call','ref')").fetchone()[0]
    L.append(f"  reference sites: {nraw}, resolved edges: {tot} (the rest are external libs/builtins/dynamic — expected)")
    if any(lang not in ("py",) for (lang,) in c.execute("SELECT DISTINCT lang FROM files")):
        L.append("  note: non-Python languages use heuristics; treat INFER/AMBIG as leads and verify with grep.")
    return "\n".join(L), set()


def cmd_clean(ctx, a):
    import shutil
    d = ctx.root / ".graft"
    if ctx.conn:
        ctx.conn.close()
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        return "removed .graft/"
    return "nothing to remove"


COMMANDS = {
    "index": cmd_index, "map": cmd_map, "find": cmd_find, "ask": cmd_ask, "show": cmd_show,
    "callers": cmd_callers, "callees": cmd_callees, "impact": cmd_impact, "path": cmd_path,
    "changes": cmd_changes, "stats": cmd_stats, "doctor": cmd_doctor, "clean": cmd_clean,
}


def build_parser():
    p = argparse.ArgumentParser(prog="graft", description="Adaptive local code intelligence for AI agents.")
    p.add_argument("--root", help="project root (default: nearest .git/.graft upward from cwd)")
    p.add_argument("--tier", choices=["tiny", "standard", "large"], help="force a tier (default: automatic)")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, budget=True, scope=True):
        if budget:
            sp.add_argument("--budget", type=int, help="max output tokens (approx.)")
        if scope:
            sp.add_argument("--scope", help="comma-separated path prefixes/globs to restrict to")
    sub.add_parser("index", help="build/refresh the index (also automatic on every command)")
    s = sub.add_parser("map", help="ranked repo overview within a token budget")
    common(s)
    s.add_argument("--focus", help="FILE|SYMBOL[,..] personalise ranking around what you're editing")
    s = sub.add_parser("find", help="ranked symbol search with signatures")
    s.add_argument("query", nargs="+")
    common(s)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--kind", choices=["function", "method", "class", "type", "const", "module"])
    s = sub.add_parser("ask", help="natural-language question -> candidates + top code, with match quality")
    s.add_argument("question", nargs="+")
    common(s)
    s.add_argument("--code", type=int, help="how many top matches get source (default: auto)")
    s.add_argument("--lines", type=int, help="max source lines per match (default 45)")
    s = sub.add_parser("show", help="source of one symbol + callers/callees (or outline of a file)")
    s.add_argument("symbol")
    common(s)
    s.add_argument("--lines", type=int, help="max source lines (default 80)")
    s.add_argument("--max-edges", type=int, default=8)
    for nm, hp in (("callers", "who calls/references SYMBOL"), ("callees", "what SYMBOL calls/references")):
        s = sub.add_parser(nm, help=hp)
        s.add_argument("symbol")
        common(s)
        s.add_argument("--min-conf", type=int, default=2, choices=[1, 2, 3], help="3=EXACT only, 2=+INFER (default), 1=all")
        s.add_argument("--all", action="store_true", help="include every symbol matching the name")
    s = sub.add_parser("impact", help="transitive blast radius of changing SYMBOL, with tests")
    s.add_argument("symbol")
    common(s)
    s.add_argument("--depth", type=int, default=3)
    s.add_argument("--min-conf", type=int, default=2, choices=[1, 2, 3])
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("path", help="shortest call path A -> B")
    s.add_argument("a")
    s.add_argument("b")
    common(s, scope=False)
    s.add_argument("--depth", type=int, default=6)
    s.add_argument("--min-conf", type=int, default=2, choices=[1, 2, 3])
    s = sub.add_parser("changes", help="symbols touched by your git diff + impact + tests")
    common(s, scope=False)
    s.add_argument("--base", default="HEAD")
    s.add_argument("--depth", type=int, default=2)
    s.add_argument("--min-conf", type=int, default=2, choices=[1, 2, 3])
    sub.add_parser("stats", help="index stats and local savings ledger")
    sub.add_parser("doctor", help="diagnose index quality per language")
    sub.add_parser("clean", help="delete .graft/")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    a = build_parser().parse_args(argv)
    root = Path(a.root).resolve() if a.root else find_root(Path.cwd())
    ctx = Ctx(root, a.tier)
    if a.cmd == "clean":
        ctx.conn = None
        print(cmd_clean(ctx, a))
        return 0
    ctx.refresh()
    res = COMMANDS[a.cmd](ctx, a)
    files = set()
    if isinstance(res, tuple):
        res, files = res
    print(res)
    # ledger local (so se .graft/ ja existe: projetos tiny nao escrevem nada)
    if ctx.persist and a.cmd not in ("index", "stats", "doctor", "clean"):
        try:
            naive = 0
            if files:
                q = ",".join("?" * len(files))
                naive = sum(r[0] for r in ctx.conn.execute(f"SELECT size FROM files WHERE path IN ({q})", list(files))) // 4
            with open(root / ".graft" / "ledger.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": int(time.time()), "cmd": a.cmd, "out": est_tokens(res), "naive": max(naive, est_tokens(res))}) + "\n")
        except Exception:
            pass
    ctx.conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
