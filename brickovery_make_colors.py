#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
brickovery_make_colors.py

Builds a future-proof LEGO color mapping table by consolidating:
- BrickLink color IDs & names (from inputs/bricklink/colors.xml)
- Rebrickable colors download (colors.csv or colors.csv.gz)
- Your existing seed mapping (colors_seed.csv)

Primary output:
  inputs/color_map.csv
  Columns (stable contract):
    name, rb_color_id, bl_color_id, bo_color_id, ldraw_color_id, bl_color_name, bo_color_name

Additional outputs:
  inputs/color_map_audit.csv
    Adds: rb_name, rb_rgb, rb_is_trans
  data/color_map_issues.csv
    All unresolved/ambiguous items with suggestions

Important rule (MANDATORY):
- bo_color_name is mandatory whenever bo_color_id exists.
  If bo_color_id is present but bo_color_name cannot be resolved, an issue is emitted (BO_NAME_MISSING).
  With --strict, the script exits with code 2 when ANY issue exists.

Notes for GitHub Actions:
- Prefer storing API keys as repository secrets and injecting them as env vars.
- This script supports both a local constant and env var (env overrides the constant).

"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


# =============================
# CONFIG: API KEYS / ENDPOINTS
# =============================
# You can put your keys here for local runs.
# In GitHub Actions, prefer setting secrets as env vars (recommended).
#
# Supported env vars:
#   BRICKOWL_API_KEY
#
# If both are present, the env var wins.
BRICKOWL_API_KEY = "70a9f2fe436f657241b6332ca02b87c55b2d644ca04c8bbbe6d98813dcb3c046"  # <-- optionally set here

# Reserved / future use (not used by this script yet):
REBRICKABLE_API_KEY = "726574f0de061233d6ba4b1c87557302"  # Rebrickable v3 API key (if you later switch to API instead of downloads)
BRICKLINK_CONSUMER_KEY = ""  # BrickLink OAuth (if you later fetch BL data via API)
BRICKLINK_CONSUMER_SECRET = ""
BRICKLINK_TOKEN = ""
BRICKLINK_TOKEN_SECRET = ""

BRICKOWL_API_BASE = "https://api.brickowl.com"
BRICKOWL_COLOR_LIST_PATH = "/v1/catalog/color_list"


# =============================
# Helpers: IO
# =============================
def open_text_maybe_gz(path: Path, encoding: str = "utf-8") -> io.TextIOBase:
    if path.suffix.lower() == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding=encoding, newline="")
    return open(path, "r", encoding=encoding, newline="")


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: List[dict], fieldnames: List[str]) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


# =============================
# Parsing: BrickLink colors.xml
# =============================
def parse_bricklink_colors_xml(path: Path) -> Dict[int, str]:
    """
    Parses a BrickLink colors.xml in a tolerant way.

    We try to locate nodes that look like "COLOR" entries with an integer id and a name.
    Supported child tags (case-insensitive):
      id:   id, colorid, color_id, bricklink_color_id, color_id
      name: name, colorname, color_name
    """
    tree = ET.parse(path)
    root = tree.getroot()

    # Normalized map
    out: Dict[int, str] = {}

    id_keys = {"id", "colorid", "color_id", "bricklink_color_id", "bricklinkcolorid", "bricklinkid", "bl_color_id", "blcolorid"}
    name_keys = {"name", "colorname", "color_name", "color"}

    for node in root.iter():
        # Consider only nodes with children (potential structured entries)
        children = list(node)
        if not children:
            continue

        d: Dict[str, str] = {}
        for c in children:
            tag = (c.tag or "").strip().lower()
            txt = (c.text or "").strip()
            if not tag or not txt:
                continue
            d[tag] = txt

        # Find id/name within this node
        cid_txt = None
        for k in id_keys:
            if k in d:
                cid_txt = d[k]
                break
        if cid_txt is None:
            # fallback: any tag that contains 'color' and ends with 'id'
            for k, v in d.items():
                if "color" in k and k.endswith("id"):
                    cid_txt = v
                    break

        name_txt = None
        for k in name_keys:
            if k in d:
                name_txt = d[k]
                break
        if name_txt is None:
            # fallback: any tag that contains 'name' and 'color'
            for k, v in d.items():
                if "name" in k and "color" in k:
                    name_txt = v
                    break

        if not cid_txt or not name_txt:
            continue

        try:
            cid = int(str(cid_txt).strip())
        except Exception:
            continue

        name_txt = str(name_txt).strip()
        if name_txt:
            out[cid] = name_txt

    return out


