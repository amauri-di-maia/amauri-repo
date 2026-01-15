#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Brickovery - build SQLite DB + CSV part_color_map from:
- BrickLink codes.xml (itemid + element_id)
- Rebrickable elements.csv (element_id -> part_num + rb_color_id)
- color_map.csv (rb_color_id -> bl_color_id / bo_color_id / ldraw_color_id)

STRICT: fails only on ERROR.
"""

from __future__ import annotations

import argparse
import csv
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


def write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames})


def iter_bl_codes_codesxml(path: Path) -> Iterable[Tuple[str, str]]:
    """
    Yields (bl_part_id, element_id) from BrickLink codes.xml.
    element_id can be CODENAME or CODE depending on source.
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
        element_id = (elem.findtext("CODENAME") or elem.findtext("CODE") or "").strip()
        if bl_part_id and element_id:
            yield bl_part_id, element_id
        elem.clear()


def load_rb_elements(path: Path) (unavailable):  # placeholder to keep syntax highlighting stable
    pass
