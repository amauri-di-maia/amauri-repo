#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Brickovery (core pipeline)

Builds a cross-platform, auditable database from:
- BrickLink XML: colors.xml, parts.xml, codes.xml
- Rebrickable CSV: colors.csv, elements.csv, inventory_parts.csv, inventories.csv, parts.csv
- Brickovery color_map.csv (rb_color_id -> bl_color_id + optional bo_color_id + ldraw_color_id), produced by brickovery_make_colors.py

Outputs:
- data/brickovery.db
- data/part_color_matrix.csv
- data/build_report.md
- data/matrix_issues.csv

Design goals:
- No silent guessing. Any ambiguity becomes an issue.
- "Strict" mode fails on ERROR issues (warnings are allowed).
- Uses Rebrickable elements.csv as the deterministic bridge:
    BrickLink codes.xml CODENAME (Element ID) -> Rebrickable elements.csv element_id -> (rb_part_num, rb_color_id, design_id)
- Propagates ldraw_color_id via color_map.

BrickOwl / BOID:
- This file includes the DB schema fields for BOID mapping and the plumbing for future integration.
- The actual BOID resolution job is intentionally optional and disabled by default.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


# =========================
# Helpers
# =========================

def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(msg, flush=True)


def open_text_maybe_gz(path: Path):
    # Supports .csv or .csv.gz (even though your current files are .csv)
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def norm_color_name(s: str) -> str:
    # Conservative normalization for BrickLink color name matching
    s = (s or "").strip().lower()
    s = s.replace("grey", "gray")
    s = s.replace("&", " and ")
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def detect_header_cols(fieldnames: List[str]) -> Dict[str, str]:
    # case-insensitive map
    return {c.lower(): c for c in fieldnames}


def pick_col(cols: Dict[str, str], *names: str) -> Optional[str]:
    for n in names:
        if n in cols:
            return cols[n]
    return None


# =========================
# DB Schema
# =========================

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at_utc TEXT NOT NULL,
  finished_at_utc TEXT,
  notes TEXT
);

