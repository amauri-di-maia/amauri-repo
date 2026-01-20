#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Brickovery - make color_map.csv with conflict resolution driven by BrickLink PART ID.

GitHub Actions secrets injection (YAML) SHOULD look like:
  REBRICKABLE_API_KEY: ${{ secrets.REBRICKABLE_API_KEY }}
  BRICKOWL_API_KEY: ${{ secrets.BRICKOWL_API_KEY }}
  BRICKLINK_CONSUMER_KEY: ${{ secrets.BRICKLINK_CONSUMER_KEY }}
  BRICKLINK_CONSUMER_SECRET: ${{ secrets.BRICKLINK_CONSUMER_SECRET }}
  BRICKLINK_TOKEN: ${{ secrets.BRICKLINK_TOKEN }}
  BRICKLINK_TOKEN_SECRET: ${{ secrets.BRICKLINK_TOKEN_SECRET }}

This script reads those values from environment variables (os.environ).

Inputs:
- BrickLink: colors.xml, codes.xml
- Rebrickable: colors.csv, elements.csv
- Seed: colors_seed.csv (authoritative overrides)

Outputs:
- data/color_map.csv
- data/color_map_audit.csv
- data/color_map_issues.csv

Conflict handling (requested):
When these arise (derived from element crosswalk):
- BL_CODE_ELEMENT_COLOR_CONFLICT
- RB_TO_BL_CONFLICT
- BL_ID_MISSING_RELEVANT

Process:
1) derive involved BrickLink part id(s)
2) query BrickLink API Get Known Colors for that part id (authoritative)
3) if BrickLink API fails -> try Rebrickable API by BrickLink id to fetch part colors and project to BL via current mapping
4) apply the best-supported BL color id, otherwise keep as unresolved WARN and suggest seed fix

Key fix (Jan 2026):
- BrickLink endpoint /items/{type}/{no}/colors expects path types like "part", not the XML itemtype "P".
  We now map P->part, S->set, M->minifig, etc.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

import requests
from requests_oauthlib import OAuth1


# -----------------------------
# Secrets / Environment
# -----------------------------

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


REBRICKABLE_API_KEY = _env("REBRICKABLE_API_KEY")
BRICKOWL_API_KEY = _env("BRICKOWL_API_KEY")

BRICKLINK_CONSUMER_KEY = _env("BRICKLINK_CONSUMER_KEY")
BRICKLINK_CONSUMER_SECRET = _env("BRICKLINK_CONSUMER_SECRET")
BRICKLINK_TOKEN = _env("BRICKLINK_TOKEN")
BRICKLINK_TOKEN_SECRET = _env("BRICKLINK_TOKEN_SECRET")


# -----------------------------
# Helpers
# -----------------------------

