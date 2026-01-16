#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Brickovery - build DB + CSV

Outputs (default paths via workflow):
  data/brickovery.db
  data/part_color_map.csv
  data/part_color_issues.csv
  data/build_checkpoint.json
  data/brickovery_build_error.log

Key behaviors (per project decisions):
- Divergências naturais NÃO bloqueiam o pipeline (WARN não falha).
- Se um BrickLink element_id (codes.xml) não existir no Rebrickable elements.csv:
    * regista WARN (ELEMENT_NOT_IN_REBRICKABLE_ELEMENTS)
    * tenta obrigatoriamente BrickLink API (known colors) pelo bl_part_id
    * insere linhas BL-only (rb_* = NULL) para não perder a peça
- BOID é opcional (ativa com --resolve-boid) e usa BrickOwl catalog/id_lookup + (fallback) catalog/lookup e catalog/bulk_lookup. Opcional: validação extra via catalog/availability.
- Debug/robustez para GitHub Actions:
    * cria ficheiros de output logo no início (evita "No files were found" quando algo falha cedo)
    * checkpoint periódico (JSON)
    * logs de progresso
    * handler SIGTERM/SIGINT para commit rápido + checkpoint antes do cancelamento

Secrets (GitHub Actions env) devem estar configuradas assim no workflow:
  REBRICKABLE_API_KEY: ${{ secrets.REBRICKABLE_API_KEY }}
  BRICKOWL_API_KEY: ${{ secrets.BRICKOWL_API_KEY }}
  BRICKLINK_CONSUMER_KEY: ${{ secrets.BRICKLINK_CONSUMER_KEY }}
  BRICKLINK_CONSUMER_SECRET: ${{ secrets.BRICKLINK_CONSUMER_SECRET }}
  BRICKLINK_TOKEN: ${{ secrets.BRICKLINK_TOKEN }}
  BRICKLINK_TOKEN_SECRET: ${{ secrets.BRICKLINK_TOKEN_SECRET }}
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sqlite3
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import requests
from requests_oauthlib import OAuth1

# -----------------------------
# ENV (secrets)
# -----------------------------
REBRICKABLE_API_KEY = os.getenv("REBRICKABLE_API_KEY", "").strip()
BRICKOWL_API_KEY = os.getenv("BRICKOWL_API_KEY", "").strip()

BRICKLINK_CONSUMER_KEY = os.getenv("BRICKLINK_CONSUMER_KEY", "").strip()
BRICKLINK_CONSUMER_SECRET = os.getenv("BRICKLINK_CONSUMER_SECRET", "").strip()
BRICKLINK_TOKEN = os.getenv("BRICKLINK_TOKEN", "").strip()
BRICKLINK_TOKEN_SECRET = os.getenv("BRICKLINK_TOKEN_SECRET", "").strip()

# -----------------------------
# BrickOwl base URLs
# -----------------------------
BRICKOWL_CATALOG_BASE_URL = "https://api.brickowl.com/v1/catalog"
BRICKOWL_USER_BASE_URL = "https://api.brickowl.com/v1/user"

# -----------------------------
# BrickLink itemtype normalization
# -----------------------------
ITEMTYPE_TO_PATH = {
    "P": "part",
    "PART": "part",
    "S": "set",
    "SET": "set",
    "M": "minifig",
    "MINIFIG": "minifig",
    "G": "gear",
    "GEAR": "gear",
    "B": "book",
    "BOOK": "book",
    "C": "catalog",
    "CATALOG": "catalog",
    "I": "instruction",
    "INSTRUCTION": "instruction",
    "O": "original_box",
    "ORIGINAL_BOX": "original_box",
    "U": "unsorted_lot",
    "UNSORTED_LOT": "unsorted_lot",
}

# -----------------------------
# Global stop flag (for SIGTERM/SIGINT)
# -----------------------------
_STOP = False
_STOP_REASON = ""
_STOP_CHECKPOINT_PATH: Optional[Path] = None
_STOP_ERROR_LOG_PATH: Optional[Path] = None


def _sig_handler(signum, _frame):
    global _STOP, _STOP_REASON
    _STOP = True
    _STOP_REASON = f"signal={signum}"
    # Best-effort: write a minimal checkpoint + note in error log.
    try:
        if _STOP_CHECKPOINT_PATH:
            save_json(
                _STOP_CHECKPOINT_PATH,
                {
                    "ts": int(time.time()),
                    "phase": "signal",
                    "reason": _STOP_REASON,
                },
            )
        if _STOP_ERROR_LOG_PATH:
            _STOP_ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _STOP_ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] STOP requested: {_STOP_REASON}\n")
    except Exception:
        pass


# -----------------------------
# Small IO helpers
# -----------------------------