-- BrickLink dimensions
CREATE TABLE IF NOT EXISTS bl_colors (
  bl_color_id INTEGER PRIMARY KEY,
  bl_color_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bl_parts (
  bl_part_id TEXT PRIMARY KEY,
  bl_part_name TEXT,
  bl_category_id TEXT
);

-- BrickLink evidence: part + color + element_id (CODENAME)
CREATE TABLE IF NOT EXISTS bl_part_color_elements (
  bl_part_id TEXT NOT NULL,
  bl_color_id INTEGER NOT NULL,
  element_id TEXT NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (bl_part_id, bl_color_id, element_id)
);

CREATE TABLE IF NOT EXISTS bl_part_color_evidence (
  bl_part_id TEXT NOT NULL,
  bl_color_id INTEGER NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (bl_part_id, bl_color_id)
);

-- Rebrickable dimensions
CREATE TABLE IF NOT EXISTS rb_colors (
  rb_color_id INTEGER PRIMARY KEY,
  rb_color_name TEXT
);

CREATE TABLE IF NOT EXISTS rb_parts (
  rb_part_num TEXT PRIMARY KEY,
  rb_part_name TEXT
);

-- Rebrickable elements bridge
CREATE TABLE IF NOT EXISTS rb_elements (
  element_id TEXT PRIMARY KEY,
  rb_part_num TEXT NOT NULL,
  rb_color_id INTEGER NOT NULL,
  design_id TEXT
);

-- Rebrickable inventory evidence
CREATE TABLE IF NOT EXISTS rb_part_color_evidence (
  rb_part_num TEXT NOT NULL,
  rb_color_id INTEGER NOT NULL,
  num_inventories INTEGER,
  total_qty INTEGER,
  PRIMARY KEY (rb_part_num, rb_color_id)
);

-- Brickovery color map (from brickovery_make_colors.py)
CREATE TABLE IF NOT EXISTS color_map (
  rb_color_id INTEGER PRIMARY KEY,
  bl_color_id INTEGER,
  bo_color_id INTEGER,
  ldraw_color_id INTEGER,
  name TEXT
);

-- Mapping BL part-color -> RB part-color via element IDs (deterministic)
CREATE TABLE IF NOT EXISTS bl_to_rb_by_element (
  bl_part_id TEXT NOT NULL,
  bl_color_id INTEGER NOT NULL,
  rb_part_num TEXT,
  rb_color_id INTEGER,
  design_id TEXT,
  element_id_count INTEGER NOT NULL DEFAULT 0,
  method TEXT NOT NULL,   -- element_id
  status TEXT NOT NULL,   -- OK | WARN | ERROR
  notes TEXT,
  PRIMARY KEY (bl_part_id, bl_color_id)
);

-- Final matrix in BrickLink space with provenance
CREATE TABLE IF NOT EXISTS part_color_matrix (
  bl_part_id TEXT NOT NULL,
  bl_color_id INTEGER NOT NULL,

  has_bl INTEGER NOT NULL DEFAULT 0,
  has_rb INTEGER NOT NULL DEFAULT 0,

  rb_part_num TEXT,
  rb_color_id INTEGER,

  bo_color_id INTEGER,
  boid TEXT,                -- optional future/step
  ldraw_color_id INTEGER,

  source_label TEXT NOT NULL,  -- BL_ONLY | RB_ONLY | BL_AND_RB
  score REAL NOT NULL,
  status TEXT NOT NULL,        -- OK | WARN | ERROR
  notes TEXT,

  run_id TEXT NOT NULL,
  PRIMARY KEY (bl_part_id, bl_color_id)
);

CREATE INDEX IF NOT EXISTS idx_pcm_source ON part_color_matrix(source_label);
CREATE INDEX IF NOT EXISTS idx_pcm_rb_part ON part_color_matrix(rb_part_num);

-- Issues: everything non-deterministic or inconsistent goes here
CREATE TABLE IF NOT EXISTS issues (
  run_id TEXT NOT NULL,
  severity TEXT NOT NULL,    -- ERROR | WARN
  issue_type TEXT NOT NULL,
  bl_part_id TEXT,
  bl_color_id INTEGER,
  rb_part_num TEXT,
  rb_color_id INTEGER,
  element_id TEXT,
  details TEXT,
  PRIMARY KEY (
    run_id, severity, issue_type,
    COALESCE(bl_part_id,''),
    COALESCE(bl_color_id,-1),
    COALESCE(rb_part_num,''),
    COALESCE(rb_color_id,-1),
    COALESCE(element_id,'')
  )
);
"""


@dataclass(frozen=True)
class RunCtx:
    run_id: str
    started_at_utc: str


def add_issue(
    conn: sqlite3.Connection,
    run_id: str,
    severity: str,
    issue_type: str,
    bl_part_id: Optional[str] = None,
    bl_color_id: Optional[int] = None,
    rb_part_num: Optional[str] = None,
    rb_color_id: Optional[int] = None,
    element_id: Optional[str] = None,
    details: str = "",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO issues(
          run_id, severity, issue_type, bl_part_id, bl_color_id, rb_part_num, rb_color_id, element_id, details
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, severity, issue_type,
            bl_part_id, bl_color_id, rb_part_num, rb_color_id, element_id,
            (details or "")[:2000],
        ),
    )


# =========================
# BrickLink parsers (XML)
# =========================

def load_bl_colors(colors_xml: Path, conn: sqlite3.Connection, run_id: str) -> Tuple[Dict[str, int], Dict[int, str]]:
    """
    BrickLink colors.xml:
      <ITEM><COLOR>1</COLOR><COLORNAME>White</COLORNAME>...</ITEM>
    Returns:
      - name_to_id (normalized)
      - id_to_name
    """
    if not colors_xml.exists():
        raise FileNotFoundError(f"Missing BrickLink colors.xml: {colors_xml}")

    tree = ET.parse(str(colors_xml))
    root = tree.getroot()

    name_to_id: Dict[str, int] = {}
    id_to_name: Dict[int, str] = {}

    # build list and detect collisions
    norm_to_ids: Dict[str, Set[int]] = {}

    for item in root.findall("ITEM"):
        cid = (item.findtext("COLOR") or "").strip()
        cname = (item.findtext("COLORNAME") or "").strip()
        if not cid.isdigit() or not cname:
            continue
        bl_id = int(cid)
        n = norm_color_name(cname)
        norm_to_ids.setdefault(n, set()).add(bl_id)
        id_to_name[bl_id] = cname

    for n, ids in norm_to_ids.items():
        if len(ids) == 1:
            name_to_id[n] = next(iter(ids))
        else:
            # ambiguous normalized name (rare) -> WARN, and do not auto-map by norm
            add_issue(conn, run_id, "WARN", "BL_COLORNAME_NORMALIZATION_COLLISION", details=f"{n} -> {sorted(ids)}")

    # persist
    rows = [(cid, nm) for cid, nm in id_to_name.items()]
    conn.executemany("INSERT OR REPLACE INTO bl_colors(bl_color_id, bl_color_name) VALUES(?,?)", rows)
    conn.commit()

    return name_to_id, id_to_name


def load_bl_parts(parts_xml: Path, conn: sqlite3.Connection) -> None:
    """
    BrickLink parts.xml:
      <ITEM><ITEMTYPE>P</ITEMTYPE><ITEMID>3001</ITEMID><ITEMNAME>...</ITEMNAME><CATEGORY>...</CATEGORY></ITEM>
    Streaming parse, store part_id -> name/category.
    """
    if not parts_xml.exists():
        raise FileNotFoundError(f"Missing BrickLink parts.xml: {parts_xml}")

    total = 0
    batch: List[Tuple[str, str, str]] = []

    for _, elem in ET.iterparse(str(parts_xml), events=("end",)):
        if elem.tag.upper() != "ITEM":
            continue

        itemtype = (elem.findtext("ITEMTYPE") or "").strip()
        if itemtype and itemtype.upper() != "P":
            elem.clear()
            continue

        part_id = (elem.findtext("ITEMID") or "").strip()
        name = (elem.findtext("ITEMNAME") or "").strip()
        cat = (elem.findtext("CATEGORY") or "").strip()

        if part_id:
            batch.append((part_id, name or None, cat or None))
            total += 1

        if len(batch) >= 5000:
            conn.executemany(
                "INSERT OR REPLACE INTO bl_parts(bl_part_id, bl_part_name, bl_category_id) VALUES(?,?,?)",
                batch,
            )
            conn.commit()
            batch.clear()

        elem.clear()

    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO bl_parts(bl_part_id, bl_part_name, bl_category_id) VALUES(?,?,?)",
            batch,
        )
        conn.commit()

    log(f"BrickLink parts loaded: {total:,}")


def load_bl_codes(
    codes_xml: Path,
    conn: sqlite3.Connection,
    run_id: str,
    bl_colorname_to_id: Dict[str, int],
) -> None:
    """
    BrickLink codes.xml:
      <ITEM><ITEMTYPE>P</ITEMTYPE><ITEMID>3001</ITEMID><COLOR>White</COLOR><CODENAME>300101</CODENAME></ITEM>

    Stores:
      - bl_part_color_elements(bl_part_id, bl_color_id, element_id)
      - bl_part_color_evidence(bl_part_id, bl_color_id)
    """
    if not codes_xml.exists():
        raise FileNotFoundError(f"Missing BrickLink codes.xml: {codes_xml}")

    combo_counts: Dict[Tuple[str, int], int] = {}
    elem_counts: Dict[Tuple[str, int, str], int] = {}

    total_items = 0
    missing_color = 0

    for _, elem in ET.iterparse(str(codes_xml), events=("end",)):
        if elem.tag.upper() != "ITEM":
            continue

        itemtype = (elem.findtext("ITEMTYPE") or "").strip().upper()
        if itemtype and itemtype != "P":
            elem.clear()
            continue

        part_id = (elem.findtext("ITEMID") or "").strip()
        color_name = (elem.findtext("COLOR") or "").strip()
        code = (elem.findtext("CODENAME") or "").strip()

        total_items += 1

        if not part_id or not color_name:
            elem.clear()
            continue

        n = norm_color_name(color_name)
        bl_color_id = bl_colorname_to_id.get(n)

        if bl_color_id is None:
            missing_color += 1
            add_issue(
                conn, run_id, "ERROR", "BL_CODE_COLOR_NOT_IN_BL_COLORS",
                bl_part_id=part_id, element_id=code or None,
                details=f"codes.xml COLOR='{color_name}' not found in colors.xml",
            )
            elem.clear()
            continue

        # keep BL part existence (best effort)
        conn.execute("INSERT OR IGNORE INTO bl_parts(bl_part_id) VALUES(?)", (part_id,))

        combo_key = (part_id, bl_color_id)
        combo_counts[combo_key] = combo_counts.get(combo_key, 0) + 1

        if code:
            elem_key = (part_id, bl_color_id, code)
            elem_counts[elem_key] = elem_counts.get(elem_key, 0) + 1

        elem.clear()

    # bulk write evidence
    conn.executemany(
        """
        INSERT INTO bl_part_color_evidence(bl_part_id, bl_color_id, occurrences)
        VALUES(?,?,?)
        ON CONFLICT(bl_part_id, bl_color_id) DO UPDATE SET
          occurrences = bl_part_color_evidence.occurrences + excluded.occurrences
        """,
        [(p, c, occ) for (p, c), occ in combo_counts.items()],
    )

    conn.executemany(
        """
        INSERT INTO bl_part_color_elements(bl_part_id, bl_color_id, element_id, occurrences)
        VALUES(?,?,?,?)
        ON CONFLICT(bl_part_id, bl_color_id, element_id) DO UPDATE SET
          occurrences = bl_part_color_elements.occurrences + excluded.occurrences
        """,
        [(p, c, e, occ) for (p, c, e), occ in elem_counts.items()],
    )

    conn.commit()

    log(f"BrickLink codes parsed: {total_items:,} items | distinct part-color: {len(combo_counts):,} | element links: {len(elem_counts):,}")
    if missing_color:
        log(f"BrickLink codes issues: {missing_color:,} items had colors missing from BrickLink colors.xml (ERROR).")


# =========================
# Rebrickable loaders (CSV)
# =========================

def load_rb_colors(colors_csv: Path, conn: sqlite3.Connection) -> None:
    if not colors_csv.exists():
        raise FileNotFoundError(f"Missing Rebrickable colors.csv: {colors_csv}")

    with open_text_maybe_gz(colors_csv) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("Rebrickable colors.csv has no header")

        cols = detect_header_cols(reader.fieldnames)
        c_id = pick_col(cols, "id", "rb_color_id", "color_id")
        c_name = pick_col(cols, "name", "color_name")
        if not c_id or not c_name:
            raise ValueError("Rebrickable colors.csv must include id and name")

        rows = []
        for row in reader:
            try:
                rid = int((row.get(c_id) or "").strip())
            except Exception:
                continue
            nm = (row.get(c_name) or "").strip() or None
            rows.append((rid, nm))

        conn.executemany("INSERT OR REPLACE INTO rb_colors(rb_color_id, rb_color_name) VALUES(?,?)", rows)
        conn.commit()

    log(f"Rebrickable colors loaded: {len(rows):,}")


def load_rb_parts(parts_csv: Path, conn: sqlite3.Connection) -> None:
    if not parts_csv.exists():
        raise FileNotFoundError(f"Missing Rebrickable parts.csv: {parts_csv}")

    with open_text_maybe_gz(parts_csv) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("Rebrickable parts.csv has no header")

        cols = detect_header_cols(reader.fieldnames)
        c_part = pick_col(cols, "part_num")
        c_name = pick_col(cols, "name", "part_name")
        if not c_part:
            raise ValueError("Rebrickable parts.csv must include part_num")

        batch = []
        n = 0
        for row in reader:
            pn = (row.get(c_part) or "").strip()
            if not pn:
                continue
            nm = (row.get(c_name) or "").strip() if c_name else ""
            batch.append((pn, nm or None))
            n += 1
            if len(batch) >= 10000:
                conn.executemany("INSERT OR REPLACE INTO rb_parts(rb_part_num, rb_part_name) VALUES(?,?)", batch)
                conn.commit()
                batch.clear()

        if batch:
            conn.executemany("INSERT OR REPLACE INTO rb_parts(rb_part_num, rb_part_name) VALUES(?,?)", batch)
            conn.commit()

    log(f"Rebrickable parts loaded: {n:,}")


def load_rb_elements(elements_csv: Path, conn: sqlite3.Connection, run_id: str) -> None:
    """
    elements.csv typical columns:
      element_id, part_num, color_id, design_id
    We treat duplicate element_id mapping to different part/color as ERROR.
    """
    if not elements_csv.exists():
        raise FileNotFoundError(f"Missing Rebrickable elements.csv: {elements_csv}")

    seen: Dict[str, Tuple[str, int, Optional[str]]] = {}
    dup_conflicts = 0
    n = 0
    batch = []

    with open_text_maybe_gz(elements_csv) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("Rebrickable elements.csv has no header")

        cols = detect_header_cols(reader.fieldnames)
        c_eid = pick_col(cols, "element_id", "elementid")
        c_part = pick_col(cols, "part_num")
        c_color = pick_col(cols, "color_id", "rb_color_id", "id_color")
        c_design = pick_col(cols, "design_id", "designid")

        if not c_eid or not c_part or not c_color:
            raise ValueError("Rebrickable elements.csv must include element_id, part_num, color_id")

        for row in reader:
            eid = (row.get(c_eid) or "").strip()
            pn = (row.get(c_part) or "").strip()
            try:
                cid = int((row.get(c_color) or "").strip())
            except Exception:
                continue
            did = (row.get(c_design) or "").strip() if c_design else ""

            if not eid or not pn:
                continue

            prev = seen.get(eid)
            if prev and (prev[0] != pn or prev[1] != cid):
                dup_conflicts += 1
                add_issue(
                    conn, run_id, "ERROR", "RB_ELEMENT_ID_CONFLICT",
                    rb_part_num=pn, rb_color_id=cid, element_id=eid,
                    details=f"element_id maps to multiple tuples: prev={prev} new={(pn, cid, did or None)}",
                )
                # keep first mapping to avoid non-determinism
                continue

            if not prev:
                seen[eid] = (pn, cid, did or None)

            n += 1
            batch.append((eid, pn, cid, did or None))

            if len(batch) >= 20000:
                conn.executemany(
                    "INSERT OR REPLACE INTO rb_elements(element_id, rb_part_num, rb_color_id, design_id) VALUES(?,?,?,?)",
                    batch,
                )
                conn.commit()
                batch.clear()

        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO rb_elements(element_id, rb_part_num, rb_color_id, design_id) VALUES(?,?,?,?)",
                batch,
            )
            conn.commit()

    log(f"Rebrickable elements loaded: {len(seen):,} unique element_ids")
    if dup_conflicts:
        log(f"Rebrickable elements conflicts: {dup_conflicts:,} (ERROR issues recorded)")


def build_rb_inventory_evidence(inventory_parts_csv: Path, conn: sqlite3.Connection) -> None:
    """
    Builds rb_part_color_evidence by exact aggregation from inventory_parts.csv (can be large).
    Uses python batching into sqlite staging table and a GROUP BY with COUNT(DISTINCT inventory_id).
    """
    if not inventory_parts_csv.exists():
        raise FileNotFoundError(f"Missing Rebrickable inventory_parts.csv: {inventory_parts_csv}")

    log("Building RB inventory evidence (exact aggregation)...")

    conn.execute("DROP TABLE IF EXISTS rb_inv_parts_stage")
    conn.execute("""
      CREATE TABLE rb_inv_parts_stage(
        inventory_id INTEGER,
        part_num TEXT,
        color_id INTEGER,
        quantity INTEGER
      )
    """)
    conn.commit()

    with open_text_maybe_gz(inventory_parts_csv) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("Rebrickable inventory_parts.csv has no header")

        cols = detect_header_cols(reader.fieldnames)
        c_inv = pick_col(cols, "inventory_id")
        c_part = pick_col(cols, "part_num")
        c_color = pick_col(cols, "color_id", "rb_color_id")
        c_qty = pick_col(cols, "quantity", "qty")

        if not c_inv or not c_part or not c_color or not c_qty:
            raise ValueError("inventory_parts.csv must include inventory_id, part_num, color_id, quantity")

        batch: List[Tuple[int, str, int, int]] = []
        BATCH = 50000
        n = 0

        for row in reader:
            try:
                inv_id = int((row.get(c_inv) or "").strip())
                pn = (row.get(c_part) or "").strip()
                cid = int((row.get(c_color) or "").strip())
                qty = int((row.get(c_qty) or "").strip())
            except Exception:
                continue
            if not pn:
                continue
            batch.append((inv_id, pn, cid, qty))
            n += 1

            if len(batch) >= BATCH:
                conn.executemany("INSERT INTO rb_inv_parts_stage VALUES(?,?,?,?)", batch)
                conn.commit()
                batch.clear()
                if n % 250000 == 0:
                    log(f"  imported {n:,} rows...")

        if batch:
            conn.executemany("INSERT INTO rb_inv_parts_stage VALUES(?,?,?,?)", batch)
            conn.commit()
            batch.clear()

    log(f"RB inventory_parts imported: {n:,} rows. Aggregating...")

    conn.execute("DELETE FROM rb_part_color_evidence")
    conn.execute(
        """
        INSERT INTO rb_part_color_evidence(rb_part_num, rb_color_id, num_inventories, total_qty)
        SELECT
          part_num,
          color_id,
          COUNT(DISTINCT inventory_id) AS num_inventories,
          SUM(quantity) AS total_qty
        FROM rb_inv_parts_stage
        GROUP BY part_num, color_id
        """
    )
    conn.commit()

    conn.execute("DROP TABLE rb_inv_parts_stage")
    conn.commit()

    cnt = conn.execute("SELECT COUNT(*) FROM rb_part_color_evidence").fetchone()[0]
    log(f"RB evidence built: {cnt:,} distinct (part,color)")


# =========================
# Brickovery color_map loader
# =========================

def load_color_map(color_map_csv: Path, conn: sqlite3.Connection, run_id: str) -> None:
    if not color_map_csv.exists():
        raise FileNotFoundError(f"Missing Brickovery color_map.csv: {color_map_csv}")

    with color_map_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("color_map.csv has no header")

        cols = detect_header_cols(reader.fieldnames)
        c_name = pick_col(cols, "name")
        c_rb = pick_col(cols, "rb_color_id", "id", "color_id")
        c_bl = pick_col(cols, "bl_color_id", "bricklink_color_id")
        c_bo = pick_col(cols, "bo_color_id", "brickowl_color_id")
        c_ld = pick_col(cols, "ldraw_color_id", "ldraw_id")

        if not c_rb:
            raise ValueError("color_map.csv must include rb_color_id")
        if not c_bl:
            add_issue(conn, run_id, "ERROR", "COLOR_MAP_MISSING_BL_COLOR_ID_COL", details="color_map.csv missing bl_color_id column")
            raise ValueError("color_map.csv must include bl_color_id")

        rows = []
        missing_bl = 0
        for row in reader:
            try:
                rb_id = int((row.get(c_rb) or "").strip())
            except Exception:
                continue

            bl_val = (row.get(c_bl) or "").strip()
            if bl_val == "":
                missing_bl += 1
                add_issue(conn, run_id, "ERROR", "COLOR_MAP_RB_WITHOUT_BL", rb_color_id=rb_id if False else None, details=f"rb_color_id={rb_id} has empty bl_color_id")
                continue
            try:
                bl_id = int(bl_val)
            except Exception:
                add_issue(conn, run_id, "ERROR", "COLOR_MAP_BL_NOT_INT", details=f"rb_color_id={rb_id} bl_color_id='{bl_val}'")
                continue

            bo_id = None
            if c_bo:
                v = (row.get(c_bo) or "").strip()
                if v.isdigit():
                    bo_id = int(v)

            ld_id = None
            if c_ld:
                v = (row.get(c_ld) or "").strip()
                if v.isdigit():
                    ld_id = int(v)

            nm = (row.get(c_name) or "").strip() if c_name else ""

            rows.append((rb_id, bl_id, bo_id, ld_id, nm))

        conn.executemany(
            "INSERT OR REPLACE INTO color_map(rb_color_id, bl_color_id, bo_color_id, ldraw_color_id, name) VALUES(?,?,?,?,?)",
            rows,
        )
        conn.commit()

    log(f"Brickovery color_map loaded: {len(rows):,} rows")


# =========================
# Mapping BL -> RB using element IDs
# =========================

def build_bl_to_rb_by_element(conn: sqlite3.Connection, run_id: str) -> None:
    """
    For each (bl_part_id, bl_color_id), collect all element_ids,
    map element_id -> (rb_part_num, rb_color_id, design_id) via rb_elements.
    Must be deterministic:
      - If multiple element_ids map to different rb tuples => ERROR (ambiguous)
      - If none found => WARN (no bridge), but still allowed as BL_ONLY
    Additionally validate color consistency:
      - rb_color_id maps to bl_color_id via color_map; if mismatch => ERROR
    """
    # load rb_elements map in-memory for speed: element_id -> tuple
    rb_elems: Dict[str, Tuple[str, int, Optional[str]]] = {}
    for eid, pn, cid, did in conn.execute("SELECT element_id, rb_part_num, rb_color_id, design_id FROM rb_elements"):
        rb_elems[str(eid)] = (str(pn), int(cid), did if did else None)

    # rb_color_id -> bl_color_id from color_map
    rb_to_bl: Dict[int, int] = {}
    for rb_id, bl_id in conn.execute("SELECT rb_color_id, bl_color_id FROM color_map"):
        rb_to_bl[int(rb_id)] = int(bl_id)

    # Iterate BL combos and element_ids
    combos = conn.execute("SELECT bl_part_id, bl_color_id FROM bl_part_color_evidence").fetchall()
    log(f"Building BL->RB element bridge for {len(combos):,} BrickLink part-color combos...")

    conn.execute("DELETE FROM bl_to_rb_by_element")
    conn.commit()

    batch = []
    for bl_part_id, bl_color_id in combos:
        bl_part_id = str(bl_part_id)
        bl_color_id = int(bl_color_id)

        eids = [r[0] for r in conn.execute(
            "SELECT element_id FROM bl_part_color_elements WHERE bl_part_id=? AND bl_color_id=?",
            (bl_part_id, bl_color_id),
        ).fetchall()]

        mapped: List[Tuple[str, int, Optional[str], str]] = []  # (rb_part_num, rb_color_id, design_id, element_id)
        missing = 0
        for eid in eids:
            t = rb_elems.get(str(eid))
            if not t:
                missing += 1
                continue
            mapped.append((t[0], t[1], t[2], str(eid)))

        if not mapped:
            status = "WARN"
            notes = f"No rb_elements match for {len(eids)} element_ids (missing={missing})"
            add_issue(
                conn, run_id, "WARN", "BL_ELEMENT_IDS_NOT_FOUND_IN_RB_ELEMENTS",
                bl_part_id=bl_part_id, bl_color_id=bl_color_id,
                details=notes,
            )
            batch.append((bl_part_id, bl_color_id, None, None, None, len(eids), "element_id", status, notes))
            continue

        # Check determinism: all mapped element_ids must agree on rb tuple
        uniq = {(m[0], m[1], m[2]) for m in mapped}
        if len(uniq) != 1:
            status = "ERROR"
            # include up to 6 examples
            examples = "; ".join(f"{pn}/{cid}/{did or ''} via {eid}" for pn, cid, did, eid in mapped[:6])
            notes = f"Ambiguous mapping: {len(uniq)} unique rb tuples for this BL combo. Examples: {examples}"
            add_issue(
                conn, run_id, "ERROR", "BL_TO_RB_AMBIGUOUS_BY_ELEMENT",
                bl_part_id=bl_part_id, bl_color_id=bl_color_id,
                details=notes,
            )
            batch.append((bl_part_id, bl_color_id, None, None, None, len(eids), "element_id", status, notes))
            continue

        rb_part_num, rb_color_id, design_id = next(iter(uniq))
        rb_color_id = int(rb_color_id)

        # Validate RB->BL color consistency using color_map
        mapped_bl = rb_to_bl.get(rb_color_id)
        if mapped_bl is None:
            status = "ERROR"
            notes = f"rb_color_id={rb_color_id} not present in color_map.csv"
            add_issue(
                conn, run_id, "ERROR", "RB_COLOR_NOT_IN_COLOR_MAP",
                bl_part_id=bl_part_id, bl_color_id=bl_color_id,
                rb_part_num=rb_part_num, rb_color_id=rb_color_id,
                details=notes,
            )
            batch.append((bl_part_id, bl_color_id, rb_part_num, rb_color_id, design_id, len(eids), "element_id", status, notes))
            continue

        if int(mapped_bl) != bl_color_id:
            status = "ERROR"
            notes = f"Color mismatch: BL bl_color_id={bl_color_id} but rb_color_id={rb_color_id} maps to bl_color_id={mapped_bl}"
            add_issue(
                conn, run_id, "ERROR", "BL_RB_COLOR_MISMATCH_VIA_ELEMENT",
                bl_part_id=bl_part_id, bl_color_id=bl_color_id,
                rb_part_num=rb_part_num, rb_color_id=rb_color_id,
                details=notes,
            )
            batch.append((bl_part_id, bl_color_id, rb_part_num, rb_color_id, design_id, len(eids), "element_id", status, notes))
            continue

        status = "OK" if missing == 0 else "WARN"
        notes = f"Mapped via element_ids={len(eids)} (matched={len(mapped)} missing={missing})"
        if status == "WARN":
            add_issue(
                conn, run_id, "WARN", "BL_ELEMENT_ID_PARTIAL_MATCH",
                bl_part_id=bl_part_id, bl_color_id=bl_color_id,
                rb_part_num=rb_part_num, rb_color_id=rb_color_id,
                details=notes,
            )

        batch.append((bl_part_id, bl_color_id, rb_part_num, rb_color_id, design_id, len(eids), "element_id", status, notes))

        if len(batch) >= 5000:
            conn.executemany(
                """
                INSERT OR REPLACE INTO bl_to_rb_by_element(
                  bl_part_id, bl_color_id, rb_part_num, rb_color_id, design_id, element_id_count, method, status, notes
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                batch,
            )
            conn.commit()
            batch.clear()

    if batch:
        conn.executemany(
            """
            INSERT OR REPLACE INTO bl_to_rb_by_element(
              bl_part_id, bl_color_id, rb_part_num, rb_color_id, design_id, element_id_count, method, status, notes
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            batch,
        )
        conn.commit()

    log("BL->RB bridge built.")


# =========================
# Build final matrix
# =========================

def compute_score(source_label: str, bridge_status: str, has_rb: bool) -> Tuple[float, str]:
    """
    Conservative scoring:
      - BL_AND_RB: 1.00 base
      - BL_ONLY:   0.70 base
      - RB_ONLY:   0.60 base
    Penalize WARN/ERROR bridge status.
    """
    if source_label == "BL_AND_RB":
        s = 1.0
    elif source_label == "BL_ONLY":
        s = 0.70
    else:
        s = 0.60

    if bridge_status == "WARN":
        s -= 0.10
    elif bridge_status == "ERROR":
        s -= 0.30

    if s < 0.0:
        s = 0.0

    if bridge_status == "ERROR":
        status = "ERROR"
    elif bridge_status == "WARN":
        status = "WARN"
    else:
        status = "OK"

    # If RB evidence is missing while we have a bridge tuple, mark WARN (not error)
    if not has_rb and status == "OK":
        status = "WARN"

    return s, status


def build_matrix(conn: sqlite3.Connection, run_id: str) -> None:
    """
    Matrix rules:
    - Start from BL part-color evidence (codes.xml)
    - Add RB_ONLY where RB combos can be mapped to BL color via color_map and to BL part via:
        a) exact part id exists in bl_parts
        b) reverse mapping from element bridge (rb_part_num -> unique bl_part_id)
      (RB_ONLY is optional; if can't map deterministically, record WARN issue and skip.)
    - Propagate:
        bo_color_id and ldraw_color_id via color_map using rb_color_id OR by matching bl_color_id through map.
    """
    conn.execute("DELETE FROM part_color_matrix WHERE run_id=?", (run_id,))
    conn.commit()

    # RB evidence set
    rb_ev: Set[Tuple[str, int]] = set()
    for pn, cid in conn.execute("SELECT rb_part_num, rb_color_id FROM rb_part_color_evidence"):
        rb_ev.add((str(pn), int(cid)))

    # color_map indexes
    rb_to_bl: Dict[int, Tuple[int, Optional[int], Optional[int]]] = {}
    # rb_color_id -> (bl_color_id, bo_color_id, ldraw_color_id)
    for rb_id, bl_id, bo_id, ld_id in conn.execute("SELECT rb_color_id, bl_color_id, bo_color_id, ldraw_color_id FROM color_map"):
        rb_to_bl[int(rb_id)] = (int(bl_id), int(bo_id) if bo_id is not None else None, int(ld_id) if ld_id is not None else None)

    # Build reverse part mapping from element bridge: rb_part_num -> {bl_part_id}
    rb_to_bl_parts: Dict[str, Set[str]] = {}
    for bl_part_id, bl_color_id, rb_part_num, rb_color_id in conn.execute(
        "SELECT bl_part_id, bl_color_id, rb_part_num, rb_color_id FROM bl_to_rb_by_element WHERE rb_part_num IS NOT NULL AND rb_color_id IS NOT NULL"
    ):
        rb_to_bl_parts.setdefault(str(rb_part_num), set()).add(str(bl_part_id))

    # Build BL matrix rows first (BL_SET)
    bl_rows = conn.execute("SELECT bl_part_id, bl_color_id, occurrences FROM bl_part_color_evidence").fetchall()

    batch = []
    for bl_part_id, bl_color_id, occ in bl_rows:
        bl_part_id = str(bl_part_id)
        bl_color_id = int(bl_color_id)

        bridge = conn.execute(
            "SELECT rb_part_num, rb_color_id, status, notes FROM bl_to_rb_by_element WHERE bl_part_id=? AND bl_color_id=?",
            (bl_part_id, bl_color_id),
        ).fetchone()

        rb_part_num = None
        rb_color_id = None
        bridge_status = "WARN"
        bridge_notes = ""
        has_rb = 0
        if bridge:
            rb_part_num = bridge[0]
            rb_color_id = bridge[1]
            bridge_status = bridge[2] or "WARN"
            bridge_notes = bridge[3] or ""
            if rb_part_num is not None and rb_color_id is not None:
                if (str(rb_part_num), int(rb_color_id)) in rb_ev:
                    has_rb = 1

        has_bl = 1

        if has_rb:
            source_label = "BL_AND_RB"
        else:
            source_label = "BL_ONLY"

        # propagate bo_color_id / ldraw_color_id
        bo_color_id = None
        ldraw_color_id = None
        if rb_color_id is not None:
            m = rb_to_bl.get(int(rb_color_id))
            if m:
                bo_color_id = m[1]
                ldraw_color_id = m[2]
        else:
            # fallback: find any rb_color_id in color_map that maps to this bl_color_id (should be 1:1 normally)
            # if multiple, leave blank (avoid risk)
            candidates = [rb for rb, (bl, bo, ld) in rb_to_bl.items() if bl == bl_color_id]
            if len(candidates) == 1:
                bo_color_id = rb_to_bl[candidates[0]][1]
                ldraw_color_id = rb_to_bl[candidates[0]][2]

        score, status = compute_score(source_label, bridge_status, bool(has_rb))
        notes = f"bl_occurrences={occ}; bridge={bridge_notes}"

        batch.append((
            bl_part_id, bl_color_id,
            has_bl, has_rb,
            rb_part_num, int(rb_color_id) if rb_color_id is not None else None,
            bo_color_id, None,  # boid None for now
            ldraw_color_id,
            source_label, float(score), status,
            notes,
            run_id,
        ))

        if len(batch) >= 10000:
            conn.executemany(
                """
                INSERT OR REPLACE INTO part_color_matrix(
                  bl_part_id, bl_color_id, has_bl, has_rb,
                  rb_part_num, rb_color_id,
                  bo_color_id, boid,
                  ldraw_color_id,
                  source_label, score, status, notes, run_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                batch,
            )
            conn.commit()
            batch.clear()

    if batch:
        conn.executemany(
            """
            INSERT OR REPLACE INTO part_color_matrix(
              bl_part_id, bl_color_id, has_bl, has_rb,
              rb_part_num, rb_color_id,
              bo_color_id, boid,
              ldraw_color_id,
              source_label, score, status, notes, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            batch,
        )
        conn.commit()

    log("Matrix built from BL evidence.")

    # Optional RB_ONLY enrichment:
    # Add RB combos that can be deterministically mapped into BrickLink space.
    # This is not mandatory for initial cross-shopping, but useful for completeness.
    added_rb_only = 0
    for rb_part_num, rb_color_id in rb_ev:
        m = rb_to_bl.get(int(rb_color_id))
        if not m:
            # should not happen if color_map is perfect; still guard
            add_issue(conn, run_id, "ERROR", "RB_COLOR_NOT_IN_COLOR_MAP_DURING_RB_ONLY", rb_part_num=rb_part_num, rb_color_id=rb_color_id)
            continue
        bl_color_id = m[0]
        bo_color_id = m[1]
        ldraw_color_id = m[2]

        # Map rb_part_num -> bl_part_id deterministically
        bl_part_id = None
        # a) if rb_part_num exists in BrickLink parts.xml, treat as same id
        exists = conn.execute("SELECT 1 FROM bl_parts WHERE bl_part_id=? LIMIT 1", (rb_part_num,)).fetchone()
        if exists:
            bl_part_id = rb_part_num
        else:
            # b) reverse element bridge
            cands = rb_to_bl_parts.get(rb_part_num) or set()
            if len(cands) == 1:
                bl_part_id = next(iter(cands))
            elif len(cands) > 1:
                add_issue(conn, run_id, "WARN", "RB_PART_MAPS_TO_MULTIPLE_BL_PARTS", rb_part_num=rb_part_num, details=f"candidates={sorted(list(cands))[:10]}")
                continue
            else:
                # no deterministic mapping, skip
                continue

        # if already exists (from BL), skip
        ex = conn.execute(
            "SELECT 1 FROM part_color_matrix WHERE run_id=? AND bl_part_id=? AND bl_color_id=? LIMIT 1",
            (run_id, bl_part_id, int(bl_color_id)),
        ).fetchone()
        if ex:
            continue

        source_label = "RB_ONLY"
        score, status = compute_score(source_label, "OK", True)
        notes = "Added from RB inventory evidence (no BL codes evidence)."

        conn.execute(
            """
            INSERT OR REPLACE INTO part_color_matrix(
              bl_part_id, bl_color_id, has_bl, has_rb,
              rb_part_num, rb_color_id,
              bo_color_id, boid,
              ldraw_color_id,
              source_label, score, status, notes, run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (bl_part_id, int(bl_color_id), 0, 1, rb_part_num, int(rb_color_id), bo_color_id, None, ldraw_color_id,
             source_label, float(score), status, notes, run_id),
        )
        added_rb_only += 1

    conn.commit()
    log(f"RB_ONLY rows added: {added_rb_only:,}")


# =========================
# Export / Report
# =========================

def export_matrix_csv(conn: sqlite3.Connection, run_id: str, out_csv: Path) -> None:
    ensure_dir(out_csv.parent)
    rows = conn.execute(
        """
        SELECT
          bl_part_id, bl_color_id,
          source_label, score, status,
          rb_part_num, rb_color_id,
          bo_color_id, boid,
          ldraw_color_id,
          notes
        FROM part_color_matrix
        WHERE run_id=?
        ORDER BY source_label DESC, score DESC, bl_part_id ASC, bl_color_id ASC
        """,
        (run_id,),
    ).fetchall()

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "bl_part_id", "bl_color_id",
            "source_label", "score", "status",
            "rb_part_num", "rb_color_id",
            "bo_color_id", "boid",
            "ldraw_color_id",
            "notes",
        ])
        for r in rows:
            w.writerow(r)

    log(f"Exported matrix CSV: {out_csv} ({len(rows):,} rows)")


