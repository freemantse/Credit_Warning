---
name: clean-codebase
description: >-
  Safely remove dead code, unused functions/variables/imports, orphan files,
  tracked build artifacts/logs, and stale/duplicate scripts, tests, data, or
  model artifacts from this repo. Use when the user asks to "clean up", "remove
  redundant/unused code", "tidy the codebase", or prune old versions. Detects
  with tooling, VERIFIES every candidate repo-wide before deleting, and flags
  (never blindly deletes) anything intentional or expensive to regenerate.
---

# Clean the program

A disciplined codebase cleaner. The guiding rule: **detect broadly, delete
narrowly.** Every removal must be backed by evidence that nothing references it,
and anything ambiguous is *reported to the user*, not deleted. All work happens
in a git repo, so removals are recoverable — but that is not licence to be
careless.

## Non-negotiable safety rules

1. **Never delete on a tool's say-so alone.** `vulture`/`ruff` are *candidate
   generators*. Confirm each candidate is unreferenced across the **whole repo**
   (Python, TS, Markdown, JSON, SQL, shell) including tests and dynamic access
   (`getattr`, string names, `__all__`) before removing it.
2. **Respect stated intent.** If a symbol's docstring/comment says it is "kept
   for…", or it is a deliberate public API surface, or you did not create it and
   its description contradicts deleting it — **flag it, don't delete it.**
3. **Don't delete expensive-to-regenerate or licensed local data.** Files that
   are `.gitignore`d are the user's local working data (large drops, DBs,
   licensed feeds). List them as candidates; let the user decide.
4. **Verify after every batch.** Byte-compile, import, run the test suite, and
   typecheck. A cleanup that breaks the build is worse than no cleanup.
5. **Report in three buckets:** *Removed* (with why), *Flagged for you*
   (ambiguous/intentional/expensive), *Kept* (looked dead but isn't — with why).

## Procedure

### 1. Inventory
```
git ls-files | sed 's#/.*##' | sort | uniq -c | sort -rn      # tracked files by dir
git ls-files <dirs>                                            # full tracked list
git status --porcelain --untracked-files=all | grep '^??'     # untracked, NOT ignored
cat .gitignore                                                 # what's intentionally local
```
Separate **tracked code** (the real target) from **gitignored local data** (flag
only). Note new/untracked work files — they are current work, not redundancy.

### 2. Set up detection tooling (non-invasively)
Linters usually aren't installed globally. Use a throwaway venv in the scratchpad
so you never mutate the user's environment:
```
python3 -m venv "$SCRATCH/cleanvenv"
"$SCRATCH/cleanvenv/bin/pip" install --quiet ruff vulture pyflakes
```
For a TS/JS frontend, use the project's own typechecker: `npx tsc --noEmit`
(temporarily consider `noUnusedLocals`/`noUnusedParameters` if not already on).

### 3. Detect (generate candidates)
- **Unused imports / vars / redefinitions (Python):**
  `ruff check --select F401,F811,F841,F541 --output-format=concise <dirs>`
- **Dead functions / classes / attributes (Python):**
  `vulture --min-confidence 80 <dirs>` then `--min-confidence 60` (review each).
  Vulture **false-positives** on: web-framework route handlers (decorated),
  `if __name__` CLI `main()`s, dataclass/TypedDict fields, dynamically-called
  names. Treat all of these as *not dead* until proven otherwise.
- **Orphan files:** files whose basename/module is imported or referenced nowhere.
- **Stray/old artifacts:** extra model files, `*.bak`, duplicated scripts,
  tracked logs (`git ls-files logs/`), tracked build output.

### 4. Verify each candidate (the important step)
For every candidate symbol, count references repo-wide — `1` (definition only)
means truly dead:
```
grep -rn --include=*.py "\bSYMBOL\b" src api scripts tests        # refs incl. tests
grep -rn "SYMBOL" --include=*.md --include=*.ts --include=*.tsx \
        --include=*.sql --include=*.json --include=*.sh .          # non-Python refs
grep -rn "getattr.*SYMBOL\|['\"]SYMBOL['\"]" <dirs>                # dynamic / string use
grep -rn "__all__" <module>                                        # exported public API?
```
Also **read the definition**. Confirm it's self-contained, note "kept for…"
docstrings, and decide: private helper with zero callers → remove; documented
public API the user may want → flag; used only by its own test → flag (removing
means dropping coverage too).

### 5. Remove (narrowly) & keep the tree tidy
- Delete verified-dead functions/vars with exact-match edits; collapse the
  surrounding blank lines so spacing stays consistent.
- Removing a function may orphan its imports — re-run `ruff --select F401 --fix`
  on the touched files and check for now-unused module-level helpers.
- **Tracked logs / build artifacts:** `git rm` them and add to `.gitignore`
  (e.g. `logs/`, `*.tsbuildinfo`).
- **Old model / data versions:** keep anything the app or backtest loads
  (verify by grepping the load paths); flag the rest.

### 6. Verify the result
```
python3 -m py_compile <edited .py files>          # syntax
python3 -c "import <edited modules>"              # NameErrors from removed symbols
python3 -m pytest -q                              # full suite must stay green
npx tsc --noEmit                                  # frontend typecheck
"$SCRATCH/cleanvenv/bin/ruff" check --select F <dirs>   # confirm no new dead code
```
If anything fails, fix or revert that item before moving on.

### 7. Report
Summarize as **Removed / Flagged for you / Kept (with reason)**, plus the
verification result (tests pass, typecheck clean). Do **not** commit unless asked.

## This repo's specifics (Credit Warning)
- **Stack:** Python model/data pipeline in `src/`, `api/`, `scripts/`; Next.js +
  TypeScript UI in `app/`, `lib/`; tests in `tests/` (pytest).
- **Keep (load-bearing, not redundant):** `data/model_vintages/*.joblib` (the
  backtest scores with these), `data/migration_eval.json` (tuned thresholds),
  `data/cases.csv`, `data/agency_ratings.csv`, `data/backtest_baseline.json`
  (used by `src/backtest.py`). See memory: keep thresholds + vintages + features
  in sync.
- **`api/main.py` functions are FastAPI routes** (decorated) — vulture flags them
  as unused; they are NOT dead.
- **Scripts are CLI tools** (`build_labels`, `seed_*`, `rebuild_cases`,
  `export_cases`, `track_universe`, …). Absence from docs ≠ dead; confirm against
  git history before removing any.
- **`src/store.py` is a data-access API layer.** A few unused accessors are
  normal; remove only plainly-superseded ones (e.g. a single-issuer getter
  replaced by a `_grouped` batch variant), and keep anything whose docstring says
  it's intentionally retained.
- **Local gitignored data — flag, don't delete:** `data/store.db`,
  `data/backtest_report.txt`, `data/backtest_results.json`,
  `data/lseg_equity_prices/` (large licensed drop), `data/universe_xref.csv`,
  `data/migration_model.joblib` (regenerated by predict).
