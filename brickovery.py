#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import sqlite3
import xml.etree.ElementTree as ET
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path: Path, data: dict) -> None:
    try:
        import json
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # cache is best-effort
        return


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


def load_rb_elements(path: Path) -> Dict[str, Tuple[str, int]]:
    """
    element_id -> (rb_part_num, rb_color_id)
    """
    m: Dict[str, Tuple[str, int]] = {}
    for row in read_csv_dicts(path):
        element_id = (row.get("element_id") or "").strip()
        part_num = (row.get("part_num") or "").strip()
        color_id = parse_int_any(row.get("color_id") or row.get("colour_id"))
        if element_id and part_num and color_id is not None:
            m[element_id] = (part_num, color_id)
    return m


def load_color_map(path: Path) -> Dict[int, Dict[str, object]]:
    """
    rb_color_id -> mapping fields
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


def build_bl_reverse_maps(color_map: Dict[int, Dict[str, object]]) -> Tuple[Dict[int, Optional[int]], Dict[int, Optional[int]], List[Dict[str, object]]]:
    """Build reverse maps bl_color_id -> bo_color_id / ldraw_color_id using the RB->BL color_map.

    Returns: (bl_to_bo, bl_to_ldraw, issues)
    """
    bl_to_bo: Dict[int, Optional[int]] = {}
    bl_to_ldraw: Dict[int, Optional[int]] = {}
    issues: List[Dict[str, object]] = []

    # group observed mappings by bl_color_id
    by_bl: Dict[int, List[Tuple[Optional[int], Optional[int]]]] = {}
    for rb_id, m in color_map.items():
        bl = m.get("bl_color_id")
        if bl is None:
            continue
        try:
            bl_int = int(bl)
        except Exception:
            continue
        by_bl.setdefault(bl_int, []).append((m.get("bo_color_id"), m.get("ldraw_color_id")))

    for bl_id, pairs in by_bl.items():
        bo_vals = {p[0] for p in pairs if p[0] is not None}
        ld_vals = {p[1] for p in pairs if p[1] is not None}

        bl_to_bo[bl_id] = next(iter(bo_vals)) if bo_vals else None
        bl_to_ldraw[bl_id] = next(iter(ld_vals)) if ld_vals else None

        if len(bo_vals) > 1:
            issues.append({
                "severity": "WARN",
                "issue_type": "BL_TO_BO_COLOR_INCONSISTENT",
                "bl_part_id": "",
                "element_id": "",
                "details": f"bl_color_id={bl_id} mapeia para múltiplos bo_color_id={sorted(bo_vals)} no color_map (usado o primeiro).",
            })
        if len(ld_vals) > 1:
            issues.append({
                "severity": "WARN",
                "issue_type": "BL_TO_LDRAW_COLOR_INCONSISTENT",
                "bl_part_id": "",
                "element_id": "",
                "details": f"bl_color_id={bl_id} mapeia para múltiplos ldraw_color_id={sorted(ld_vals)} no color_map (usado o primeiro).",
            })

    return bl_to_bo, bl_to_ldraw, issues


def bricklink_oauth_from_env() -> Optional[OAuth1]:
    ck = BRICKLINK_CONSUMER_KEY
    cs = BRICKLINK_CONSUMER_SECRET
    tk = BRICKLINK_TOKEN
    ts = BRICKLINK_TOKEN_SECRET
    if not (ck and cs and tk and ts):
        return None
    return OAuth1(ck, cs, tk, ts)


def bricklink_list_item_colors(bl_part_id: str, oauth: OAuth1, timeout_s: int = 30) -> List[int]:
    """Return BrickLink color_ids for a given Part (ITEMTYPE=P).

    Endpoint: GET /items/P/{itemNo}/colors
    """
    url = f"https://api.bricklink.com/api/store/v1/items/P/{bl_part_id}/colors"
    r = requests.get(url, auth=oauth, timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    items = data.get("data") or []
    out: List[int] = []
    for it in items:
        try:
            cid = int(it.get("color_id"))
            out.append(cid)
        except Exception:
            continue
    # unique, stable order
    return sorted(set(out))


def rebrickable_key_from_env() -> Optional[str]:
    key = REBRICKABLE_API_KEY
    return key.strip() if key else None


class BrickOwlAPI:
    def __init__(self, api_key: str, min_interval_s: float = 0.25, timeout_s: int = 30, cache: Optional[dict] = None):
        self.api_key = api_key
        self.min_interval_s = float(min_interval_s)
        self.timeout_s = int(timeout_s)
        self._last_call = 0.0
        self.cache = cache if cache is not None else {}

    def _sleep_if_needed(self) -> None:
        dt = time.time() - self._last_call
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def get(self, endpoint: str, params: Dict[str, object]) -> dict:
        base = "https://api.brickowl.com/v1"
        # BrickOwl usa 'key' como query param
        q = {"key": self.api_key}
        q.update({k: v for k, v in params.items() if v is not None})
        # cache key deterministico
        ck = "|".join([endpoint] + [f"{k}={q[k]}" for k in sorted(q.keys())])
        if ck in self.cache:
            return self.cache[ck]
        self._sleep_if_needed()
        url = f"{base}{endpoint}"
        r = requests.get(url, params=q, timeout=self.timeout_s)
        self._last_call = time.time()
        r.raise_for_status()
        data = r.json()
        self.cache[ck] = data
        return data

    @staticmethod
    def _extract_boids(payload: object) -> List[str]:
        out: List[str] = []
        if payload is None:
            return out
        if isinstance(payload, str):
            return out
        if isinstance(payload, list):
            for it in payload:
                out.extend(BrickOwlAPI._extract_boids(it))
            return out
        if isinstance(payload, dict):
            # comum: {"boid": "..."} ou {"data": [...]}
            if "boid" in payload and payload.get("boid"):
                out.append(str(payload.get("boid")))
            for k in ("boids", "data", "items", "results"):
                if k in payload:
                    out.extend(BrickOwlAPI._extract_boids(payload.get(k)))
            return out
        return out

    def id_lookup_boids_for_bl_part(self, bl_part_id: str) -> List[str]:
        data = self.get("/catalog/id_lookup", {
            "id": bl_part_id,
            "type": "Part",
            "id_type": "bl_item_no",
        })
        boids = self._extract_boids(data)
        # unique, stable
        boids = sorted(set([b for b in boids if b]))
        return boids

    def bulk_lookup(self, boids: List[str]) -> List[dict]:
        if not boids:
            return []
        # max 100, conforme docs
        data = self.get("/catalog/bulk_lookup", {
            "boids": ",".join(boids[:100]),
        })
        # resposta costuma estar em 'data'
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data, list):
            return data
        return []


def brickowl_key_from_env() -> Optional[str]:
    key = BRICKOWL_API_KEY
    return key.strip() if key else None


def api_selftest(issues: List[Dict[str, object]], debug: bool = True) -> None:
    """Self-test simples e auditável para BrickLink, Rebrickable e BrickOwl.

    - Não falha o run; regista INFO/WARN em issues e imprime no log.
    """
    if not debug:
        return

    print("\n=== API SELFTEST (BrickLink / Rebrickable / BrickOwl) ===")

    # BrickLink
    oauth = bricklink_oauth_from_env()
    if oauth is None:
        msg = "BrickLink OAuth: indisponível (secrets não definidos)."
        print("WARN:", msg)
        issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_BRICKLINK_UNAVAILABLE", "bl_part_id": "", "element_id": "", "details": msg})
    else:
        try:
            colors = bricklink_list_item_colors("3001", oauth, timeout_s=20)
            msg = f"BrickLink OAuth: OK (exemplo 3001 -> {len(colors)} cores)."
            print("OK:", msg)
            issues.append({"severity": "INFO", "issue_type": "API_SELFTEST_BRICKLINK_OK", "bl_part_id": "3001", "element_id": "", "details": msg})
        except Exception as e:
            msg = f"BrickLink OAuth: FALHA ao chamar /items/P/3001/colors: {e}"
            print("WARN:", msg)
            issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_BRICKLINK_FAILED", "bl_part_id": "3001", "element_id": "", "details": msg})

    # Rebrickable
    rb_key = rebrickable_key_from_env()
    if not rb_key:
        msg = "Rebrickable: indisponível (REBRICKABLE_API_KEY não definido)."
        print("WARN:", msg)
        issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_REBRICKABLE_UNAVAILABLE", "bl_part_id": "", "element_id": "", "details": msg})
    else:
        try:
            url = "https://rebrickable.com/api/v3/lego/colors/?page_size=1"
            r = requests.get(url, headers={"Authorization": f"key {rb_key}"}, timeout=20)
            r.raise_for_status()
            msg = "Rebrickable: OK (/lego/colors?page_size=1)."
            print("OK:", msg)
            issues.append({"severity": "INFO", "issue_type": "API_SELFTEST_REBRICKABLE_OK", "bl_part_id": "", "element_id": "", "details": msg})
        except Exception as e:
            msg = f"Rebrickable: FALHA no selftest: {e}"
            print("WARN:", msg)
            issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_REBRICKABLE_FAILED", "bl_part_id": "", "element_id": "", "details": msg})

    # BrickOwl
    bo_key = brickowl_key_from_env()
    if not bo_key:
        msg = "BrickOwl: indisponível (BRICKOWL_API_KEY não definido)."
        print("WARN:", msg)
        issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_BRICKOWL_UNAVAILABLE", "bl_part_id": "", "element_id": "", "details": msg})
    else:
        try:
            bo = BrickOwlAPI(bo_key, min_interval_s=0.0, timeout_s=20, cache={})
            # endpoint user/details é estável; catalog/color_list valida acesso ao catalog API
            _ = bo.get("/user/details", {})
            _ = bo.get("/catalog/color_list", {})
            msg = "BrickOwl: OK (/user/details + /catalog/color_list)."
            print("OK:", msg)
            issues.append({"severity": "INFO", "issue_type": "API_SELFTEST_BRICKOWL_OK", "bl_part_id": "", "element_id": "", "details": msg})
        except Exception as e:
            msg = f"BrickOwl: FALHA no selftest: {e}"
            print("WARN:", msg)
            issues.append({"severity": "WARN", "issue_type": "API_SELFTEST_BRICKOWL_FAILED", "bl_part_id": "", "element_id": "", "details": msg})

    print("=== END API SELFTEST ===\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bl-codes-xml", required=True)
    ap.add_argument("--rb-elements", required=True)
    ap.add_argument("--color-map", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--issues", required=True)
    ap.add_argument("--debug-apis", action="store_true", help="Executa um selftest às APIs (BrickLink/Rebrickable/BrickOwl) e imprime no log")
    ap.add_argument("--resolve-boid", action="store_true", help="Resolve BOIDs via BrickOwl (id_lookup + bulk_lookup)")
    ap.add_argument("--boid-cache-json", default="", help="(Opcional) cache JSON para respostas BrickOwl (reduz chamadas repetidas)")
    ap.add_argument("--boid-min-interval", type=float, default=0.25, help="Intervalo minimo entre chamadas BrickOwl (segundos)")
    ap.add_argument("--boid-timeout", type=int, default=30, help="Timeout BrickOwl (segundos)")
    ap.add_argument("--boid-max-parts", type=int, default=5000, help="Limite de parts (bl_part_id) para resolver BOID num run (0 = ilimitado)")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    issues: List[Dict[str, object]] = []
    if args.debug_apis:
        api_selftest(issues, debug=True)

    rb_elements = load_rb_elements(Path(args.rb_elements))
    color_map = load_color_map(Path(args.color_map))

    bl_to_bo, bl_to_ldraw, revmap_issues = build_bl_reverse_maps(color_map)

    rows: List[Dict[str, object]] = []

    # include any reverse-map warnings
    issues.extend(revmap_issues)

    # BrickLink API setup (mandatory attempt when element_id missing in RB)
    bl_oauth = bricklink_oauth_from_env()
    bl_colors_cache: Dict[str, List[int]] = {}
    fallback_parts_done: set[str] = set()

    for bl_part_id, element_id in iter_bl_codes_codesxml(Path(args.bl_codes_xml)):
        if element_id not in rb_elements:
            # 1) Always log the divergence, but do NOT drop the part from DB/CSV.
            issues.append({
                "severity": "WARN",
                "issue_type": "ELEMENT_NOT_IN_REBRICKABLE_ELEMENTS",
                "bl_part_id": bl_part_id,
                "element_id": element_id,
                "details": "Element ID do BrickLink não encontrado em Rebrickable elements.csv (divergência aceitável). Será tentado fallback via BrickLink API usando bl_part_id.",
            })

            # 2) Mandatory verification attempt: query BrickLink by bl_part_id and obtain known colors.
            # To avoid duplicating rows massively, we only materialize the BL color list once per bl_part_id.
            if bl_part_id in fallback_parts_done:
                issues.append({
                    "severity": "INFO",
                    "issue_type": "ELEMENT_MISSING_RB_ALREADY_FALLBACKED",
                    "bl_part_id": bl_part_id,
                    "element_id": element_id,
                    "details": "Já foi feito fallback BrickLink (cores) para este bl_part_id nesta execução; não foram criadas linhas duplicadas.",
                })
                continue

            if bl_oauth is None:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "BRICKLINK_API_UNAVAILABLE",
                    "bl_part_id": bl_part_id,
                    "element_id": element_id,
                    "details": "Credenciais BrickLink (OAuth) não estão definidas no ambiente; não foi possível obter lista de cores via API. A peça será incluída com cores em branco.",
                })
                rows.append({
                    "bl_part_id": bl_part_id,
                    "element_id": "",  # BL-only fallback: sem element_id determinístico por cor
                    "rb_part_num": "",
                    "rb_color_id": None,
                    "bl_color_id": None,
                    "bo_color_id": None,
                    "ldraw_color_id": None,
                    "boid": "",
                })
                fallback_parts_done.add(bl_part_id)
                continue

            try:
                if bl_part_id not in bl_colors_cache:
                    bl_colors_cache[bl_part_id] = bricklink_list_item_colors(bl_part_id, bl_oauth)
                colors = bl_colors_cache[bl_part_id]
                issues.append({
                    "severity": "INFO",
                    "issue_type": "BRICKLINK_PART_COLORS_LOOKUP",
                    "bl_part_id": bl_part_id,
                    "element_id": element_id,
                    "details": f"BrickLink API devolveu {len(colors)} cores para bl_part_id={bl_part_id}: {colors[:20]}{'...' if len(colors) > 20 else ''}",
                })

                if not colors:
                    rows.append({
                        "bl_part_id": bl_part_id,
                        "element_id": "",
                        "rb_part_num": "",
                        "rb_color_id": None,
                        "bl_color_id": None,
                        "bo_color_id": None,
                        "ldraw_color_id": None,
                        "boid": "",
                    })
                    issues.append({
                        "severity": "WARN",
                        "issue_type": "BRICKLINK_PART_COLORS_EMPTY",
                        "bl_part_id": bl_part_id,
                        "element_id": element_id,
                        "details": "BrickLink API não devolveu cores (lista vazia). A peça foi incluída na DB sem cores.",
                    })
                else:
                    for bl_color_id in colors:
                        rows.append({
                            "bl_part_id": bl_part_id,
                            "element_id": "",
                            "rb_part_num": "",
                            "rb_color_id": None,
                            "bl_color_id": bl_color_id,
                            "bo_color_id": bl_to_bo.get(bl_color_id),
                            "ldraw_color_id": bl_to_ldraw.get(bl_color_id),
                            "boid": "",
                        })
                    issues.append({
                        "severity": "INFO",
                        "issue_type": "ELEMENT_MISSING_RB_INCLUDED_USING_BRICKLINK_COLORS",
                        "bl_part_id": bl_part_id,
                        "element_id": element_id,
                        "details": f"Peça incluída na DB/CSV via BrickLink colors (linhas criadas={len(colors)}).",
                    })

            except Exception as e:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "BRICKLINK_PART_COLORS_LOOKUP_FAILED",
                    "bl_part_id": bl_part_id,
                    "element_id": element_id,
                    "details": f"Falha ao obter cores via BrickLink API para bl_part_id={bl_part_id}: {e}. A peça será incluída na DB sem cores.",
                })
                rows.append({
                    "bl_part_id": bl_part_id,
                    "element_id": "",
                    "rb_part_num": "",
                    "rb_color_id": None,
                    "bl_color_id": None,
                    "bo_color_id": None,
                    "ldraw_color_id": None,
                    "boid": "",
                })

            fallback_parts_done.add(bl_part_id)
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

        rows.append({
            "bl_part_id": bl_part_id,
            "element_id": element_id,
            "rb_part_num": rb_part_num,
            "rb_color_id": rb_color_id,
            "bl_color_id": cm.get("bl_color_id"),
            "bo_color_id": cm.get("bo_color_id"),
            "ldraw_color_id": cm.get("ldraw_color_id"),
            "boid": "",  # reservado (próximo passo)
        })

    # -----------------------------
    # Optional: resolve BrickOwl BOID per (bl_part_id, bo_color_id)
    # -----------------------------
    if args.resolve_boid:
        bo_key = brickowl_key_from_env()
        if not bo_key:
            issues.append({
                "severity": "WARN",
                "issue_type": "BRICKOWL_API_UNAVAILABLE",
                "bl_part_id": "",
                "element_id": "",
                "details": "--resolve-boid ativo, mas BRICKOWL_API_KEY não está definido no ambiente. BOID ficará em branco.",
            })
            print("[BOID] BrickOwl: UNAVAILABLE (missing BRICKOWL_API_KEY)")
        else:
            cache_path = Path(args.boid_cache_json) if args.boid_cache_json else None
            cache = load_json(cache_path) if cache_path else {}
            bo_api = BrickOwlAPI(bo_key, min_interval_s=args.boid_min_interval, timeout_s=args.boid_timeout, cache=cache)

            def extract_color_id(d: dict) -> Optional[int]:
                # tenta chaves comuns
                for k in ("color_id", "colour_id", "colorId", "colorID"):
                    if k in d:
                        v = parse_int_any(d.get(k))
                        if v is not None:
                            return v
                # tenta nested "color" dict
                c = d.get("color")
                if isinstance(c, dict):
                    for k in ("color_id", "id", "colour_id"):
                        v = parse_int_any(c.get(k))
                        if v is not None:
                            return v
                return None

            def extract_boid(d: dict) -> Optional[str]:
                for k in ("boid", "BOID", "id"):
                    if k in d and d.get(k):
                        s = str(d.get(k)).strip()
                        if s:
                            return s
                return None

            # Parts a resolver (apenas onde temos bo_color_id)
            parts = sorted({r["bl_part_id"] for r in rows if r.get("bo_color_id") is not None})
            if args.boid_max_parts and args.boid_max_parts > 0 and len(parts) > args.boid_max_parts:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "BOID_RESOLUTION_PART_LIMIT",
                    "bl_part_id": "",
                    "element_id": "",
                    "details": f"Existem {len(parts)} parts com bo_color_id. Limite boid_max_parts={args.boid_max_parts}; BOID será resolvido apenas para as primeiras {args.boid_max_parts} (ordem determinística).",
                })
                parts = parts[: args.boid_max_parts]

            part_to_boids: Dict[str, List[str]] = {}
            all_boids: List[str] = []

            for p in parts:
                try:
                    boids = bo_api.id_lookup_boids_for_bl_part(p)
                    part_to_boids[p] = boids
                    all_boids.extend(boids)
                except Exception as e:
                    issues.append({
                        "severity": "WARN",
                        "issue_type": "BRICKOWL_ID_LOOKUP_FAILED",
                        "bl_part_id": p,
                        "element_id": "",
                        "details": f"BrickOwl id_lookup falhou para bl_part_id={p}: {e}",
                    })

            all_boids = sorted(set([b for b in all_boids if b]))
            boid_details: Dict[str, dict] = {}
            for i in range(0, len(all_boids), 100):
                batch = all_boids[i:i+100]
                try:
                    items = bo_api.bulk_lookup(batch)
                    for it in items:
                        if isinstance(it, dict):
                            b = extract_boid(it)
                            if b:
                                boid_details[b] = it
                except Exception as e:
                    issues.append({
                        "severity": "WARN",
                        "issue_type": "BRICKOWL_BULK_LOOKUP_FAILED",
                        "bl_part_id": "",
                        "element_id": "",
                        "details": f"BrickOwl bulk_lookup falhou (batch {i}-{i+len(batch)}): {e}",
                    })

            # part -> bo_color_id -> chosen boid
            part_color_boid: Dict[str, Dict[int, str]] = {}
            for p, boids in part_to_boids.items():
                cmap: Dict[int, List[str]] = {}
                for b in boids:
                    det = boid_details.get(b)
                    if not isinstance(det, dict):
                        continue
                    cid = extract_color_id(det)
                    if cid is None:
                        continue
                    cmap.setdefault(cid, []).append(b)
                chosen: Dict[int, str] = {}
                for cid, blist in cmap.items():
                    blist = sorted(set(blist))
                    chosen[cid] = blist[0]
                    if len(blist) > 1:
                        issues.append({
                            "severity": "WARN",
                            "issue_type": "BRICKOWL_MULTIPLE_BOIDS_SAME_COLOR",
                            "bl_part_id": p,
                            "element_id": "",
                            "details": f"BrickOwl retornou múltiplos BOIDs para bl_part_id={p} bo_color_id={cid}: {blist}. Usado o primeiro.",
                        })
                part_color_boid[p] = chosen

            # Apply to rows (log missing once per pair)
            missing_pairs: set[Tuple[str, int]] = set()
            filled = 0
            for r in rows:
                p = r.get("bl_part_id")
                cid = r.get("bo_color_id")
                if not p or cid is None:
                    continue
                if p not in part_color_boid:
                    continue
                boid = part_color_boid[p].get(int(cid))
                if boid:
                    if not r.get("boid"):
                        r["boid"] = boid
                        filled += 1
                else:
                    missing_pairs.add((p, int(cid)))

            for p, cid in sorted(list(missing_pairs))[:5000]:
                issues.append({
                    "severity": "WARN",
                    "issue_type": "BRICKOWL_BOID_NOT_FOUND_FOR_COLOR",
                    "bl_part_id": p,
                    "element_id": "",
                    "details": f"Não foi encontrado BOID BrickOwl para bl_part_id={p} com bo_color_id={cid}.",
                })

            issues.append({
                "severity": "INFO",
                "issue_type": "BRICKOWL_BOID_RESOLUTION_SUMMARY",
                "bl_part_id": "",
                "element_id": "",
                "details": f"BOID resolution: parts_considered={len(parts)} unique_boids={len(all_boids)} rows_filled={filled} missing_pairs={len(missing_pairs)}.",
            })
            print(f"[BOID] Done: parts={len(parts)} unique_boids={len(all_boids)} rows_filled={filled} missing_pairs={len(missing_pairs)}")

            if cache_path:
                save_json(cache_path, bo_api.cache)

    # Write CSVs
    out_csv = Path(args.out_csv)
    write_csv(out_csv,
              ["bl_part_id", "element_id", "rb_part_num", "rb_color_id", "bl_color_id", "bo_color_id", "ldraw_color_id", "boid"],
              rows)

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
      element_id TEXT,
      rb_part_num TEXT,
      rb_color_id INTEGER,
      bl_color_id INTEGER,
      bo_color_id INTEGER,
      ldraw_color_id INTEGER,
      boid TEXT
    )
    """)
    cur.execute("DELETE FROM part_color_map")
    cur.executemany("""
      INSERT INTO part_color_map
      (bl_part_id, element_id, rb_part_num, rb_color_id, bl_color_id, bo_color_id, ldraw_color_id, boid)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (r["bl_part_id"], r["element_id"], r["rb_part_num"], r["rb_color_id"],
         r["bl_color_id"], r["bo_color_id"], r["ldraw_color_id"], r["boid"])
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
