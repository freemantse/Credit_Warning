"""
Batch methodology classifier: CSV of issuers in → per-issuer Moody's + S&P sector
methodology out (CSV and/or JSON).

For each issuer this emits ONE Moody's methodology and ONE S&P sector (plus a
confidence and the source of the decision) via methodology_classifier.classify_methodology.

INPUT CSV — one issuer per row. Columns are detected case-insensitively; you need
enough to identify the issuer's industry:
  - `sic`                 → classified directly, fully OFFLINE (fastest).
  - `ticker` and/or `cik` → the SIC is resolved from SEC EDGAR first (needs network;
                            set SEC_USER_AGENT with your contact — see edgar.py).
  - `name`                → optional; improves the LLM fallback for ambiguous SICs and
                            is used as a display label.
  - `sector` / `trbc`     → optional TRBC cross-check that can lower confidence on a conflict.
A `sic` column, when present, always wins and skips the EDGAR round-trip.

OUTPUT — one row per input issuer with: input_ticker, input_cik, cik (resolved), name,
sic, sic_description, moodys_methodology, sp_sector, methodology_confidence,
methodology_source, is_financial, notes.

Usage:
    python -m methodology_classifier issuers.csv                 # → issuers_methodology.csv + .json
    python -m methodology_classifier issuers.csv -o out/result   # → out/result.csv + out/result.json
    python -m methodology_classifier issuers.csv --format csv    # CSV only
    python -m methodology_classifier issuers.csv --sic-only      # never call EDGAR (requires a sic column)
    python -m methodology_classifier issuers.csv --llm           # enable LLM fallback for ambiguous SICs
    python -m methodology_classifier issuers.csv --limit 50      # first N rows (pilot)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .classifier import classify_methodology

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
        "sic": find(lambda l: l in ("sic", "sic_code", "siccode")),
        "ticker": find(lambda l: "ticker" in l or l == "symbol"),
        "cik": find(lambda l: l == "cik"),
        "name": find(lambda l: l == "name" or "company" in l or "issuer" in l),
        "trbc": find(lambda l: "trbc" in l or l == "sector" or "economic sector" in l),
    }


def _clean(v) -> str | None:
    """Normalise a cell to a stripped string, or None for empty / NaN-like values."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "<na>", "null"}:
        return None
    return s


def classify_rows(
    rows: list[dict],
    columns: list[str],
    *,
    sic_only: bool = False,
    use_llm: bool = False,
) -> list[dict]:
    """Classify a list of input row dicts and return the output records."""
    cols = _detect_columns(columns)

    if cols["sic"] is None and cols["ticker"] is None and cols["cik"] is None:
        raise SystemExit(
            "Input CSV needs at least one of: a 'sic' column (offline), or a "
            "'ticker'/'cik' column to resolve the SIC from EDGAR. "
            f"Found columns: {columns}"
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

    # Import the EDGAR resolver lazily so pure --sic-only runs need no network stack.
    resolve_sic = None
    if cols["sic"] is None or not sic_only:
        from .edgar import resolve_sic as _resolve_sic
        resolve_sic = _resolve_sic

    records: list[dict] = []
    total = len(rows)
    for i, row in enumerate(rows, 1):
        in_ticker = _clean(row.get(cols["ticker"])) if cols["ticker"] else None
        in_cik = _clean(row.get(cols["cik"])) if cols["cik"] else None
        name = _clean(row.get(cols["name"])) if cols["name"] else None
        trbc = _clean(row.get(cols["trbc"])) if cols["trbc"] else None
        sic = _clean(row.get(cols["sic"])) if cols["sic"] else None
        sic_desc = None
        resolved_cik = in_cik.zfill(10) if in_cik else None
        note_prefix = ""

        # Resolve the SIC from EDGAR when the row didn't carry one.
        if sic is None and not sic_only and resolve_sic is not None:
            res = resolve_sic(in_cik or in_ticker or "")
            if "error" in res:
                note_prefix = f"EDGAR resolve failed ({res['error']}); "
            else:
                sic = res.get("sic")
                sic_desc = res.get("sic_description")
                resolved_cik = res.get("cik") or resolved_cik
                name = name or res.get("name")

        mc = classify_methodology(
            sic, sic_description=sic_desc, name=name, trbc=trbc, llm_client=llm_client,
        )
        notes = (note_prefix + mc.notes).strip().strip(";").strip()

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


def _read_csv(path: Path) -> tuple[list[dict], list[str]]:
    """Read a CSV into (row dicts, column names) using the stdlib csv module."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def _write_outputs(records: list[dict], out_base: Path, fmt: str) -> list[Path]:
    """Write records to CSV and/or JSON next to `out_base`; return the paths written."""
    written: list[Path] = []
    out_base.parent.mkdir(parents=True, exist_ok=True)
    if fmt in ("csv", "both"):
        csv_path = out_base.with_suffix(".csv")
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
            w.writeheader()
            w.writerows(records)
        written.append(csv_path)
    if fmt in ("json", "both"):
        json_path = out_base.with_suffix(".json")
        json_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        written.append(json_path)
    return written


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="methodology_classifier",
        description="Classify a CSV of issuers into their Moody's and S&P sector methodologies.",
    )
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
    args = p.parse_args(argv)

    if not args.input.exists():
        raise SystemExit(f"Input file not found: {args.input}")

    rows, columns = _read_csv(args.input)
    if args.limit is not None:
        rows = rows[: args.limit]

    out_base = args.output or args.input.with_name(args.input.stem + "_methodology")

    print(f"Classifying issuers from {args.input} ...")
    records = classify_rows(rows, columns, sic_only=args.sic_only, use_llm=args.llm)
    written = _write_outputs(records, out_base, args.format)

    conf: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    for r in records:
        conf[r["methodology_confidence"]] = conf.get(r["methodology_confidence"], 0) + 1
    print(f"\nClassified {len(records)} issuers — "
          f"high: {conf['high']}, medium: {conf['medium']}, low: {conf['low']} "
          f"(low = needs analyst review).")
    for path in written:
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()
