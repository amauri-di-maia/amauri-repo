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
import gzip
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



def _open_maybe_gzip(path: Path):
    return gzip.open if path.suffix.lower() == '.gz' else open


def apply_weights_from_csv(con, cur, weights_csv: Path, *, overwrite: bool, add_issue) -> int:
    """Apply part weights to part_color_map.weight.

    Expected: a CSV (optionally .gz) with at least:
      - bl_part_id (or compatible alias)
      - weight (grams) (or compatible alias)

    Default: only fills rows where weight IS NULL, unless overwrite=True.
    Leaves NULL when no match exists in the CSV (as requested).

    Returns number of updated rows (SQLite rowcount best-effort).
    """
    wp = Path(weights_csv)
    if not wp.exists():
        add_issue('WARN', 'WEIGHTS_FILE_MISSING', str(wp), f'weights file not found: {wp}')
        return 0

    opener = _open_maybe_gzip(wp)
    updated = 0
    batch = []
    batch_size = 5000

    # Header aliases (tolerant)
    part_keys = {'bl_part_id','part_id','item_no','itemid','part'}
    weight_keys = {'weight','weight_g','grams','g'}

    try:
        with opener(wp, 'rt', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                add_issue('WARN', 'WEIGHTS_EMPTY', str(wp), 'weights file has no header/rows')
                return 0

            fn = {h.strip().lower(): h for h in reader.fieldnames if h}
            part_col = next((fn[k] for k in fn.keys() if k in part_keys), None)
            weight_col = next((fn[k] for k in fn.keys() if k in weight_keys), None)

            if not part_col or not weight_col:
                add_issue('WARN', 'WEIGHTS_BAD_HEADER', str(wp), f'weights header missing part/weight: {reader.fieldnames}')
                return 0

            for row in reader:
                bl = (row.get(part_col) or '').strip()
                ws = (row.get(weight_col) or '').strip()
                if not bl or not ws:
                    continue
                # allow comma decimal
                ws = ws.replace(',', '.')
                try:
                    wv = float(ws)
                except Exception:
                    continue
                batch.append((wv, bl))
                if len(batch) >= batch_size:
                    if overwrite:
                        cur.executemany('UPDATE part_color_map SET weight=? WHERE bl_part_id=?', batch)
                    else:
                        cur.executemany('UPDATE part_color_map SET weight=? WHERE bl_part_id=? AND weight IS NULL', batch)
                    updated += cur.rowcount if cur.rowcount is not None else 0
                    con.commit()
                    batch.clear()

        if batch:
            if overwrite:
                cur.executemany('UPDATE part_color_map SET weight=? WHERE bl_part_id=?', batch)
            else:
                cur.executemany('UPDATE part_color_map SET weight=? WHERE bl_part_id=? AND weight IS NULL', batch)
            updated += cur.rowcount if cur.rowcount is not None else 0
            con.commit()
            batch.clear()

        add_issue('INFO', 'WEIGHTS_APPLIED', str(wp), f'weights applied (updated_rows={updated})')
        return updated

    except Exception as e:
        add_issue('WARN', 'WEIGHTS_APPLY_FAILED', str(wp), f'Falha a aplicar weights: {e}')
        return updated



def fill_missing_weights_from_bricklink(
    con,
    cur,
    oauth: OAuth1,
    *,
    add_issue: callable,
    min_interval_s: float = 0.25,
    commit_every: int = 200,
    max_runtime_seconds: float = 0.0,
    t0: float = 0.0,
) -> int:
    """Preenche weights em falta consultando BrickLink (GET /items/{type}/{no}).

    Estratégia:
      - Faz lookup por bl_part_id (sem cor) e tenta extrair 'weight' (gramas)
      - Atualiza todas as linhas desse bl_part_id onde weight IS NULL

    Nota:
      - Só corre para parts com weight IS NULL.
      - Respeita min_interval_s para evitar rate-limit.
    """
    try:
        rows = cur.execute(
            "SELECT DISTINCT bl_part_id FROM part_color_map WHERE weight IS NULL AND bl_part_id IS NOT NULL"
        ).fetchall()
    except Exception as e:
        add_issue('WARN', 'WEIGHTS_BRICKLINK_QUERY_FAILED', '', f'Falha ao listar parts sem weight: {e}')
        return 0

    parts = [r[0] for r in rows if r and r[0]]
    if not parts:
        return 0

    updated_rows = 0
    filled_parts = 0
    missing_parts = 0

    last_call = 0.0

    for i, part in enumerate(parts, 1):
        if _STOP:
            break
        if max_runtime_seconds and t0 and (now_s() - float(t0)) > float(max_runtime_seconds):
            add_issue('WARN', 'WEIGHTS_BRICKLINK_STOP_MAX_RUNTIME', '', f'Parado por max-runtime-seconds após {i-1} partes.')
            break

        # throttle
        dt = time.time() - last_call
        if dt < float(min_interval_s):
            time.sleep(float(min_interval_s) - dt)
        last_call = time.time()

        w = None
        try:
            w = bricklink_get_item_weight(str(part), oauth, item_type='P', timeout_s=30)
        except Exception:
            w = None

        if w is None:
            missing_parts += 1
        else:
            try:
                cur.execute(
                    "UPDATE part_color_map SET weight=? WHERE bl_part_id=? AND weight IS NULL",
                    (float(w), str(part)),
                )
                if cur.rowcount:
                    updated_rows += int(cur.rowcount)
                    filled_parts += 1
            except Exception:
                pass

        if commit_every and (i % int(commit_every) == 0):
            con.commit()

    con.commit()
    add_issue(
        'INFO',
        'WEIGHTS_BRICKLINK_DONE',
        '',
        f'BrickLink weight fill: parts_missing_before={len(parts)}, parts_filled={filled_parts}, parts_still_missing={missing_parts}, rows_updated={updated_rows}.',
    )
    return updated_rows



def persist_brickowl_cache(cache_path: Path, cache: dict) -> None:
    """Persist BrickOwl cache to disk, filtering negative/transient entries.

    We avoid persisting:
      - id_lookup:* entries that are an empty list (can be transient / parsing-related)
      - boid_resolve:* entries that are falsy (None/empty)
      - lookup:* and availability:* responses that contain {error: ...} (e.g. rate-limit / temporary failures)

    This prevents 'sticky' failures across workflow runs.
    """
    filtered: dict = {}
    for k, v in (cache or {}).items():
        if isinstance(k, str):
            if k.startswith('id_lookup:') and isinstance(v, list) and len(v) == 0:
                continue
            if k.startswith('boid_resolve:') and not v:
                continue
            if k.startswith('lookup:') and isinstance(v, dict) and v.get('error'):
                continue
            if k.startswith('availability:') and isinstance(v, dict) and v.get('error'):
                continue
        filtered[k] = v
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(filtered, ensure_ascii=False), encoding='utf-8')


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



