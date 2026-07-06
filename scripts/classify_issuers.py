"""
Batch methodology classifier: CSV of issuers in → per-issuer Moody's + S&P sector
methodology out (CSV and/or JSON). Standalone — no database.

For each issuer this emits ONE Moody's methodology and ONE S&P sector (plus a
confidence and the source of the decision) using src.methodology.classify_methodology.

INPUT CSV — one issuer per row. Columns are detected case-insensitively; you need
enough to identify the issuer's industry:
  - `sic`                          → classified directly, fully OFFLINE (fastest).
  - `ticker` and/or `cik`          → the SIC is resolved from SEC EDGAR first
                                     (needs network; EDGAR responses are disk-cached).
  - `name`                         → optional, improves the LLM fallback for ambiguous
                                     SICs; also used as a display label.
  - `sector` / `trbc` (any col with "trbc"/"sector"/"economic sector") → optional TRBC
                                     cross-check that can lower confidence on a conflict.
A `sic` column, when present, always wins and skips the EDGAR round-trip.

OUTPUT — one row per input issuer with: input_ticker, input_cik, cik (resolved), name,
sic, sic_description, moodys_methodology, sp_sector, methodology_confidence,
methodology_source, is_financial, notes.

Usage:
    python3 -m scripts.classify_issuers issuers.csv                  # → issuers_methodology.csv + .json
    python3 -m scripts.classify_issuers issuers.csv -o out/result    # → out/result.csv + out/result.json
    python3 -m scripts.classify_issuers issuers.csv --format csv     # CSV only
    python3 -m scripts.classify_issuers issuers.csv --sic-only       # never call EDGAR (requires a sic column)
    python3 -m scripts.classify_issuers issuers.csv --llm            # enable the LLM fallback for ambiguous SICs
    python3 -m scripts.classify_issuers issuers.csv --limit 50       # first N rows (pilot)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from src.methodology import classify_methodology

# Output column order (stable, so downstream diffs are clean).
OUTPUT_COLUMNS = [
    "input_ticker", "input_cik", "cik", "name", "sic", "sic_description",
    "moodys_methodology", "sp_sector", "methodology_confidence",
    "methodology_source", "is_financial", "notes",
]


def _detect_columns(columns: list[str]) -> dict[str, str | None]:
    """Case-insensitively locate the identifier columns in the input CSV."""
    low = {c: str(c).strip().lower() for c in columns}

    def find(pred) -> str | None:
        return next((c for c in columns if pred(low[c])), None)

    return {
        "sic": find(lambda l: l == "sic" or l == "sic_code" or l == "siccode"),
        "ticker": find(lambda l: "ticker" in l or l == "symbol"),
        "cik": find(lambda l: l == "cik"),
        "name": find(lambda l: l == "name" or "company" in l or "issuer" in l),
        "trbc": find(lambda l: "trbc" in l or l == "sector" or "economic sector" in l),
    }


def _clean(v) -> str | None:
    """Normalise a cell to a stripped string, or None for empty / pandas NaN."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "<na>", "null"}:
        return None
    return s


def _resolve_sic(cik: str | None, ticker: str | None) -> dict:
    """
    Resolve an issuer's SIC + identity from SEC EDGAR (ticker or CIK).

    Returns {cik, name, sic, sic_description} on success, or {"error": msg} on any
    failure — the caller records the error and still emits an output row so one bad
    identifier never aborts the batch. Imported lazily so --sic-only runs need no
    network stack at all.
    """
    from src.ingest import get_company_info, resolve_identifier
    try:
        ident = cik or ticker
        resolved_cik = resolve_identifier(ident)
        info = get_company_info(resolved_cik)
        return {
            "cik": info.get("cik"),
            "name": info.get("name"),
            "sic": info.get("sic"),
            "sic_description": info.get("sic_description"),
        }
    except Exception as e:  # noqa: BLE001 — any failure degrades to a flagged row
        return {"error": f"{type(e).__name__}: {e}"}


