#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

def parse_int_any(v: object) -> Optional[int]:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return None
    try:
        return int(float(s))
    except Exception:
        return None

def read_csv_dicts(path: Path) -> Iterable[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            yield {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}

def iter_bl_codes_codesxml(path: Path) -> Iterable[Tuple[str, str, str]]:
    """
    Yields (bl_part_id, element_id, color_val) from BrickLink codes.xml.
    Supports CODE or CODENAME as element identifier.
    """
    ctx = ET.iterparse(str(path), events=("end",))
    for _, elem in ctx:
        if elem.tag != "ITEM":
            continue
        itemtype = (elem.findtext("ITEMTYPE") or "").strip().upper()
        if itemtype and itemtype != "P":
            elem.clear()
            continue
        bl_part_id = (elem.findtext("ITEMID") or "").strip()
        color_val = (elem.findtext("COLOR") or "").strip()
        element_id = (elem.findtext("CODENAME") or elem.findtext("CODE") or "").strip()
        if bl_part_id and element_id and color_val:
            yield bl_part_id, element_id, color_val
        elem.clear()

def load_rb_elements(path: Path) -> Dict[str, Tuple[str, int]]:
    """
    element_id -> (rb_part_num, rb_color_id)
    """
    m: Dict[str, Tuple[str, int]] = {}
    for row in read_csv_dicts(path):
        element_id = (row.get("element_id") or "").strip()
        part_num = (row.get("part_num") or "").strip()
        color_id = parse_int_any(row.get("color_id"))
        if element_id and part_num and color_id is not None:
            m[element_id] = (part_num, color_id)
    return m

def load_color_map(path: Path) -> Dict[int, Dict[str, object]]:
    """
    rb_color_id -> {bl_color_id, bo_color_id, ldraw_color_id, name}
    """
    m: Dict[int, Dict[str, object]] = {}
    for row in read_csv_dicts(path):
        rb_id = parse_int_any(row.get("rb_color_id"))
        if rb_id is None:
            continue
        m[rb_id] = {
            "name": (row.get("name") or "").strip(),
            "bl_color_id": parse_int_any(row.get("bl_color_id")),
            "bo_color_id": parse_int_any(row.get("bo_color_id")),
            "ldraw_color_id": parse_int_any(row.get("ldraw_color_id")),
        }
    return m

def write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames})

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bl-codes-xml", required=True)
    ap.add_argument("--rb-elements", required=True)
    ap.add_argument("--color-map", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--issues", required=True)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    rb_elements = load_rb_elements(Path(args.rb_elements))
    color_map = load_color_map(Path(args.color_map))

    issues: List[Dict[str, object]] = []
    rows: List[Dict[str, object]] = []

    for bl_part_id, element_id, _color_val in iter_bl_codes_codesxml(Path(args.bl_codes_xml)):
        if element_id not in rb_elements:
            issues.append({
                "severity": "ERROR",
                "issue_type": "ELEMENT_NOT_IN_REBRICKABLE_ELEMENTS",
                "bl_part_id": bl_part_id,
                "element_id": element_id,
                "details": "Element ID do BrickLink não encontrado em Rebrickable elements.csv",
            })
            continue

        rb_part_num, rb_color_id = rb_elements[element_id]
        cm = color_map.get(rb_color_id)

        if cm is None:
            issues.append({
                "severity": "ERROR",
                "issue_type": "RB_COLOR_NOT_IN_COLOR_MAP",
                "bl_part_id": bl_part_id,
                "element_id": element_id,
                "details": f"rb_color_id={rb_color_id} não está no color_map.csv",
            })
            continue

        bl_color_id = cm.get("bl_color_id")
        bo_color_id = cm.get("bo_color_id")
        ldraw_color_id = cm.get("ldraw_color_id")

        if bl_color_id is None:
            issues.append({
                "severity": "ERROR",
                "issue_type": "BL_COLOR_ID_MISSING",
                "bl_part_id": bl_part_id,
                "element_id": element_id,
                "details": f"rb_color_id={rb_color_id} sem bl_color_id no color_map",
            })

        rows.append({
            "bl_part_id": bl_part_id,
            "bl_color_id": bl_color_id,
            "element_id": element_id,
            "rb_part_num": rb_part_num,
            "rb_color_id": rb_color_id,
            "ldraw_color_id": ldraw_color_id,
            "bo_color_id": bo_color_id,
            "boid": "",  # reservado (próximo passo)
        })

    # Write CSV
    out_csv = Path(args.out_csv)
    write_csv(out_csv,
              ["bl_part_id", "bl_color_id", "element_id", "rb_part_num", "rb_color_id", "ldraw_color_id", "bo_color_id", "boid"],
              rows)

    # Write issues CSV
    issues_path = Path(args.issues)
    write_csv(issues_path,
              ["severity", "issue_type", "bl_part_id", "element_id", "details"],
              issues)

    # Build DB
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS part_color_map (
      bl_part_id TEXT NOT NULL,
      bl_color_id INTEGER,
      element_id TEXT NOT NULL,
      rb_part_num TEXT NOT NULL,
      rb_color_id INTEGER NOT NULL,
      ldraw_color_id INTEGER,
      bo_color_id INTEGER,
      boid TEXT
    )
    """)
    cur.execute("DELETE FROM part_color_map")
    cur.executemany("""
      INSERT INTO part_color_map
      (bl_part_id, bl_color_id, element_id, rb_part_num, rb_color_id, ldraw_color_id, bo_color_id, boid)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (r["bl_part_id"], r["bl_color_id"], r["element_id"], r["rb_part_num"], r["rb_color_id"],
         r["ldraw_color_id"], r["bo_color_id"], r["boid"])
        for r in rows
    ])

    cur.execute("""
    CREATE TABLE IF NOT EXISTS build_issues (
      severity TEXT,
      issue_type TEXT,
      bl_part_id TEXT,
      element_id TEXT,
      details TEXT
    )
    """)
    cur.execute("DELETE FROM build_issues")
    cur.executemany("""
      INSERT INTO build_issues(severity, issue_type, bl_part_id, element_id, details)
      VALUES (?, ?, ?, ?, ?)
    """, [
        (i["severity"], i["issue_type"], i.get("bl_part_id",""), i.get("element_id",""), i["details"])
        for i in issues
    ])

    con.commit()
    con.close()

    n_err = sum(1 for i in issues if i["severity"] == "ERROR")
    n_warn = sum(1 for i in issues if i["severity"] == "WARN")
    print(f"✅ Wrote: {out_csv} (rows={len(rows)})")
    print(f"✅ Wrote: {issues_path} (issues={len(issues)} | ERR={n_err} WARN={n_warn})")
    print(f"✅ Wrote: {db_path}")

    if args.strict and n_err > 0:
        print("❌ STRICT mode: ERROR issues found. Exiting with code 2.")
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
