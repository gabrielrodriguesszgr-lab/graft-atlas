---
name: graft-atlas
description: >-
  Use this skill to navigate, search, and analyze source code with high token efficiency
  before reading full files or running broad grep searches. Trigger when the user or agent
  needs to: (1) understand repository structure or architecture ("map"), (2) locate where a
  concept, feature, or symbol is implemented ("ask", "find"), (3) inspect a function/class
  signature and body with its callers and callees ("show"), (4) trace call chains or check
  blast radius / affected tests before refactoring ("callers", "callees", "impact", "path",
  "changes"). Do NOT use for non-code tasks, pure string/config literal lookups (use grep),
  or editing files directly.
---

# Graft Atlas — Adaptive Local Code Intelligence

Graft Atlas builds an auto-adaptive, zero-dependency code graph (AST for Python, heuristic + import resolution for JS/TS/Go/Rust/Java/C++/etc.) with **Personalized PageRank**, **BM25+IDF ranking**, and **confidence-tagged edges** (`EXACT`, `~INFER`, `?AMBIG`).

Execute via the bundled CLI script (never read `graft.py` into context):

```bash
python .agents/skills/graft-atlas/scripts/graft.py <command> [args]
```

*(If installed globally in `~/.gemini/config/skills/graft-atlas`, use that path instead.)*

---

## Auto-Adaptive Tiers

Graft automatically selects the optimal execution tier on every run and incrementally updates changed files via `mtime`/`size`:

| Tier | Threshold | Persistence | Behavior |
|---|---|---|---|
| `tiny` | ≤ 25 files & ≤ 200 KB | In-memory (`:memory:`) | Zero disk footprint. Full symbol map fits in a single pass. |
| `standard` | 26 – 1,500 files | `.graft/index.db` (WAL) | Incremental SQLite index + PageRank + local token-savings ledger. |
| `large` | > 1,500 files or > 25 MB | `.graft/index.db` (WAL) | Multiprocess parallel indexing + directory-first hierarchical maps. |

---

## Core Workflow for Agents

1. **Orient in an unfamiliar repo or subpackage**
   ```bash
   python .agents/skills/graft-atlas/scripts/graft.py map [--focus FILE_OR_SYMBOL] [--scope DIR] [--budget 1500]
   ```
2. **Locate a feature or answer a natural-language question**
   ```bash
   python .agents/skills/graft-atlas/scripts/graft.py ask "how are secrets redacted" [--budget 1800]
   ```
   - Check the first line (`match: strong | medium | weak`).
   - If `strong` or `medium`, the top candidate signatures and source code are already included—**do not re-read those lines**.
   - If `weak` or `none`, immediately fall back to targeted `grep` for string literals or config keys.
3. **Inspect a specific symbol (source + callers + callees in one shot)**
   ```bash
   python .agents/skills/graft-atlas/scripts/graft.py show <symbol_or_file:symbol> [--lines 80]
   ```
4. **Check blast radius & affected tests before modifying a symbol**
   ```bash
   python .agents/skills/graft-atlas/scripts/graft.py impact <symbol> [--depth 3] [--min-conf 2]
   python .agents/skills/graft-atlas/scripts/graft.py callers <symbol>
   ```
5. **Audit your current git diff before committing**
   ```bash
   python .agents/skills/graft-atlas/scripts/graft.py changes [--base HEAD]
   ```

---

## Command Reference

| Command | Purpose | Key Flags |
|---|---|---|
| `map` | Ranked structural overview (Personalized PageRank) | `--focus X`, `--scope DIR`, `--budget N` |
| `ask "<question>"` | NL search → ranked candidates + top source + call edges | `--code K`, `--lines N`, `--scope DIR`, `--budget N` |
| `find <query>` | Fast symbol/signature lookup | `--kind {function,method,class,type,const}`, `--limit N` |
| `show <sym\|file>` | Exact source slice + callers/callees (or file outline) | `--lines N`, `--max-edges N` |
| `callers <sym>` | Direct callers/references grouped by file | `--min-conf {1,2,3}`, `--all` |
| `callees <sym>` | Outbound calls/references from symbol | `--min-conf {1,2,3}`, `--all` |
| `impact <sym>` | Transitive blast radius by hop depth + test files to run | `--depth N`, `--min-conf {1,2,3}` |
| `path <A> <B>` | Shortest directed call path between two symbols | `--depth N`, `--min-conf {1,2,3}` |
| `changes` | Symbols modified in `git diff` + transitive impact + tests | `--base REF`, `--depth N` |
| `stats` | Index metrics & estimated token savings ledger | — |
| `doctor` | Per-language parser mode & edge confidence breakdown | — |
| `clean` | Remove `.graft/` directory | — |

---

## Confidence Tags & Guardrails

- **No mark (`EXACT`, conf=3):** Resolved via explicit import, same-file definition, or `self`/`cls` inheritance chain.
- **`~` (`INFER`, conf=2):** Resolved via unique project-wide symbol name or suffix module match.
- **`?` (`AMBIG`, conf=1):** Multiple candidates share the name (hidden by default when `--min-conf 2`; pass `--min-conf 1` to inspect).
- **Deep audit & design rationale:** Read [references/audit.md](references/audit.md) only when debugging index behavior or extending language support.
