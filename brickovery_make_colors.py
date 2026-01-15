#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Brickovery - make color_map.csv with conflict resolution driven by BrickLink PART ID.

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

import requests
from requests_oauthlib import OAuth1


# =========================
# API SECRETS (via environment variables)
#
# Em GitHub Actions, injeta as secrets assim (exemplo):
#
#   REBRICKABLE_API_KEY: ${{ secrets.REBRICKABLE_API_KEY }}
#   BRICKOWL_API_KEY: ${{ secrets.BRICKOWL_API_KEY }}
#   BRICKLINK_CONSUMER_KEY: ${{ secrets.BRICKLINK_CONSUMER_KEY }}
#   BRICKLINK_CONSUMER_SECRET: ${{ secrets.BRICKLINK_CONSUMER_SECRET }}
#   BRICKLINK_TOKEN: ${{ secrets.BRICKLINK_TOKEN }}
#   BRICKLINK_TOKEN_SECRET: ${{ secrets.BRICKLINK_TOKEN_SECRET }}
#
# Este script apenas le essas variaveis do ambiente.
# =========================

def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()

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
    """
    Yields (itemtype, itemid, element_id, color_val) from BrickLink codes.xml.
    element_id = CODENAME or CODE.
    """
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


def load_rb_elements(rb_elements_csv: Path) -> Dict[str, int]:
    """
    element_id -> rb_color_id
    """
    out: Dict[str, int] = {}
    for row in read_csv_dicts(rb_elements_csv):
        element_id = (row.get("element_id") or "").strip()
        color_id = parse_int_any(row.get("color_id") or row.get("colour_id"))
        if element_id and color_id is not None:
            out[element_id] = color_id
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


# -----------------------------
# APIs
# -----------------------------
class BrickLinkAPI:
    """
    BrickLink Store API v1.
    Known colors endpoint:
      GET /items/{type}/{no}/colors
    """
    base = "https://api.bricklink.com/api/store/v1"

    def __init__(self, consumer_key: str, consumer_secret: str, token: str, token_secret: str) -> None:
        self.auth = OAuth1(consumer_key, consumer_secret, token, token_secret)

    def get_known_colors(self, item_type: str, item_no: str, timeout: int = 60) -> List[int]:
        url = f"{self.base}/items/{item_type}/{item_no}/colors"
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

    def find_part_nums_by_bricklink_id(self, bl_part_id: str) -> List[str]:
        """
        Try multiple query strategies (API behaviour may vary).
        """
        url = f"{self.base}/lego/parts/"
        # Strategy A: external filter (may exist)
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
                    # ext format may vary; validate if possible
                    bl_ext = None
                    if isinstance(ext, dict):
                        # common key "BrickLink"
                        bl_ext = ext.get("BrickLink") or ext.get("bricklink")
                    if part_num:
                        # if we can confirm external id match, do it; else accept if search used
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
        """
        GET /api/v3/lego/parts/{part_num}/colors/
        """
        url = f"{self.base}/lego/parts/{part_num}/colors/"
        r = requests.get(url, headers=self.headers, timeout=60)
        r.raise_for_status()
        j = r.json()
        out: List[int] = []
        for it in (j.get("results") or j or []):
            # depending on endpoint response, "color_id" may be direct
            if isinstance(it, dict):
                cid = parse_int_any(it.get("color_id") or (it.get("color") or {}).get("id"))
                if cid is not None:
                    out.append(cid)
        return sorted(set(out))


# -----------------------------
# Resolver logic
# -----------------------------
def backoff_sleep(attempt: int) -> None:
    time.sleep(min(30.0, 1.5 ** attempt))


def get_bl_api() -> Optional[BrickLinkAPI]:
    ck = BRICKLINK_CONSUMER_KEY
    cs = BRICKLINK_CONSUMER_SECRET
    tk = BRICKLINK_TOKEN
    ts = BRICKLINK_TOKEN_SECRET
    if ck and cs and tk and ts:
        return BrickLinkAPI(ck, cs, tk, ts)
    return None


def get_rb_api() -> Optional[RebrickableAPI]:
    key = REBRICKABLE_API_KEY
    if key:
        return RebrickableAPI(key)
    return None