def export_issues_csv(conn: sqlite3.Connection, run_id: str, out_csv: Path) -> None:
    ensure_dir(out_csv.parent)
    rows = conn.execute(
        """
        SELECT severity, issue_type, bl_part_id, bl_color_id, rb_part_num, rb_color_id, element_id, details
        FROM issues
        WHERE run_id=?
        ORDER BY severity DESC, issue_type ASC
        """,
        (run_id,),
    ).fetchall()

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["severity", "issue_type", "bl_part_id", "bl_color_id", "rb_part_num", "rb_color_id", "element_id", "details"])
        for r in rows:
            w.writerow(r)

    log(f"Exported issues CSV: {out_csv} ({len(rows):,} rows)")


def export_report(conn: sqlite3.Connection, run_id: str, out_md: Path) -> None:
    ensure_dir(out_md.parent)

    total = conn.execute("SELECT COUNT(*) FROM part_color_matrix WHERE run_id=?", (run_id,)).fetchone()[0]
    by_source = conn.execute(
        "SELECT source_label, COUNT(*) FROM part_color_matrix WHERE run_id=? GROUP BY source_label",
        (run_id,),
    ).fetchall()

    by_status = conn.execute(
        "SELECT status, COUNT(*) FROM part_color_matrix WHERE run_id=? GROUP BY status",
        (run_id,),
    ).fetchall()

    issues = conn.execute(
        "SELECT severity, issue_type, COUNT(*) FROM issues WHERE run_id=? GROUP BY severity, issue_type ORDER BY severity DESC, COUNT(*) DESC",
        (run_id,),
    ).fetchall()

    err_cnt = conn.execute("SELECT COUNT(*) FROM issues WHERE run_id=? AND severity='ERROR'", (run_id,)).fetchone()[0]
    warn_cnt = conn.execute("SELECT COUNT(*) FROM issues WHERE run_id=? AND severity='WARN'", (run_id,)).fetchone()[0]

    lines: List[str] = []
    lines.append("# Brickovery – Build report")
    lines.append(f"- run_id: `{run_id}`")
    lines.append("")
    lines.append("## Matrix totals")
    lines.append(f"- Total rows: **{total:,}**")
    lines.append("")
    lines.append("### By source_label")
    for k, v in by_source:
        lines.append(f"- {k}: **{int(v):,}**")
    lines.append("")
    lines.append("### By status")
    for k, v in by_status:
        lines.append(f"- {k}: **{int(v):,}**")
    lines.append("")
    lines.append("## Issues")
    lines.append(f"- ERROR: **{err_cnt:,}**")
    lines.append(f"- WARN: **{warn_cnt:,}**")
    lines.append("")
    if issues:
        lines.append("### By type")
        for sev, it, cnt in issues:
            lines.append(f"- {sev} / {it}: **{int(cnt):,}**")
    else:
        lines.append("- No issues recorded.")

    out_md.write_text("\n".join(lines), encoding="utf-8")
    log(f"Exported report: {out_md}")