def now_s() -> float:
    return time.time()


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def touch_with_header_csv(path: Path, header: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(list(header))


def append_error_log(path: Path, msg: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(msg.rstrip() + "\n")


# -----------------------------
# Parsing inputs
# -----------------------------

def iter_codes_xml(codes_xml: Path) -> Iterable[Tuple[str, str, str]]:
    """Yield (itemtype, bl_part_id, element_id) from BrickLink codes.xml.

    Expected structure (varies, but typical):
      <ITEM>
        <ITEMTYPE>P</ITEMTYPE>
        <ITEMID>3001</ITEMID>
        <CODE>300121</CODE>   # element_id
      </ITEM>

    Some exports use <CODENAME> or nested tags; we try common variants.
    """
    context = ET.iterparse(str(codes_xml), events=("end",))
    for _ev, el in context:
        if el.tag.upper() != "ITEM":
            continue
        itemtype = (el.findtext("ITEMTYPE") or el.findtext("ItemType") or "").strip()
        itemid = (el.findtext("ITEMID") or el.findtext("ItemID") or "").strip()
        code = (el.findtext("CODE") or el.findtext("Code") or el.findtext("CODENAME") or el.findtext("CodeName") or "").strip()
        el.clear()
        if not itemid or not code:
            continue
        yield (itemtype or "P"), itemid, code


def load_rb_elements(elements_csv: Path) -> Dict[str, Tuple[str, int]]:
    """Return dict: element_id(str) -> (rb_part_num(str), rb_color_id(int))."""
    out: Dict[str, Tuple[str, int]] = {}
    with elements_csv.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            eid = (row.get("element_id") or row.get("element") or row.get("id") or "").strip()
            part = (row.get("part_num") or row.get("part") or "").strip()
            cid = (row.get("color_id") or row.get("rb_color_id") or "").strip()
            if not eid or not part or not cid:
                continue
            try:
                out[eid] = (part, int(cid))
            except Exception:
                continue
    return out


def load_color_map(color_map_csv: Path) -> Dict[int, Dict[str, Optional[int]]]:
    """Return dict: rb_color_id -> {bl_color_id, bo_color_id, ldraw_color_id} (ints or None)."""
    out: Dict[int, Dict[str, Optional[int]]] = {}
    with color_map_csv.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            rb = (row.get("rb_color_id") or "").strip()
            if rb == "":
                continue
            try:
                rb_id = int(rb)
            except Exception:
                continue
            def _to_int(v: Optional[str]) -> Optional[int]:
                v = (v or "").strip()
                if v == "":
                    return None
                try:
                    return int(v)
                except Exception:
                    return None
            out[rb_id] = {
                "bl_color_id": _to_int(row.get("bl_color_id")),
                "bo_color_id": _to_int(row.get("bo_color_id")),
                "ldraw_color_id": _to_int(row.get("ldraw_color_id")),
            }
    return out


def build_bl_reverse_maps(color_map: Dict[int, Dict[str, Optional[int]]]) -> Tuple[Dict[int, int], Dict[int, int], List[Tuple[str, str, str, str]]]:
    """Build reverse maps from bl_color_id -> bo_color_id/ldraw_color_id.

    Returns:
      bl_to_bo, bl_to_ldraw, issues_rows
    issues_rows are tuples suitable for build_issues insert.
    """
    bl_to_bo: Dict[int, int] = {}
    bl_to_ldraw: Dict[int, int] = {}
    issues: List[Tuple[str, str, str, str]] = []

    for rb_id, m in color_map.items():
        bl = m.get("bl_color_id")
        bo = m.get("bo_color_id")
        ld = m.get("ldraw_color_id")
        if bl is None:
            continue
        if bo is not None:
            if bl in bl_to_bo and bl_to_bo[bl] != bo:
                issues.append(("WARN", "BL_COLOR_TO_BO_COLOR_CONFLICT", str(bl), f"bl_color_id={bl} mapped to multiple bo_color_id: {bl_to_bo[bl]} vs {bo}"))
            else:
                bl_to_bo[bl] = bo
        if ld is not None:
            if bl in bl_to_ldraw and bl_to_ldraw[bl] != ld:
                issues.append(("WARN", "BL_COLOR_TO_LDRAW_COLOR_CONFLICT", str(bl), f"bl_color_id={bl} mapped to multiple ldraw_color_id: {bl_to_ldraw[bl]} vs {ld}"))
            else:
                bl_to_ldraw[bl] = ld

    return bl_to_bo, bl_to_ldraw, issues


# -----------------------------
# BrickLink API
# -----------------------------

def bricklink_oauth_from_env() -> Optional[OAuth1]:
    if not (BRICKLINK_CONSUMER_KEY and BRICKLINK_CONSUMER_SECRET and BRICKLINK_TOKEN and BRICKLINK_TOKEN_SECRET):
        return None
    return OAuth1(BRICKLINK_CONSUMER_KEY, BRICKLINK_CONSUMER_SECRET, BRICKLINK_TOKEN, BRICKLINK_TOKEN_SECRET)


def bricklink_list_item_colors(bl_part_id: str, oauth: OAuth1, item_type: str = "P", timeout_s: int = 30) -> List[int]:
    """GET /items/{type}/{no}/colors and return list of BL color_ids."""
    t = ITEMTYPE_TO_PATH.get((item_type or "P").strip().upper(), "part")
    no = quote((bl_part_id or "").strip(), safe="")
    url = f"https://api.bricklink.com/api/store/v1/items/{t}/{no}/colors"
    r = requests.get(url, auth=oauth, timeout=timeout_s)
    r.raise_for_status()
    data = r.json() or {}
    items = data.get("data") or []
    out: List[int] = []
    for it in items:
        try:
            out.append(int(it.get("color_id")))
        except Exception:
            continue
    return sorted(set(out))


# -----------------------------
# BrickOwl API
# -----------------------------

class BrickOwlAPI:
    """Minimal BrickOwl wrapper with throttling + cache.

    Docs: https://www.brickowl.com/api_docs
    Key must have Catalog API access for catalog endpoints.
    """

    def __init__(
        self,
        api_key: str,
        min_interval_s: float = 0.11,
        bulk_min_interval_s: float = 0.65,
        timeout_s: int = 30,
        cache: Optional[dict] = None,
    ):
        self.api_key = api_key
        self.min_interval_s = float(min_interval_s)
        self.bulk_min_interval_s = float(bulk_min_interval_s)
        self.timeout_s = int(timeout_s)
        self._last_call = 0.0
        self.cache = cache if cache is not None else {}

    def _sleep(self, min_interval: float) -> None:
        dt = time.time() - self._last_call
        if dt < min_interval:
            time.sleep(min_interval - dt)

    def _get(self, url: str, params: dict, min_interval: float) -> dict:
        self._sleep(min_interval)
        self._last_call = time.time()
        r = requests.get(url, params=params, timeout=self.timeout_s)
        r.raise_for_status()
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else json.loads(r.text)

    def user_details(self) -> dict:
        url = f"{BRICKOWL_USER_BASE_URL}/details"
        return self._get(url, {"key": self.api_key}, self.min_interval_s)

    def catalog_color_list(self) -> dict:
        url = f"{BRICKOWL_CATALOG_BASE_URL}/color_list"
        return self._get(url, {"key": self.api_key}, self.min_interval_s)

    def catalog_id_lookup(self, *, id_value: str, item_type: str = "Part", id_type: str = "bl_item_no") -> List[str]:
        """Return list of candidate BOIDs (strings) for an external id.

        IMPORTANT: BrickOwl BOID is commonly formatted like "<item_id>-<color_id>" (string),
        so we must NOT coerce to int.

        Docs: GET /catalog/id_lookup?id=...&type=Part&id_type=bl_item_no
        """
        cache_key = f"id_lookup:{item_type}:{id_type}:{id_value}"
        if cache_key in self.cache:
            return list(self.cache[cache_key])

        url = f"{BRICKOWL_CATALOG_BASE_URL}/id_lookup"
        data = self._get(
            url,
            {"key": self.api_key, "id": id_value, "type": item_type, "id_type": id_type},
            self.min_interval_s,
        )

        # BrickOwl responses vary by key / shape. Normalizamos para lista de BOIDs (strings).
        items = None
        if isinstance(data, dict):
            # Most common: {"data": [...]}
            for k in ("data", "items", "boids", "result", "results"): 
                if k in data:
                    items = data.get(k)
                    break
            # Some variants nest inside data={...}
            if items is None and isinstance(data.get("data"), dict):
                d = data.get("data") or {}
                for k in ("items", "boids", "result"): 
                    if k in d:
                        items = d.get(k)
                        break
        else:
            items = data

        boids: List[str] = []
        if isinstance(items, list):
            for it in items:
                b = None
                if isinstance(it, dict):
                    b = it.get("boid") or it.get("id") or it.get("bo_id")
                else:
                    b = it
                if b is None:
                    continue
                bs = str(b).strip()
                if not bs or bs == "0":
                    continue
                boids.append(bs)
        elif isinstance(items, (str, int)):
            bs = str(items).strip()
            if bs and bs != "0":
                boids.append(bs)

        boids = sorted(set(boids))
        self.cache[cache_key] = boids
        return boids


    def catalog_bulk_lookup(self, boids: Sequence[str]) -> List[dict]:
        """GET /catalog/bulk_lookup?boids=... (max 100).

        Accepts BOIDs as strings (may contain '-') and returns list of item dicts.
        """
        boids = [str(b).strip() for b in boids if str(b).strip()]
        if not boids:
            return []
        boids = sorted(set(boids))
        key = "bulk_lookup:" + ",".join(boids)
        if key in self.cache:
            return list(self.cache[key])

        url = f"{BRICKOWL_CATALOG_BASE_URL}/bulk_lookup"
        data = self._get(url, {"key": self.api_key, "boids": ",".join(boids)}, self.bulk_min_interval_s)
        items = data.get("data") if isinstance(data, dict) else data
        out: List[dict] = []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    out.append(it)
        self.cache[key] = out
        return out

    def catalog_lookup(self, boid: str) -> dict:
        """GET /catalog/lookup?boid=... and return parsed JSON.

        Primarily used to validate a *guessed* BOID constructed from a base item id + "-<color_id>".
        """
        b = str(boid).strip()
        if not b:
            raise ValueError("boid vazio")
        key = f"lookup:{b}"
        if key in self.cache:
            return dict(self.cache[key]) if isinstance(self.cache[key], dict) else self.cache[key]

        url = f"{BRICKOWL_CATALOG_BASE_URL}/lookup"
        data = self._get(url, {"key": self.api_key, "boid": b}, self.min_interval_s)
        # Cache raw response; callers decide how to interpret.
        self.cache[key] = data
        return data

    def catalog_availability(self, boid: str, country: str, quantity: int = 1, store_country: str = '') -> dict:
        """GET /catalog/availability?boid=...&country=...

        Useful to validate that a BOID is accepted by the API in a realistic call-path.
        Note: availability may legitimately return an empty list depending on market supply.
        """
        b = str(boid).strip()
        if not b:
            raise ValueError('boid vazio')
        c = (country or '').strip().upper()
        if len(c) != 2:
            raise ValueError('country deve ser ISO2 (ex: PT)')
        q = int(quantity) if int(quantity) > 0 else 1
        key = f"availability:{b}:{c}:{q}:{store_country}"
        if key in self.cache:
            v = self.cache[key]
            return dict(v) if isinstance(v, dict) else v
        url = f"{BRICKOWL_CATALOG_BASE_URL}/availability"
        params = {'key': self.api_key, 'boid': b, 'country': c, 'quantity': q}
        sc = (store_country or '').strip().upper()
        if sc:
            params['store_country'] = sc
        data = self._get(url, params, self.min_interval_s)
        self.cache[key] = data
        return data





def pick_boid_base(boids: List[str]) -> str:
    """Preferimos um BOID base (sem sufixo -<cor>) quando existir.

    Se só existirem BOIDs com cor, devolve a parte antes do '-' do 1º candidato.
    """
    for b in boids:
        if '-' not in str(b):
            return str(b).strip()
    if boids:
        return str(boids[0]).split('-', 1)[0].strip()
    raise ValueError('id_lookup não devolveu BOIDs para este bl_item_no.')
def resolve_boid_for_pair(
    bo_api: BrickOwlAPI,
    bl_part_id: str,
    bo_color_id: int,
    issues_add: callable,
    *,
    country: str = "PT",
    validate_availability: bool = False,
) -> Optional[str]:
    """Resolve BOID (string) for (bl_part_id, bo_color_id) using BrickOwl catalog.

    Strategy:
    1) id_lookup(bl_item_no) -> candidate boids (strings)
    2) fast-path: pick candidate that endswith f"-{bo_color_id}" when present
    3) bulk_lookup(candidates) -> confirm by matching returned color_id

    Returns boid string or None.
    """
    cache_key = f"boid_resolve:{bl_part_id}-{bo_color_id}"
    if cache_key in bo_api.cache:
        v = bo_api.cache[cache_key]
        return str(v) if v else None

    try:
        candidates = bo_api.catalog_id_lookup(id_value=bl_part_id, item_type="Part", id_type="bl_item_no")
    except Exception as e:
        issues_add("WARN", "BRICKOWL_ID_LOOKUP_FAILED", f"{bl_part_id}", f"id_lookup falhou: {e}")
        bo_api.cache[cache_key] = None
        return None

    if not candidates:
        issues_add(
            "WARN",
            "BRICKOWL_ID_LOOKUP_EMPTY",
            f"{bl_part_id}",
            f"id_lookup devolveu 0 BOIDs para bl_part_id={bl_part_id}",
        )
        bo_api.cache[cache_key] = None
        return None

    def _boid_looks_valid(boid: str) -> bool:
        """Valida BOID via /catalog/lookup e, opcionalmente, /catalog/availability.

        Nota: availability pode devolver vazio, isso não invalida o BOID.
        """
        b = str(boid).strip()
        if not b:
            return False
        try:
            resp = bo_api.catalog_lookup(b)
            if isinstance(resp, dict) and resp.get('error'):
                return False
        except Exception:
            return False
        if validate_availability:
            try:
                a = bo_api.catalog_availability(b, country=country)
                if isinstance(a, dict) and a.get('error'):
                    return False
            except Exception:
                return False
        return True

    # Fast-path: BrickOwl BOID is often "<item_id>-<color_id>"
    target_suffix = f"-{int(bo_color_id)}"
    for b in candidates:
        try:
            if str(b).endswith(target_suffix):
                bo_api.cache[cache_key] = str(b)
                return str(b)
        except Exception:
            continue

    # Caso o id_lookup não traga explicitamente a cor desejada, tentamos:
    # 1) construir BOID a partir do BOID base preferido (sem '-') e validar via lookup/availability
    # 2) (fallback) tentar outros bases extraídos dos candidatos
    try:
        base_pref = pick_boid_base(candidates)
    except Exception:
        base_pref = ''

    target_suffix = f"-{int(bo_color_id)}"

    if base_pref:
        guess = f"{base_pref}{target_suffix}"
        if _boid_looks_valid(guess):
            bo_api.cache[cache_key] = guess
            issues_add(
                'INFO',
                'BRICKOWL_BOID_GUESS_OK',
                f"{bl_part_id}",
                f"BOID não veio no id_lookup para bo_color_id={bo_color_id}; construído e validado via lookup: {guess}",
            )
            return guess

    # Fallback: tentar outros bases (se o base preferido falhar)
    bases: Set[str] = set()
    for c in candidates:
        cs = str(c).strip()
        if not cs:
            continue
        base = cs.split('-', 1)[0].strip()
        if base:
            bases.add(base)

    for base in sorted(bases):
        if base_pref and base == base_pref:
            continue
        guess = f"{base}{target_suffix}"
        if _boid_looks_valid(guess):
            bo_api.cache[cache_key] = guess
            issues_add(
                'INFO',
                'BRICKOWL_BOID_GUESS_OK',
                f"{bl_part_id}",
                f"BOID não veio no id_lookup para bo_color_id={bo_color_id}; construído e validado via lookup: {guess}",
            )
            return guess

    # bulk_lookup in chunks of 100
    chosen: Optional[str] = None
    for i in range(0, len(candidates), 100):
        chunk = candidates[i : i + 100]
        try:
            details = bo_api.catalog_bulk_lookup(chunk)
        except Exception as e:
            issues_add("WARN", "BRICKOWL_BULK_LOOKUP_FAILED", f"{bl_part_id}", f"bulk_lookup falhou: {e}")
            continue

        for it in details:
            # Heuristics to read color id from item dict
            try:
                c = None
                if isinstance(it, dict):
                    if "color_id" in it:
                        c = it.get("color_id")
                    elif "color" in it and isinstance(it.get("color"), dict):
                        c = it["color"].get("id")
                if c is None:
                    continue
                if int(c) == int(bo_color_id):
                    raw = it.get("boid") or it.get("id")
                    if raw is None:
                        continue
                    s = str(raw).strip()
                    if s and s != "0":
                        chosen = s
                        break
            except Exception:
                continue
        if chosen:
            break

    if not chosen:
        # fallback: pick first candidate (still useful as diagnostic)
        chosen = str(candidates[0]).strip()
        issues_add(
            "WARN",
            "BRICKOWL_COLOR_MATCH_NOT_FOUND",
            f"{bl_part_id}",
            f"não foi possível confirmar bo_color_id={bo_color_id}; usando 1º candidato: {chosen}",
        )

    bo_api.cache[cache_key] = chosen
    return chosen


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    # If table exists with legacy schema (boid INTEGER), drop it so we can recreate with boid TEXT.
    try:
        cols = {row[1]: (row[2] or "").upper() for row in cur.execute("PRAGMA table_info(part_color_map)").fetchall()}
        if cols and (cols.get("boid", "") != "TEXT"):
            cur.execute("DROP TABLE IF EXISTS part_color_map")
            con.commit()
    except Exception:
        pass

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS part_color_map (
          bl_part_id TEXT NOT NULL,
          element_id TEXT NOT NULL,
          rb_part_num TEXT,
          rb_color_id INTEGER,
          bl_color_id INTEGER,
          bo_color_id INTEGER,
          ldraw_color_id INTEGER,
          boid TEXT,
          source TEXT,
          PRIMARY KEY (bl_part_id, element_id, bl_color_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS build_issues (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL,
          severity TEXT NOT NULL,
          issue_type TEXT NOT NULL,
          key TEXT,
          details TEXT
        )
        """
    )

    con.commit()
    con.close()


# -----------------------------
# Selftests
# -----------------------------

def api_selftests(add_issue) -> None:
    # Rebrickable
    try:
        rebrickable_selftest()
        add_issue("INFO", "API_SELFTEST_REBRICKABLE_OK", "", "Rebrickable OK (/lego/colors?page_size=1).")
    except Exception as e:
        add_issue("WARN", "API_SELFTEST_REBRICKABLE_FAILED", "", f"Rebrickable selftest falhou: {e}")

    # BrickLink
    oauth = bricklink_oauth_from_env()
    if oauth is None:
        add_issue("WARN", "API_SELFTEST_BRICKLINK_SKIPPED", "", "BrickLink OAuth não configurado (secrets ausentes).")
    else:
        try:
            _ = bricklink_list_item_colors("3001", oauth, item_type="P", timeout_s=30)
            add_issue("INFO", "API_SELFTEST_BRICKLINK_OK", "", "BrickLink OAuth OK (/items/part/3001/colors).")
        except Exception as e:
            add_issue("WARN", "API_SELFTEST_BRICKLINK_FAILED", "", f"BrickLink selftest falhou: {e}")

    # BrickOwl
    if not BRICKOWL_API_KEY:
        add_issue("WARN", "API_SELFTEST_BRICKOWL_SKIPPED", "", "BRICKOWL_API_KEY não configurado.")
    else:
        try:
            bo = BrickOwlAPI(BRICKOWL_API_KEY)
            bo.user_details()
            bo.catalog_color_list()
            add_issue("INFO", "API_SELFTEST_BRICKOWL_OK", "", "BrickOwl OK (/user/details + /catalog/color_list).")
        except Exception as e:
            add_issue("WARN", "API_SELFTEST_BRICKOWL_FAILED", "", f"BrickOwl selftest falhou: {e}")


# -----------------------------
# Main
# -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser()

    ap.add_argument("--bl-codes-xml", required=True)
    ap.add_argument("--rb-elements", required=True)
    ap.add_argument("--color-map", required=True)

    ap.add_argument("--db", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--issues", required=True)

    ap.add_argument("--strict", action="store_true", help="Falha apenas se existirem ERROR (WARN não falha).")

    ap.add_argument("--debug-apis", action="store_true")

    ap.add_argument("--progress-every", type=int, default=50000)
    ap.add_argument("--commit-every", type=int, default=5000)
    ap.add_argument("--checkpoint", default="data/build_checkpoint.json")
    ap.add_argument("--max-items", type=int, default=0, help="DEBUG: processa no máximo N ITEMS (0 = sem limite).")
    ap.add_argument("--max-runtime-seconds", type=int, default=0, help="Se definido, termina de forma limpa após este tempo (evita timeout).")

    ap.add_argument("--resolve-boid", action="store_true")
    ap.add_argument("--boid-cache-json", default="data/brickowl_api_cache.json")
    ap.add_argument("--boid-min-interval", type=float, default=0.11)
    ap.add_argument("--boid-bulk-min-interval", type=float, default=0.65)
    ap.add_argument("--boid-timeout", type=int, default=30)

    ap.add_argument("--boid-country", default="PT", help="ISO2 do país destino para /catalog/availability (ex: PT).")
    ap.add_argument("--boid-validate-availability", action="store_true", help="Valida BOID também via /catalog/availability (mais lento).")
    ap.add_argument("--boid-max-pairs", type=int, default=0, help="DEBUG: limita nº de pares (part,bo_color) para resolver; 0 = sem limite")

    args = ap.parse_args()

    t0 = now_s()

    codes_xml = Path(args.bl_codes_xml)
    rb_elements_csv = Path(args.rb_elements)
    color_map_csv = Path(args.color_map)

    db_path = Path(args.db)
    out_csv = Path(args.out_csv)
    issues_csv = Path(args.issues)

    checkpoint_path = Path(args.checkpoint)
    error_log_path = out_csv.parent / "brickovery_build_error.log"

    # register globals for signal handler
    global _STOP_CHECKPOINT_PATH, _STOP_ERROR_LOG_PATH
    _STOP_CHECKPOINT_PATH = checkpoint_path
    _STOP_ERROR_LOG_PATH = error_log_path

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    # Ensure output files exist early
    init_db(db_path)
    touch_with_header_csv(
        out_csv,
        [
            "bl_part_id",
            "element_id",
            "rb_part_num",
            "rb_color_id",
            "bl_color_id",
            "bo_color_id",
            "ldraw_color_id",
            "boid",
            "source",
        ],
    )
    touch_with_header_csv(issues_csv, ["severity", "issue_type", "key", "details"])
    if not error_log_path.exists():
        error_log_path.write_text("", encoding="utf-8")

    # Open DB
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    # Clear tables (fresh rebuild)
    cur.execute("DELETE FROM part_color_map")
    cur.execute("DELETE FROM build_issues")
    con.commit()

    def add_issue(sev: str, typ: str, key: str, details: str) -> None:
        cur.execute(
            "INSERT INTO build_issues(ts,severity,issue_type,key,details) VALUES (?,?,?,?,?)",
            (int(time.time()), sev, typ, key, details),
        )

    def checkpoint(phase: str, extra: dict) -> None:
        payload = {"ts": int(time.time()), "phase": phase, **extra}
        save_json(checkpoint_path, payload)

    try:
        if args.debug_apis:
            api_selftests(add_issue)
            con.commit()

        print("[LOAD] inputs...")
        print(f"  codes.xml: {codes_xml} ({codes_xml.stat().st_size/1024/1024:,.1f} MiB)")
        print(f"  elements.csv: {rb_elements_csv} ({rb_elements_csv.stat().st_size/1024/1024:,.1f} MiB)")
        print(f"  color_map.csv: {color_map_csv} ({color_map_csv.stat().st_size/1024/1024:,.1f} MiB)")

        rb_elements = load_rb_elements(rb_elements_csv)
        color_map = load_color_map(color_map_csv)
        bl_to_bo, bl_to_ldraw, rev_issues = build_bl_reverse_maps(color_map)
        for sev, typ, key, details in rev_issues:
            add_issue(sev, typ, key, details)
        con.commit()

        oauth = bricklink_oauth_from_env()
        bl_colors_cache: Dict[str, List[int]] = {}
        fallback_done_parts: Set[str] = set()

        inserted = 0
        processed = 0
        missing_elements = 0
        missing_color_map = 0
        fallback_parts = 0

        batch_rows: List[Tuple] = []

        checkpoint("start", {"processed": 0, "inserted": 0, "stop": False})

        for itemtype, bl_part_id, element_id in iter_codes_xml(codes_xml):
            if _STOP:
                add_issue("WARN", "STOP_SIGNAL", "", f"Stop requested ({_STOP_REASON}).")
                break
            processed += 1

            if args.max_items and processed > args.max_items:
                add_issue("WARN", "DEBUG_MAX_ITEMS", "", f"Paragem por --max-items={args.max_items}.")
                break

            if args.max_runtime_seconds and (now_s() - t0) > float(args.max_runtime_seconds):
                add_issue("WARN", "EARLY_EXIT_MAX_RUNTIME", "", f"Paragem limpa por --max-runtime-seconds={args.max_runtime_seconds}.")
                break

            # We only care about parts for this DB
            if (itemtype or "P").strip().upper() not in ("P", "PART"):
                continue

            if element_id in rb_elements:
                rb_part_num, rb_color_id = rb_elements[element_id]
                cm = color_map.get(rb_color_id)
                if not cm:
                    missing_color_map += 1
                    add_issue(
                        "ERROR",
                        "RB_COLOR_ID_NOT_IN_COLOR_MAP",
                        f"rb_color_id={rb_color_id}",
                        f"rb_color_id={rb_color_id} não existe em color_map.csv (element_id={element_id}, bl_part_id={bl_part_id}).",
                    )
                    # Still insert row with NULL mapping to keep traceability
                    bl_color_id = None
                    bo_color_id = None
                    ld = None
                else:
                    bl_color_id = cm.get("bl_color_id")
                    bo_color_id = cm.get("bo_color_id")
                    ld = cm.get("ldraw_color_id")

                batch_rows.append(
                    (
                        bl_part_id,
                        element_id,
                        rb_part_num,
                        rb_color_id,
                        bl_color_id,
                        bo_color_id,
                        ld,
                        None,
                        "codes.xml+elements.csv",
                    )
                )
                inserted += 1

            else:
                # element missing in RB -> mandatory BrickLink fallback by part_id
                missing_elements += 1
                add_issue(
                    "WARN",
                    "ELEMENT_NOT_IN_REBRICKABLE_ELEMENTS",
                    f"{bl_part_id}|{element_id}",
                    "Element ID do BrickLink não encontrado em Rebrickable elements.csv (divergência aceitável). Fallback BrickLink API por bl_part_id.",
                )

                if bl_part_id in fallback_done_parts:
                    continue
                fallback_done_parts.add(bl_part_id)
                fallback_parts += 1

                if oauth is None:
                    add_issue(
                        "WARN",
                        "BRICKLINK_API_UNAVAILABLE",
                        bl_part_id,
                        "BrickLink OAuth não configurado; incluído sem cores (BL-only).",
                    )
                    batch_rows.append((bl_part_id, "", None, None, None, None, None, None, "bricklink_fallback_no_oauth"))
                    inserted += 1
                else:
                    try:
                        if bl_part_id not in bl_colors_cache:
                            bl_colors_cache[bl_part_id] = bricklink_list_item_colors(bl_part_id, oauth, item_type="P", timeout_s=30)
                        colors = bl_colors_cache[bl_part_id]
                        if not colors:
                            add_issue(
                                "WARN",
                                "BRICKLINK_PART_COLORS_EMPTY",
                                bl_part_id,
                                "BrickLink devolveu 0 cores; incluído sem cores (BL-only).",
                            )
                            batch_rows.append((bl_part_id, "", None, None, None, None, None, None, "bricklink_fallback_empty"))
                            inserted += 1
                        else:
                            for bl_color_id in colors:
                                bo_color_id = bl_to_bo.get(bl_color_id)
                                ld = bl_to_ldraw.get(bl_color_id)
                                batch_rows.append(
                                    (
                                        bl_part_id,
                                        "",
                                        None,
                                        None,
                                        int(bl_color_id),
                                        int(bo_color_id) if bo_color_id is not None else None,
                                        int(ld) if ld is not None else None,
                                        None,
                                        "bricklink_fallback_colors",
                                    )
                                )
                                inserted += 1
                            add_issue(
                                "INFO",
                                "ELEMENT_MISSING_RB_INCLUDED_USING_BRICKLINK_COLORS",
                                bl_part_id,
                                f"Incluído via BrickLink colors: {len(colors)} cores.",
                            )
                    except requests.HTTPError as e:
                        add_issue(
                            "WARN",
                            "BRICKLINK_PART_COLORS_LOOKUP_FAILED",
                            bl_part_id,
                            f"BrickLink known colors falhou (HTTP {e.response.status_code if e.response else '??'}).",
                        )
                        batch_rows.append((bl_part_id, "", None, None, None, None, None, None, "bricklink_fallback_failed"))
                        inserted += 1
                    except Exception as e:
                        add_issue(
                            "WARN",
                            "BRICKLINK_PART_COLORS_LOOKUP_FAILED",
                            bl_part_id,
                            f"BrickLink known colors falhou: {e}",
                        )
                        batch_rows.append((bl_part_id, "", None, None, None, None, None, None, "bricklink_fallback_failed"))
                        inserted += 1

            # flush batches
            if len(batch_rows) >= int(args.commit_every):
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO part_color_map(
                      bl_part_id, element_id, rb_part_num, rb_color_id,
                      bl_color_id, bo_color_id, ldraw_color_id, boid, source
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    batch_rows,
                )
                con.commit()
                batch_rows.clear()

            if args.progress_every and (processed % int(args.progress_every) == 0):
                elapsed = now_s() - t0
                rate = processed / elapsed if elapsed > 0 else 0.0
                print(
                    f"[PROGRESS] processed={processed:,} inserted={inserted:,} missing_elements={missing_elements:,} fallback_parts={fallback_parts:,} missing_color_map={missing_color_map:,} rate={rate:,.1f}/s elapsed={elapsed:,.0f}s"
                )
                checkpoint(
                    "build",
                    {
                        "processed": processed,
                        "inserted": inserted,
                        "missing_elements": missing_elements,
                        "fallback_parts": fallback_parts,
                        "missing_color_map": missing_color_map,
                        "elapsed_sec": int(elapsed),
                    },
                )

        # flush remaining
        if batch_rows:
            cur.executemany(
                """
                INSERT OR REPLACE INTO part_color_map(
                  bl_part_id, element_id, rb_part_num, rb_color_id,
                  bl_color_id, bo_color_id, ldraw_color_id, boid, source
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                batch_rows,
            )
            con.commit()
            batch_rows.clear()

        checkpoint(
            "built",
            {
                "processed": processed,
                "inserted": inserted,
                "missing_elements": missing_elements,
                "fallback_parts": fallback_parts,
                "missing_color_map": missing_color_map,
                "elapsed_sec": int(now_s() - t0),
                "stop": bool(_STOP),
                "stop_reason": _STOP_REASON,
            },
        )

        # -----------------
        # BOID resolution
        # -----------------
        if args.resolve_boid:
            if not BRICKOWL_API_KEY:
                add_issue("WARN", "BRICKOWL_API_UNAVAILABLE", "", "BRICKOWL_API_KEY não definido; a coluna boid ficará vazia.")
                con.commit()
            else:
                cache_path = Path(args.boid_cache_json)
                cache: dict = {}
                if cache_path.exists():
                    try:
                        cache = json.loads(cache_path.read_text(encoding="utf-8"))
                    except Exception:
                        cache = {}

                bo_api = BrickOwlAPI(
                    BRICKOWL_API_KEY,
                    min_interval_s=float(args.boid_min_interval),
                    bulk_min_interval_s=float(args.boid_bulk_min_interval),
                    timeout_s=int(args.boid_timeout),
                    cache=cache,
                )

                # pairs where bo_color_id is known and boid missing
                rows_pairs = cur.execute(
                    """
                    SELECT DISTINCT bl_part_id, bo_color_id
                    FROM part_color_map
                    WHERE bo_color_id IS NOT NULL AND (boid IS NULL OR boid = '')
                    """
                ).fetchall()

                if args.boid_max_pairs and int(args.boid_max_pairs) > 0:
                    rows_pairs = rows_pairs[: int(args.boid_max_pairs)]

                total_pairs = len(rows_pairs)
                add_issue("INFO", "BRICKOWL_BOID_RESOLVE_START", "", f"A resolver BOID para {total_pairs} pares (part,bo_color).")
                con.commit()

                updated = 0
                for idx, (bl_part_id, bo_color_id) in enumerate(rows_pairs, start=1):
                    if _STOP:
                        add_issue("WARN", "STOP_SIGNAL", "", f"Stop requested ({_STOP_REASON}) durante boid resolve.")
                        break
                    try:
                        boid = resolve_boid_for_pair(
                            bo_api,
                            str(bl_part_id),
                            int(bo_color_id),
                            add_issue,
                            country=str(args.boid_country),
                            validate_availability=bool(args.boid_validate_availability),
                        )
                        if boid:
                            cur.execute(
                                "UPDATE part_color_map SET boid=? WHERE bl_part_id=? AND bo_color_id=?",
                                (str(boid), str(bl_part_id), int(bo_color_id)),
                            )
                            updated += 1
                    except Exception as e:
                        add_issue("WARN", "BRICKOWL_BOID_RESOLVE_FAILED", f"{bl_part_id}|{bo_color_id}", f"Falha boid resolve: {e}")

                    if idx % 200 == 0:
                        con.commit()
                        # persist cache
                        try:
                            cache_path.parent.mkdir(parents=True, exist_ok=True)
                            cache_path.write_text(json.dumps(bo_api.cache, ensure_ascii=False), encoding="utf-8")
                        except Exception:
                            pass
                        elapsed = now_s() - t0
                        print(f"[BOID] {idx:,}/{total_pairs:,} updated={updated:,} elapsed={elapsed:,.0f}s")
                        checkpoint(
                            "boid",
                            {
                                "processed": processed,
                                "inserted": inserted,
                                "boid_pairs_total": total_pairs,
                                "boid_pairs_done": idx,
                                "boid_pairs_updated": updated,
                                "elapsed_sec": int(elapsed),
                            },
                        )

                con.commit()
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(bo_api.cache, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
                add_issue("INFO", "BRICKOWL_BOID_RESOLVE_DONE", "", f"BOID resolve terminado. Updated_pairs={updated}/{total_pairs}.")
                con.commit()

        # -----------------
        # Export CSVs
        # -----------------
        print("[EXPORT] part_color_map.csv...")
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["bl_part_id", "element_id", "rb_part_num", "rb_color_id", "bl_color_id", "bo_color_id", "ldraw_color_id", "boid", "source"])
            for row in cur.execute(
                """
                SELECT bl_part_id, element_id, rb_part_num, rb_color_id, bl_color_id, bo_color_id, ldraw_color_id, boid, source
                FROM part_color_map
                ORDER BY bl_part_id, element_id, bl_color_id
                """
            ):
                w.writerow(row)

        print("[EXPORT] part_color_issues.csv...")
        with issues_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["severity", "issue_type", "key", "details"])
            for row in cur.execute(
                "SELECT severity, issue_type, key, details FROM build_issues ORDER BY id"
            ):
                w.writerow(row)

        con.commit()

        # Summary
        n_err = cur.execute("SELECT COUNT(1) FROM build_issues WHERE severity='ERROR'").fetchone()[0]
        n_warn = cur.execute("SELECT COUNT(1) FROM build_issues WHERE severity='WARN'").fetchone()[0]
        n_rows = cur.execute("SELECT COUNT(1) FROM part_color_map").fetchone()[0]
        elapsed = now_s() - t0
        print(f"✅ DB rows={n_rows:,} | issues ERR={n_err} WARN={n_warn} | elapsed={elapsed:,.1f}s")

        checkpoint(
            "done",
            {
                "processed": processed,
                "inserted": inserted,
                "rows_db": n_rows,
                "errors": n_err,
                "warnings": n_warn,
                "elapsed_sec": int(elapsed),
            },
        )

        if args.strict and n_err:
            return 2
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        append_error_log(error_log_path, tb)
        try:
            add_issue("ERROR", "UNHANDLED_EXCEPTION", "", f"{e}")
            con.commit()
        except Exception:
            pass
        checkpoint("crash", {"error": str(e)})
        return 1

    finally:
        try:
            con.commit()
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