def brickowl_key_from_env() -> Optional[str]:
    key = BRICKOWL_API_KEY
    return key if key else None


def api_selftest(issues: List[Dict[str, object]], bl_api: Optional[BrickLinkAPI], rb_api: Optional[RebrickableAPI]) -> None:
    """Smoke test às APIs (não bloqueante)."""
    print("\n=== API SELFTEST (BrickLink / Rebrickable / BrickOwl) ===")

    # BrickLink
    if bl_api is None:
        msg = "BrickLink OAuth: indisponível (secrets não definidos)."
        print("WARN:", msg)
        issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_BRICKLINK_UNAVAILABLE", "rb_color_id": "", "name": "", "details": msg, "suggestions": ""})
    else:
        try:
            cols = bl_api.get_known_colors("P", "3001", timeout=20)
            msg = f"BrickLink OAuth: OK (exemplo 3001 -> {len(cols)} cores)."
            print("OK:", msg)
            issues.append({"severity": "INFO", "issue_type": "API_SELFTEST_BRICKLINK_OK", "rb_color_id": "", "name": "", "details": msg, "suggestions": ""})
        except Exception as e:
            msg = f"BrickLink OAuth: FALHA ao chamar /items/P/3001/colors: {e}"
            print("WARN:", msg)
            issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_BRICKLINK_FAILED", "rb_color_id": "", "name": "", "details": msg, "suggestions": ""})

    # Rebrickable
    if rb_api is None:
        msg = "Rebrickable: indisponível (REBRICKABLE_API_KEY não definido)."
        print("WARN:", msg)
        issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_REBRICKABLE_UNAVAILABLE", "rb_color_id": "", "name": "", "details": msg, "suggestions": ""})
    else:
        try:
            url = f"{rb_api.base}/lego/colors/?page_size=1"
            r = requests.get(url, headers=rb_api.headers, timeout=20)
            r.raise_for_status()
            msg = "Rebrickable: OK (/lego/colors?page_size=1)."
            print("OK:", msg)
            issues.append({"severity": "INFO", "issue_type": "API_SELFTEST_REBRICKABLE_OK", "rb_color_id": "", "name": "", "details": msg, "suggestions": ""})
        except Exception as e:
            msg = f"Rebrickable: FALHA no selftest: {e}"
            print("WARN:", msg)
            issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_REBRICKABLE_FAILED", "rb_color_id": "", "name": "", "details": msg, "suggestions": ""})

    # BrickOwl
    bo_key = brickowl_key_from_env()
    if not bo_key:
        msg = "BrickOwl: indisponível (BRICKOWL_API_KEY não definido)."
        print("WARN:", msg)
        issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_BRICKOWL_UNAVAILABLE", "rb_color_id": "", "name": "", "details": msg, "suggestions": ""})
    else:
        try:
            base = "https://api.brickowl.com/v1"
            # 1) user/details
            r1 = requests.get(f"{base}/user/details", params={"key": bo_key}, timeout=20)
            r1.raise_for_status()
            # 2) catalog/color_list (valida acesso ao catalog API)
            r2 = requests.get(f"{base}/catalog/color_list", params={"key": bo_key}, timeout=20)
            r2.raise_for_status()
            msg = "BrickOwl: OK (/user/details + /catalog/color_list)."
            print("OK:", msg)
            issues.append({"severity": "INFO", "issue_type": "API_SELFTEST_BRICKOWL_OK", "rb_color_id": "", "name": "", "details": msg, "suggestions": ""})
        except Exception as e:
            msg = f"BrickOwl: FALHA no selftest: {e}"
            print("WARN:", msg)
            issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_BRICKOWL_FAILED", "rb_color_id": "", "name": "", "details": msg, "suggestions": ""})

    print("=== END API SELFTEST ===\n")


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
                issues.append({
                    "severity": "WARN",
                    "issue_type": "BRICKLINK_KNOWN_COLORS_FAILED",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"BrickLink known colors falhou para part {bl_part_id} (HTTP {status}).",
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
            for pn in part_nums[:5]:  # cap: avoid explosion
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
                "suggestions": "Resolver via seed ou credenciais BrickLink.",
            })
        except Exception as e:
            issues.append({
                "severity": "WARN",
                "issue_type": "REBRICKABLE_FALLBACK_FAILED",
                "rb_color_id": "",
                "name": "",
                "details": f"Fallback Rebrickable falhou para BL part {bl_part_id}: {e}",
                "suggestions": "Resolver via seed ou credenciais BrickLink.",
            })

    cache["known_colors"][bl_part_id] = []
    return set()