def fail_if_errors(conn: sqlite3.Connection, run_id: str) -> None:
    err_cnt = conn.execute("SELECT COUNT(*) FROM issues WHERE run_id=? AND severity='ERROR'", (run_id,)).fetchone()[0]
    if err_cnt:
        raise RuntimeError(f"STRICT: build failed due to {err_cnt} ERROR issues (see data/matrix_issues.csv)")


# =========================
# Main
# =========================

def main() -> int:
    ap = argparse.ArgumentParser(description="Brickovery - Build DB + part/color matrix from BrickLink XML and Rebrickable CSV")
    ap.add_argument("--db", default="data/brickovery.db")
    ap.add_argument("--outdir", default="data")

    ap.add_argument("--bl-dir", default="inputs/bricklink", help="BrickLink XML folder (colors.xml, parts.xml, codes.xml)")
    ap.add_argument("--rb-dir", default="inputs/rebrickable", help="Rebrickable CSV folder (colors.csv, elements.csv, inventory_parts.csv, inventories.csv, parts.csv)")

    ap.add_argument("--color-map", default="data/color_map.csv", help="Brickovery master color_map.csv produced by brickovery_make_colors.py")

    ap.add_argument("--strict", action="store_true", help="Fail build if any ERROR issues are recorded")

    args = ap.parse_args()

    outdir = Path(args.outdir)
    ensure_dir(outdir)

    bl_dir = Path(args.bl_dir)
    rb_dir = Path(args.rb_dir)

    bl_colors_xml = bl_dir / "colors.xml"
    bl_parts_xml = bl_dir / "parts.xml"
    bl_codes_xml = bl_dir / "codes.xml"

    rb_colors_csv = rb_dir / "colors.csv"
    rb_parts_csv = rb_dir / "parts.csv"
    rb_elements_csv = rb_dir / "elements.csv"
    rb_inventory_parts_csv = rb_dir / "inventory_parts.csv"

    color_map_csv = Path(args.color_map)

    run_id = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    ctx = RunCtx(run_id=run_id, started_at_utc=utc_now_iso())

    db_path = Path(args.db)
    ensure_dir(db_path.parent)

    log(f"Run: {run_id}")
    log("Opening DB...")

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT OR REPLACE INTO runs(run_id, started_at_utc) VALUES(?,?)", (ctx.run_id, ctx.started_at_utc))
    conn.commit()

    # Load color_map first (so RB element bridge can validate colors)
    log("Loading Brickovery color_map.csv ...")
    load_color_map(color_map_csv, conn, ctx.run_id)

    # BrickLink: colors + parts + codes
    log("Loading BrickLink colors.xml ...")
    bl_colorname_to_id, _id_to_name = load_bl_colors(bl_colors_xml, conn, ctx.run_id)

    log("Loading BrickLink parts.xml ...")
    load_bl_parts(bl_parts_xml, conn)

    log("Parsing BrickLink codes.xml ...")
    load_bl_codes(bl_codes_xml, conn, ctx.run_id, bl_colorname_to_id)

    # Rebrickable: colors + parts + elements + inventory evidence
    log("Loading Rebrickable colors.csv ...")
    load_rb_colors(rb_colors_csv, conn)

    log("Loading Rebrickable parts.csv ...")
    load_rb_parts(rb_parts_csv, conn)

    log("Loading Rebrickable elements.csv ...")
    load_rb_elements(rb_elements_csv, conn, ctx.run_id)

    log("Building Rebrickable inventory evidence from inventory_parts.csv ...")
    build_rb_inventory_evidence(rb_inventory_parts_csv, conn)

    # Build deterministic bridge and final matrix
    log("Building BL->RB bridge via element IDs ...")
    build_bl_to_rb_by_element(conn, ctx.run_id)

    log("Building final matrix ...")
    build_matrix(conn, ctx.run_id)

    # Exports
    export_matrix_csv(conn, ctx.run_id, outdir / "part_color_matrix.csv")
    export_issues_csv(conn, ctx.run_id, outdir / "matrix_issues.csv")
    export_report(conn, ctx.run_id, outdir / "build_report.md")

    # Strict gate
    if args.strict:
        fail_if_errors(conn, ctx.run_id)

    conn.execute("UPDATE runs SET finished_at_utc=? WHERE run_id=?", (utc_now_iso(), ctx.run_id))
    conn.commit()
    conn.close()

    log("DONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
