"""spec delta 解析與併回試算（移植 OpenSpec `requirement-blocks.ts`／`specs-apply.ts`
的核心規則，MVP 只支援 ADDED／MODIFIED／REMOVED）。

- requirement 以 `### Requirement: <名稱>` 為界，名稱比對區分大小寫（同 OpenSpec）
- 程式碼圍欄（``` 或 ~~~）內的行不參與結構判斷
- MODIFIED 整塊取代；目前主 spec 有、新區塊沒有的情境（`####`）即拒絕
- RENAMED：MVP 不支援，出現即為錯誤（不默默忽略）

所有比對與 hash 都先正規化（去 BOM、CRLF→LF、去行尾空白），寫回主 spec 時沿用原檔行尾。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

REQ_HEADER = re.compile(r"^###\s*Requirement:\s*(.+?)\s*$", re.IGNORECASE)
SECTION_HEADER = re.compile(r"^##\s+(.+?)\s*$")
SCENARIO_HEADER = re.compile(r"^####\s+")
_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_ATX_CLOSE = re.compile(r"[ \t]+#+[ \t]*$")
_NORMATIVE = re.compile(r"\b(SHALL|MUST)\b")

OP_ADDED = "ADDED"
OP_MODIFIED = "MODIFIED"
OP_REMOVED = "REMOVED"
_DELTA_SECTIONS = {
    "added requirements": OP_ADDED,
    "modified requirements": OP_MODIFIED,
    "removed requirements": OP_REMOVED,
    "renamed requirements": "RENAMED",
}


class DeltaError(ValueError):
    """delta 無法套用或格式不合；`messages` 列出每一條原因。"""

    def __init__(self, messages: list[str]) -> None:
        super().__init__("；".join(messages))
        self.messages = messages


def normalize_text(text: str) -> str:
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def normalize_name(name: str) -> str:
    return _ATX_CLOSE.sub("", name).strip()


def normalize_block(raw: str) -> str:
    lines = normalize_text(raw).split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def block_hash(raw: str) -> str:
    return hashlib.sha256(normalize_block(raw).encode("utf-8")).hexdigest()


def requirement_key(capability: str, name: str) -> str:
    """`base`／`notes` 的鍵：`<capability>/<requirement 名稱>`。"""
    return f"{capability}/{normalize_name(name)}"


def requirement_topic(key: str) -> str:
    """note topic `req:<capability>/<slug>`：保留 Unicode，只把空白壓成 `-`
    （中文標題不可因去掉非 ASCII 而撞在一起）。"""
    capability, _, name = key.partition("/")
    slug = re.sub(r"\s+", "-", name.strip())
    return f"req:{capability}/{slug}"


def change_topic(name: str) -> str:
    return f"change:{name}"


def fence_mask(lines: list[str]) -> list[bool]:
    mask = [False] * len(lines)
    active: tuple[str, int] | None = None
    for i, line in enumerate(lines):
        if active is None:
            m = _FENCE_OPEN.match(line)
            if m:
                active = (m.group(1)[0], len(m.group(1)))
                mask[i] = True
            continue
        mask[i] = True
        m = _FENCE_CLOSE.match(line)
        if m and m.group(1)[0] == active[0] and len(m.group(1)) >= active[1]:
            active = None
    return mask


@dataclass(frozen=True)
class Block:
    name: str
    raw: str


def _scenario_names(raw: str) -> list[str]:
    lines = normalize_text(raw).split("\n")
    mask = fence_mask(lines)
    names = []
    for i, line in enumerate(lines):
        if not mask[i] and SCENARIO_HEADER.match(line):
            text = _ATX_CLOSE.sub("", SCENARIO_HEADER.sub("", line))
            names.append(re.sub(r"^Scenario:\s*", "", text, flags=re.I).strip())
    return names


def _blocks_in(lines: list[str], mask: list[bool]) -> list[Block]:
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        m = None if mask[i] else REQ_HEADER.match(lines[i])
        if m is None:
            i += 1
            continue
        buf = [lines[i]]
        i += 1
        while i < len(lines) and not (
            not mask[i]
            and (REQ_HEADER.match(lines[i]) or SECTION_HEADER.match(lines[i]))
        ):
            buf.append(lines[i])
            i += 1
        blocks.append(Block(normalize_name(m.group(1)), "\n".join(buf).rstrip()))
    return blocks


@dataclass
class DeltaPlan:
    added: list[Block] = field(default_factory=list)
    modified: list[Block] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    has_renamed: bool = False
    # 寫在 delta 區段外的 requirement（不會被套用）：(名稱, 所在區段, 行號)
    orphans: list[tuple[str, str | None, int]] = field(default_factory=list)
    purpose: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.added or self.modified or self.removed)

    def operations(self) -> list[tuple[str, str]]:
        """(op, requirement 名稱)，依 ADDED、MODIFIED、REMOVED 順序。"""
        return (
            [(OP_ADDED, b.name) for b in self.added]
            + [(OP_MODIFIED, b.name) for b in self.modified]
            + [(OP_REMOVED, n) for n in self.removed]
        )


def parse_delta(text: str) -> DeltaPlan:
    lines = normalize_text(text).split("\n")
    mask = fence_mask(lines)
    plan = DeltaPlan()
    # 切 `## ` 區段
    starts = [
        (i, SECTION_HEADER.match(line).group(1).strip())  # type: ignore[union-attr]
        for i, line in enumerate(lines)
        if not mask[i] and SECTION_HEADER.match(line)
    ]
    first_section = starts[0][0] if starts else len(lines)
    for i in range(first_section):
        if not mask[i] and (m := REQ_HEADER.match(lines[i])):
            plan.orphans.append((normalize_name(m.group(1)), None, i + 1))
    for idx, (start, title) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        body = lines[start + 1 : end]
        body_mask = mask[start + 1 : end]
        op = _DELTA_SECTIONS.get(title.lower())
        if title.lower() == "purpose":
            plan.purpose = "\n".join(body).strip() or None
            continue
        if op is None:
            for j, line in enumerate(body):
                if not body_mask[j] and (m := REQ_HEADER.match(line)):
                    plan.orphans.append(
                        (normalize_name(m.group(1)), title, start + j + 2)
                    )
            continue
        if op == "RENAMED":
            plan.has_renamed = True
        elif op == OP_ADDED:
            plan.added.extend(_blocks_in(body, body_mask))
        elif op == OP_MODIFIED:
            plan.modified.extend(_blocks_in(body, body_mask))
        else:
            for j, line in enumerate(body):
                if body_mask[j]:
                    continue
                m = REQ_HEADER.match(line) or re.match(
                    r"^\s*[-*+]\s*`?###\s*Requirement:\s*(.+?)`?\s*$", line
                )
                if m:
                    plan.removed.append(normalize_name(m.group(1)))
    return plan


def check_plan(plan: DeltaPlan) -> list[str]:
    """delta 本身的格式檢查（不看主 spec）。"""
    errors: list[str] = []
    if plan.has_renamed:
        errors.append(
            "RENAMED Requirements：任務層 MVP 不支援改名，請改用 REMOVED＋ADDED"
        )
    for name, section, line in plan.orphans:
        where = f"「## {section}」下" if section else "第一個 `## ` 區段之前"
        errors.append(f"requirement「{name}」（第 {line} 行）位於{where}，不會被套用")
    if plan.empty and not plan.has_renamed:
        errors.append("沒有任何 ADDED／MODIFIED／REMOVED requirement")
    seen: dict[str, str] = {}
    for op, name in plan.operations():
        if name in seen:
            if seen[name] == op:
                errors.append(f"{op} 內重複的 requirement「{name}」")
            else:
                errors.append(f"requirement「{name}」同時出現在 {seen[name]} 與 {op}")
        else:
            seen[name] = op
    for block in plan.added + plan.modified:
        if not _scenario_names(block.raw):
            errors.append(f"requirement「{block.name}」至少要有一個 `#### Scenario:`")
        statement = block.raw.split("\n", 1)[1] if "\n" in block.raw else ""
        if not _NORMATIVE.search(statement):
            errors.append(f"requirement「{block.name}」內文須含 SHALL 或 MUST")
    return errors


@dataclass
class MainSpec:
    before: str
    header: str
    preamble: str
    blocks: list[Block]
    after: str

    def block(self, name: str) -> Block | None:
        for b in self.blocks:
            if b.name == name:
                return b
        return None


def parse_main(text: str) -> MainSpec:
    lines = normalize_text(text).split("\n")
    mask = fence_mask(lines)
    header_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if not mask[i] and re.match(r"^##\s+Requirements\s*$", line, re.I)
        ),
        None,
    )
    if header_idx is None:
        before = text.rstrip()
        return MainSpec(
            before + "\n\n" if before else "", "## Requirements", "", [], "\n"
        )
    end = next(
        (
            i
            for i in range(header_idx + 1, len(lines))
            if not mask[i] and re.match(r"^##\s+", lines[i])
        ),
        len(lines),
    )
    body = lines[header_idx + 1 : end]
    body_mask = mask[header_idx + 1 : end]
    first_req = next(
        (
            j
            for j, line in enumerate(body)
            if not body_mask[j] and REQ_HEADER.match(line)
        ),
        len(body),
    )
    before = "\n".join(lines[:header_idx])
    return MainSpec(
        before=before,
        header=lines[header_idx],
        preamble="\n".join(body[:first_req]).rstrip(),
        blocks=_blocks_in(body, body_mask),
        after="\n".join(lines[end:]),
    )


def _collapse_blank_runs(text: str) -> str:
    lines = text.split("\n")
    mask = fence_mask(lines)
    kept: list[str] = []
    blank = 0
    for i, line in enumerate(lines):
        if not mask[i] and line == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        kept.append(line)
    return "\n".join(kept)


def skeleton(capability: str, change_name: str, purpose: str | None) -> str:
    body = (purpose or "").strip() or f"TBD - 由 change {change_name} 封存時建立。"
    return f"# {capability} Specification\n\n## Purpose\n{body}\n\n## Requirements\n"


def apply_delta(
    main_text: str | None, plan: DeltaPlan, capability: str, change_name: str
) -> str:
    """試算 delta 併回主 spec 後的全文（LF）；不能套用拋 `DeltaError`。"""
    errors = check_plan(plan)
    if errors:
        raise DeltaError(errors)
    is_new = main_text is None
    if is_new:
        if plan.modified:
            raise DeltaError([f"{capability}：主 spec 不存在，只能用 ADDED"])
        main_text = skeleton(capability, change_name, plan.purpose)
    spec = parse_main(main_text)
    order = [b.name for b in spec.blocks]
    current = {b.name: b for b in spec.blocks}
    for name in plan.removed:
        if name not in current:
            if not is_new:
                errors.append(f"REMOVED「{name}」不在主 spec 中")
            continue
        del current[name]
    for mod in plan.modified:
        existing = current.get(mod.name)
        if existing is None:
            errors.append(f"MODIFIED「{mod.name}」不在主 spec 中")
            continue
        incoming = _scenario_names(mod.raw)
        remaining = list(incoming)
        missing = []
        for s in _scenario_names(existing.raw):
            if s in remaining:
                remaining.remove(s)
            else:
                missing.append(s)
        if missing:
            errors.append(
                f"MODIFIED「{mod.name}」漏掉主 spec 既有情境："
                + "、".join(f"「{s}」" for s in missing)
            )
            continue
        current[mod.name] = mod
    for add in plan.added:
        existing = current.get(add.name)
        if existing is not None:
            if normalize_block(existing.raw) != normalize_block(add.raw):
                errors.append(f"ADDED「{add.name}」已存在於主 spec")
            continue
        current[add.name] = add
        order.append(add.name)
    if errors:
        raise DeltaError(errors)
    kept = [current[n].raw for n in order if n in current]
    req_body = "\n\n".join(([spec.preamble] if spec.preamble.strip() else []) + kept)
    parts = [spec.before.rstrip(), spec.header, req_body.rstrip(), spec.after.strip()]
    return _collapse_blank_runs("\n\n".join(p for p in parts if p)).rstrip() + "\n"


def main_block_hash(main_text: str | None, name: str) -> str | None:
    """主 spec 中某 requirement 的正規化內容 hash；不存在回 None。"""
    if main_text is None:
        return None
    block = parse_main(main_text).block(name)
    return None if block is None else block_hash(block.raw)


def delta_applied(main_text: str | None, plan: DeltaPlan) -> list[str]:
    """對帳：主 spec 是否已含 delta 的結果；回傳不一致的說明。"""
    problems = []
    spec = parse_main(main_text) if main_text is not None else None
    for block in plan.added + plan.modified:
        current = spec.block(block.name) if spec else None
        if current is None:
            problems.append(f"「{block.name}」不在主 spec")
        elif normalize_block(current.raw) != normalize_block(block.raw):
            problems.append(f"「{block.name}」內容與 delta 不同")
    for name in plan.removed:
        if spec is not None and spec.block(name) is not None:
            problems.append(f"「{name}」應已移除，仍在主 spec")
    return problems


def detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"
