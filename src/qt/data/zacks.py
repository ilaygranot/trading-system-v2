"""Import of the monthly Zacks screen export that Ilay uploads (the course screens with Zacks).

The export format is not confirmed yet (first upload expected next month), so the importer maps
column names loosely and reports anything it could not map instead of guessing.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from qt.data.store import Store

# canonical field -> accepted header spellings (normalized: lowercase, alphanumerics only)
COLUMNS = {
    "ticker": ["ticker", "symbol"],
    "company": ["companyname", "company"],
    "sector": ["sector"],
    "industry": ["industry", "mindustry", "expandedindustry"],
    "price": ["lastclose", "price", "lastprice"],
    "market_cap_mil": ["marketcapmil", "marketcap"],
    "eps_f0": ["lastreportedfiscalyearactual", "f0actual", "epsf0", "actualepsf0", "f0consensusest"],
    "eps_f1": ["f1consensusest", "currentfiscalyearest", "epsf1"],
    "eps_f2": ["f2consensusest", "nextfiscalyearest", "epsf2"],
    "rev_f1": ["f1salesest", "salesf1", "revenuef1"],
    "rev_f2": ["f2salesest", "salesf2", "revenuef2"],
    "zacks_rank": ["zacksrank"],
    "pe_f1": ["pefy1", "pef1", "pecurrentfy"],
    "ps": ["ps", "pricetosales", "psttm"],
    "short_float": ["shortinterestfloat", "shortfloat"],
}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


@dataclass
class ZacksImport:
    as_of: date
    rows: list[dict]
    mapped: dict[str, str]
    unmapped_headers: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)


def _num(v: str) -> float | None:
    v = (v or "").replace(",", "").replace("$", "").replace("%", "").strip()
    if v in ("", "NA", "N/A", "--"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse(text: str, as_of: date) -> ZacksImport:
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    mapped: dict[str, str] = {}
    for h in headers:
        for field_name, spellings in COLUMNS.items():
            if _norm(h) in spellings and field_name not in mapped:
                mapped[field_name] = h
    rows = []
    for raw in reader:
        row = {}
        for f, h in mapped.items():
            row[f] = raw[h].strip() if f in ("ticker", "company", "sector", "industry") else _num(raw[h])
        rows.append(row)
    return ZacksImport(as_of, rows, mapped,
                       unmapped_headers=[h for h in headers if h not in mapped.values()],
                       missing_fields=[f for f in COLUMNS if f not in mapped])


def import_file(path: str | Path, as_of: date, store: Store | None = None) -> ZacksImport:
    imp = parse(Path(path).read_text(encoding="utf-8-sig"), as_of)
    if store is not None:
        for r in imp.rows:
            if r.get("ticker"):
                store.snapshot("zacks_screen", r["ticker"], as_of, f"zacks:{Path(path).name}", r)
    return imp
