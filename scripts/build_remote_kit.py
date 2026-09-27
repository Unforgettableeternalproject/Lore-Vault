"""主機端：打包「其他機器安裝 MCP 殼」用的 kit（wheel＋pm skill＋安裝程式＋README）。

    uv run python scripts/build_remote_kit.py [--out DIR] [--no-zip] [--force]

產出 `<out>/lore-vault-kit-<版本>-<日期>-<commit>/` 與同名 `.zip`：
- `lore_vault-<版本>-py3-none-any.whl`
  （`uv build --wheel -o <kit>`，不寫進 repo 的 dist/）
- `SKILL.md`：取自 repo 的 `integrations/claude/skills/pm/SKILL.md`（不從 ~/.claude 撈）
- `install.py`：取自 `integrations/remote/install.py`
- `README.txt`：版本、wheel sha256 與目標機的執行方式

kit 只供自用機器；不含任何密鑰。工作樹有未提交變更時 commit 標 `-dirty`。
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
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
    dirty = runner(
        ["git", "status", "--porcelain", "--", "src", "pyproject.toml"], repo
    )
    if dirty.returncode == 0 and dirty.stdout.strip():
        sha += "-dirty"
    return sha


def kit_name(version: str, date: dt.date, rev: str) -> str:
    return f"lore-vault-kit-{version}-{date:%Y%m%d}-{rev}"


def render_readme(name: str, wheel: str, sha256: str) -> str:
    return (
        f"{name}\n"
        f"{'=' * len(name)}\n\n"
        "Lore Vault MCP 殼的安裝 kit（自用機器）。不含任何密鑰。\n\n"
        f"wheel：{wheel}\n"
        f"sha256：{sha256}\n\n"
        "目標機器（人類執行）：\n"
        "  1. 確認已有 ~/.cloudflared/pm-token.env、Python >= 3.12、uv、\n"
        "     Claude Code CLI\n"
        "  2. 建議先完全結束 Claude Code\n"
        "  3. 在本資料夾執行：python install.py\n"
        "     先看流程：python install.py --dry-run\n"
        "     只更新 wheel：python install.py --update\n"
        "  4. token 會以不回顯方式詢問；Git Bash 請改用 winpty 或 PowerShell\n"
        "  5. 把最後印出的驗證報告整段貼到聊天室給主機核對\n"
        "  6. 重開 Claude Code，由主機請本機 agent 驗 status／recall／ask\n\n"
        "回退：python install.py --rollback\n"
        "詳細步驟與實測坑：Lore-Vault repo 的 docs/guides/REMOTE-INSTALL.md\n"
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

    name = kit_name(
        project_version(repo), today or dt.date.today(), git_rev(repo, runner)
    )
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
    sha = installer.sha256_file(wheels[0])
    (kit / "README.txt").write_bytes(
        render_readme(name, wheels[0].name, sha).encode("utf-8")
    )

    if make_zip:
        archive = out_dir / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(kit.iterdir()):
                zf.write(path, f"{name}/{path.name}")
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
