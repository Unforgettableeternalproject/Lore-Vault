#!/usr/bin/env python3
"""一次性重新編號：修掉舊配號規則（``c-{len:03d}``）造成的 concept id 撞號。

遷移用，不會被管線自動執行；預設 dry-run 只印統計，``--write`` 才寫檔，
而且絕不就地覆寫輸入（輸出路徑與任何輸入相同即拒絕）。

規則：
- 每組重複 id 保留**第一次出現**者的原 id，其後各筆發新號，
  從 ``max(全檔最大號, 輸入檔高水位) + 1`` 起連續配號。
- 對照表記錄「舊 id + 出現序 → 新 id」（出現序從 1 起，1 是保留原號的那筆）。
- 注入紀錄：``injected`` 裡含有重複過的 id 者，無法判斷當時注入的是哪一條，
  加上 ``ambiguous_ids`` 標記；**不猜測、不刪除、不改 injected**，其餘行逐字保留。
- 輸出檔旁同時寫入新的高水位 sidecar（見 concept_ids）。

統計只印數量，不輸出任何 statement／cue 內容。

用法::

    python agent_memory_spike/renumber_concepts.py --in concepts.json --out new.json \\
        --map map.json [--injections injections.jsonl --injections-out new.jsonl] [--write]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from concept_ids import (  # noqa: E402
    format_id,
    load_high_water,
    max_id_number,
    save_high_water,
)


def renumber(concepts: list[dict[str, Any]], start_after: int
             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int]:
    """回傳（新池子、對照表、重複過的 id、新最大號）。不改動傳入的 concepts。"""
    counts = Counter(c.get("id") for c in concepts)
    duplicated = [cid for cid, n in counts.items() if n > 1 and cid is not None]
    seen: Counter[Any] = Counter()
    last = start_after
    out: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []
    for index, concept in enumerate(concepts):
        cid = concept.get("id")
        seen[cid] += 1
        if cid is None or seen[cid] == 1:
            out.append(concept)
            continue
        last += 1
        new_id = format_id(last)
        out.append({**concept, "id": new_id})
        mapping.append({"old_id": cid, "occurrence": seen[cid], "index": index,
                        "new_id": new_id})
    return out, mapping, duplicated, max(last, max_id_number(out))


def mark_injections(lines: list[str], duplicated: set[str]) -> tuple[list[str], int, int]:
    """逐行處理注入紀錄；回傳（新行、紀錄筆數、標記 ambiguous 的筆數）。

    解析失敗或不是物件的行原樣保留——load_injections 本來就會跳過它們，
    遷移工具不該順手清資料。
    """
    out: list[str] = []
    records = 0
    ambiguous = 0
    for raw in lines:
        body = raw.rstrip("\r\n")
        ending = raw[len(body):]
        try:
            record = json.loads(body) if body.strip() else None
        except json.JSONDecodeError:
            record = None
        if not isinstance(record, dict):
            out.append(raw)
            continue
        records += 1
        injected = record.get("injected") if isinstance(record.get("injected"), list) else []
        hit: list[str] = []
        for cid in injected:
            if cid in duplicated and cid not in hit:
                hit.append(cid)
        if not hit:
            out.append(raw)
            continue
        ambiguous += 1
        record["ambiguous_ids"] = hit
        out.append(json.dumps(record, ensure_ascii=False) + (ending or "\n"))
    return out, records, ambiguous


def _same(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="concept id 撞號的一次性重新編號（遷移用）")
    parser.add_argument("--in", dest="src", type=Path, required=True, help="輸入 concepts.json")
    parser.add_argument("--out", type=Path, required=True, help="輸出的新 concepts 檔")
    parser.add_argument("--map", type=Path, required=True, help="輸出的對照表 JSON")
    parser.add_argument("--injections", type=Path, help="輸入 injections.jsonl")
    parser.add_argument("--injections-out", type=Path, help="輸出的新注入紀錄")
    parser.add_argument("--write", action="store_true", help="實際寫檔（預設只印統計）")
    args = parser.parse_args(argv)

    if (args.injections is None) != (args.injections_out is None):
        parser.error("--injections 與 --injections-out 必須一起給")
    inputs = [args.src] + ([args.injections] if args.injections else [])
    outputs = [args.out, args.map] + ([args.injections_out] if args.injections_out else [])
    for out in outputs:
        for src in inputs:
            if _same(out, src):
                parser.error(f"輸出不可與輸入相同（拒絕就地覆寫）：{out}")
    if len({o.resolve() for o in outputs}) != len(outputs):
        parser.error("各輸出路徑必須互不相同")

    concepts = json.loads(args.src.read_text(encoding="utf-8"))
    start_after = max(max_id_number(concepts), load_high_water(args.src))
    new_pool, mapping, duplicated, new_max = renumber(concepts, start_after)

    ids = [c.get("id") for c in concepts]
    print(f"[renumber] {len(concepts)} 筆、唯一 id {len(set(ids))}、"
          f"重複 id {len(duplicated)} 組", file=sys.stderr)
    print(f"  重新編號 {len(mapping)} 筆；原最大號 {format_id(start_after)} → "
          f"新最大號 {format_id(new_max)}", file=sys.stderr)
    new_ids = [c.get("id") for c in new_pool]
    if len(new_ids) != len(set(new_ids)):
        print("  ⚠ 重新編號後仍有重複 id——中止", file=sys.stderr)
        return 1

    new_lines: list[str] = []
    if args.injections:
        raw = ""
        if args.injections.exists():
            # newline="" 保留原行尾：未改動的行要逐字寫回
            with args.injections.open(encoding="utf-8", newline="") as fh:
                raw = fh.read()
        new_lines, records, ambiguous = mark_injections(raw.splitlines(keepends=True),
                                                        set(duplicated))
        print(f"  注入紀錄 {records} 筆，標記 ambiguous_ids {ambiguous} 筆", file=sys.stderr)

    if not args.write:
        print("  （dry-run：未寫檔；加 --write 才寫出）", file=sys.stderr)
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(new_pool, ensure_ascii=False, indent=2), encoding="utf-8")
    save_high_water(args.out, new_max)
    args.map.parent.mkdir(parents=True, exist_ok=True)
    args.map.write_text(json.dumps({
        "source": str(args.src),
        "duplicated_ids": duplicated,
        "renumbered": mapping,
        "max_id": new_max,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.injections_out:
        args.injections_out.parent.mkdir(parents=True, exist_ok=True)
        with args.injections_out.open("w", encoding="utf-8", newline="") as fh:
            fh.writelines(new_lines)
    print(f"  已寫出 → {args.out}、{args.map}"
          + (f"、{args.injections_out}" if args.injections_out else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