# =============================
# Parsing: Rebrickable colors.csv(.gz)
# =============================
@dataclass(frozen=True)
class RBColor:
    rb_color_id: int
    name: str
    ldraw_color_id: Optional[int]
    rgb: Optional[str]
    is_trans: Optional[bool]


def parse_rebrickable_colors_csv(path: Path) -> List[RBColor]:
    with open_text_maybe_gz(path) as f:
        r = csv.DictReader(f)
        # normalize header -> lower
        field_map = {k.lower(): k for k in (r.fieldnames or [])}

        def get(row: dict, key: str) -> str:
            k = field_map.get(key.lower())
            return (row.get(k, "") if k else "") or ""

        out: List[RBColor] = []
        for row in r:
            rb_id_raw = get(row, "id")
            if not str(rb_id_raw).strip():
                continue
            try:
                rb_id = int(str(rb_id_raw).strip())
            except Exception:
                continue

            name = get(row, "name").strip()

            # ldraw id can appear as ldraw_id in some exports
            ldraw_raw = get(row, "ldraw_id").strip()
            ldraw_val: Optional[int] = None
            if ldraw_raw:
                try:
                    ldraw_val = int(ldraw_raw)
                except Exception:
                    ldraw_val = None

            rgb = get(row, "rgb").strip() or None

            is_trans_raw = get(row, "is_trans").strip().lower()
            is_trans: Optional[bool] = None
            if is_trans_raw in {"0", "false", "f", "no", "n"}:
                is_trans = False
            elif is_trans_raw in {"1", "true", "t", "yes", "y"}:
                is_trans = True

            out.append(RBColor(rb_id, name, ldraw_val, rgb, is_trans))

    # stable order
    out.sort(key=lambda x: x.rb_color_id)
    return out


# =============================
# Parsing: Seed file
# =============================
@dataclass
class SeedRow:
    rb_color_id: int
    name: Optional[str] = None
    bl_color_id: Optional[int] = None
    bo_color_id: Optional[int] = None
    ldraw_color_id: Optional[int] = None
    bo_color_name: Optional[str] = None


def _parse_int_opt(v: str) -> Optional[int]:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return None


def parse_seed_csv(path: Path) -> Dict[int, SeedRow]:
    with open_text_maybe_gz(path) as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            return {}

        # normalize columns
        cols = {c.lower().strip(): c for c in r.fieldnames}

        def pick(row: dict, *keys: str) -> str:
            for k in keys:
                kk = cols.get(k.lower())
                if kk and (row.get(kk) is not None):
                    return str(row.get(kk) or "")
            return ""

        out: Dict[int, SeedRow] = {}
        for row in r:
            rb_raw = pick(row, "rb_color_id", "rb_id", "rebrickable_color_id", "rbcolorid", "rb")
            rb_id = _parse_int_opt(rb_raw)
            if rb_id is None:
                continue

            name = pick(row, "name", "color_name", "color").strip() or None
            bl_id = _parse_int_opt(pick(row, "bl_color_id", "bricklink_color_id", "blcolorid"))
            bo_id = _parse_int_opt(pick(row, "bo_color_id", "brickowl_color_id", "bocolorid", "bo"))
            ldraw_id = _parse_int_opt(pick(row, "ldraw_color_id", "ldraw_id", "ldraw"))
            bo_name = pick(row, "bo_color_name", "brickowl_color_name", "bo_name").strip() or None

            out[rb_id] = SeedRow(
                rb_color_id=rb_id,
                name=name,
                bl_color_id=bl_id,
                bo_color_id=bo_id,
                ldraw_color_id=ldraw_id,
                bo_color_name=bo_name,
            )
        return out


