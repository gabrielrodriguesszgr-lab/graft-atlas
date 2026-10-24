# Graft Atlas

**Adaptive, zero-dependency local code intelligence skill & CLI for AI coding agents (Claude Code, Gemini / Antigravity, Cursor, Codex, GitHub Copilot).**

Graft Atlas builds a fast, incremental code graph using only the Python 3 standard library (`ast`, `sqlite3`, `tokenize`), combining **Personalized PageRank**, **BM25/IDF lexical + docstring ranking**, and **confidence-tagged edges** (`EXACT`, `~INFER`, `?AMBIG`) to cut agent token consumption by **70%–85%** without cloud dependencies, telemetry, or global config tampering.

---

## Why Graft Atlas?

Born from a reverse audit and head-to-head benchmark against **Graphify** and **CodeGraph**, Graft Atlas keeps the best of both and eliminates their weaknesses:

- **Zero external dependencies:** Pure Python 3.10+ stdlib. No `node_modules`, no WASM binaries, no heavy pip trees.
- **100% private & side-effect free:** Zero network calls, zero telemetry, never modifies global agent configs (`CLAUDE.md`, `AGENTS.md`, or `~/.claude`) unless you explicitly copy it there.
- **Auto-adaptive 3-tier engine:**
  - `tiny` (≤ 25 files & ≤ 200 KB): Runs **100% in RAM (`:memory:`)** — leaves zero files on disk.
  - `standard` (26 – 1,500 files): Incremental SQLite WAL index in `.graft/index.db` (auto-gitignored) + local token-savings ledger.
  - `large` (> 1,500 files or > 25 MB): Multiprocess parallel parsing + directory-first hierarchical maps.
- **One-shot answers (`ask`, `show`):** Returns ranked candidates, confidence indicator (`match: strong | medium | weak`), exact source code headers, and caller/callee trails inside a strict `--budget`.

---

## Benchmark Summary (Python & TypeScript Corpora)

| Metric | **Graft Atlas** | Graphify | CodeGraph |
|---|---|---|---|
| **Initial Index Speed (97 Py / 295 TS files)** | **5.3s / 9.3s** | 21.4s / 45.6s | 14.9s / 26.5s |
| **Direct Callers Precision / Recall (Python)** | **90.0% / 100.0%** | 90.0% / 100.0% | 84.4% / 100.0% |
| **Direct Callers Precision / Recall (TypeScript)** | **96.4% / 96.4%** | 96.2% / 89.3% | 95.8% / 82.1% |
| **Natural Language Queries Hit Rate (15 tasks)** | **15 / 15 (100%)** | 13 / 15 (86.7%) | 11 / 15 (73.3%) |
| **Avg Tokens per NL Query** | **~640 tok** *(with code)* | ~1,950 tok *(no code)* | ~4,350 tok *(with code)* |
| **Avg Query Latency** | **~0.30s** | ~1.74s | ~1.32s |

See [`references/audit.md`](references/audit.md) for the full reverse-audit and architectural breakdown.

---

## Installation as an Agent Skill

Clone directly into your workspace or global skills folder:

```bash
# Workspace skill (Antigravity / Gemini / Claude / Cursor)
git clone https://github.com/gabrielrodriguesszgr-lab/graft-atlas.git .agents/skills/graft-atlas

# Or global skill for Antigravity / Gemini
git clone https://github.com/gabrielrodriguesszgr-lab/graft-atlas.git ~/.gemini/config/skills/graft-atlas

# Or global skill for Claude Code
git clone https://github.com/gabrielrodriguesszgr-lab/graft-atlas.git ~/.claude/skills/graft-atlas
```

---

## CLI Usage

```bash
# 1. Structural repository map (Personalized PageRank)
python scripts/graft.py map [--focus FILE_OR_SYMBOL] [--scope DIR] [--budget 1500]

# 2. Natural-language question -> ranked candidates + code + callers
python scripts/graft.py ask "how are secrets redacted"

# 3. Fast symbol lookup
python scripts/graft.py find build_from_json

# 4. Inspect a symbol (source code + callers + callees) or file outline
python scripts/graft.py show build_from_json

# 5. Blast radius & affected tests before refactoring
python scripts/graft.py impact build_from_json --depth 3
python scripts/graft.py callers build_from_json

# 6. Shortest call path between two symbols
python scripts/graft.py path main redact_secrets

# 7. Audit uncommitted git diff impact & tests to run
python scripts/graft.py changes

# 8. Index health & token savings ledger
python scripts/graft.py stats
python scripts/graft.py doctor
```

---

## Supported Languages

- **Full AST + multi-depth import & inheritance resolution:** Python (`.py`, `.pyw`)
- **Heuristic extraction + relative/barrel import resolution:** TypeScript / JavaScript (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`)
- **Heuristic extraction + package/directory resolution:** Go, Rust, Java, Kotlin, Scala, C#, C/C++, PHP, Ruby, Swift

## License

MIT
