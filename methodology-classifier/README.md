# methodology-classifier

Classify a corporate issuer into the **rating methodology each agency would apply to
it** — one **Moody's** sector methodology and one **S&P** sector — from its industry
code. Feed it a CSV of issuers, get back a CSV/JSON with the methodology routing for
each.

Moody's and S&P each rate a corporate under exactly **one** sector methodology (its own
scorecard), chosen by the issuer's dominant business. Neither agency publishes a
mechanical classifier or an official crosswalk from a standard code (SIC/NAICS/GICS) to
their buckets — an analyst assigns it by judgment. They publish only the *list of
buckets*. This tool reproduces that routing decision automatically so a downstream team
knows **which scorecard to apply to which issuer**.

---

## What it does

For each issuer it produces:

| Output | Meaning |
|---|---|
| `moodys_methodology` | The Moody's sector methodology to apply, e.g. `Regulated Electric and Gas Utilities` |
| `sp_sector` | The S&P sector section to apply, e.g. `Regulated Utilities` |
| `methodology_confidence` | `high` \| `medium` \| `low` — how sure the classification is (see below) |
| `methodology_source` | `sic_table` \| `llm_fallback` \| `default` — *how* the answer was produced |
| `is_financial` | `True` for banks/insurers (SIC 60–67): the industrial corporate scorecard does **not** apply — route to the agency's separate Financial Institutions / Insurance framework |
| `notes` | Provenance / caveats (e.g. a TRBC disagreement, or an EDGAR lookup failure) |

### How it decides (in order)

1. **Deterministic SIC table (primary).** A hand-built map from SIC prefix → (Moody's
   bucket, S&P bucket), matched **longest-prefix-first** so a 4-digit code (e.g. `2834`
   pharma) overrides its 2-digit major group (`28` chemicals). Auditable and offline.
   - A specific 3–4 digit hit → `confidence = high`.
   - Only the 2-digit major group matched → `confidence = medium`.
2. **LLM fallback (ambiguous SICs only).** For SICs that carry no industry signal —
   blank-check shells (`6770`), non-classifiable (`999x`), bare holding companies
   (`67xx`) — it asks an LLM to pick one bucket from each agency's list using the
   issuer name / business description. `confidence = low`, `source = llm_fallback`.
   *Only runs when enabled* (see `--llm`); otherwise these fall through to a default.
3. **Default (last resort).** No table match and no usable LLM answer → a generic
   catch-all bucket, `confidence = low`, `source = default`. Never crashes.
4. **TRBC cross-check (optional).** If you supply a TRBC / sector column, a
   disagreement with the SIC result at the top-level economic group **lowers**
   confidence one step and is recorded in `notes`. It never raises confidence — SIC is
   the primary signal.

The bucket universe: **~34 Moody's** corporate methodologies + 4 Infrastructure
(utilities/power) + 5 financial-institution groups, and **S&P's 40** Sector-Specific
Corporate Methodology sections + 2 external financial labels. (Note the encoded
asymmetry: regulated utilities are in Moody's *Infrastructure* group but inside S&P's
*corporate* document as `Regulated Utilities`.)

---

## Input

A **CSV, one issuer per row.** Column names are detected case-insensitively; you need
enough to identify the issuer's industry:

| Column | Required? | Purpose |
|---|---|---|
| `sic` | one of these | 4-digit SEC SIC code. If present, classification is **fully offline** and fastest. |
| `ticker` and/or `cik` | one of these | Used to look the SIC up from **SEC EDGAR** when no `sic` column is given (needs network). |
| `name` | optional | Improves the LLM fallback for ambiguous SICs; also a display label. |
| `sector` / `trbc` | optional | TRBC "Economic Sector Name" (or any column containing `trbc`/`sector`) for the cross-check. |

A `sic` column always wins and skips the EDGAR round-trip. Example
([`examples/issuers_sample.csv`](examples/issuers_sample.csv)):

```csv
ticker,cik,name,sic,sector
DUK,,Duke Energy,4911,Utilities
XOM,,Exxon Mobil,2911,Energy
AAPL,,Apple Inc,3571,Technology
JPM,,JPMorgan Chase,6021,Financials
ACME,,Acme Acquisition Corp,6770,
```

If you only have tickers, drop the `sic` column and the tool resolves it from EDGAR:

```csv
ticker
DUK
XOM
AAPL
```

---

## Output

One row per input issuer, as **CSV and/or JSON** (default: both). Columns:

```
input_ticker, input_cik, cik, name, sic, sic_description,
moodys_methodology, sp_sector, methodology_confidence,
methodology_source, is_financial, notes
```

CSV example (from the sample above, `--sic-only`):

