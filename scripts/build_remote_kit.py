"""服務主機端：打包客戶端連線用的 kit（wheel＋skill＋安裝程式＋hook＋README）。

    uv run python scripts/build_remote_kit.py [--out DIR] [--no-zip] [--force]

產出 `<out>/lore-vault-kit-<版本>-<日期>-<commit>/` 與同名 `.zip`：
- `lore_vault-<版本>-py3-none-any.whl`
  （`uv build --wheel -o <kit>`，不寫進 repo 的 dist/）
- `SKILL.md`：取自 repo 的 `integrations/claude/skills/pm/SKILL.md`（不從 ~/.claude 撈）
- `install.py`：取自 `integrations/remote/install.py`
- `hooks/`：episode hook 的只用標準庫子集（D13），檔案清單直接取 doctor
  `hooks.stdlib_only` 的掃描結果（`check_hook_imports(...).scanned`），不另列清單；
  `hooks/spike/` 放 `agent_memory_spike/` 的 hook 與平鋪依賴，`hooks/src/` 放
  `lore_vault` 允許的子套件——hook 以 `parents[1] / "src"` 找套件，佈局必須如此。
  `hooks/VERSION.json` 記版本、commit 與每個檔案的 sha256，安裝器據此驗證
- `README.txt`：版本、wheel sha256 與目標機的執行方式

HTTP 模式只用到 install.py、SKILL.md 與 hooks/；wheel 給完整殼模式。
kit 不含任何密鑰與服務位址。工作樹有未提交變更時 commit 標 `-dirty`。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
SKILL_SRC = Path("integrations/claude/skills/pm/SKILL.md")
INSTALLER_SRC = Path("integrations/remote/install.py")
SPIKE_SRC = Path("agent_memory_spike")
PACKAGE_SRC = Path("src/lore_vault")
HOOKS_KIT_DIR = "hooks"
HOOKS_MANIFEST = "VERSION.json"
DEFAULT_OUT = Path(tempfile.gettempdir()) / "lore-vault-kits"

Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess]


def run_cmd(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def load_installer(repo: Path) -> ModuleType:
    path = repo / INSTALLER_SRC
    spec = importlib.util.spec_from_file_location("lore_vault_remote_install", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def project_version(repo: Path) -> str:
    with (repo / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def git_rev(repo: Path, runner: Runner) -> str:
    rev = runner(["git", "rev-parse", "--short", "HEAD"], repo)
    if rev.returncode != 0:
        return "nogit"
    sha = rev.stdout.strip()
    # kit 內容來自這些路徑（wheel、hook、安裝器、skill）
    dirty = runner(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "src",
            "pyproject.toml",
            str(SPIKE_SRC),
            "integrations",
        ],
        repo,
    )
    if dirty.returncode == 0 and dirty.stdout.strip():
        sha += "-dirty"
    return sha


def kit_name(version: str, date: dt.date, rev: str) -> str:
    return f"lore-vault-kit-{version}-{date:%Y%m%d}-{rev}"


def hook_files(repo: Path) -> list[tuple[Path, str]]:
    """(來源檔, kit 內相對路徑)。清單取自 doctor `hooks.stdlib_only` 的掃描結果：
    hook 進入點與它們遞迴 import 的同目錄模組、允許的 `lore_vault` 子套件。
    掃描有違規就停止打包——違規的 hook 在遠端系統 Python 下會 ImportError。"""
    from lore_vault.doctor.hook_imports import check_hook_imports

    package = repo / PACKAGE_SRC
    spike = repo / SPIKE_SRC
    report = check_hook_imports(hooks_dir=package / "hooks", spike_dir=spike)
    if not report.ok:
        lines = [f"{v.path}:{v.lineno} {v.module}" for v in report.violations]
        raise SystemExit("hook 未通過只用標準庫檢查：\n  " + "\n  ".join(lines))
    src_root = package.parent.resolve()
    spike_root = spike.resolve()
    out: list[tuple[Path, str]] = []
    for path in report.scanned:
        resolved = path.resolve()
        if resolved.is_relative_to(spike_root):
            rel = "spike/" + resolved.relative_to(spike_root).as_posix()
        elif resolved.is_relative_to(src_root):
            rel = "src/" + resolved.relative_to(src_root).as_posix()
        else:
            raise SystemExit(f"hook 掃描結果在預期目錄外：{path}")
        out.append((resolved, rel))
    return sorted(out, key=lambda item: item[1])


def copy_hooks(repo: Path, kit: Path, *, version: str, rev: str) -> dict:
    """把 hook 子集複製到 `<kit>/hooks/`，寫 VERSION.json；回傳 manifest。"""
    dest = kit / HOOKS_KIT_DIR
    files: dict[str, str] = {}
    for src, rel in hook_files(repo):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # 以位元組複製：hash 與安裝器驗證的內容一致
        data = src.read_bytes()
        target.write_bytes(data)
        files[rel] = hashlib.sha256(data).hexdigest()
    manifest = {"version": version, "commit": rev, "files": files}
    (dest / HOOKS_MANIFEST).write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return manifest


def render_readme(name: str, wheel: str, sha256: str) -> str:
    return (
        f"{name}\n"
        f"{'=' * len(name)}\n\n"
        "Lore Vault MCP 客戶端安裝 kit。不含任何密鑰與服務位址。\n\n"
        f"wheel：{wheel}\n"
        f"sha256：{sha256}\n\n"
        "目標機器（人類執行）：\n"
        "  1. 需要 Python >= 3.12 與 Claude Code CLI；完整殼模式另需 uv\n"
        "  2. 準備服務位址（如 https://vault.example.com）與服務的 API token；\n"
        "     服務前面有 Cloudflare Access 時，另備含 CF_ACCESS_CLIENT_ID／\n"
        "     CF_ACCESS_CLIENT_SECRET 的檔案並加 --cf-access-env <檔案>\n"
        "  3. 建議先完全結束 Claude Code\n"
        "  4. 在本資料夾執行：python install.py --base-url <服務位址>\n"
        "     模式：--mode http（免殼，建議）或 --mode shell（本機 venv＋殼）\n"
        "     先看流程：python install.py --dry-run --base-url <服務位址>\n"
        "     只更新 wheel（完整殼）：python install.py --update\n"
        "  5. token 會以不回顯方式詢問；Git Bash 請改用 winpty 或 PowerShell\n"
        "  6. 最後印出的驗證報告不含密鑰，可交給服務管理者核對\n"
        "  7. 重開 Claude Code，請 agent 驗 status／recall\n"
        "  8. 選配：加 --episodes 安裝 episode hook（對話原文會推到服務，\n"
        "     需服務開啟收料）；--no-episodes 跳過，不加時互動詢問\n\n"
        "回退：python install.py --rollback\n"
        "詳細步驟：Lore-Vault repo 的 docs/guides/REMOTE-INSTALL.md\n"
    )


def build_kit(
    out_dir: Path,
    *,
    repo: Path = REPO,
    runner: Runner = run_cmd,
    today: dt.date | None = None,
    make_zip: bool = True,
    force: bool = False,
) -> Path:
    installer = load_installer(repo)
    skill_text = (repo / SKILL_SRC).read_text(encoding="utf-8")
    problems = installer.check_skill_content(skill_text)
    if problems:
        raise SystemExit("SKILL.md 未通過機器中立檢查：" + "；".join(problems))

    version = project_version(repo)
    rev = git_rev(repo, runner)
    name = kit_name(version, today or dt.date.today(), rev)
    kit = out_dir / name
    if kit.exists():
        if not force:
            raise SystemExit(f"{kit} 已存在；加 --force 覆蓋")
        shutil.rmtree(kit)
    kit.mkdir(parents=True)

    built = runner(["uv", "build", "--wheel", "-o", str(kit)], repo)
    if built.returncode != 0:
        raise SystemExit(f"uv build 失敗：\n{built.stderr or built.stdout}")
    wheels = sorted(kit.glob("lore_vault-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"預期 1 個 wheel，實際 {len(wheels)} 個")
    # uv build 會在輸出目錄留 .gitignore；kit 不需要
    (kit / ".gitignore").unlink(missing_ok=True)

    shutil.copyfile(repo / SKILL_SRC, kit / "SKILL.md")
    shutil.copyfile(repo / INSTALLER_SRC, kit / "install.py")
    copy_hooks(repo, kit, version=version, rev=rev)
    sha = installer.sha256_file(wheels[0])
    (kit / "README.txt").write_bytes(
        render_readme(name, wheels[0].name, sha).encode("utf-8")
    )

    if make_zip:
        archive = out_dir / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(kit.rglob("*")):
                if path.is_file():
                    zf.write(path, f"{name}/{path.relative_to(kit).as_posix()}")
    return kit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_remote_kit.py")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="輸出目錄")
    parser.add_argument("--no-zip", action="store_true", help="不打 zip")
    parser.add_argument("--force", action="store_true", help="同名 kit 已存在時覆蓋")
    args = parser.parse_args(argv)
    kit = build_kit(args.out, make_zip=not args.no_zip, force=args.force)
    print(f"kit：{kit}")
    for path in sorted(kit.iterdir()):
        print(f"  {path.name}")
    if not args.no_zip:
        print(f"zip：{kit.with_name(kit.name + '.zip')}")
    print((kit / "README.txt").read_text(encoding="utf-8").split("\n\n")[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