def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("grey", "gray")
    s = s.replace("&", " and ")
    s = re.sub(r"[-–—_/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return s


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


def is_sentinel_rb_color_id(rb_color_id: int) -> bool:
    """Return True for placeholder/sentinel Rebrickable color ids (e.g., -1 = [Unknown])."""
    return rb_color_id < 0


# RB colors that are allowed to have no BrickLink equivalent (keep bl_color_id NULL without WARN).
# These are typically IR/Lens or internal/special colors that do not exist in BrickLink's color guide.
RB_COLOR_IDS_ALLOW_BL_NULL = {32}

def is_disallowed_bl_color_id(bl_color_id: Optional[int]) -> bool:
    # BrickLink color_id=0 is 'Not Applicable' and MUST NOT be used as an automatic mapping for real colors.
    return bl_color_id == 0


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


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def trunc(s: str, n: int = 800) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "..."


# -----------------------------
# Models
# -----------------------------

@dataclass
class RBColor:
    rb_color_id: int
    name: str
    rgb: str
    is_trans: Optional[int]
    ldraw_color_id: Optional[int]

@dataclass
class RBElement:
    element_id: str
    part_num: str
    rb_color_id: int


def _hex_to_rgb(hexs: str):
    hexs = (hexs or '').strip().lstrip('#')
    if len(hexs) != 6:
        return None
    try:
        r = int(hexs[0:2], 16)
        g = int(hexs[2:4], 16)
        b = int(hexs[4:6], 16)
        return (r, g, b)
    except Exception:
        return None


def _rgb_dist(a: str, b: str) -> Optional[int]:
    ra = _hex_to_rgb(a)
    rb = _hex_to_rgb(b)
    if not ra or not rb:
        return None
    return (ra[0]-rb[0])**2 + (ra[1]-rb[1])**2 + (ra[2]-rb[2])**2


def _jaccard_tokens(a: str, b: str) -> float:
    ta = set(norm(a).split())
    tb = set(norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def tie_break_by_rb_color(rb_name: str, rb_rgb: str, candidates: List[int], bl_id_to_name: Dict[int, str], bl_id_to_rgb: Dict[int, str]) -> Optional[int]:
    """Resolve candidate BL color by comparing to Rebrickable color metadata.

    Strategy:
    1) pick max Jaccard token similarity between RB name and BL name
    2) if tie, pick min RGB distance (if available)
    3) if still tie, return None
    """
    if not candidates:
        return None
    # Name similarity stage
    sims = {c: _jaccard_tokens(rb_name, bl_id_to_name.get(c, '')) for c in candidates}
    best_sim = max(sims.values())
    top = [c for c, v in sims.items() if v == best_sim]
    if len(top) == 1 and best_sim > 0.0:
        return top[0]

    # RGB distance stage
    dists = {}
    for c in top:
        d = _rgb_dist(rb_rgb, bl_id_to_rgb.get(c, ''))
        if d is not None:
            dists[c] = d
    if dists:
        best = sorted(dists.items(), key=lambda kv: (kv[1], kv[0]))[0][0]
        # ensure uniqueness
        best_d = dists[best]
        if sum(1 for _, v in dists.items() if v == best_d) == 1:
            return best
    return None



@dataclass
class SeedColor:
    rb_color_id: int
    name: str
    bl_color_id: Optional[int]
    bo_color_id: Optional[int]
    ldraw_color_id: Optional[int]


# -----------------------------
# BrickLink XML
# -----------------------------

def load_bl_colors_xml(bl_colors_xml: Path) -> Tuple[Dict[str, int], Dict[int, str], Dict[int, str]]:
    root = ET.parse(str(bl_colors_xml)).getroot()
    items = root.findall("ITEM")
    if not items:
        raise RuntimeError(f"BrickLink colors.xml has 0 ITEM nodes: {bl_colors_xml}")

    name_to_id: Dict[str, int] = {}
    id_to_name: Dict[int, str] = {}
    id_to_rgb: Dict[int, str] = {}

    for it in items:
        cid = parse_int_any(it.findtext("COLOR"))
        nm = (it.findtext("COLORNAME") or "").strip()
        rgb = (it.findtext("COLORRGB") or "").strip()
        if cid is None or not nm:
            continue
        name_to_id[norm(nm)] = cid
        id_to_name[cid] = nm
        id_to_rgb[cid] = rgb

    return name_to_id, id_to_name, id_to_rgb


def iter_bl_codes_items(bl_codes_xml: Path) -> Iterable[Tuple[str, str, str, str]]:
    """Yields (itemtype, itemid, element_id, color_val) from BrickLink codes.xml."""
    ctx = ET.iterparse(str(bl_codes_xml), events=("end",))
    for _, elem in ctx:
        if elem.tag != "ITEM":
            continue
        itemtype = (elem.findtext("ITEMTYPE") or "").strip().upper()
        itemid = (elem.findtext("ITEMID") or "").strip()
        element_id = (elem.findtext("CODENAME") or elem.findtext("CODE") or "").strip()
        color_val = (elem.findtext("COLOR") or "").strip()
        if itemtype and itemid and element_id and color_val:
            yield itemtype, itemid, element_id, color_val
        elem.clear()


# -----------------------------
# Rebrickable CSV
# -----------------------------

def load_rb_colors(rb_colors_csv: Path) -> Dict[int, RBColor]:
    out: Dict[int, RBColor] = {}
    for row in read_csv_dicts(rb_colors_csv):
        rb_id = parse_int_any(row.get("id") or row.get("rb_color_id") or row.get("color_id"))
        if rb_id is None:
            continue
        name = (row.get("name") or row.get("color_name") or f"RB_{rb_id}").strip()
        rgb = (row.get("rgb") or "").strip()
        is_trans = parse_int_any(row.get("is_trans") or row.get("transparent"))
        ldraw = parse_int_any(row.get("ldraw_id") or row.get("ldraw_color_id"))
        out[rb_id] = RBColor(rb_color_id=rb_id, name=name, rgb=rgb, is_trans=is_trans, ldraw_color_id=ldraw)
    return out


def load_rb_elements(rb_elements_csv: Path) -> Dict[str, RBElement]:
    """element_id -> RBElement(part_num, rb_color_id).

    Expected headers (Rebrickable elements.csv variants):
      - element_id
      - part_num (or part_id)
      - color_id (or colour_id)
    """
    out: Dict[str, RBElement] = {}
    for row in read_csv_dicts(rb_elements_csv):
        element_id = (row.get('element_id') or row.get('element') or '').strip()
        part_num = (row.get('part_num') or row.get('part_id') or row.get('part') or '').strip()
        color_id = parse_int_any(row.get('color_id') or row.get('colour_id') or row.get('rb_color_id'))
        if element_id and color_id is not None:
            out[element_id] = RBElement(element_id=element_id, part_num=part_num, rb_color_id=color_id)
    return out


# -----------------------------
# Seed
# -----------------------------

def load_seed(seed_csv: Path, rb_colors: Dict[int, RBColor], bl_id_to_name: Dict[int, str]) -> Tuple[Dict[int, SeedColor], List[Dict[str, object]]]:
    seed: Dict[int, SeedColor] = {}
    issues: List[Dict[str, object]] = []

    if not seed_csv.exists():
        return seed, issues

    seen = set()
    for line_no, row in enumerate(read_csv_dicts(seed_csv), start=2):
        name = (row.get("name") or "").strip()
        rb_id = parse_int_any(row.get("rb_color_id") or row.get("id") or row.get("color_id"))
        bl_id = parse_int_any(row.get("bl_color_id"))
        bo_id = parse_int_any(row.get("bo_color_id"))
        # Treat 0 as "unset".
        # This avoids false "multiple mapping" conflicts when downstream
        # tooling coerces empty bo_color_id to 0.
        if bo_id == 0:
            bo_id = None
        ld_id = parse_int_any(row.get("ldraw_color_id") or row.get("ldraw_id"))

        if rb_id is None:
            issues.append({
                "severity": "ERROR",
                "issue_type": "SEED_RB_COLOR_ID_MISSING",
                "rb_color_id": "",
                "name": name,
                "details": f"Seed line {line_no}: rb_color_id vazio/ inválido.",
                "suggestions": "Corrigir rb_color_id.",
            })
            continue

        if rb_id in seen:
            issues.append({
                "severity": "ERROR",
                "issue_type": "SEED_RB_COLOR_ID_DUPLICATE",
                "rb_color_id": rb_id,
                "name": name,
                "details": f"Seed line {line_no}: rb_color_id duplicado.",
                "suggestions": "Remover duplicado.",
            })
            continue
        seen.add(rb_id)

        if rb_id not in rb_colors:
            issues.append({
                "severity": "ERROR",
                "issue_type": "SEED_RB_COLOR_ID_UNKNOWN",
                "rb_color_id": rb_id,
                "name": name,
                "details": "rb_color_id não existe no Rebrickable colors.csv deste run.",
                "suggestions": "Confirmar inputs/rebrickable/colors.csv.",
            })

        if bl_id is not None and bl_id not in bl_id_to_name:
            issues.append({
                "severity": "ERROR",
                "issue_type": "SEED_BL_COLOR_ID_UNKNOWN",
                "rb_color_id": rb_id,
                "name": name,
                "details": f"bl_color_id={bl_id} não existe no BrickLink colors.xml deste run.",
                "suggestions": "Corrigir bl_color_id ou atualizar inputs/bricklink/colors.xml.",
            })

        seed[rb_id] = SeedColor(
            rb_color_id=rb_id,
            name=name,
            bl_color_id=bl_id,
            bo_color_id=bo_id,
            ldraw_color_id=ld_id,
        )

    return seed, issues


def load_element_overrides(path: Path, bl_id_to_name: Dict[int, str], issues: List[Dict[str, object]]) -> Dict[str, int]:
    """Optional CSV: element_id, bl_color_id.

    Use only to pin BrickLink color for problematic element ids in BrickLink codes.xml.
    This is orthogonal to colors_seed.csv (which is RB-color driven).
    """
    out: Dict[str, int] = {}
    if not path or not str(path).strip():
        return out
    if not path.exists():
        issues.append({
            'severity': 'WARN',
            'issue_type': 'ELEMENT_OVERRIDE_FILE_MISSING',
            'rb_color_id': '',
            'name': '',
            'details': f'element_overrides file not found: {path}',
            'suggestions': 'Se não pretendes usar overrides por element_id, ignora este aviso.',
        })
        return out

    for line_no, row in enumerate(read_csv_dicts(path), start=2):
        eid = (row.get('element_id') or row.get('element') or '').strip()
        bl_id = parse_int_any(row.get('bl_color_id') or row.get('bl_id') or row.get('color_id'))
        if not eid or bl_id is None:
            issues.append({
                'severity': 'ERROR',
                'issue_type': 'ELEMENT_OVERRIDE_INVALID_ROW',
                'rb_color_id': '',
                'name': '',
                'details': f'element_overrides line {line_no}: element_id ou bl_color_id inválido.',
                'suggestions': 'Formato esperado: element_id,bl_color_id',
            })
            continue
        if bl_id not in bl_id_to_name:
            issues.append({
                'severity': 'ERROR',
                'issue_type': 'ELEMENT_OVERRIDE_BL_COLOR_UNKNOWN',
                'rb_color_id': '',
                'name': '',
                'details': f'element_overrides line {line_no}: bl_color_id={bl_id} não existe no BrickLink colors.xml deste run.',
                'suggestions': 'Corrigir bl_color_id ou atualizar inputs/bricklink/colors.xml.',
            })
            continue
        prev = out.get(eid)
        if prev is not None and prev != bl_id:
            issues.append({
                'severity': 'ERROR',
                'issue_type': 'ELEMENT_OVERRIDE_DUPLICATE_CONFLICT',
                'rb_color_id': '',
                'name': '',
                'details': f'element_overrides: element_id={eid} repetido com bl_color_id diferente: {prev} vs {bl_id}',
                'suggestions': 'Manter apenas 1 linha por element_id.',
            })
            continue
        out[eid] = bl_id
    return out


# -----------------------------
# APIs
# -----------------------------

ITEMTYPE_TO_BL_PATH = {
    "P": "part",
    "S": "set",
    "M": "minifig",
    "B": "book",
    "G": "gear",
    "C": "catalog",
    "I": "instruction",
    "O": "original_box",
    "U": "unsorted_lot",
    # allow already normalized
    "PART": "part",
    "SET": "set",
    "MINIFIG": "minifig",
}


class BrickLinkAPI:
    """BrickLink Store API v1."""

    base = "https://api.bricklink.com/api/store/v1"

    def __init__(self, consumer_key: str, consumer_secret: str, token: str, token_secret: str) -> None:
        self.auth = OAuth1(consumer_key, consumer_secret, token, token_secret)

    def _type_to_path(self, item_type: str) -> str:
        t = (item_type or "").strip().upper()
        if t in ITEMTYPE_TO_BL_PATH:
            return ITEMTYPE_TO_BL_PATH[t]
        # fallback: accept a pre-normalized token
        return (item_type or "part").strip().lower()

    def get_known_colors(self, item_type: str, item_no: str, timeout: int = 60) -> List[int]:
        # IMPORTANT: API expects /items/part/{no}/colors, not /items/P/{no}/colors
        t = self._type_to_path(item_type)
        no = quote((item_no or "").strip(), safe="")
        url = f"{self.base}/items/{t}/{no}/colors"
        r = requests.get(url, auth=self.auth, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        out: List[int] = []
        for c in (j.get("data") or []):
            cid = parse_int_any(c.get("color_id"))
            if cid is not None:
                out.append(cid)
        return sorted(set(out))


class RebrickableAPI:
    base = "https://rebrickable.com/api/v3"

    def __init__(self, api_key: str) -> None:
        self.headers = {"Authorization": f"key {api_key}"}

    def selftest(self) -> None:
        url = f"{self.base}/lego/colors/"
        r = requests.get(url, headers=self.headers, params={"page_size": 1}, timeout=30)
        r.raise_for_status()

    def find_part_nums_by_bricklink_id(self, bl_part_id: str) -> List[str]:
        url = f"{self.base}/lego/parts/"
        params_list = [
            {"bricklink_id": bl_part_id, "page_size": 1000, "inc_part_details": 1},
            {"search": bl_part_id, "page_size": 1000, "inc_part_details": 1},
        ]
        found: Set[str] = set()
        for params in params_list:
            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=60)
                if r.status_code >= 400:
                    continue
                j = r.json()
                for it in (j.get("results") or []):
                    part_num = (it.get("part_num") or "").strip()
                    ext = it.get("external_ids") or {}
                    bl_ext = None
                    if isinstance(ext, dict):
                        bl_ext = ext.get("BrickLink") or ext.get("bricklink")
                    if part_num:
                        if params.get("bricklink_id"):
                            found.add(part_num)
                        else:
                            if bl_ext == bl_part_id or bl_ext is None:
                                found.add(part_num)
                if found:
                    break
            except Exception:
                continue
        return sorted(found)

    def get_part_colors(self, part_num: str) -> List[int]:
        url = f"{self.base}/lego/parts/{part_num}/colors/"
        r = requests.get(url, headers=self.headers, timeout=60)
        r.raise_for_status()
        j = r.json()
        out: List[int] = []
        results = j.get("results") if isinstance(j, dict) else j
        for it in (results or []):
            if isinstance(it, dict):
                cid = parse_int_any(it.get("color_id") or (it.get("color") or {}).get("id"))
                if cid is not None:
                    out.append(cid)
        return sorted(set(out))


class BrickOwlAPI:
    base = "https://api.brickowl.com/v1"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _get(self, path: str, timeout: int = 30) -> requests.Response:
        url = f"{self.base}{path}"
        r = requests.get(url, params={"key": self.api_key}, timeout=timeout)
        r.raise_for_status()
        return r

    def selftest(self) -> None:
        self._get("/user/details")
        self._get("/catalog/color_list")


# -----------------------------
# API factories + selftest
# -----------------------------

def get_bl_api() -> Optional[BrickLinkAPI]:
    if BRICKLINK_CONSUMER_KEY and BRICKLINK_CONSUMER_SECRET and BRICKLINK_TOKEN and BRICKLINK_TOKEN_SECRET:
        return BrickLinkAPI(BRICKLINK_CONSUMER_KEY, BRICKLINK_CONSUMER_SECRET, BRICKLINK_TOKEN, BRICKLINK_TOKEN_SECRET)
    return None


def get_rb_api() -> Optional[RebrickableAPI]:
    if REBRICKABLE_API_KEY:
        return RebrickableAPI(REBRICKABLE_API_KEY)
    return None


def get_bo_api() -> Optional[BrickOwlAPI]:
    if BRICKOWL_API_KEY:
        return BrickOwlAPI(BRICKOWL_API_KEY)
    return None


def api_selftest(issues: List[Dict[str, object]], bl_api: Optional[BrickLinkAPI], rb_api: Optional[RebrickableAPI], bo_api: Optional[BrickOwlAPI]) -> None:
    # BrickLink
    if bl_api is None:
        issues.append({
            "severity": "WARN",
            "issue_type": "API_SELFTEST_BRICKLINK_SKIPPED",
            "rb_color_id": "",
            "name": "",
            "details": "BrickLink OAuth não configurado (secrets em falta).",
            "suggestions": "Confirmar BRICKLINK_* no GitHub Secrets e no env do workflow.",
        })
    else:
        try:
            _ = bl_api.get_known_colors("P", "3001")
            issues.append({
                "severity": "INFO",
                "issue_type": "API_SELFTEST_BRICKLINK_OK",
                "rb_color_id": "",
                "name": "",
                "details": "BrickLink OAuth OK (/items/part/3001/colors).",
                "suggestions": "",
            })
        except Exception as e:
            details = f"BrickLink OAuth: FALHA ao chamar /items/part/3001/colors: {e}"
            if isinstance(e, requests.HTTPError) and e.response is not None:
                details += f" | body={trunc(e.response.text)}"
            issues.append({
                "severity": "WARN",
                "issue_type": "API_SELFTEST_BRICKLINK_FAILED",
                "rb_color_id": "",
                "name": "",
                "details": details,
                "suggestions": "Verificar credenciais OAuth, clock do runner e se o endpoint/type estão corretos.",
            })

    # Rebrickable
    if rb_api is None:
        issues.append({
            "severity": "WARN",
            "issue_type": "API_SELFTEST_REBRICKABLE_SKIPPED",
            "rb_color_id": "",
            "name": "",
            "details": "Rebrickable API key não configurada (secrets em falta).",
            "suggestions": "Confirmar REBRICKABLE_API_KEY no GitHub Secrets e no env do workflow.",
        })
    else:
        try:
            rb_api.selftest()
            issues.append({
                "severity": "INFO",
                "issue_type": "API_SELFTEST_REBRICKABLE_OK",
                "rb_color_id": "",
                "name": "",
                "details": "Rebrickable OK (/lego/colors?page_size=1).",
                "suggestions": "",
            })
        except Exception as e:
            details = f"Rebrickable selftest falhou: {e}"
            if isinstance(e, requests.HTTPError) and e.response is not None:
                details += f" | body={trunc(e.response.text)}"
            issues.append({
                "severity": "WARN",
                "issue_type": "API_SELFTEST_REBRICKABLE_FAILED",
                "rb_color_id": "",
                "name": "",
                "details": details,
                "suggestions": "Verificar API key / rate limits.",
            })

    # BrickOwl
    if bo_api is None:
        issues.append({
            "severity": "WARN",
            "issue_type": "API_SELFTEST_BRICKOWL_SKIPPED",
            "rb_color_id": "",
            "name": "",
            "details": "BrickOwl API key não configurada (secrets em falta).",
            "suggestions": "Confirmar BRICKOWL_API_KEY no GitHub Secrets e no env do workflow.",
        })
    else:
        try:
            bo_api.selftest()
            issues.append({
                "severity": "INFO",
                "issue_type": "API_SELFTEST_BRICKOWL_OK",
                "rb_color_id": "",
                "name": "",
                "details": "BrickOwl OK (/user/details + /catalog/color_list).",
                "suggestions": "Se catalog falhar, confirmar permissões da key BrickOwl.",
            })
        except Exception as e:
            details = f"BrickOwl selftest falhou: {e}"
            if isinstance(e, requests.HTTPError) and e.response is not None:
                details += f" | body={trunc(e.response.text)}"
            issues.append({
                "severity": "WARN",
                "issue_type": "API_SELFTEST_BRICKOWL_FAILED",
                "rb_color_id": "",
                "name": "",
                "details": details,
                "suggestions": "Verificar API key / permissões.",
            })


# -----------------------------
# Resolver logic
# -----------------------------

def backoff_sleep(attempt: int) -> None:
    time.sleep(min(30.0, 1.5 ** attempt))


def known_colors_for_part(
    bl_part_id: str,
    bl_api: Optional[BrickLinkAPI],
    rb_api: Optional[RebrickableAPI],
    rb_to_bl_current: Dict[int, Optional[int]],
    cache: dict,
    issues: List[Dict[str, object]],
) -> Set[int]:
    """
    Tiered:
    1) BrickLink Get Known Colors (authoritative)
    2) Rebrickable Part Colors by BrickLink id -> project to BL via rb_to_bl_current
    """

    cache.setdefault("known_colors", {})
    if bl_part_id in cache["known_colors"]:
        return set(cache["known_colors"][bl_part_id])

    # 1) BrickLink
    if bl_api is not None:
        for attempt in range(6):
            try:
                cols = bl_api.get_known_colors("P", bl_part_id)
                cache["known_colors"][bl_part_id] = cols
                return set(cols)
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status in (429, 500, 502, 503, 504):
                    backoff_sleep(attempt)
                    continue
                body = ""
                if e.response is not None:
                    body = trunc(e.response.text)
                issues.append({
                    "severity": "WARN",
                    "issue_type": "BRICKLINK_KNOWN_COLORS_FAILED",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"BrickLink known colors falhou para part {bl_part_id} (HTTP {status}). body={body}",
                    "suggestions": "Fallback para Rebrickable será usado se possível.",
                })
                break
            except Exception as e:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "BRICKLINK_KNOWN_COLORS_FAILED",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"BrickLink known colors falhou para part {bl_part_id}: {e}",
                    "suggestions": "Fallback para Rebrickable será usado se possível.",
                })
                break

    # 2) Rebrickable fallback
    if rb_api is not None:
        try:
            part_nums = rb_api.find_part_nums_by_bricklink_id(bl_part_id)
            projected: Set[int] = set()
            for pn in part_nums[:5]:
                rb_colors = rb_api.get_part_colors(pn)
                for rb_c in rb_colors:
                    bl_c = rb_to_bl_current.get(rb_c)
                    if bl_c is not None:
                        projected.add(bl_c)
            if projected:
                cache["known_colors"][bl_part_id] = sorted(projected)
                return projected
            issues.append({
                "severity": "WARN",
                "issue_type": "REBRICKABLE_FALLBACK_EMPTY",
                "rb_color_id": "",
                "name": "",
                "details": f"Fallback Rebrickable não devolveu cores projetáveis para BL part {bl_part_id}.",
                "suggestions": "Resolver via seed ou garantir credenciais BrickLink.",
            })
        except Exception as e:
            issues.append({
                "severity": "WARN",
                "issue_type": "REBRICKABLE_FALLBACK_FAILED",
                "rb_color_id": "",
                "name": "",
                "details": f"Fallback Rebrickable falhou para BL part {bl_part_id}: {e}",
                "suggestions": "Resolver via seed ou garantir credenciais BrickLink.",
            })

    cache["known_colors"][bl_part_id] = []
    return set()