```csv
input_ticker,input_cik,cik,name,sic,sic_description,moodys_methodology,sp_sector,methodology_confidence,methodology_source,is_financial,notes
DUK,,,Duke Energy,4911,,Regulated Electric and Gas Utilities,Regulated Utilities,high,sic_table,False,
XOM,,,Exxon Mobil,2911,,Integrated Oil and Gas,Refining And Marketing,high,sic_table,False,
AAPL,,,Apple Inc,3571,,Software and Diversified Technology,Technology Hardware And Semiconductors,high,sic_table,False,
JPM,,,JPMorgan Chase,6021,,Banks,Banks (Financial Institutions criteria),medium,sic_table,True,Financial-sector issuer (SIC 60–67): industrial corporate scorecard does not apply; route to the FIG/Insurance framework.
ACME,,,Acme Acquisition Corp,6770,,Business and Consumer Services,Business And Consumer Services,low,default,True,Ambiguous SIC and no LLM available; generic default applied
```

JSON is the same records as an array of objects. The run also prints a
`high / medium / low` tally to the console — the **low** count is your analyst-review
queue.

---

## Install & run

Core and CLI are **pure Python stdlib** — no install needed to run from the repo root:

```bash
python -m methodology_classifier examples/issuers_sample.csv --sic-only
# → examples/issuers_sample_methodology.csv  and  .json
```

Or install it (adds the `methodology-classifier` command and, optionally, the LLM extra):

```bash
pip install -e .            # core only
pip install -e ".[llm]"     # + anthropic, for the LLM fallback
```

### CLI

```
python -m methodology_classifier INPUT.csv [options]

  -o, --output BASE   Output base path without extension (default: <input>_methodology)
  --format {csv,json,both}   Output format(s) (default: both)
  --sic-only          Classify only from a 'sic' column; never call EDGAR
  --llm               Enable the LLM fallback for ambiguous SICs (needs ANTHROPIC_API_KEY)
  --limit N           Classify only the first N rows (pilot)
```

Examples:

```bash
python -m methodology_classifier issuers.csv                     # CSV + JSON, resolve SIC via EDGAR if needed
python -m methodology_classifier issuers.csv --sic-only          # offline; requires a sic column
python -m methodology_classifier issuers.csv --llm -o out/result # classify ambiguous SICs with the LLM
python -m methodology_classifier issuers.csv --format json --limit 50
```

### As a library

```python
from methodology_classifier import classify_methodology

mc = classify_methodology("4911", name="Duke Energy", trbc="Utilities")
mc.moodys_methodology   # 'Regulated Electric and Gas Utilities'
mc.sp_sector            # 'Regulated Utilities'
mc.confidence           # 'high'
mc.is_financial         # False
```

---

## Notes & configuration

- **SEC EDGAR** requires a descriptive User-Agent. When resolving SICs from
  tickers/CIKs, set your contact:
  ```bash
  export SEC_USER_AGENT="Your Name your.email@example.com"
  ```
  On macOS the stdlib often lacks usable TLS roots (`CERTIFICATE_VERIFY_FAILED`); the
  resolver uses `certifi` automatically when present. Install it with
  `pip install -e ".[edgar]"` (or `pip install certifi`) if EDGAR lookups fail on TLS.
  `--sic-only` runs need none of this.
- **LLM fallback** uses the Anthropic API (Claude Haiku) and needs `ANTHROPIC_API_KEY`.
  Without `--llm` (or without a key), ambiguous SICs simply degrade to a flagged
  `default` — the deterministic path never depends on the LLM.
- **Robustness:** a bad identifier (EDGAR lookup fails) is recorded in `notes` and the
  row is still emitted — one bad row never aborts the batch.

## Scope & caveats

- Classification is by the issuer's **primary SIC**. True multi-segment conglomerates
  are only resolved by their dominant segment via the LLM fallback; full
  revenue-weighted segment classification is out of scope.
- The SIC→bucket table encodes credit-sector judgment and known agency asymmetries; it
  is a transparent, editable rule table (`methodology_classifier/classifier.py`), not a
  statistical model. Treat `low`-confidence rows as an analyst-review queue.
- The bucket lists reflect the agencies' published sector methodologies (S&P's April
  2024 Sector-Specific Corporate Methodology; Moody's ~30 corporate sector methodologies
  plus the Infrastructure group). They are not affiliated with or endorsed by Moody's or
  S&P.

## Tests

```bash
pip install pytest
pytest -q
```

## Layout

```
methodology_classifier/
  classifier.py   # core: bucket lists, SIC table, classify_methodology(), LLM fallback
  edgar.py        # minimal SEC EDGAR ticker/CIK → SIC resolver (stdlib only)
  cli.py          # batch CSV → CSV/JSON
  __main__.py     # `python -m methodology_classifier`
tests/            # unit tests (deterministic mapping, fallback, TRBC cross-check)
examples/         # sample input CSV
```