def bricklink_get_item_weight(bl_part_id: str, oauth: OAuth1, item_type: str = 'P', timeout_s: int = 30) -> Optional[float]:
    """GET /items/{type}/{no} and extract catalog weight (grams) when available."""
    t = ITEMTYPE_TO_PATH.get((item_type or 'P').strip().upper(), 'part')
    no = quote((bl_part_id or '').strip(), safe='')
    url = f"https://api.bricklink.com/api/store/v1/items/{t}/{no}"
    r = requests.get(url, auth=oauth, timeout=timeout_s)
    r.raise_for_status()
    data = r.json() or {}
    item = data.get('data') or {}
    w = item.get('weight')
    if w is None or str(w).strip() == '':
        return None
    try:
        return float(w)
    except Exception:
        return None


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
        """HTTP GET with basic throttling + retries.

        Retries are applied for:
          - 429 (rate limiting)
          - 5xx (temporary server errors)

        Backoff uses a small exponential wait with a cap, to avoid turning transient throttling
        into hard failures.
        """
        max_attempts = 5
        base_sleep = 0.6
        max_sleep = 8.0

        last_exc = None
        for attempt in range(1, max_attempts + 1):
            self._sleep(min_interval)
            self._last_call = time.time()
            try:
                r = requests.get(url, params=params, timeout=self.timeout_s)

                # Retryable conditions
                if r.status_code == 429 or (500 <= r.status_code <= 599):
                    # Try to respect Retry-After when present
                    ra = r.headers.get('Retry-After')
                    sleep_s = None
                    if ra:
                        try:
                            sleep_s = float(ra)
                        except Exception:
                            sleep_s = None
                    if sleep_s is None:
                        sleep_s = min(max_sleep, base_sleep * (2 ** (attempt - 1)))
                    time.sleep(sleep_s)
                    continue

                r.raise_for_status()

                ct = (r.headers.get('content-type') or '').lower()
                if ct.startswith('application/json'):
                    return r.json()

                # Some endpoints reply with JSON but without proper content-type.
                return json.loads(r.text)

            except Exception as e:
                last_exc = e
                # Final attempt: raise
                if attempt >= max_attempts:
                    raise
                time.sleep(min(max_sleep, base_sleep * (2 ** (attempt - 1))))

        # Defensive (should not reach)
        if last_exc:
            raise last_exc
        raise RuntimeError('BrickOwl request failed')

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

        # NOTE: do NOT treat cached empty lists as authoritative. They can be caused by
        # transient errors or older parsing bugs, and would otherwise become "sticky".
        if cache_key in self.cache:
            cached = self.cache.get(cache_key)
            if isinstance(cached, list) and len(cached) > 0:
                return [str(x) for x in cached]

        url = f"{BRICKOWL_CATALOG_BASE_URL}/id_lookup"
        data = self._get(
            url,
            {"key": self.api_key, "id": id_value, "type": item_type, "id_type": id_type},
            self.min_interval_s,
        )

        def _extract_list(obj):
            # BrickOwl responses can vary:
            #  - ["123-1", "123-2"]
            #  - {"data": [ ... ]}
            #  - {"data": {"boids": [ ... ]}}
            #  - {"boids": [ ... ]}
            if isinstance(obj, list):
                return obj
            if isinstance(obj, dict):
                # direct list fields
                for k in ("items", "boids", "result", "results", "data"):
                    v = obj.get(k)
                    if isinstance(v, list):
                        return v
                # nested dict fields (common: data={...})
                for k in ("data", "result", "results"):
                    v = obj.get(k)
                    if isinstance(v, dict):
                        for kk in ("items", "boids", "result", "results", "data"):
                            vv = v.get(kk)
                            if isinstance(vv, list):
                                return vv
                # as a last resort, first list-valued entry
                for v in obj.values():
                    if isinstance(v, list):
                        return v
            return []

        items = _extract_list(data)

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

        boids = sorted(set(boids))

        # Cache positive results only; keep empty in-memory during this run but avoid sticky persistence.
        if boids:
            self.cache[cache_key] = boids
        else:
            self.cache[cache_key] = []

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


    def catalog_search(self, query: str, page: int = 1) -> List[dict]:
        """GET /catalog/search?query=...&page=...

        Usado apenas como caminho de recuperação quando id_lookup + lookup não convergem.
        Devolve uma lista de dicts (pode ser vazia).
        """
        q = str(query).strip()
        if not q:
            return []
        page_i = int(page) if int(page) > 0 else 1
        key = f"search:{q}:{page_i}"
        if key in self.cache:
            v = self.cache[key]
            return list(v) if isinstance(v, list) else []

        url = f"{BRICKOWL_CATALOG_BASE_URL}/search"
        data = self._get(url, {"key": self.api_key, "query": q, "page": page_i}, self.min_interval_s)
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
    """Escolhe um BOID base de forma não destrutiva.

    - Se existir um candidato sem '-' (BOID aparentemente 'base'), usa-o.
    - Caso contrário, usa o 1º candidato tal como está (NÃO faz split pelo '-').

    Isto evita construir BOIDs inválidos quando o /catalog/id_lookup devolve apenas BOIDs já coloridos.
    """
    for b in boids:
        bs = str(b).strip()
        if bs and '-' not in bs:
            return bs
    if boids:
        return str(boids[0]).strip()
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
    """Resolve BOID for (bl_part_id, bo_color_id) sem "forçar" cor.

    Problema observado (real): o BrickOwl tem casos em que o BOID válido é um número "base" (sem "-<cor>")
    e/ou o lookup devolve color_id=0 (não-colorido). Nesses casos, construir "<base>-<cor>" cria falsos
    negativos e leva a BRICKOWL_BOID_LOOKUP_INVALID.

    Estratégia (orientação do utilizador):
      1) Usar /catalog/id_lookup (id_type=bl_item_no) e tentar validar os BOIDs *tal como vieram*.
      2) Se falhar, tentar a variante "base" (removendo o sufixo "-<cor>") quando aplicável.
      3) Só como recuperação: usar /catalog/lookup (em seed válido) para extrair design_id/item_no e voltar a
         /catalog/id_lookup para obter BOIDs adicionais e listar possibilidades.
      4) Aceitação:
         - Aceita se boid termina com "-<bo_color_id>"; OU
         - color_id do lookup == bo_color_id; OU
         - lookup expõe lista de cores que inclui bo_color_id; OU
         - (caso especial) BOID base único (id_lookup devolveu 1 candidato) e lookup valida mas color_id é 0/None.

    Retorna BOID validado (string) ou None.
    """

    bl_part_id = str(bl_part_id).strip()
    if not bl_part_id:
        return None

    try:
        bo_color_id_i = int(bo_color_id)
    except Exception:
        return None

    cache_key = f"boid_resolve:{bl_part_id}-{bo_color_id_i}"
    cached = bo_api.cache.get(cache_key)
    if cached:
        return str(cached)

    target_suffix = f"-{bo_color_id_i}"

    def _unwrap(obj):
        """Normalize BrickOwl payloads that sometimes wrap results under 'data'/'result' keys."""
        if isinstance(obj, dict):
            for k in ("data", "result", "results"):
                v = obj.get(k)
                if isinstance(v, dict):
                    return v
        return obj

    def _extract_color_id(info: Optional[dict]) -> Optional[int]:
        if not isinstance(info, dict):
            return None
        info2 = _unwrap(info)
        if isinstance(info2, dict):
            for k in ("color_id", "colour_id", "colorId", "colourId"):
                if k in info2 and info2.get(k) is not None:
                    try:
                        return int(info2.get(k))
                    except Exception:
                        pass
            c = info2.get("color")
            if isinstance(c, dict):
                for k in ("id", "color_id", "colour_id"):
                    if k in c and c.get(k) is not None:
                        try:
                            return int(c.get(k))
                        except Exception:
                            pass
        return None

    def _extract_color_hints(info: Optional[dict]) -> set:
        """Best-effort: extract available color ids list if present."""
        out = set()
        if not isinstance(info, dict):
            return out
        info2 = _unwrap(info)
        if not isinstance(info2, dict):
            return out

        # Common patterns: colors=[{id:..}, ...] OR color_ids=[..]
        for k in ("color_ids", "colour_ids", "colors", "colours", "available_colors", "available_colours"):
            v = info2.get(k)
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict):
                        for kk in ("id", "color_id", "colour_id"):
                            if kk in it and it.get(kk) is not None:
                                try:
                                    out.add(int(it.get(kk)))
                                except Exception:
                                    pass
                    else:
                        try:
                            out.add(int(it))
                        except Exception:
                            pass
        return out

    def _extract_lookup_ids(info: Optional[dict]) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        if not isinstance(info, dict):
            return out
        info2 = _unwrap(info)
        if not isinstance(info2, dict):
            return out

        for k, id_type in (("design_id", "design_id"), ("item_no", "item_no"), ("set_number", "set_number")):
            v = info2.get(k)
            if v is not None and str(v).strip() != "":
                out.append((id_type, str(v).strip()))

        ids = info2.get("ids")
        if isinstance(ids, dict):
            for k, id_type in (("design_id", "design_id"), ("item_no", "item_no"), ("bl_item_no", "bl_item_no"), ("set_number", "set_number")):
                v = ids.get(k)
                if v is not None and str(v).strip() != "":
                    out.append((id_type, str(v).strip()))

        seen = set()
        uniq: List[Tuple[str, str]] = []
        for t, v in out:
            key = (t, v)
            if key in seen:
                continue
            seen.add(key)
            uniq.append((t, v))
        return uniq

    def _lookup_ok(boid: str) -> Tuple[bool, Optional[dict]]:
        b = str(boid).strip()
        if not b:
            return False, None
        try:
            raw = bo_api.catalog_lookup(b)
        except Exception:
            return False, None
        if isinstance(raw, dict) and raw.get("error"):
            return False, None

        if validate_availability:
            try:
                a = bo_api.catalog_availability(b, country=country)
                if isinstance(a, dict) and a.get("error"):
                    return False, None
            except Exception:
                return False, None

        return True, raw if isinstance(raw, dict) else None

    def _accept(boid: str, info: Optional[dict], origin: str, *, id_lookup_count: int) -> Optional[str]:
        b = str(boid).strip()
        if not b:
            return None

        # Fast accept by suffix (quando existe)
        if b.endswith(target_suffix):
            bo_api.cache[cache_key] = b
            issues_add("INFO", "BRICKOWL_BOID_OK", f"{bl_part_id}|{bo_color_id_i}", f"BOID validado ({origin}): {b}")
            return b

        cid = _extract_color_id(info)
        if cid is not None and int(cid) == bo_color_id_i:
            bo_api.cache[cache_key] = b
            issues_add("INFO", "BRICKOWL_BOID_OK", f"{bl_part_id}|{bo_color_id_i}", f"BOID validado ({origin}): {b}")
            return b

        # If lookup exposes available colors, use as a hint.
        hints = _extract_color_hints(info)
        if hints:
            if bo_color_id_i in hints:
                bo_api.cache[cache_key] = b
                issues_add(
                    "INFO",
                    "BRICKOWL_BOID_OK_BY_COLORS_HINT",
                    f"{bl_part_id}|{bo_color_id_i}",
                    f"BOID validado por hint de cores ({origin}): {b} (hints={len(hints)})",
                )
                return b
            else:
                # record mismatch but keep searching
                issues_add(
                    "INFO",
                    "BRICKOWL_LOOKUP_AVAILABLE_COLORS",
                    f"{bl_part_id}|{bo_color_id_i}",
                    f"Lookup expôs cores mas não inclui target={bo_color_id_i} ({origin}) boid={b}",
                )

        # Special case: uncolored/base BOID.
        # Observed: lookup color_id=0 (ou ausente) mas BOID é utilizável e existe no BrickOwl.
        if ("-" not in b) and (cid is None or int(cid) == 0) and int(id_lookup_count) == 1:
            bo_api.cache[cache_key] = b
            issues_add(
                "INFO",
                "BRICKOWL_BOID_OK_UNCOLORED_UNIQUE",
                f"{bl_part_id}|{bo_color_id_i}",
                f"Aceite BOID base único sem cor explícita (color_id={cid}) ({origin}): {b}",
            )
            return b

        if cid is not None and int(cid) != bo_color_id_i:
            issues_add(
                "INFO",
                "BRICKOWL_BOID_COLOR_MISMATCH",
                f"{bl_part_id}|{bo_color_id_i}",
                f"BOID validou mas cor diferente (lookup color_id={cid}, target={bo_color_id_i}) boid={b} origin={origin}",
            )
        return None

    # --------------------
    # Step 1: /catalog/id_lookup by BL item id
    # --------------------
    try:
        boids1 = bo_api.catalog_id_lookup(id_value=bl_part_id, item_type="Part", id_type="bl_item_no")
    except Exception as e:
        issues_add("WARN", "BRICKOWL_ID_LOOKUP_FAILED", f"{bl_part_id}", f"id_lookup falhou: {e}")
        return None

    if not boids1:
        issues_add("WARN", "BRICKOWL_ID_LOOKUP_EMPTY", f"{bl_part_id}", f"id_lookup devolveu 0 BOIDs para bl_item_no={bl_part_id}")
        return None

    boids1 = [str(b).strip() for b in boids1 if str(b).strip()]
    boids1 = list(dict.fromkeys(boids1))  # keep order, dedup
    id_lookup_count = len(boids1)

    # Candidate order:
    #  (a) candidates that already match target suffix
    #  (b) other candidates "as returned"
    #  (c) base (strip suffix) for any candidate with '-'
    cands: List[str] = []
    for b in boids1:
        if b.endswith(target_suffix):
            cands.append(b)
    for b in boids1:
        if b not in cands:
            cands.append(b)
    for b in boids1:
        if "-" in b:
            base = b.split("-", 1)[0].strip()
            if base and base not in cands:
                cands.append(base)

    validated_infos: List[Tuple[str, dict]] = []  # (boid, info)

    for b in cands[:30]:
        ok, info = _lookup_ok(b)
        if not ok:
            continue
        if isinstance(info, dict):
            validated_infos.append((b, info))
        acc = _accept(b, info, "id_lookup:bl_item_no", id_lookup_count=id_lookup_count)
        if acc:
            return acc

    # --------------------
    # Step 2: recovery via lookup->(design_id/item_no)->id_lookup
    # --------------------
    # Use any validated lookup payload (even mismatch) to discover alternative BOIDs.
    extra_boids: List[str] = []
    for b, info in validated_infos[:5]:
        for id_type, id_val in _extract_lookup_ids(info):
            if id_type == "bl_item_no":
                continue
            try:
                more = bo_api.catalog_id_lookup(id_value=id_val, item_type="Part", id_type=id_type)
            except Exception:
                continue
            for x in more or []:
                xs = str(x).strip()
                if xs and xs not in extra_boids:
                    extra_boids.append(xs)

    if extra_boids:
        # Re-apply the same candidate logic without fabricating.
        extra = list(dict.fromkeys(extra_boids))
        cands2: List[str] = []
        for b in extra:
            if b.endswith(target_suffix):
                cands2.append(b)
        for b in extra:
            if b not in cands2:
                cands2.append(b)
        for b in extra:
            if "-" in b:
                base = b.split("-", 1)[0].strip()
                if base and base not in cands2:
                    cands2.append(base)

        for b in cands2[:40]:
            ok, info = _lookup_ok(b)
            if not ok:
                continue
            acc = _accept(b, info, "recovery:id_lookup", id_lookup_count=id_lookup_count)
            if acc:
                issues_add("INFO", "BRICKOWL_BOID_RECOVERED", f"{bl_part_id}|{bo_color_id_i}", f"Recuperado via lookup->id_lookup (extra_boids={len(extra_boids)})")
                return acc

    # --------------------
    # Step 3: last resort search
    # --------------------
    try:
        hits = bo_api.catalog_search(bl_part_id, page=1)
    except Exception:
        hits = []

    boids3: List[str] = []
    for h in hits or []:
        if not isinstance(h, dict):
            continue
        b = h.get("boid") or h.get("id") or h.get("bo_id")
        if b is None:
            continue
        bs = str(b).strip()
        if bs and bs not in boids3:
            boids3.append(bs)

    if boids3:
        cands3: List[str] = []
        for b in boids3:
            if b.endswith(target_suffix):
                cands3.append(b)
        for b in boids3:
            if b not in cands3:
                cands3.append(b)
        for b in boids3:
            if "-" in b:
                base = b.split("-", 1)[0].strip()
                if base and base not in cands3:
                    cands3.append(base)

        for b in cands3[:40]:
            ok, info = _lookup_ok(b)
            if not ok:
                continue
            acc = _accept(b, info, "recovery:search", id_lookup_count=id_lookup_count)
            if acc:
                issues_add("INFO", "BRICKOWL_BOID_RECOVERED_BY_SEARCH", f"{bl_part_id}|{bo_color_id_i}", f"Recuperado via catalog/search (cands={len(boids3)})")
                return acc

    issues_add(
        "WARN",
        "BRICKOWL_BOID_LOOKUP_INVALID",
        f"{bl_part_id}|{bo_color_id_i}",
        f"Nenhum BOID validado. candidatos id_lookup={id_lookup_count} (ex: {boids1[0] if boids1 else ''})",
    )
    return None