def resolve_bl_color_by_part_support(
    context_type: str,
    context_id: str,
    part_ids: List[str],
    candidates: List[int],
    bl_api: Optional[BrickLinkAPI],
    rb_api: Optional[RebrickableAPI],
    rb_to_bl_current: Dict[int, Optional[int]],
    cache: dict,
    issues: List[Dict[str, object]],
    max_part_checks: int,
) -> Tuple[Optional[int], Dict[int, int], int]:
    """Returns: (best_candidate_or_none, support_counts, parts_checked)"""

    if not candidates:
        return None, {}, 0
    if len(candidates) == 1:
        if is_disallowed_bl_color_id(candidates[0]):
            return None, {candidates[0]: 0}, 0
        return candidates[0], {candidates[0]: 0}, 0

    parts = part_ids[:max_part_checks]

    issues.append({
        "severity": "INFO",
        "issue_type": "CONFLICT_RESOLUTION_ATTEMPT",
        "rb_color_id": "",
        "name": "",
        "details": f"ctx={context_type}:{context_id} candidates={candidates} parts={len(parts)} apis={{bl:{'Y' if bl_api else 'N'}, rb:{'Y' if rb_api else 'N'}}}",
        "suggestions": "",
    })

    support: Dict[int, int] = {c: 0 for c in candidates}

    for pid in parts:
        known = known_colors_for_part(pid, bl_api, rb_api, rb_to_bl_current, cache, issues)
        for c in candidates:
            if c in known:
                support[c] += 1

    # Choose best by support
    best_c, best_n = sorted(support.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    # tie check
    top = [c for c, n in support.items() if n == best_n]
    if len(top) > 1:
        return None, support, len(parts)

    # confidence check
    if len(parts) > 0 and best_n == 0:
        return None, support, len(parts)

    return best_c, support, len(parts)


# -----------------------------
# Main
# -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bl-colors-xml", required=True)
    ap.add_argument("--bl-codes-xml", required=True)
    ap.add_argument("--rb-elements", required=True)
    ap.add_argument("--rb-colors", required=True)
    ap.add_argument("--seed", required=True)
    ap.add_argument("--element-overrides", default="", help="CSV opcional: element_id,bl_color_id para fixar conflicts do codes.xml")
    ap.add_argument("--emit-element-override-suggestions", action="store_true", help="Escreve element_overrides_suggested.csv (para revisão manual).")


    ap.add_argument("--out", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--issues", required=True)

    ap.add_argument("--cache-json", default="data/api_cache.json")
    ap.add_argument("--max-part-checks", type=int, default=60)

    ap.add_argument("--debug-apis", action="store_true", help="Executa selftests (BrickLink/Rebrickable/BrickOwl) e regista no issues.csv")
    ap.add_argument("--strict", action="store_true", help="Falha apenas com ERROR (estrutural).")
    ap.add_argument("--strict-all", action="store_true", help="Falha com ERROR+WARN (auditoria total).")
    args = ap.parse_args()

    bl_name_to_id, bl_id_to_name, bl_id_to_rgb = load_bl_colors_xml(Path(args.bl_colors_xml))
    rb_colors = load_rb_colors(Path(args.rb_colors))
    rb_elements = load_rb_elements(Path(args.rb_elements))
    seed, seed_issues = load_seed(Path(args.seed), rb_colors, bl_id_to_name)

    issues: List[Dict[str, object]] = list(seed_issues)
    element_overrides = load_element_overrides(Path(args.element_overrides) if args.element_overrides else None, bl_id_to_name, issues)

    cache_path = Path(args.cache_json)
    cache = load_json(cache_path)

    bl_api = get_bl_api()
    rb_api = get_rb_api()
    bo_api = get_bo_api()

    if args.debug_apis:
        api_selftest(issues, bl_api, rb_api, bo_api)

    # Initial RB->BL mapping: seed OR name match
    rb_to_bl: Dict[int, Optional[int]] = {}
    rb_to_bl_source: Dict[int, str] = {}
    for rb_id, rb in rb_colors.items():
        if is_sentinel_rb_color_id(rb_id):
            rb_to_bl[rb_id] = None
            rb_to_bl_source[rb_id] = "sentinel"
            continue
        s = seed.get(rb_id)
        if s and s.bl_color_id is not None:
            rb_to_bl[rb_id] = s.bl_color_id
            rb_to_bl_source[rb_id] = "seed"
            continue
        m = bl_name_to_id.get(norm(rb.name))
        rb_to_bl[rb_id] = m
        rb_to_bl_source[rb_id] = "name" if m is not None else "none"

    # Parse BrickLink codes.xml into:
    # element_id -> Counter(bl_color_id)
    # element_id -> set(part_id)
    element_color_counts: Dict[str, Counter] = defaultdict(Counter)
    element_parts: Dict[str, Set[str]] = defaultdict(set)

    unknown_color_tokens = Counter()

    for itemtype, itemid, element_id, color_val in iter_bl_codes_items(Path(args.bl_codes_xml)):
        if itemtype != "P":
            continue
        bl_id = parse_int_any(color_val)
        if bl_id is None:
            bl_id = bl_name_to_id.get(norm(color_val))
        if bl_id is None:
            unknown_color_tokens[color_val] += 1
            continue
        # Ignore BrickLink color_id=0 ('Not Applicable') from codes.xml to avoid poisoning resolution.
        if is_disallowed_bl_color_id(bl_id):
            continue
        element_color_counts[element_id][bl_id] += 1
        element_parts[element_id].add(itemid)

    if unknown_color_tokens:
        top = ", ".join([f"{k}({v})" for k, v in unknown_color_tokens.most_common(10)])
        issues.append({
            "severity": "WARN",
            "issue_type": "BL_CODES_COLOR_NOT_RESOLVED",
            "rb_color_id": "",
            "name": "",
            "details": f"codes.xml contém tokens de cor não resolvidos via colors.xml (top10): {top}",
            "suggestions": "Atualizar inputs/bricklink/colors.xml ou normalização.",
        })

    # Diagnose element conflicts and attempt resolution by part support
    element_resolved_color: Dict[str, Optional[int]] = {}
    element_override_suggestions: List[Dict[str, object]] = []

    for element_id, c in element_color_counts.items():
        # Only process element_ids that exist in Rebrickable elements.csv.
        # If an element_id from BrickLink codes.xml does not exist in rb_elements,
        # it cannot contribute to RB->BL mapping and should not generate noise/conflicts.
        if element_id not in rb_elements:
            continue

        if len(c) <= 1:
            element_resolved_color[element_id] = next(iter(c.keys())) if c else None
            continue

        candidates = [bid for bid, _ in c.most_common()]
        parts = sorted(element_parts.get(element_id, set()))

        # Highest precedence: explicit element overrides (if provided)
        ov = element_overrides.get(element_id) if 'element_overrides' in locals() else None
        if ov is not None:
            element_resolved_color[element_id] = ov
            issues.append({
                'severity': 'INFO',
                'issue_type': 'BL_CODE_ELEMENT_COLOR_CONFLICT_RESOLVED_BY_ELEMENT_OVERRIDE',
                'rb_color_id': '',
                'name': '',
                'details': f'Element {element_id} tinha múltiplos BL color_id {candidates}; resolvido para {ov} via element_overrides.',
                'suggestions': 'Override aplicado. (Opcional) Mantém este ficheiro sob versionamento para determinismo total.',
            })
            continue

        # Prefer Rebrickable element color as authoritative tie-breaker when available.
        rb_el = rb_elements.get(element_id)
        if rb_el is not None and (not is_sentinel_rb_color_id(rb_el.rb_color_id)):
            expected_bl = rb_to_bl.get(rb_el.rb_color_id)
            if expected_bl is not None and expected_bl in candidates:
                element_resolved_color[element_id] = expected_bl
                issues.append({
                    'severity': 'INFO',
                    'issue_type': 'BL_CODE_ELEMENT_COLOR_CONFLICT_RESOLVED_BY_RB_ELEMENT',
                    'rb_color_id': '',
                    'name': '',
                    'details': f'Element {element_id} tinha múltiplos BL color_id {candidates}; resolvido para {expected_bl} via Rebrickable element (rb_color_id={rb_el.rb_color_id}).',
                    'suggestions': 'Se quiseres fixar definitivamente, adiciona override no seed (ou regra interna por Element).',
                })
                continue
            # If RB->BL not known yet, attempt a deterministic heuristic by name/RGB.
            rb_col = rb_colors.get(rb_el.rb_color_id)
            if rb_col is not None:
                heuristic = tie_break_by_rb_color(rb_col.name, rb_col.rgb, candidates, bl_id_to_name, bl_id_to_rgb)
                if heuristic is not None:
                    element_resolved_color[element_id] = heuristic
                    issues.append({
                        'severity': 'INFO',
                        'issue_type': 'BL_CODE_ELEMENT_COLOR_CONFLICT_RESOLVED_BY_NAME_RGB',
                        'rb_color_id': '',
                        'name': '',
                        'details': f'Element {element_id} tinha múltiplos BL color_id {candidates}; resolvido para {heuristic} por heurística name/RGB (rb_color_id={rb_el.rb_color_id}).',
                        'suggestions': 'Se quiseres fixar definitivamente, adiciona override no seed (ou regra interna por Element).',
                    })
                    continue

        best, support, checked = resolve_bl_color_by_part_support(
            "element", element_id, parts, candidates, bl_api, rb_api, rb_to_bl, cache, issues, args.max_part_checks
        )

        if best is not None:
            element_resolved_color[element_id] = best
            issues.append({
                "severity": "INFO",
                "issue_type": "BL_CODE_ELEMENT_COLOR_CONFLICT_RESOLVED",
                "rb_color_id": "",
                "name": "",
                "details": f"Element {element_id} tinha múltiplos BL color_id {candidates}; resolvido para {best} via parts_checked={checked}, support={support}",
                "suggestions": "Se quiseres fixar definitivamente, adiciona override no seed ou regra interna para este element.",
            })
        else:
            element_resolved_color[element_id] = None
            element_override_suggestions.append({
                'element_id': element_id,
                'candidates': ';'.join(str(x) for x in candidates),
                'candidate_names': ';'.join((bl_id_to_name.get(x, '') or '').replace(';', ' ') for x in candidates),
                'parts_count': len(parts),
            })
            issues.append({
                "severity": "WARN",
                "issue_type": "BL_CODE_ELEMENT_COLOR_CONFLICT",
                "rb_color_id": "",
                "name": "",
                "details": f"Element {element_id} aparece com múltiplos BL color_id {candidates}; não foi possível resolver (parts_checked={checked}, support={support})",
                "suggestions": "Não há evidência suficiente. Para fixar definitivamente, adicionar override no seed (ou regra interna por Element).",
            })

    # Build RB candidates from element crosswalk:
    # rb_color_id -> Counter(candidate_bl_color_id) + involved parts
    rb_candidate_counts: Dict[int, Counter] = defaultdict(Counter)
    rb_involved_parts: Dict[int, Set[str]] = defaultdict(set)

    sentinel_rb_elements = Counter()
    sentinel_example_elements: List[str] = []

    for element_id, rb_el in rb_elements.items():
        if is_sentinel_rb_color_id(rb_el.rb_color_id):
            sentinel_rb_elements[rb_el.rb_color_id] += 1
            if len(sentinel_example_elements) < 10:
                sentinel_example_elements.append(element_id)
            continue
        if element_id in element_resolved_color and element_resolved_color[element_id] is not None:
            bl_cands = [element_resolved_color[element_id]]
        else:
            bl_cands = list(element_color_counts.get(element_id, {}).keys())
        if not bl_cands:
            continue
        for blc in bl_cands:
            rb_candidate_counts[rb_el.rb_color_id][blc] += 1
        for pid in element_parts.get(element_id, set()):
            rb_involved_parts[rb_el.rb_color_id].add(pid)

    if sentinel_rb_elements:
        issues.append({
            "severity": "INFO",
            "issue_type": "RB_ELEMENTS_SENTINEL_COLOR_SKIPPED",
            "rb_color_id": "",
            "name": "",
            "details": f"rb_elements contém rb_color_id(s) sentinela {dict(sentinel_rb_elements)}. Foram ignorados para evitar mapear [Unknown] para cores BrickLink. Exemplos de element_id: {sentinel_example_elements}",
            "suggestions": "Normalmente é seguro ignorar. Se quiseres, limpa esses element_id no rb_elements.csv.",
        })

    # Resolve RB_TO_BL_CONFLICT / BL_ID_MISSING_RELEVANT using BrickLink PARTs + Known Colors
    for rb_id, c in rb_candidate_counts.items():
        if is_sentinel_rb_color_id(rb_id):
            continue
        if not c:
            continue
        candidates = [bid for bid, _ in c.most_common()]
        current = rb_to_bl.get(rb_id)
        source = rb_to_bl_source.get(rb_id, "none")

        needs_resolve = (current is None and len(candidates) >= 1) or (len(candidates) > 1 and source != "seed")
        if not needs_resolve:
            continue

        parts = sorted(rb_involved_parts.get(rb_id, set()))
        best, support, checked = resolve_bl_color_by_part_support(
            "rb_color", str(rb_id), parts, candidates, bl_api, rb_api, rb_to_bl, cache, issues, args.max_part_checks
        )

        if best is not None:
            if source != "seed":
                rb_to_bl[rb_id] = best
                rb_to_bl_source[rb_id] = "api_resolve"
            issues.append({
                "severity": "WARN",
                "issue_type": "RB_TO_BL_CONFLICT_RESOLVED" if len(candidates) > 1 else "BL_ID_MISSING_RELEVANT_RESOLVED",
                "rb_color_id": rb_id,
                "name": rb_colors.get(rb_id, RBColor(rb_id, f"RB_{rb_id}", "", None, None)).name,
                "details": f"RB candidates={candidates}; escolhido={best}; parts_checked={checked}; support={support}; source_before={source}",
                "suggestions": "Se quiseres ‘perfeição auditável’, fixa este rb_color_id no seed.",
            })
        else:
            issues.append({
                "severity": "WARN",
                "issue_type": "RB_TO_BL_CONFLICT" if len(candidates) > 1 else "BL_ID_MISSING_RELEVANT",
                "rb_color_id": rb_id,
                "name": rb_colors.get(rb_id, RBColor(rb_id, f"RB_{rb_id}", "", None, None)).name,
                "details": f"RB candidates={candidates}; não resolvido via parts_checked={checked}; support={support}; current={current}; source={source}",
                "suggestions": "Adicionar override no seed para resolver definitivamente.",
            })

    # Save cache
    save_json(cache_path, cache)

    # Build an authoritative BL->BO mapping from the seed.
    # Rationale: Rebrickable can have multiple rb_color_id that legitimately
    # project to the SAME BrickLink color_id. If only one of those rb_color_id
    # is present in the seed, other rows would have bo_color_id empty, and
    # downstream code that coerces empty->0 will see a false conflict like
    # "bl_color_id=66 mapped to multiple bo_color_id: 0 vs 122".
    bl_to_bo_seed: Dict[int, int] = {}
    for sc in seed.values():
        if sc.bl_color_id is None:
            continue
        if sc.bo_color_id is None or sc.bo_color_id == 0:
            continue
        prev = bl_to_bo_seed.get(sc.bl_color_id)
        if prev is None:
            bl_to_bo_seed[sc.bl_color_id] = sc.bo_color_id
        elif prev != sc.bo_color_id:
            issues.append({
                "severity": "ERROR",
                "issue_type": "SEED_BL_TO_BO_CONFLICT",
                "rb_color_id": sc.rb_color_id,
                "name": sc.name,
                "details": f"Seed define bl_color_id={sc.bl_color_id} com múltiplos bo_color_id: {prev} vs {sc.bo_color_id}",
                "suggestions": "Corrigir o seed: BL->BO deve ser 1:1.",
            })

    # Build outputs
    out_rows: List[Dict[str, object]] = []
    audit_rows: List[Dict[str, object]] = []

    for rb_id in sorted(rb_colors.keys()):
        rb = rb_colors[rb_id]
        s = seed.get(rb_id)

        sentinel = is_sentinel_rb_color_id(rb_id)

        bl_id = rb_to_bl.get(rb_id)
        bl_source = rb_to_bl_source.get(rb_id, "none")
        if sentinel:
            bl_id = None
            bl_source = "sentinel"

        bo_id: Optional[int] = None
        if not sentinel:
            if s and s.bo_color_id is not None and s.bo_color_id != 0:
                bo_id = s.bo_color_id
            elif bl_id is not None:
                # Fill missing BO mapping by BrickLink color id (authoritative from seed).
                bo_id = bl_to_bo_seed.get(bl_id)
        ldraw_id = rb.ldraw_color_id
        if s and s.ldraw_color_id is not None:
            ldraw_id = s.ldraw_color_id

        if bl_id is None and not sentinel:
            if rb_id in RB_COLOR_IDS_ALLOW_BL_NULL:
                issues.append({
                    "severity": "INFO",
                    "issue_type": "BL_ID_MISSING_ALLOWED",
                    "rb_color_id": rb_id,
                    "name": rb.name,
                    "details": "Sem bl_color_id (permitido por allowlist; manter NULL).",
                    "suggestions": "Nenhuma ação necessária.",
                })
            else:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "BL_ID_MISSING",
                    "rb_color_id": rb_id,
                    "name": rb.name,
                    "details": "Sem bl_color_id (aceitável: pode não existir no BrickLink).",
                    "suggestions": "Se existir no BrickLink, fixa no seed.",
                })

        out_rows.append({
            "name": rb.name,
            "rb_color_id": rb_id,
            "bl_color_id": bl_id,
            "bo_color_id": bo_id,
            "bo_color_name": "",
            "ldraw_color_id": ldraw_id,
        })

        audit_rows.append({
            "name": rb.name,
            "rb_color_id": rb_id,
            "rb_rgb": ("" if sentinel else rb.rgb),
            "rb_is_trans": ("" if sentinel else (rb.is_trans if rb.is_trans is not None else "")),
            "bl_color_id": bl_id if bl_id is not None else "",
            "bl_color_name": bl_id_to_name.get(bl_id, "") if bl_id is not None else "",
            "bl_rgb": bl_id_to_rgb.get(bl_id, "") if bl_id is not None else "",
            "bl_source": bl_source,
            "ldraw_color_id": ldraw_id if ldraw_id is not None else "",
            "seed_bl": s.bl_color_id if s and s.bl_color_id is not None else "",
        })

    write_csv(Path(args.out),
              ["name", "rb_color_id", "bl_color_id", "bo_color_id", "bo_color_name", "ldraw_color_id"],
              out_rows)
    write_csv(Path(args.audit),
              ["name", "rb_color_id", "rb_rgb", "rb_is_trans",
               "bl_color_id", "bl_color_name", "bl_rgb", "bl_source",
               "ldraw_color_id", "seed_bl"],
              audit_rows)
    write_csv(Path(args.issues),
              ["severity", "issue_type", "rb_color_id", "name", "details", "suggestions"],
              issues)
    if element_override_suggestions and args.emit_element_override_suggestions:
        sug_path = Path(args.issues).with_name('element_overrides_suggested.csv')
        write_csv(sug_path, ['element_id','candidates','candidate_names','parts_count'], element_override_suggestions)
        print(f'✅ Wrote: {sug_path} (rows={len(element_override_suggestions)})')

    n_err = sum(1 for x in issues if x.get("severity") == "ERROR")
    n_warn = sum(1 for x in issues if x.get("severity") == "WARN")

    print(f"✅ Wrote: {args.out} (rows={len(out_rows)})")
    print(f"✅ Wrote: {args.audit} (rows={len(audit_rows)})")
    print(f"✅ Wrote: {args.issues} (issues={len(issues)} | ERR={n_err} WARN={n_warn})")
    print(f"✅ Cache: {cache_path}")



    # Emit root causes in CI logs before failing (when strict is enabled).
    # This avoids situations where the job fails and the user does not inspect the artifacts.
    if (args.strict_all and (n_err + n_warn) > 0) or (args.strict and n_err > 0):
        print("\n===== ISSUE SUMMARY (for CI logs) =====")
        err_types = Counter((x.get("issue_type") or "") for x in issues if x.get("severity") == "ERROR")
        warn_types = Counter((x.get("issue_type") or "") for x in issues if x.get("severity") == "WARN")

        if err_types:
            print(f"ERROR types (top20): {err_types.most_common(20)}")
        if warn_types:
            print(f"WARN types (top20): {warn_types.most_common(20)}")

        if err_types:
            print("\n--- TOP ERROR issues (up to 20) ---")
            shown = 0
            for x in issues:
                if x.get("severity") == "ERROR":
                    print(f"ERROR,{x.get('issue_type')},{x.get('rb_color_id')},{x.get('name')},{x.get('details')}")
                    shown += 1
                    if shown >= 20:
                        break

        if args.strict_all and warn_types:
            print("\n--- TOP WARN issues (up to 20) ---")
            shown = 0
            for x in issues:
                if x.get("severity") == "WARN":
                    print(f"WARN,{x.get('issue_type')},{x.get('rb_color_id')},{x.get('name')},{x.get('details')}")
                    shown += 1
                    if shown >= 20:
                        break

        print("===== END ISSUE SUMMARY =====\n")
    if args.strict_all and (n_err + n_warn) > 0:
        print("❌ STRICT-ALL mode: issues found. Exiting with code 2.")
        return 2
    if args.strict and n_err > 0:
        print("❌ STRICT mode: ERROR issues found. Exiting with code 2.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
