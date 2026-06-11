"""
One-off helper: verify the backtest case-library roster against EDGAR.

For each candidate it resolves the CIK, then checks identity and data
availability directly from SEC sources:
  - registrant name + former names (submissions API)
  - 10-K count filed in the 3.5y window before the event (the backtest window)
  - an 8-K filed within 30 days after the claimed petition date
    (bankruptcies trigger an Item 1.03 8-K, so this corroborates the date)
  - XBRL companyfacts present (us-gaap concept count)

Usage:  python3 -m scripts.verify_cases
"""

from datetime import date, timedelta

from src.ingest import (
    resolve_identifier,
    find_cik_by_name,
    get_company_info,
    get_filings,
    get_company_facts,
)

# (case_id, identifier, event_date, how) — identifier is a ticker, CIK, or
# name (how="name"). CIKs below are candidates to VERIFY, not trusted inputs.
CANDIDATES = [
    ("peabody-2016",    "BTU",                 "2016-04-13", "ticker"),
    ("sunedison-2016",  "SunEdison",           "2016-04-21", "name"),
    ("linn-2016",       "1326428",             "2016-05-11", "cik"),  # Linn Energy LLC (pre-bk filer)
    ("iheart-2018",     "IHRT",                "2018-03-14", "ticker"),
    ("sears-2018",      "Sears Holdings",      "2018-10-15", "name"),
    ("pge-2019",        "PCG",                 "2019-01-29", "ticker"),
    ("windstream-2019", "Windstream Holdings", "2019-02-25", "name"),
    ("deanfoods-2019",  "Dean Foods",          "2019-11-12", "name"),
    ("whiting-2020",    "1255474",             "2020-04-01", "cik"),  # Whiting Petroleum
    ("frontier-2020",   "20520",               "2020-04-14", "cik"),  # Frontier Communications
    ("jcpenney-2020",   "1166126",             "2020-05-15", "cik"),  # J.C. Penney Co (now Old COPPER)
    ("hertz-2020",      "47129",               "2020-05-22", "cik"),  # The Hertz Corporation (operating co.; full XBRL history)
    ("gnc-2020",        "1502034",             "2020-06-23", "cik"),  # GNC Holdings
    ("chesapeake-2020", "CHK",                 "2020-06-28", "ticker"),
    ("mallinckrodt-2020","1567892",            "2020-10-12", "cik"),  # Mallinckrodt plc
    ("revlon-2022",     "887921",              "2022-06-15", "cik"),  # Revlon Inc
    ("partycity-2023",  "1592058",             "2023-01-17", "cik"),  # Party City Holdco
    ("bbby-2023",       "886158",              "2023-04-23", "cik"),  # Bed Bath & Beyond
    ("yellow-2023",     "716006",              "2023-08-06", "cik"),  # Yellow Corp
    ("riteaid-2023",    "84129",               "2023-10-15", "cik"),  # Rite Aid
    ("diebold-2023",    "28823",               "2023-06-01", "cik"),  # Diebold Nixdorf (prepackaged Ch11)
    # healthy controls (no event check)
    ("aapl", "AAPL", "", "ticker"),
    ("msft", "MSFT", "", "ticker"),
    ("jnj",  "JNJ",  "", "ticker"),
    ("pg",   "PG",   "", "ticker"),
    ("ko",   "KO",   "", "ticker"),
    ("wmt",  "WMT",  "", "ticker"),
    ("hd",   "HD",   "", "ticker"),
]


def main() -> None:
    for case_id, ident, event_str, how in CANDIDATES:
        try:
            if how == "name":
                cik = find_cik_by_name(ident)
            else:
                cik = resolve_identifier(ident)
            info = get_company_info(cik)
            former = "; ".join(fn.get("name", "") for fn in info["formerNames"]) or "—"

            tenks = get_filings(cik, ["10-K"])
            eightks = get_filings(cik, ["8-K"])
            facts = get_company_facts(cik)
            n_concepts = len(facts.get("facts", {}).get("us-gaap", {}))

            if event_str:
                event = date.fromisoformat(event_str)
                window_start = (event - timedelta(days=int(3.5 * 365))).isoformat()
                tenks_in_window = [
                    f for f in tenks if window_start <= f["filingDate"] <= event_str
                ]
                ek_after = [
                    f for f in eightks
                    if event_str <= f["filingDate"] <= (event + timedelta(days=30)).isoformat()
                ]
                print(
                    f"{case_id:<18} CIK {cik}  {info['name']!r:<40} "
                    f"10-Ks-in-window={len(tenks_in_window)}  "
                    f"8-K-near-event={'YES' if ek_after else 'NO!'}  "
                    f"concepts={n_concepts}"
                )
            else:
                print(
                    f"{case_id:<18} CIK {cik}  {info['name']!r:<40} "
                    f"10-Ks-total={len(tenks)}  concepts={n_concepts}"
                )
            if former != "—":
                print(f"{'':<18} formerly: {former}")
        except Exception as e:
            print(f"{case_id:<18} FAILED: {e}")


if __name__ == "__main__":
    main()