# =============================
# BrickOwl color list (file or API)
# =============================
def _parse_bo_color_list_payload(payload) -> Dict[int, str]:
    """
    Accepts multiple known shapes:
      - dict: { "0": "Not Applicable", "10": "Black", ... }
      - dict: { "0": {"name":"Not Applicable", ...}, ... }
      - list: [ {"id":10,"name":"Black"}, {"color_id":10,"name":"Black"}, ... ]
    Returns: {bo_color_id:int -> bo_color_name:str}
    """
    out: Dict[int, str] = {}

    if isinstance(payload, dict):
        for k, v in payload.items():
            ks = str(k).strip()
            if not ks.isdigit():
                continue
            cid = int(ks)
            if isinstance(v, str):
                nm = v.strip()
            elif isinstance(v, dict):
                nm = str(v.get("name") or v.get("lego_name") or v.get("color_name") or "").strip()
            else:
                nm = ""
            if nm:
                out[cid] = nm
        return out

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            cid_raw = item.get("color_id", item.get("id", item.get("bo_color_id")))
            try:
                cid = int(str(cid_raw).strip())
            except Exception:
                continue
            nm = str(item.get("name") or item.get("lego_name") or item.get("color_name") or "").strip()
            if nm:
                out[cid] = nm
        return out

    return out