def resolve_bl_color_by_part_support(
    part_ids: List[str],
    candidates: List[int],
    bl_api: Optional[BrickLinkAPI],
    rb_api: Optional[RebrickableAPI],
    rb_to_bl_current: Dict[int, Optional[int]],
    cache: dict,
    issues: List[Dict[str, object]],
    max_part_checks: int,
) -> Tuple[Optional[int], Dict[int, int], int]:
    """
    Returns: (best_candidate_or_none, support_counts, parts_checked)
    """
    if not candidates:
        return None, {}, 0
    if len(candidates) == 1:
        return candidates[0], {candidates[0]: 0}, 0

    # Limit API work
    parts = part_ids[:max_part_checks]
    support: Dict[int, int] = {c: 0 for c in candidates}

    for pid in parts:
        known = known_colors_for_part(pid, bl_api, rb_api, rb_to_bl_current, cache, issues)
        for c in candidates:
            if c in known:
                support[c] += 1

    # Pick best
    best = sorted(support.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    best_c, best_n = best
    # If tie on top, return None (unresolved)
    top = [c for c, n in support.items() if n == best_n]
    if len(top) > 1:
        return None, support, len(parts)

    # require some confidence if we actually checked parts
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

    ap.add_argument("--out", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--issues", required=True)

    ap.add_argument("--cache-json", default="data/api_cache.json")
    ap.add_argument("--max-part-checks", type=int, default=60)

    ap.add_argument("--debug-apis", action="store_true", help="Executa um selftest às APIs (BrickLink/Rebrickable/BrickOwl) e imprime no log")

    ap.add_argument("--strict", action="store_true", help="Falha apenas com ERROR (estrutural).")
    ap.add_argument("--strict-all", action="store_true", help="Falha com ERROR+WARN (auditoria total).")
    args = ap.parse_args()

    bl_name_to_id, bl_id_to_name, bl_id_to_rgb = load_bl_colors_xml(Path(args.bl_colors_xml))
    rb_colors = load_rb_colors(Path(args.rb_colors))
    rb_elements = load_rb_elements(Path(args.rb_elements))
    seed, seed_issues = load_seed(Path(args.seed), rb_colors, bl_id_to_name)

    issues: List[Dict[str, object]] = list(seed_issues)

    cache_path = Path(args.cache_json)
    cache = load_json(cache_path)

    bl_api = get_bl_api()
    rb_api = get_rb_api()

    if args.debug_apis:
        api_selftest(issues, bl_api, rb_api)

    # -----------------------------
    # API SELFTEST (best-effort; não bloqueia)
    # -----------------------------
    if args.debug_apis:
        print("\n=== API SELFTEST (BrickLink / Rebrickable / BrickOwl) ===")
        # BrickLink
        if bl_api is None:
            issues.append({
                "severity": "WARN",
                "issue_type": "API_SELFTEST_BRICKLINK_UNAVAILABLE",
                "rb_color_id": "",
                "name": "",
                "details": "BrickLink OAuth não definido no ambiente; selftest BrickLink não executado.",
                "suggestions": "Configurar BRICKLINK_* secrets no GitHub.",
            })
            print("[SELFTEST] BrickLink: UNAVAILABLE (missing OAuth env vars)")
        else:
            try:
                cols = bl_api.get_known_colors("P", "3001", timeout=20)
                issues.append({
                    "severity": "INFO",
                    "issue_type": "API_SELFTEST_BRICKLINK_OK",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"BrickLink OK. /items/P/3001/colors devolveu {len(cols)} cores.",
                    "suggestions": "",
                })
                print(f"[SELFTEST] BrickLink: OK (3001 -> {len(cols)} cores)")
            except Exception as e:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "API_SELFTEST_BRICKLINK_FAILED",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"BrickLink selftest falhou: {e}",
                    "suggestions": "Verificar rate limits/credenciais.",
                })
                print(f"[SELFTEST] BrickLink: FAIL ({e})")

        # Rebrickable
        if rb_api is None:
            issues.append({
                "severity": "WARN",
                "issue_type": "API_SELFTEST_REBRICKABLE_UNAVAILABLE",
                "rb_color_id": "",
                "name": "",
                "details": "REBRICKABLE_API_KEY não definido no ambiente; selftest Rebrickable não executado.",
                "suggestions": "Configurar REBRICKABLE_API_KEY no GitHub Secrets.",
            })
            print("[SELFTEST] Rebrickable: UNAVAILABLE (missing REBRICKABLE_API_KEY)")
        else:
            try:
                url = "https://rebrickable.com/api/v3/lego/colors/?page_size=1"
                r = requests.get(url, headers=rb_api.headers, timeout=20)
                r.raise_for_status()
                issues.append({
                    "severity": "INFO",
                    "issue_type": "API_SELFTEST_REBRICKABLE_OK",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"Rebrickable OK. /lego/colors?page_size=1 status={r.status_code}.",
                    "suggestions": "",
                })
                print(f"[SELFTEST] Rebrickable: OK (status={r.status_code})")
            except Exception as e:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "API_SELFTEST_REBRICKABLE_FAILED",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"Rebrickable selftest falhou: {e}",
                    "suggestions": "Verificar credencial/rate limit.",
                })
                print(f"[SELFTEST] Rebrickable: FAIL ({e})")

        # BrickOwl
        bo_key = (os.getenv("BRICKOWL_API_KEY") or "").strip()
        if not bo_key:
            issues.append({
                "severity": "WARN",
                "issue_type": "API_SELFTEST_BRICKOWL_UNAVAILABLE",
                "rb_color_id": "",
                "name": "",
                "details": "BRICKOWL_API_KEY não definido no ambiente; selftest BrickOwl não executado.",
                "suggestions": "Configurar BRICKOWL_API_KEY no GitHub Secrets.",
            })
            print("[SELFTEST] BrickOwl: UNAVAILABLE (missing BRICKOWL_API_KEY)")
        else:
            try:
                base = "https://api.brickowl.com/v1"
                r1 = requests.get(f"{base}/user/details", params={"key": bo_key}, timeout=20)
                r1.raise_for_status()
                ok_catalog = True
                try:
                    r2 = requests.get(f"{base}/catalog/color_list", params={"key": bo_key}, timeout=20)
                    r2.raise_for_status()
                except Exception:
                    ok_catalog = False
                issues.append({
                    "severity": "INFO" if ok_catalog else "WARN",
                    "issue_type": "API_SELFTEST_BRICKOWL_OK" if ok_catalog else "API_SELFTEST_BRICKOWL_CATALOG_FAILED",
                    "rb_color_id": "",
                    "name": "",
                    "details": "BrickOwl OK (/user/details)" + (" + catalog OK (/catalog/color_list)." if ok_catalog else "; catalog FAIL (/catalog/color_list)."),
                    "suggestions": "Se catalog falhar, confirmar permissões da key BrickOwl.",
                })
                print("[SELFTEST] BrickOwl: OK (/user/details)" + (" + catalog OK" if ok_catalog else " + catalog FAIL"))
            except Exception as e:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "API_SELFTEST_BRICKOWL_FAILED",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"BrickOwl selftest falhou: {e}",
                    "suggestions": "Verificar credencial/permissões.",
                })
                print(f"[SELFTEST] BrickOwl: FAIL ({e})")

        print("=== END API SELFTEST ===\n")

    # -----------------------------
    # Initial RB->BL mapping (seed authoritative, name-match fallback)
    # This MUST exist before any conflict-resolution that may need Rebrickable fallback projection.
    # -----------------------------
    rb_to_bl: Dict[int, Optional[int]] = {}
    rb_to_bl_source: Dict[int, str] = {}
    for rb_id, rb in rb_colors.items():
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

    for element_id, c in element_color_counts.items():
        if len(c) <= 1:
            element_resolved_color[element_id] = next(iter(c.keys())) if c else None
            continue

        candidates = [bid for bid, _ in c.most_common()]
        parts = sorted(element_parts.get(element_id, set()))
        best, support, checked = resolve_bl_color_by_part_support(
            parts, candidates, bl_api, rb_api, rb_to_bl, cache, issues, args.max_part_checks
        )

        if best is not None:
            element_resolved_color[element_id] = best
            issues.append({
                "severity": "WARN",
                "issue_type": "BL_CODE_ELEMENT_COLOR_CONFLICT_RESOLVED",
                "rb_color_id": "",
                "name": "",
                "details": f"Element {element_id} tinha múltiplos BL color_id {candidates}; resolvido para {best} via parts_checked={checked}, support={support}",
                "suggestions": "Se quiseres, fixa este caso no seed/nota interna; mapping continua.",
            })
        else:
            element_resolved_color[element_id] = None
            issues.append({
                "severity": "WARN",
                "issue_type": "BL_CODE_ELEMENT_COLOR_CONFLICT",
                "rb_color_id": "",
                "name": "",
                "details": f"Element {element_id} aparece com múltiplos BL color_id {candidates}; não foi possível resolver (parts_checked={checked}, support={support})",
                "suggestions": "Sem resolução automática. Para fixar definitivamente, adicionar override no seed (colors_seed.csv) para o rb_color_id relevante (ou regra interna se for caso de Element específico).",
            })

    # (rb_to_bl already initialised above)

    # Build RB candidates from element crosswalk:
    # rb_color_id -> Counter(candidate_bl_color_id) + involved parts
    rb_candidate_counts: Dict[int, Counter] = defaultdict(Counter)
    rb_involved_parts: Dict[int, Set[str]] = defaultdict(set)

    for element_id, rb_c in rb_elements.items():
        # pick deterministic element color if resolved; else consider all candidates
        if element_id in element_resolved_color and element_resolved_color[element_id] is not None:
            bl_cands = [element_resolved_color[element_id]]
        else:
            bl_cands = list(element_color_counts.get(element_id, {}).keys())
        if not bl_cands:
            continue
        for blc in bl_cands:
            rb_candidate_counts[rb_c][blc] += 1
        for pid in element_parts.get(element_id, set()):
            rb_involved_parts[rb_c].add(pid)

    # Resolve RB_TO_BL_CONFLICT / BL_ID_MISSING_RELEVANT using BrickLink PARTs + Known Colors
    for rb_id, c in rb_candidate_counts.items():
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
            parts, candidates, bl_api, rb_api, rb_to_bl, cache, issues, args.max_part_checks
        )

        if best is not None:
            # don't override seed
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
            # unresolved: keep current; if missing, stay missing
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

    # Build outputs
    out_rows: List[Dict[str, object]] = []
    audit_rows: List[Dict[str, object]] = []

    for rb_id in sorted(rb_colors.keys()):
        rb = rb_colors[rb_id]
        s = seed.get(rb_id)

        bl_id = rb_to_bl.get(rb_id)
        bl_source = rb_to_bl_source.get(rb_id, "none")

        bo_id = s.bo_color_id if s else None
        ldraw_id = rb.ldraw_color_id
        if s and s.ldraw_color_id is not None:
            ldraw_id = s.ldraw_color_id

        if bl_id is None:
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
            "bo_color_name": "",  # preenchimento BO pode ser adicionado depois (opcional)
            "ldraw_color_id": ldraw_id,
        })

        audit_rows.append({
            "name": rb.name,
            "rb_color_id": rb_id,
            "rb_rgb": rb.rgb,
            "rb_is_trans": rb.is_trans if rb.is_trans is not None else "",
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

    n_err = sum(1 for x in issues if x.get("severity") == "ERROR")
    n_warn = sum(1 for x in issues if x.get("severity") == "WARN")

    print(f"✅ Wrote: {args.out} (rows={len(out_rows)})")
    print(f"✅ Wrote: {args.audit} (rows={len(audit_rows)})")
    print(f"✅ Wrote: {args.issues} (issues={len(issues)} | ERR={n_err} WARN={n_warn})")
    print(f"✅ Cache: {cache_path}")

    if args.strict_all and (n_err + n_warn) > 0:
        print("❌ STRICT-ALL mode: issues found. Exiting with code 2.")
        return 2
    if args.strict and n_err > 0:
        print("❌ STRICT mode: ERROR issues found. Exiting with code 2.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
