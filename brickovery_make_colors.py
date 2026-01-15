#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Brickovery - build color_map (RB <-> BL [+ optional BO]) with realistic strictness.

Key points:
- Deterministic RB->BL mapping via Element ID crosswalk:
  BrickLink codes.xml (element_id -> BL color) + Rebrickable elements.csv (element_id -> RB color)
- Seed (colors_seed.csv) is authoritative (overrides) and validated.
- STRICT fails only on ERROR; WARN does not fail (realistic: not all colors converge across platforms).
- Outputs:
  data/color_map.csv
  data/color_map_audit.csv
  data/color_map_issues.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from requests_oauthlib import OAuth1


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
    """
    Returns:
      name_norm -> bl_color_id
      bl_color_id -> name
      bl_color_id -> rgb
    """
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


def iter_bl_codes(bl_codes_xml: Path) -> Iterable[Tuple[str, str]]:
    """
    Yields (element_id, color_val) from BrickLink codes.xml.
    element_id usually in CODENAME, sometimes CODE.
    color_val can be either a color name or a numeric id (depends on source).
    """
    ctx = ET.iterparse(str(bl_codes_xml), events=("end",))
    for _, elem in ctx:
        if elem.tag != "ITEM":
            continue
        element_id = (elem.findtext("CODENAME") or elem.findtext("CODE") or "").strip()
        color_val = (elem.findtext("COLOR") or "").strip()
        if element_id and color_val:
            yield element_id, color_val
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
                "details": "rb_color_id não existe no Rebrickable colors.csv usado neste run.",
                "suggestions": "Confirmar inputs/rebrickable/colors.csv.",
            })

        if bl_id is not None and bl_id not in bl_id_to_name:
            issues.append({
                "severity": "ERROR",
                "issue_type": "SEED_BL_COLOR_ID_UNKNOWN",
                "rb_color_id": rb_id,
                "name": name,
                "details": f"bl_color_id={bl_id} não existe no BrickLink colors.xml usado neste run.",
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
# Optional API verifiers (low call)
# -----------------------------
class BrickLinkAPI:
    base = "https://api.bricklink.com/api/store/v1"

    def __init__(self, consumer_key: str, consumer_secret: str, token: str, token_secret: str) -> None:
        self.auth = OAuth1(consumer_key, consumer_secret, token, token_secret)

    def get_colors(self) -> Dict[int, str]:
        url = f"{self.base}/colors"
        r = requests.get(url, auth=self.auth, timeout=60)
        r.raise_for_status()
        j = r.json()
        out: Dict[int, str] = {}
        for c in (j.get("data") or []):
            cid = parse_int_any(c.get("color_id"))
            nm = (c.get("color_name") or "").strip()
            if cid is not None and nm:
                out[cid] = nm
        return out


class RebrickableAPI:
    base = "https://rebrickable.com/api/v3"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def get_colors(self) -> Dict[int, str]:
        url = f"{self.base}/lego/colors/"
        headers = {"Authorization": f"key {self.api_key}"}
        out: Dict[int, str] = {}
        page = 1
        while True:
            r = requests.get(url, headers=headers, params={"page": page, "page_size": 1000}, timeout=60)
            r.raise_for_status()
            j = r.json()
            for c in (j.get("results") or []):
                cid = parse_int_any(c.get("id"))
                nm = (c.get("name") or "").strip()
                if cid is not None and nm:
                    out[cid] = nm
            if not j.get("next"):
                break
            page += 1
        return out


class BrickOwlAPI:
    base = "https://api.brickowl.com/v1"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def color_list(self) -> Dict[int, str]:
        url = f"{self.base}/catalog/color_list"
        r = requests.get(url, params={"key": self.api_key}, timeout=60)
        r.raise_for_status()
        j = r.json()
        out: Dict[int, str] = {}
        data = j.get("data")
        if isinstance(data, dict):
            for k, v in data.items():
                cid = parse_int_any(k)
                nm = ""
                if isinstance(v, dict):
                    nm = (v.get("name") or v.get("color_name") or "").strip()
                if cid is not None:
                    out[cid] = nm
        elif isinstance(data, list):
            for v in data:
                cid = parse_int_any(v.get("color_id") or v.get("id"))
                nm = (v.get("name") or v.get("color_name") or "").strip()
                if cid is not None:
                    out[cid] = nm
        return out


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
    ap.add_argument("--strict", action="store_true", help="Falha apenas com ERROR.")
    ap.add_argument("--strict-all", action="store_true", help="Falha com ERROR ou WARN (não recomendado).")
    ap.add_argument("--verify-apis", choices=["none", "colors"], default="none",
                    help="Verifica IDs via APIs (poucas chamadas). Não bloqueia se creds faltarem.")
    args = ap.parse_args()

    bl_name_to_id, bl_id_to_name, bl_id_to_rgb = load_bl_colors_xml(Path(args.bl_colors_xml))
    rb_colors = load_rb_colors(Path(args.rb_colors))
    rb_elements = load_rb_elements(Path(args.rb_elements))
    seed, seed_issues = load_seed(Path(args.seed), rb_colors, bl_id_to_name)

    issues: List[Dict[str, object]] = list(seed_issues)

    # element_id -> BL color_id (from codes.xml + colors.xml)
    element_to_bl: Dict[str, int] = {}
    unknown_code_colors = Counter()

    for element_id, color_val in iter_bl_codes(Path(args.bl_codes_xml)):
        bl_id = parse_int_any(color_val)
        if bl_id is None:
            bl_id = bl_name_to_id.get(norm(color_val))
        if bl_id is None:
            unknown_code_colors[color_val] += 1
            continue

        prev = element_to_bl.get(element_id)
        if prev is None:
            element_to_bl[element_id] = bl_id
        elif prev != bl_id:
            issues.append({
                "severity": "ERROR",
                "issue_type": "BL_CODE_ELEMENT_COLOR_CONFLICT",
                "rb_color_id": "",
                "name": "",
                "details": f"Element {element_id} aparece com múltiplos BL color_id: {prev} vs {bl_id}",
                "suggestions": "Verificar inputs/bricklink/codes.xml.",
            })

    if unknown_code_colors:
        top = ", ".join([f"{k}({v})" for k, v in unknown_code_colors.most_common(10)])
        issues.append({
            "severity": "WARN",
            "issue_type": "BL_CODE_COLOR_NOT_IN_COLORSXML",
            "rb_color_id": "",
            "name": "",
            "details": f"Nomes/IDs em codes.xml não resolvidos via colors.xml (top10): {top}",
            "suggestions": "Atualizar colors.xml ou normalização.",
        })

    # Crosswalk counts: rb_color_id -> Counter(bl_color_id)
    pair_counts: Dict[int, Counter] = defaultdict(Counter)
    relevant_rb: set[int] = set()

    for element_id, rb_color_id in rb_elements.items():
        bl_id = element_to_bl.get(element_id)
        if bl_id is None:
            continue
        pair_counts[rb_color_id][bl_id] += 1
        relevant_rb.add(rb_color_id)

    # Resolve rb_color_id -> bl_color_id by dominance
    resolved_rb_to_bl: Dict[int, int] = {}
    for rb_id, c in pair_counts.items():
        if not c:
            continue
        best_bl, best_n = c.most_common(1)[0]
        total = sum(c.values())
        # conflict if significant disagreement
        if len(c) > 1 and (best_n / total) < 0.95:
            suggestions = "; ".join([f"{bid}:{n}" for bid, n in c.most_common(5)])
            issues.append({
                "severity": "ERROR",
                "issue_type": "RB_TO_BL_CONFLICT",
                "rb_color_id": rb_id,
                "name": rb_colors.get(rb_id, RBColor(rb_id, f"RB_{rb_id}", "", None, None)).name,
                "details": f"RB color mapeia para múltiplos BL colors (confiança {best_n}/{total}={best_n/total:.2%})",
                "suggestions": f"Fixar no seed. Candidatos: {suggestions}",
            })
            continue
        resolved_rb_to_bl[rb_id] = best_bl

    # Seed overrides are authoritative
    for rb_id, s in seed.items():
        if s.bl_color_id is not None:
            resolved_rb_to_bl[rb_id] = s.bl_color_id

    # Optional BrickOwl color list (single call) for bo_color_name + validation
    bo_id_to_name: Dict[int, str] = {}
    brickowl_key = os.environ.get("BRICKOWL_API_KEY", "").strip()
    if brickowl_key:
        try:
            bo_id_to_name = BrickOwlAPI(brickowl_key).color_list()
        except Exception as e:
            issues.append({
                "severity": "WARN",
                "issue_type": "BRICKOWL_COLOR_LIST_FAILED",
                "rb_color_id": "",
                "name": "",
                "details": f"Falha BrickOwl color_list (ignorado): {e}",
                "suggestions": "Verificar BRICKOWL_API_KEY/limites.",
            })

    # API verify (optional, low-call, does not block if creds missing)
    if args.verify_apis == "colors":
        # BrickLink
        ck = os.environ.get("BRICKLINK_CONSUMER_KEY", "").strip()
        cs = os.environ.get("BRICKLINK_CONSUMER_SECRET", "").strip()
        tk = os.environ.get("BRICKLINK_TOKEN", "").strip()
        ts = os.environ.get("BRICKLINK_TOKEN_SECRET", "").strip()
        if ck and cs and tk and ts:
            try:
                api_bl_colors = BrickLinkAPI(ck, cs, tk, ts).get_colors()
                for rb_id, bl_id in resolved_rb_to_bl.items():
                    if bl_id not in api_bl_colors:
                        issues.append({
                            "severity": "ERROR",
                            "issue_type": "BRICKLINK_API_COLOR_ID_UNKNOWN",
                            "rb_color_id": rb_id,
                            "name": rb_colors.get(rb_id, RBColor(rb_id, f"RB_{rb_id}", "", None, None)).name,
                            "details": f"BL color_id={bl_id} não existe na API BrickLink (colors).",
                            "suggestions": "Atualizar colors.xml/seed.",
                        })
            except Exception as e:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "BRICKLINK_API_COLORS_FAILED",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"Falha BrickLink colors via API (ignorado): {e}",
                    "suggestions": "Verificar OAuth/limites.",
                })
        else:
            issues.append({
                "severity": "WARN",
                "issue_type": "BRICKLINK_API_SKIPPED",
                "rb_color_id": "",
                "name": "",
                "details": "Credenciais BrickLink OAuth ausentes; verificação por API ignorada.",
                "suggestions": "Definir secrets BRICKLINK_*.",
            })

        # Rebrickable
        rb_key = os.environ.get("REBRICKABLE_API_KEY", "").strip()
        if rb_key:
            try:
                api_rb_colors = RebrickableAPI(rb_key).get_colors()
                for rb_id, rb in rb_colors.items():
                    if rb_id not in api_rb_colors:
                        issues.append({
                            "severity": "ERROR",
                            "issue_type": "REBRICKABLE_API_COLOR_ID_UNKNOWN",
                            "rb_color_id": rb_id,
                            "name": rb.name,
                            "details": "RB color_id existe no ficheiro, mas não aparece na API.",
                            "suggestions": "Confirmirar versão do ficheiro colors.csv.",
                        })
            except Exception as e:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "REBRICKABLE_API_COLORS_FAILED",
                    "rb_color_id": "",
                    "name": "",
                    "details": f"Falha Rebrickable colors via API (ignorado): {e}",
                    "suggestions": "Verificar REBRICKABLE_API_KEY/limites.",
                })
        else:
            issues.append({
                "severity": "WARN",
                "issue_type": "REBRICKABLE_API_SKIPPED",
                "rb_color_id": "",
                "name": "",
                "details": "REBRICKABLE_API_KEY ausente; verificação por API ignorada.",
                "suggestions": "Definir secret REBRICKABLE_API_KEY.",
            })

        # BrickOwl: validate provided bo_color_id if we have a list
        if bo_id_to_name:
            for rb_id, s in seed.items():
                if s.bo_color_id is not None and s.bo_color_id not in bo_id_to_name:
                    issues.append({
                        "severity": "ERROR",
                        "issue_type": "BRICKOWL_COLOR_ID_UNKNOWN",
                        "rb_color_id": rb_id,
                        "name": rb_colors.get(rb_id, RBColor(rb_id, f"RB_{rb_id}", "", None, None)).name,
                        "details": f"bo_color_id={s.bo_color_id} não existe no BrickOwl color_list.",
                        "suggestions": "Corrigir seed bo_color_id.",
                    })

    # Build outputs
    out_rows: List[Dict[str, object]] = []
    audit_rows: List[Dict[str, object]] = []

    for rb_id in sorted(rb_colors.keys()):
        rb = rb_colors[rb_id]
        s = seed.get(rb_id)

        bl_id = resolved_rb_to_bl.get(rb_id)
        bo_id = s.bo_color_id if s else None
        bo_name = bo_id_to_name.get(bo_id, "") if bo_id is not None else ""

        ldraw_id = rb.ldraw_color_id
        if s and s.ldraw_color_id is not None:
            ldraw_id = s.ldraw_color_id

        # Missing BL: ERROR only if RB color is relevant (seen in element crosswalk)
        if bl_id is None and rb_id in relevant_rb:
            issues.append({
                "severity": "ERROR",
                "issue_type": "BL_ID_MISSING_RELEVANT",
                "rb_color_id": rb_id,
                "name": rb.name,
                "details": "Sem bl_color_id apesar de existir evidência via Element ID crosswalk.",
                "suggestions": "Fixar no seed (rb_color_id -> bl_color_id).",
            })
        elif bl_id is None:
            issues.append({
                "severity": "WARN",
                "issue_type": "BL_ID_MISSING_RB_ONLY",
                "rb_color_id": rb_id,
                "name": rb.name,
                "details": "Sem bl_color_id e sem evidência de convergência via Element ID (aceitável).",
                "suggestions": "",
            })

        # Missing BO: warn (for now)
        if bo_id is None:
            issues.append({
                "severity": "WARN",
                "issue_type": "BO_ID_MISSING",
                "rb_color_id": rb_id,
                "name": rb.name,
                "details": "Sem bo_color_id (ainda não é obrigatório).",
                "suggestions": "",
            })

        out_rows.append({
            "name": rb.name,
            "rb_color_id": rb_id,
            "bl_color_id": bl_id,
            "bo_color_id": bo_id,
            "bo_color_name": bo_name,
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
            "bo_color_id": bo_id if bo_id is not None else "",
            "bo_color_name": bo_name,
            "ldraw_color_id": ldraw_id if ldraw_id is not None else "",
            "seed_bl": s.bl_color_id if s and s.bl_color_id is not None else "",
            "seed_bo": s.bo_color_id if s and s.bo_color_id is not None else "",
        })

    out_path = Path(args.out)
    audit_path = Path(args.audit)
    issues_path = Path(args.issues)

    write_csv(out_path,
              ["name", "rb_color_id", "bl_color_id", "bo_color_id", "bo_color_name", "ldraw_color_id"],
              out_rows)
    write_csv(audit_path,
              ["name", "rb_color_id", "rb_rgb", "rb_is_trans",
               "bl_color_id", "bl_color_name", "bl_rgb",
               "bo_color_id", "bo_color_name",
               "ldraw_color_id", "seed_bl", "seed_bo"],
              audit_rows)
    write_csv(issues_path,
              ["severity", "issue_type", "rb_color_id", "name", "details", "suggestions"],
              issues)

    n_err = sum(1 for x in issues if x.get("severity") == "ERROR")
    n_warn = sum(1 for x in issues if x.get("severity") == "WARN")

    print(f"✅ Wrote: {out_path} (rows={len(out_rows)})")
    print(f"✅ Wrote: {audit_path} (rows={len(audit_rows)})")
    print(f"✅ Wrote: {issues_path} (issues={len(issues)} | ERR={n_err} WARN={n_warn})")

    if args.strict_all and (n_err + n_warn) > 0:
        print("❌ STRICT-ALL mode: issues found. Exiting with code 2.")
        return 2
    if args.strict and n_err > 0:
        print("❌ STRICT mode: ERROR issues found. Exiting with code 2.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