def fetch_brickowl_color_list(api_key: str, timeout_s: int = 25) -> Dict[int, str]:
    """
    Calls BrickOwl Catalog API:
      GET https://api.brickowl.com/v1/catalog/color_list?key=...
    This endpoint requires Catalog API access enabled for the key.
    """
    base = BRICKOWL_API_BASE.rstrip("/") + BRICKOWL_COLOR_LIST_PATH
    url = base + "?" + urllib.parse.urlencode({"key": api_key})
    req = urllib.request.Request(url, headers={"User-Agent": "Brickovery/1.0 (color-map)"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    return _parse_bo_color_list_payload(payload)


def read_brickowl_colors_file(path: Path) -> Dict[int, str]:
    """
    Reads BrickOwl color list from:
      - JSON (optionally .gz)
      - CSV  (optionally .gz)

    For CSV, accepts flexible columns (case-insensitive):
      - bo_color_id | color_id | id  (required)
      - bo_color_name | name | color_name | lego_name (required)
    """
    suffixes = "".join(path.suffixes).lower()
    if suffixes.endswith(".json") or suffixes.endswith(".json.gz"):
        with open_text_maybe_gz(path) as f:
            payload = json.load(f)
        return _parse_bo_color_list_payload(payload)

    # CSV
    with open_text_maybe_gz(path) as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            return {}

        cols = {c.lower().strip(): c for c in r.fieldnames}

        def pick(row: dict, *keys: str) -> str:
            for k in keys:
                kk = cols.get(k.lower())
                if kk and (row.get(kk) is not None):
                    return str(row.get(kk) or "")
            return ""

        out: Dict[int, str] = {}
        for row in r:
            cid = _parse_int_opt(pick(row, "bo_color_id", "color_id", "id"))
            nm = pick(row, "bo_color_name", "name", "color_name", "lego_name").strip()
            if cid is None or not nm:
                continue
            out[cid] = nm
        return out


# =============================
# Name normalization & matching
# =============================
_norm_re = re.compile(r"[^a-z0-9]+")

def norm_name(s: str) -> str:
    s = (s or "").strip().lower()
    # collapse some common noise
    s = s.replace("&", "and")
    s = _norm_re.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_by_name(name: str, id_to_name: Dict[int, str]) -> List[int]:
    """
    Returns candidate IDs whose normalized name matches the provided name.
    """
    nn = norm_name(name)
    if not nn:
        return []
    candidates: List[int] = []
    for cid, cname in id_to_name.items():
        if norm_name(cname) == nn:
            candidates.append(cid)
    return sorted(candidates)


# =============================
# Build mapping & issues
# =============================
def build_color_map(
    rb_colors: List[RBColor],
    bl_colors: Dict[int, str],
    seed: Dict[int, SeedRow],
    bo_colors: Dict[int, str],
) -> Tuple[List[dict], List[dict]]:
    """
    Returns:
      rows_out (for color_map.csv)
      issues   (for issues.csv)
    """
    rows: List[dict] = []
    issues: List[dict] = []

    for rb in rb_colors:
        s = seed.get(rb.rb_color_id)

        name = (s.name if (s and s.name) else rb.name).strip()
        bl_color_id = s.bl_color_id if s else None
        bo_color_id = s.bo_color_id if s else None
        ldraw_color_id = s.ldraw_color_id if (s and s.ldraw_color_id is not None) else rb.ldraw_color_id

        # If no BL id from seed, try name match
        if bl_color_id is None and name:
            cands = match_by_name(name, bl_colors)
            if len(cands) == 1:
                bl_color_id = cands[0]
            elif len(cands) > 1:
                issues.append({
                    "rb_color_id": rb.rb_color_id,
                    "name": name,
                    "issue": "AMBIGUOUS_BL_NAME",
                    "details": f"Multiple BrickLink color IDs match name '{name}'",
                    "suggestions": ",".join(map(str, cands)),
                })

        # If no BO id from seed, try name match (only if we have a BO color list)
        if bo_color_id is None and bo_colors and name:
            cands = match_by_name(name, bo_colors)
            if len(cands) == 1:
                bo_color_id = cands[0]
            elif len(cands) > 1:
                issues.append({
                    "rb_color_id": rb.rb_color_id,
                    "name": name,
                    "issue": "AMBIGUOUS_BO_NAME",
                    "details": f"Multiple BrickOwl color IDs match name '{name}'",
                    "suggestions": ",".join(map(str, cands)),
                })

        # Derive names
        bl_color_name = bl_colors.get(bl_color_id, "") if bl_color_id is not None else ""

        bo_color_name = ""
        if s and s.bo_color_name:
            bo_color_name = s.bo_color_name.strip()
        if not bo_color_name and bo_color_id is not None:
            bo_color_name = (bo_colors.get(bo_color_id) or "").strip()

        # Issues: missing IDs
        if bl_color_id is None:
            issues.append({
                "rb_color_id": rb.rb_color_id,
                "name": name,
                "issue": "BL_ID_MISSING",
                "details": "No BrickLink color ID resolved (seed missing and no unique name match)",
                "suggestions": "",
            })

        if bo_color_id is None:
            issues.append({
                "rb_color_id": rb.rb_color_id,
                "name": name,
                "issue": "BO_ID_MISSING",
                "details": "No BrickOwl color ID resolved (seed missing and no unique name match)",
                "suggestions": "",
            })

        # Mandatory: bo_color_name if bo_color_id exists
        if bo_color_id is not None and not bo_color_name:
            issues.append({
                "rb_color_id": rb.rb_color_id,
                "name": name,
                "issue": "BO_NAME_MISSING",
                "details": "bo_color_id is present but bo_color_name could not be resolved (seed missing and/or BO color list unavailable)",
                "suggestions": "Provide --bo-colors (file) or BRICKOWL_API_KEY / --bo-api-key to fetch color_list; or add bo_color_name in seed",
            })

        # Optional: validate bo_color_id exists in bo_colors list (when list present)
        if bo_color_id is not None and bo_colors and bo_color_id not in bo_colors:
            issues.append({
                "rb_color_id": rb.rb_color_id,
                "name": name,
                "issue": "INVALID_BO_ID",
                "details": f"bo_color_id={bo_color_id} not found in BrickOwl color list source",
                "suggestions": "",
            })

        row = {
            "name": name,
            "rb_color_id": rb.rb_color_id,
            "bl_color_id": bl_color_id if bl_color_id is not None else "",
            "bo_color_id": bo_color_id if bo_color_id is not None else "",
            "ldraw_color_id": ldraw_color_id if ldraw_color_id is not None else "",
            "bl_color_name": bl_color_name,
            "bo_color_name": bo_color_name,
        }
        rows.append(row)

    return rows, issues


def build_audit_rows(rows: List[dict], rb_colors: Dict[int, RBColor]) -> List[dict]:
    out: List[dict] = []
    for r in rows:
        rb_id = int(r["rb_color_id"])
        rb = rb_colors.get(rb_id)
        audit = dict(r)
        audit["rb_name"] = rb.name if rb else ""
        audit["rb_rgb"] = rb.rgb if (rb and rb.rgb) else ""
        audit["rb_is_trans"] = "" if (not rb or rb.is_trans is None) else ("1" if rb.is_trans else "0")
        out.append(audit)
    return out


# =============================
# CLI
# =============================
def resolve_bo_api_key(cli_key: str) -> str:
    if cli_key and cli_key.strip():
        return cli_key.strip()
    env = (os.environ.get("BRICKOWL_API_KEY") or "").strip()
    if env:
        return env
    if BRICKOWL_API_KEY and BRICKOWL_API_KEY.strip():
        return BRICKOWL_API_KEY.strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bl-colors-xml", required=True, help="BrickLink colors.xml (e.g., inputs/bricklink/colors.xml)")
    ap.add_argument("--rb-colors", required=True, help="Rebrickable downloads colors.csv or colors.csv.gz")
    ap.add_argument("--seed", required=True, help="Seed mapping CSV (your current map), renamed to avoid collision (e.g., inputs/colors_seed.csv)")
    ap.add_argument("--bo-colors", default="", help="Optional BrickOwl color list file (.json/.csv, optionally .gz) to populate bo_color_name and validate bo_color_id")
    ap.add_argument("--bo-api-key", default="", help="Optional BrickOwl API key (overrides env/constant) used to call /v1/catalog/color_list")
    ap.add_argument("--out", default="inputs/color_map.csv", help="Output mapping CSV")
    ap.add_argument("--audit", default="inputs/color_map_audit.csv", help="Output audit CSV")
    ap.add_argument("--issues", default="data/color_map_issues.csv", help="Issues CSV")
    ap.add_argument("--strict", action="store_true", help="Fail (exit 2) if any issues are found")
    args = ap.parse_args()

    bl_xml = Path(args.bl_colors_xml)
    rb_csv = Path(args.rb_colors)
    seed_csv = Path(args.seed)
    bo_colors_path = Path(args.bo_colors) if args.bo_colors else None

    for p in (bl_xml, rb_csv, seed_csv):
        if not p.exists():
            raise FileNotFoundError(f"Missing: {p}")

    # Load sources
    bl_colors = parse_bricklink_colors_xml(bl_xml)
    rb_list = parse_rebrickable_colors_csv(rb_csv)
    seed = parse_seed_csv(seed_csv)

    # Load BrickOwl colors (file then API)
    bo_colors: Dict[int, str] = {}
    if bo_colors_path:
        if not bo_colors_path.exists():
            raise FileNotFoundError(f"Missing: {bo_colors_path}")
        bo_colors = read_brickowl_colors_file(bo_colors_path)

    if not bo_colors:
        key = resolve_bo_api_key(args.bo_api_key)
        if key:
            try:
                bo_colors = fetch_brickowl_color_list(key)
            except Exception as e:
                # keep running; issues will explain that BO names cannot be resolved
                bo_colors = {}

    rows, issues = build_color_map(rb_list, bl_colors, seed, bo_colors)

    # Build audit
    rb_map = {c.rb_color_id: c for c in rb_list}
    audit_rows = build_audit_rows(rows, rb_map)

    # Write outputs
    out_path = Path(args.out)
    audit_path = Path(args.audit)
    issues_path = Path(args.issues)

    write_csv(
        out_path,
        rows,
        ["name", "rb_color_id", "bl_color_id", "bo_color_id", "ldraw_color_id", "bl_color_name", "bo_color_name"],
    )
    write_csv(
        audit_path,
        audit_rows,
        ["name", "rb_color_id", "bl_color_id", "bo_color_id", "ldraw_color_id", "bl_color_name", "bo_color_name", "rb_name", "rb_rgb", "rb_is_trans"],
    )
    write_csv(
        issues_path,
        issues,
        ["rb_color_id", "name", "issue", "details", "suggestions"],
    )

    # Summary
    print(f"✅ Wrote: {out_path}  (rows={len(rows)})")
    print(f"✅ Wrote: {audit_path} (rows={len(audit_rows)})")
    print(f"✅ Wrote: {issues_path} (issues={len(issues)})")

    if issues and args.strict:
        print("❌ STRICT mode: issues found. Exiting with code 2.")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