def init_db(db_path: Path) -> None:

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    def _cols(table: str) -> dict:
        try:
            return {row[1]: (row[2] or '').upper() for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
        except Exception:
            return {}

    cols = _cols('part_color_map')

    # Desired schema (no weight_g; boid TEXT)
    def _create_schema() -> None:
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
              weight REAL,
              boid TEXT,
              source TEXT,
              PRIMARY KEY (bl_part_id, element_id, bl_color_id)
            )
            """
        )

    need_rebuild = False
    if cols:
        if cols.get('boid', 'TEXT') != 'TEXT':
            need_rebuild = True
        if 'weight_g' in cols:
            # Must be removed (user request). Prefer DROP COLUMN; if unavailable, rebuild.
            try:
                cur.execute('ALTER TABLE part_color_map DROP COLUMN weight_g')
                con.commit()
                cols = _cols('part_color_map')
            except Exception:
                need_rebuild = True
        if 'weight' not in cols and 'weight_g' in cols:
            need_rebuild = True

    if cols and need_rebuild:
        # Non-destructive migration: rebuild table preserving data.
        cur.execute('ALTER TABLE part_color_map RENAME TO part_color_map_old')
        _create_schema()

        old_cols = _cols('part_color_map_old')
        w_expr = 'NULL'
        if 'weight' in old_cols:
            w_expr = 'weight'
        elif 'weight_g' in old_cols:
            w_expr = 'weight_g'

        boid_expr = 'NULL'
        if 'boid' in old_cols:
            boid_expr = 'CAST(boid AS TEXT)'

        source_expr = 'NULL'
        if 'source' in old_cols:
            source_expr = 'source'

        cur.execute(
            f"""
            INSERT OR REPLACE INTO part_color_map (
              bl_part_id, element_id, rb_part_num, rb_color_id,
              bl_color_id, bo_color_id, ldraw_color_id,
              weight, boid, source
            )
            SELECT
              bl_part_id, element_id, rb_part_num, rb_color_id,
              bl_color_id, bo_color_id, ldraw_color_id,
              {w_expr} AS weight,
              {boid_expr} AS boid,
              {source_expr} AS source
            FROM part_color_map_old
            """
        )
        cur.execute('DROP TABLE part_color_map_old')
        con.commit()
        cols = _cols('part_color_map')

    # Create if missing
    _create_schema()

    # Ensure weight column exists (safety)
    cols2 = _cols('part_color_map')
    if 'weight' not in cols2:
        try:
            cur.execute('ALTER TABLE part_color_map ADD COLUMN weight REAL')
            con.commit()
        except Exception:
            pass

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

    ap.add_argument(
        "--mode",
        choices=["all", "build", "boid", "export"],
        default="all",
        help=(
            "Modo de execução: "
            "all=build + (opcional) boid + export; "
            "build=build + export; "
            "boid=resolve boid + export (sem rebuild); "
            "export=apenas exportar CSVs a partir da DB."
        ),
    )

    # Inputs (apenas obrigatórios no modo build/all)
    ap.add_argument("--bl-codes-xml")
    ap.add_argument("--rb-elements")
    ap.add_argument("--color-map")

    # Outputs / DB (sempre necessários)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--issues", required=True)

    # Weights
    ap.add_argument("--weights-csv", default="inputs/bricklink/parts_weight.csv", help="CSV (ou .gz) com pesos (colunas: bl_part_id, weight).")
    ap.add_argument("--weights-overwrite", action="store_true", help="Se definido, sobrescreve weight mesmo quando já existe; por defeito preenche apenas NULL.")
    ap.add_argument("--skip-weights", action="store_true", help="Se definido, não aplica weights mesmo que o ficheiro exista.")

    ap.add_argument("--strict", action="store_true", help="Falha apenas se existirem ERROR (WARN não falha).")
    ap.add_argument("--debug-apis", action="store_true")

    # Build tuning
    ap.add_argument("--progress-every", type=int, default=50000)
    ap.add_argument("--commit-every", type=int, default=5000)
    ap.add_argument("--checkpoint", default="data/build_checkpoint.json")
    ap.add_argument("--max-items", type=int, default=0, help="DEBUG: processa no máximo N ITEMS (0 = sem limite).")
    ap.add_argument("--max-runtime-seconds", type=int, default=0, help="Se definido, termina de forma limpa após este tempo (evita timeout).")

    # BOID tuning
    ap.add_argument("--resolve-boid", action="store_true")
    ap.add_argument("--boid-cache-json", default="data/brickowl_api_cache.json")
    ap.add_argument("--boid-min-interval", type=float, default=0.11)
    ap.add_argument("--boid-bulk-min-interval", type=float, default=0.65)
    ap.add_argument("--boid-timeout", type=int, default=30)
    ap.add_argument("--boid-commit-every", type=int, default=200, help="Commit/flush do progresso BOID a cada N pares.")

    ap.add_argument("--boid-country", default="PT", help="ISO2 do país destino para /catalog/availability (ex: PT).")
    ap.add_argument("--boid-validate-availability", action="store_true", help="Valida BOID também via /catalog/availability (mais lento).")
    ap.add_argument("--boid-max-pairs", type=int, default=0, help="DEBUG: limita nº de pares (part,bo_color) para resolver; 0 = sem limite")

    args = ap.parse_args()

    # Timestamp to filter exported issues to *this* run only (avoids confusing old warnings in boid mode)
    run_ts = int(time.time())

    mode = (args.mode or "all").strip().lower()
    if mode not in ("all", "build", "boid", "export"):
        print(f"::error::Modo inválido: {mode}")
        return 2

    t0 = now_s()

    # Paths
    codes_xml = Path(args.bl_codes_xml) if args.bl_codes_xml else None
    rb_elements_csv = Path(args.rb_elements) if args.rb_elements else None
    color_map_csv = Path(args.color_map) if args.color_map else None

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
            "weight",
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

    def add_issue(sev: str, typ: str, key: str, details: str) -> None:
        cur.execute(
            "INSERT INTO build_issues(ts,severity,issue_type,key,details) VALUES (?,?,?,?,?)",
            (int(time.time()), sev, typ, key, details),
        )

    def checkpoint(phase: str, extra: dict) -> None:
        payload = {"ts": int(time.time()), "phase": phase, **extra}
        save_json(checkpoint_path, payload)

    def require_file(pth: Path, label: str) -> None:
        if not pth.exists():
            raise FileNotFoundError(f"Ficheiro obrigatório em falta ({label}): {pth}")

    # Fresh rebuild only in build/all
    if mode in ("all", "build"):
        cur.execute("DELETE FROM part_color_map")
        cur.execute("DELETE FROM build_issues")
        con.commit()

    # Load color map early if present (used in BOID fixups too)
    color_map = None
    bl_to_bo = {}
    bl_to_ldraw = {}

    try:
        if mode in ("all", "build"):
            if codes_xml is None:
                raise FileNotFoundError("--bl-codes-xml é obrigatório em mode=all/build")
            if rb_elements_csv is None:
                raise FileNotFoundError("--rb-elements é obrigatório em mode=all/build")
            if color_map_csv is None:
                raise FileNotFoundError("--color-map é obrigatório em mode=all/build")
            require_file(codes_xml, "--bl-codes-xml")
            require_file(rb_elements_csv, "--rb-elements")
            require_file(color_map_csv, "--color-map")

        # color-map é altamente recomendado no boid mode; se faltar, continuamos usando bo_color_id da DB
        if color_map_csv and color_map_csv.exists():
            color_map = load_color_map(color_map_csv)
            bl_to_bo, bl_to_ldraw, rev_issues = build_bl_reverse_maps(color_map)
            for sev, typ, key, details in rev_issues:
                add_issue(sev, typ, key, details)
            con.commit()
        else:
            if mode in ("all", "build"):
                # já teria sido exigido
                pass
            elif mode in ("boid",):
                add_issue("WARN", "COLOR_MAP_MISSING", "", "--color-map não fornecido; fixups BL->BO não serão aplicados.")
                con.commit()

        if args.debug_apis:
            api_selftests(add_issue)
            con.commit()

        processed = 0
        inserted = 0
        missing_elements = 0
        missing_color_map = 0
        fallback_parts = 0

        checkpoint("start", {"mode": mode, "processed": 0, "inserted": 0, "stop": False})

        # -----------------
        # BUILD (DB rebuild)
        # -----------------
        if mode in ("all", "build"):
            assert codes_xml is not None
            assert rb_elements_csv is not None
            assert color_map_csv is not None

            print("[LOAD] inputs...")
            print(f"  codes.xml: {codes_xml} ({codes_xml.stat().st_size/1024/1024:,.1f} MiB)")
            print(f"  elements.csv: {rb_elements_csv} ({rb_elements_csv.stat().st_size/1024/1024:,.1f} MiB)")
            print(f"  color_map.csv: {color_map_csv} ({color_map_csv.stat().st_size/1024/1024:,.1f} MiB)")

            rb_elements = load_rb_elements(rb_elements_csv)
            if color_map is None:
                color_map = load_color_map(color_map_csv)

            oauth = bricklink_oauth_from_env()
            bl_colors_cache: Dict[str, List[int]] = {}
            fallback_done_parts: Set[str] = set()

            batch_rows: List[Tuple] = []

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
                    cm = (color_map or {}).get(rb_color_id)
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
                        ldraw_color_id = None
                    else:
                        bl_color_id = cm.get("bl_color_id")
                        bo_color_id = cm.get("bo_color_id")
                        ldraw_color_id = cm.get("ldraw_color_id")

                    batch_rows.append(
                        (
                            str(bl_part_id),
                            str(element_id),
                            str(rb_part_num),
                            int(rb_color_id) if rb_color_id is not None else None,
                            int(bl_color_id) if bl_color_id is not None else None,
                            int(bo_color_id) if bo_color_id is not None else None,
                            int(ldraw_color_id) if ldraw_color_id is not None else None,
                            None,
                            "RB",
                        )
                    )
                    inserted += 1

                else:
                    # element_id missing from Rebrickable elements.csv -> mandatory BrickLink fallback
                    missing_elements += 1
                    add_issue(
                        "WARN",
                        "ELEMENT_NOT_IN_REBRICKABLE_ELEMENTS",
                        str(element_id),
                        f"element_id={element_id} não existe em elements.csv (bl_part_id={bl_part_id}).",
                    )

                    if str(bl_part_id) not in fallback_done_parts:
                        fallback_done_parts.add(str(bl_part_id))
                        if oauth is None:
                            add_issue(
                                "WARN",
                                "BRICKLINK_API_UNAVAILABLE",
                                str(bl_part_id),
                                "BrickLink OAuth não configurado; não é possível obter cores conhecidas para fallback.",
                            )
                        else:
                            try:
                                if str(bl_part_id) in bl_colors_cache:
                                    known_colors = bl_colors_cache[str(bl_part_id)]
                                else:
                                    known_colors = bricklink_list_item_colors(str(bl_part_id), oauth, item_type="P", timeout_s=30)
                                    bl_colors_cache[str(bl_part_id)] = known_colors

                                if not known_colors:
                                    add_issue(
                                        "WARN",
                                        "BRICKLINK_NO_COLORS",
                                        str(bl_part_id),
                                        f"BrickLink devolveu 0 cores para bl_part_id={bl_part_id}.",
                                    )
                                else:
                                    fallback_parts += 1

                                    for blc in known_colors:
                                        bo_c = bl_to_bo.get(int(blc))
                                        ld_c = bl_to_ldraw.get(int(blc))
                                        batch_rows.append(
                                            (
                                                str(bl_part_id),
                                                str(element_id),
                                                None,
                                                None,
                                                int(blc),
                                                int(bo_c) if bo_c is not None else None,
                                                int(ld_c) if ld_c is not None else None,
                                                None,
                                                "BL_FALLBACK",
                                            )
                                        )
                                        inserted += 1

                            except Exception as e:
                                add_issue(
                                    "WARN",
                                    "BRICKLINK_FALLBACK_FAILED",
                                    str(bl_part_id),
                                    f"BrickLink fallback falhou para bl_part_id={bl_part_id}: {e}",
                                )

                # flush batch
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

                if processed % int(args.progress_every) == 0:
                    elapsed = now_s() - t0
                    rate = processed / elapsed if elapsed > 0 else 0
                    print(
                        f"[PROGRESS] processed={processed:,} inserted={inserted:,} missing_elements={missing_elements:,} "
                        f"fallback_parts={fallback_parts:,} missing_color_map={missing_color_map:,} rate={rate:,.1f}/s elapsed={elapsed:,.0f}s"
                    )
                    checkpoint(
                        "build",
                        {
                            "mode": mode,
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
                    "mode": mode,
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
        # WEIGHTS (apply from inputs/bricklink/parts_weight.csv by default)
        # -----------------
        if (not args.skip_weights) and mode in ("all", "build", "boid", "export"):
            try:
                wp = Path(args.weights_csv)
                # Skip if nothing missing and not overwrite
                try:
                    missing_w = cur.execute("SELECT COUNT(1) FROM part_color_map WHERE weight IS NULL").fetchone()[0]
                except Exception:
                    missing_w = None

                if args.weights_overwrite or (missing_w is None) or (int(missing_w) > 0):
                    print(f"[WEIGHT] applying weights from: {wp} (missing={missing_w})")
                    apply_weights_from_csv(con, cur, wp, overwrite=bool(args.weights_overwrite), add_issue=add_issue)
                    con.commit()

                    # Fallback: preencher weights em falta via BrickLink API (por BL ID, sem cor)
                    try:
                        missing_after_csv = cur.execute("SELECT COUNT(1) FROM part_color_map WHERE weight IS NULL").fetchone()[0]
                    except Exception:
                        missing_after_csv = None

                    if missing_after_csv is not None and int(missing_after_csv) > 0:
                        oauth_w = bricklink_oauth_from_env()
                        if oauth_w is None:
                            add_issue("WARN", "WEIGHTS_BRICKLINK_OAUTH_MISSING", "", "BrickLink OAuth não configurado; weights em falta permanecerão NULL.")
                            con.commit()
                        else:
                            print(f"[WEIGHT] BrickLink fallback: missing_after_csv={missing_after_csv}")
                            fill_missing_weights_from_bricklink(
                                con,
                                cur,
                                oauth_w,
                                add_issue=add_issue,
                                min_interval_s=0.25,
                                commit_every=200,
                                max_runtime_seconds=float(args.max_runtime_seconds or 0),
                                t0=float(t0),
                            )
                            con.commit()
                else:
                    print("[WEIGHT] skip (weight já preenchido)")
            except Exception as e:
                add_issue("WARN", "WEIGHTS_APPLY_FAILED", "", f"Falha ao aplicar weights: {e}")
                con.commit()


        # -----------------
        # BOID resolution (resume)
        # -----------------
        do_boid = bool(args.resolve_boid) and mode in ("all", "boid")
        if do_boid:
            # avoid starting BOID if we're already beyond max-runtime
            if args.max_runtime_seconds and (now_s() - t0) > float(args.max_runtime_seconds):
                add_issue(
                    "WARN",
                    "SKIP_BOID_MAX_RUNTIME",
                    "",
                    f"A saltar BOID resolve porque já excedeu --max-runtime-seconds={args.max_runtime_seconds}.",
                )
                con.commit()
            elif not BRICKOWL_API_KEY:
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

                rows_pairs = cur.execute(
                    """
                    SELECT DISTINCT bl_part_id, bl_color_id, bo_color_id
                    FROM part_color_map
                    WHERE (boid IS NULL OR boid = '')
                    """
                ).fetchall()

                if args.boid_max_pairs and int(args.boid_max_pairs) > 0:
                    rows_pairs = rows_pairs[: int(args.boid_max_pairs)]

                total_pairs = len(rows_pairs)
                add_issue("INFO", "BRICKOWL_BOID_RESOLVE_START", "", f"A resolver BOID para {total_pairs} pares (part,bo_color).")
                con.commit()

                updated = 0
                commit_every = max(1, int(args.boid_commit_every))

                for idx, (bl_part_id, bl_color_id, bo_color_id_db) in enumerate(rows_pairs, start=1):
                    if _STOP:
                        add_issue("WARN", "STOP_SIGNAL", "", f"Stop requested ({_STOP_REASON}) durante boid resolve.")
                        break

                    if args.max_runtime_seconds and (now_s() - t0) > float(args.max_runtime_seconds):
                        add_issue(
                            "WARN",
                            "EARLY_EXIT_MAX_RUNTIME",
                            "",
                            f"Paragem limpa por --max-runtime-seconds={args.max_runtime_seconds} durante boid resolve.",
                        )
                        break

                    # Prefer mapping BL->BO at resolve time (authoritative). If missing, fall back to DB.
                    blc = None
                    try:
                        blc = int(bl_color_id) if bl_color_id is not None else None
                    except Exception:
                        blc = None

                    bo_color_id_eff = None
                    if blc is not None and bl_to_bo:
                        mapped = bl_to_bo.get(blc)
                        if mapped is not None:
                            bo_color_id_eff = int(mapped)
                            try:
                                if bo_color_id_db is not None and int(bo_color_id_db) != int(mapped):
                                    add_issue(
                                        "WARN",
                                        "BO_COLOR_ID_MISMATCH_FIXUP",
                                        f"{bl_part_id}|{blc}",
                                        f"bo_color_id DB={bo_color_id_db} difere do mapeamento BL->BO={mapped}; a usar mapeamento.",
                                    )
                            except Exception:
                                pass

                    if bo_color_id_eff is None and bo_color_id_db is not None:
                        try:
                            bo_color_id_eff = int(bo_color_id_db)
                        except Exception:
                            bo_color_id_eff = None

                    if bo_color_id_eff is None:
                        add_issue(
                            "WARN",
                            "BRICKOWL_BO_COLOR_ID_MISSING",
                            str(bl_part_id),
                            "Sem bo_color_id (mapeamento BL->BO indisponível e DB não tem valor).",
                        )
                        continue

                    try:
                        boid = resolve_boid_for_pair(
                            bo_api,
                            str(bl_part_id),
                            int(bo_color_id_eff),
                            add_issue,
                            country=str(args.boid_country),
                            validate_availability=bool(args.boid_validate_availability),
                        )
                    except Exception as e:
                        add_issue("WARN", "BRICKOWL_BOID_RESOLVE_FAILED", f"{bl_part_id}|{bo_color_id_eff}", f"Falha boid resolve: {e}")
                        boid = None

                    if boid:
                        if blc is not None:
                            cur.execute(
                                "UPDATE part_color_map SET boid=?, bo_color_id=? WHERE bl_part_id=? AND bl_color_id=?",
                                (str(boid), int(bo_color_id_eff), str(bl_part_id), int(blc)),
                            )
                        else:
                            cur.execute(
                                "UPDATE part_color_map SET boid=?, bo_color_id=? WHERE bl_part_id=? AND bo_color_id=? AND (bl_color_id IS NULL)",
                                (str(boid), int(bo_color_id_eff), str(bl_part_id), int(bo_color_id_eff)),
                            )
                        updated += 1

                    if idx % commit_every == 0:
                        con.commit()
                        try:
                            persist_brickowl_cache(cache_path, bo_api.cache)
                        except Exception:
                            pass
                        elapsed = now_s() - t0
                        print(f"[BOID] {idx:,}/{total_pairs:,} updated={updated:,} elapsed={elapsed:,.0f}s")
                        checkpoint(
                            "boid",
                            {
                                "mode": mode,
                                "boid_pairs_total": total_pairs,
                                "boid_pairs_done": idx,
                                "boid_pairs_updated": updated,
                                "elapsed_sec": int(elapsed),
                            },
                        )

                con.commit()
                try:
                    persist_brickowl_cache(cache_path, bo_api.cache)
                except Exception:
                    pass
                add_issue("INFO", "BRICKOWL_BOID_RESOLVE_DONE", "", f"BOID resolve terminado. Updated_pairs={updated}/{total_pairs}.")
                con.commit()

        # -----------------
        # Export CSVs
        # -----------------
        if mode in ("all", "build", "boid", "export"):
            print("[EXPORT] part_color_map.csv...")
            with out_csv.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["bl_part_id", "element_id", "rb_part_num", "rb_color_id", "bl_color_id", "bo_color_id", "ldraw_color_id", "weight", "boid", "source"])
                for row in cur.execute(
                    """
                    SELECT bl_part_id, element_id, rb_part_num, rb_color_id, bl_color_id, bo_color_id, ldraw_color_id, weight, boid, source
                    FROM part_color_map
                    ORDER BY bl_part_id, element_id, bl_color_id
                    """
                ):
                    # DB mantém NULL; CSV marca no_color quando ambos IDs são NULL
                    blc=row[4]
                    boc=row[5]
                    if blc is None and boc is None:
                        rr=list(row)
                        rr[4]='no_color'
                        rr[5]='no_color'
                        w.writerow(rr)
                    else:
                        w.writerow(row)

            print("[EXPORT] part_color_issues.csv...")
            with issues_csv.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["severity", "issue_type", "key", "details"])
                for row in cur.execute("SELECT severity, issue_type, key, details FROM build_issues WHERE ts>=? ORDER BY id", (run_ts,)):
                    w.writerow(row)

            con.commit()

        # Summary
        n_err = cur.execute("SELECT COUNT(1) FROM build_issues WHERE severity='ERROR' AND ts>=?", (run_ts,)).fetchone()[0]
        n_warn = cur.execute("SELECT COUNT(1) FROM build_issues WHERE severity='WARN' AND ts>=?", (run_ts,)).fetchone()[0]
        n_rows = cur.execute("SELECT COUNT(1) FROM part_color_map").fetchone()[0]
        elapsed = now_s() - t0
        print(f"✅ mode={mode} | DB rows={n_rows:,} | issues ERR={n_err} WARN={n_warn} | elapsed={elapsed:,.1f}s")

        checkpoint(
            "done",
            {
                "mode": mode,
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
        checkpoint("crash", {"mode": mode, "error": str(e)})
        return 1

    finally:
        try:
            con.commit()
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