def classify_file(
    input_path: Path,
    *,
    sic_only: bool = False,
    use_llm: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Classify every issuer row in `input_path` and return the output records."""
    df = pd.read_csv(input_path, dtype=str)
    if limit is not None:
        df = df.head(limit)
    cols = _detect_columns(list(df.columns))

    if cols["sic"] is None and cols["ticker"] is None and cols["cik"] is None:
        raise SystemExit(
            "Input CSV needs at least one of: a 'sic' column (offline), or a "
            "'ticker'/'cik' column to resolve the SIC from EDGAR. "
            f"Found columns: {list(df.columns)}"
        )
    if sic_only and cols["sic"] is None:
        raise SystemExit("--sic-only was set but the input CSV has no 'sic' column.")

    # Build the LLM client once (shared across rows) only when asked for.
    llm_client = None
    if use_llm:
        try:
            import anthropic
            llm_client = anthropic.Anthropic()
        except Exception as e:  # noqa: BLE001
            print(f"  ! --llm requested but no client available ({e}); "
                  "ambiguous SICs will use the generic default.", file=sys.stderr)

    records: list[dict] = []
    total = len(df)
    for i, (_, row) in enumerate(df.iterrows(), 1):
        in_ticker = _clean(row[cols["ticker"]]) if cols["ticker"] else None
        in_cik = _clean(row[cols["cik"]]) if cols["cik"] else None
        name = _clean(row[cols["name"]]) if cols["name"] else None
        trbc = _clean(row[cols["trbc"]]) if cols["trbc"] else None
        sic = _clean(row[cols["sic"]]) if cols["sic"] else None
        sic_desc = None
        resolved_cik = in_cik.zfill(10) if in_cik else None
        note_prefix = ""

        # Resolve the SIC from EDGAR when the row didn't carry one.
        if sic is None and not sic_only:
            res = _resolve_sic(in_cik, in_ticker)
            if "error" in res:
                note_prefix = f"EDGAR resolve failed ({res['error']}); "
            else:
                sic = res.get("sic")
                sic_desc = res.get("sic_description")
                resolved_cik = res.get("cik") or resolved_cik
                name = name or res.get("name")

        mc = classify_methodology(
            sic,
            sic_description=sic_desc,
            name=name,
            trbc=trbc,
            llm_client=llm_client,
        )
        notes = (note_prefix + mc.notes).strip().rstrip(";").strip("; ")

        records.append({
            "input_ticker": in_ticker,
            "input_cik": in_cik,
            "cik": resolved_cik,
            "name": name,
            "sic": sic,
            "sic_description": sic_desc,
            "moodys_methodology": mc.moodys_methodology,
            "sp_sector": mc.sp_sector,
            "methodology_confidence": mc.confidence,
            "methodology_source": mc.source,
            "is_financial": mc.is_financial,
            "notes": notes,
        })

        label = in_ticker or in_cik or name or f"row {i}"
        print(f"  [{i}/{total}] {label:16} → {mc.moodys_methodology}  |  "
              f"{mc.sp_sector}  ({mc.confidence})")

    return records


def _write_outputs(records: list[dict], out_base: Path, fmt: str) -> list[Path]:
    """Write records to CSV and/or JSON next to `out_base`; return the paths written."""
    written: list[Path] = []
    out_base.parent.mkdir(parents=True, exist_ok=True)
    if fmt in ("csv", "both"):
        csv_path = out_base.with_suffix(".csv")
        pd.DataFrame(records, columns=OUTPUT_COLUMNS).to_csv(csv_path, index=False)
        written.append(csv_path)
    if fmt in ("json", "both"):
        json_path = out_base.with_suffix(".json")
        json_path.write_text(json.dumps(records, indent=2))
        written.append(json_path)
    return written


def main() -> None:
    p = argparse.ArgumentParser(description="Classify a CSV of issuers into their Moody's and S&P sector methodologies.")
    p.add_argument("input", type=Path, help="Input CSV (columns: sic and/or ticker/cik, optional name/sector)")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="Output base path (no extension). Default: <input>_methodology")
    p.add_argument("--format", choices=["csv", "json", "both"], default="both",
                   help="Output format(s). Default: both")
    p.add_argument("--sic-only", action="store_true",
                   help="Classify only from a 'sic' column; never call EDGAR")
    p.add_argument("--llm", action="store_true",
                   help="Enable the LLM fallback for ambiguous SICs (needs ANTHROPIC_API_KEY)")
    p.add_argument("--limit", type=int, default=None, help="Classify only the first N rows")
    args = p.parse_args()

    if not args.input.exists():
        raise SystemExit(f"Input file not found: {args.input}")

    out_base = args.output or args.input.with_name(args.input.stem + "_methodology")

    print(f"Classifying issuers from {args.input} ...")
    records = classify_file(
        args.input, sic_only=args.sic_only, use_llm=args.llm, limit=args.limit,
    )

    written = _write_outputs(records, out_base, args.format)

    # Summary: confidence mix + the low-confidence review queue count.
    conf = {"high": 0, "medium": 0, "low": 0}
    for r in records:
        conf[r["methodology_confidence"]] = conf.get(r["methodology_confidence"], 0) + 1
    print(f"\nClassified {len(records)} issuers — "
          f"high: {conf['high']}, medium: {conf['medium']}, low: {conf['low']} "
          f"(low = needs analyst review).")
    for path in written:
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()
