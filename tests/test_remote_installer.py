"""integrations/remote/install.py：安裝程式的純邏輯與流程。

所有路徑以 tmp HOME 隔離；外部指令用假 runner。
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "integrations" / "remote" / "install.py"
SKILL = REPO / "integrations" / "claude" / "skills" / "pm" / "SKILL.md"


def _skill_lf() -> bytes:
    # 安裝器以文字讀取 kit 的 SKILL.md、寫出一律 LF；
    # repo 工作區在 autocrlf 下可能是 CRLF
    return SKILL.read_bytes().replace(b"\r\n", b"\n")


FAKE_TOKEN = "FAKE-TOKEN-7f3a9c-DO-NOT-LEAK"
BASE_URL = "https://vault.example.com"


def _load():
    spec = importlib.util.spec_from_file_location("lv_remote_install", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inst = _load()


def _probe(version=(3, 13, 1), venv=False, executable="C:/sys/python.exe") -> dict:
    return {"version": list(version), "venv": venv, "executable": executable}


class FakeRunner:
    """記錄每次呼叫；依 argv 回應。`mcp get` 預設回傳不存在。"""

    def __init__(
        self,
        *,
        existing: dict[str, str] | None = None,
        status=None,
        hook_python: dict[str, dict] | None = None,
    ):
        self.calls: list[list[str]] = []
        self.existing = dict(existing or {})
        # hook 用 Python 探測：{執行檔: 探測結果}；未列出的回 exit 1（找不到）
        self.hook_python = (
            {"C:/py/python.exe": _probe()} if hook_python is None else hook_python
        )
        self.status = status or {
            "category": "ok",
            "ok": True,
            "schema_version": 13,
            "schema_expected": 13,
            "wheel_schema": 13,
            "doctor": {"total": 38, "pass": 31, "fail": 0, "warn": 0, "skipped": 7},
            "doctor_fails": [],
        }

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:] == ["--version"]:
            return inst.Result(0, f"{Path(argv[0]).name} 9.9.9\n")
        if argv[1:3] == ["mcp", "get"]:
            name = argv[3]
            if name in self.existing:
                return inst.Result(0, f"{name}:\n  Scope: {self.existing[name]}\n")
            return inst.Result(1, "", f"No MCP server found with name: {name}")
        if argv[1:3] == ["mcp", "remove"]:
            self.existing.pop(argv[3], None)
            return inst.Result(0, "removed")
        if argv[1:3] == ["mcp", "add"]:
            self.existing[argv[3]] = "User config"
            return inst.Result(0, "added")
        if argv[1:3] == ["mcp", "list"]:
            return inst.Result(
                0, "lore-vault: " + " ".join(argv[:1]) + " -m lore_vault.mcp - ok\n"
            )
        if argv[1] == "venv":
            py = Path(argv[2]) / "Scripts" / "python.exe"
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("")
            return inst.Result(0)
        if argv[1:3] == ["pip", "install"]:
            return inst.Result(0)
        if len(argv) > 2 and argv[1] == "-c" and argv[2] == inst.HOOK_PYTHON_PROBE:
            data = self.hook_python.get(argv[0])
            if data is None:
                return inst.Result(1, "", "not found")
            return inst.Result(
                0, "noise\n" + inst.HOOK_PYTHON_MARKER + json.dumps(data)
            )
        if len(argv) > 2 and argv[1] == "-c" and "import lore_vault.mcp" in argv[2]:
            return inst.Result(0, "ok\n")
        if len(argv) > 2 and argv[1] == "-c":
            return inst.Result(0, "noise\n" + json.dumps(self.status) + "\n")
        raise AssertionError(f"未預期的指令：{argv}")

    def flat(self) -> str:
        return "\n".join(" ".join(c) for c in self.calls)


class Capture:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, text):
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _no_input(prompt):
    raise AssertionError(f"不該提問：{prompt}")


HOOK_FILES = {
    "spike/hook_stop.py": b"# stop\n",
    "spike/hook_pretooluse.py": b"# pre\n",
    "spike/paths.py": b"# paths\n",
    "src/lore_vault/__init__.py": b"",
    "src/lore_vault/hooks/client_env.py": b"# env\n",
}


def make_hooks(kit: Path, files: dict[str, bytes] | None = None, commit="abc1234"):
    import hashlib

    files = HOOK_FILES if files is None else files
    hooks = kit / "hooks"
    for rel, data in files.items():
        (hooks / rel).parent.mkdir(parents=True, exist_ok=True)
        (hooks / rel).write_bytes(data)
    manifest = {
        "version": "0.1.0",
        "commit": commit,
        "files": {rel: hashlib.sha256(d).hexdigest() for rel, d in files.items()},
    }
    (hooks / "VERSION.json").write_text(json.dumps(manifest), encoding="utf-8")


def make_kit(tmp_path: Path) -> Path:
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "lore_vault-0.1.0-py3-none-any.whl").write_bytes(b"fake wheel")
    (kit / "SKILL.md").write_bytes(SKILL.read_bytes())
    make_hooks(kit)
    return kit


def make_home(tmp_path: Path, *, cf=True) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    if cf:
        (home / ".cloudflared").mkdir()
        (home / ".cloudflared" / "pm-token.env").write_text(
            "CF_ACCESS_CLIENT_ID=cid-secret-value\nCF_ACCESS_CLIENT_SECRET=csec-value\n",
            encoding="utf-8",
        )
    return home


def make_installer(tmp_path, runner, *, answers=None, secret=None, **kw):
    home = kw.pop("home", None) or make_home(tmp_path)
    kit = kw.pop("kit", None) or make_kit(tmp_path)
    cap = Capture()
    answers = list(answers or [])

    def input_fn(prompt):
        if not answers:
            raise AssertionError(f"沒有預備答案：{prompt}")
        return answers.pop(0)

    ui = inst.UI(
        assume_yes=kw.pop("assume_yes", False),
        input_fn=input_fn if answers or not kw.get("no_prompt") else _no_input,
        secret_fn=secret or _no_input,
        out=cap,
    )
    kw.pop("no_prompt", None)
    # 服務位址必填；要測「未給位址」的情境時明確傳 base_url=None
    kw.setdefault("base_url", BASE_URL)
    # hook 用 Python：不依賴跑測試的 venv，只從假 runner 探測 C:/py/python.exe
    kw.setdefault("python_in_venv", False)
    kw.setdefault("base_python", None)
    installer = inst.Installer(
        paths=inst.Paths(home),
        kit_dir=kit,
        ui=ui,
        runner=runner,
        which=kw.pop("which", lambda name: f"C:/bin/{name}.exe"),
        python_exe="C:/py/python.exe",
        python_version=(3, 14, 0),
        environ=kw.pop("environ", {}),
        **kw,
    )
    return installer, cap


# ── 內容產生 ──


def test_render_mcp_toml_has_expected_keys_and_no_secret():
    text = inst.render_mcp_toml(BASE_URL + "/")
    import tomllib

    data = tomllib.loads(text)["mcp"]
    # 未用 CF Access 時不寫 cf_access_env_file（殼遇到指向不存在檔案的鍵會拒絕啟動）
    assert data == {
        "base_url": BASE_URL,
        "snapshot_dir": "~/.lore-vault/snapshot",
        "timeout": 15.0,
    }
    with_cf = tomllib.loads(
        inst.render_mcp_toml(BASE_URL, cf_access_env_file="~/cf.env")
    )["mcp"]
    assert with_cf["cf_access_env_file"] == "~/cf.env"
    # 只有路徑值含 token 字樣，鍵名都不是密鑰類
    assert all("token" not in k and "secret" not in k for k in data)


def test_render_mcp_toml_custom_base_url_escaped():
    import tomllib

    text = inst.render_mcp_toml('https://x.example/"q"\\', timeout=3)
    assert tomllib.loads(text)["mcp"]["base_url"] == 'https://x.example/"q"\\'


def test_mcp_toml_loads_with_real_settings(tmp_path):
    """產出的設定檔能被殼的設定載入接受（不被當成含密鑰而拒絕）。"""
    from lore_vault.mcp.settings import load_shell_settings

    cfg = tmp_path / "mcp.toml"
    cfg.write_text(inst.render_mcp_toml(BASE_URL), encoding="utf-8")
    env = tmp_path / "mcp.env"
    env.write_bytes(inst.render_mcp_env(FAKE_TOKEN))
    settings = load_shell_settings(config_path=cfg, env_file=env, environ={})
    assert settings.base_url == BASE_URL
    assert settings.cf_access is None
    assert settings.token.reveal() == FAKE_TOKEN
    assert settings.timeout == 15.0


def test_mcp_env_utf8_without_bom():
    data = inst.render_mcp_env(FAKE_TOKEN)
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data == f"LORE_VAULT_API_TOKEN={FAKE_TOKEN}\n".encode()


@pytest.mark.parametrize("bad", ["", "  ", "a\nb", " lead", "trail\r"])
def test_mcp_env_rejects_bad_token(bad):
    with pytest.raises(inst.StepFailed):
        inst.render_mcp_env(bad)


def test_env_file_has_token_rejects_bom_and_utf16(tmp_path):
    p = tmp_path / "mcp.env"
    p.write_bytes(b"LORE_VAULT_API_TOKEN=x\n")
    assert inst.env_file_has_token(p)
    p.write_bytes(b"\xef\xbb\xbfLORE_VAULT_API_TOKEN=x\n")
    assert not inst.env_file_has_token(p)
    p.write_bytes("LORE_VAULT_API_TOKEN=x\n".encode("utf-16"))
    assert not inst.env_file_has_token(p)
    p.write_bytes(b"LORE_VAULT_API_TOKEN=\n")
    assert not inst.env_file_has_token(p)


def test_cf_env_keys_present_reports_only_booleans(tmp_path):
    p = tmp_path / "pm-token.env"
    p.write_text("CF_ACCESS_CLIENT_ID=abc\n", encoding="utf-8")
    assert inst.cf_env_keys_present(p) == {
        "CF_ACCESS_CLIENT_ID": True,
        "CF_ACCESS_CLIENT_SECRET": False,
    }


# ── 指令組裝 ──


def test_mcp_add_argv_is_list_with_separator_and_no_token(tmp_path):
    paths = inst.Paths(tmp_path)
    argv = inst.mcp_add_argv("claude", paths)
    assert isinstance(argv, list)
    assert argv[:7] == ["claude", "mcp", "add", "lore-vault", "-s", "user", "--"]
    assert argv[7:] == [
        str(paths.venv_python),
        "-m",
        "lore_vault.mcp",
        "--config",
        str(paths.mcp_toml),
        "--env-file",
        str(paths.mcp_env),
    ]
    assert "add-json" not in argv
    assert all(FAKE_TOKEN not in a for a in argv)


def test_pip_install_uses_reinstall(tmp_path):
    argv = inst.pip_install_argv("uv", inst.Paths(tmp_path), Path("w.whl"))
    assert argv[:4] == ["uv", "pip", "install", "--reinstall"]


def test_parse_mcp_scope():
    assert inst.parse_mcp_scope("x:\n  Scope: User config (all)\n") == "user"
    assert inst.parse_mcp_scope("  Scope: Local config\n") == "local"
    assert inst.parse_mcp_scope("  Scope: Project config (.mcp.json)\n") == "project"
    assert inst.parse_mcp_scope("nothing") is None


def test_repo_skill_passes_neutrality_check():
    assert inst.check_skill_content(SKILL.read_text(encoding="utf-8")) == []


def test_skill_check_flags_old_tools_and_user_paths():
    text = "allowed-tools: mcp__open-notebook__search\nC:\\Users\\bob\\x"
    problems = inst.check_skill_content(text)
    assert len(problems) == 3


# ── 備份 ──


def test_backup_does_not_overwrite_existing(tmp_path):
    runner = FakeRunner()
    installer, _ = make_installer(tmp_path, runner)
    p = installer.paths
    p.claude_json.write_text("current", encoding="utf-8")
    inst.backup_path(p.claude_json).write_text("ORIGINAL", encoding="utf-8")
    p.skill.parent.mkdir(parents=True)
    p.skill.write_text("skill-now", encoding="utf-8")
    installer.step_backup()
    assert inst.backup_path(p.claude_json).read_text(encoding="utf-8") == "ORIGINAL"
    assert inst.backup_path(p.skill).read_text(encoding="utf-8") == "skill-now"


def test_backup_missing_originals_is_not_failure(tmp_path):
    installer, _ = make_installer(tmp_path, FakeRunner())
    installer.step_backup()
    assert [r.status for r in installer.records] == ["skip", "skip"]
    assert not inst.backup_path(installer.paths.claude_json).exists()


# ── 完整流程 ──


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_dry_run_writes_nothing_and_runs_only_probes(tmp_path):
    home = make_home(tmp_path)
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    before = _snapshot_tree(home)
    runner = FakeRunner()
    installer, cap = make_installer(
        tmp_path, runner, home=home, dry_run=True, assume_yes=True, no_prompt=True
    )
    assert installer.install() == 0
    assert _snapshot_tree(home) == before
    assert all(c[1:] == ["--version"] for c in runner.calls), runner.calls
    assert "[DRY]" in cap.text
    assert "dry-run" in cap.text


def test_full_install_interactive(tmp_path):
    runner = FakeRunner(existing={"open-notebook": "User config (all projects)"})
    home = make_home(tmp_path)
    (home / ".claude.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    old_skill = home / ".claude" / "skills" / "pm" / "SKILL.md"
    old_skill.parent.mkdir(parents=True)
    old_skill.write_text(
        "allowed-tools: mcp__open-notebook__search\n", encoding="utf-8"
    )

    installer, cap = make_installer(
        tmp_path,
        runner,
        home=home,
        base_url=None,
        # episode hook？（預設否）／開始？／服務位址／CF Access？（用建議路徑）／
        # 憑證檔路徑（預設）／覆寫 skill？
        answers=["", "y", BASE_URL + "/mcp", "y", "", "y"],
        secret=lambda prompt: FAKE_TOKEN,
    )
    # 測試環境 stdin 不是主控台；這裡模擬在 PowerShell 內執行
    inst_is_console = inst._stdin_is_console
    inst._stdin_is_console = lambda: True
    try:
        assert installer.install() == 0
    finally:
        inst._stdin_is_console = inst_is_console

    p = installer.paths
    assert p.mcp_env.read_bytes() == inst.render_mcp_env(FAKE_TOKEN)
    import tomllib

    written = tomllib.loads(p.mcp_toml.read_text(encoding="utf-8"))["mcp"]
    assert written["base_url"] == BASE_URL
    # --home 導向 tmp 時寫絕對路徑，殼不會讀到真實 ~/.cloudflared
    assert written["cf_access_env_file"] == p.cf_env.as_posix()
    assert written["snapshot_dir"] == p.snapshot_dir.as_posix()
    assert p.skill.read_bytes() == _skill_lf()
    assert inst.backup_path(p.claude_json).exists()
    assert (
        inst.backup_path(p.skill)
        .read_text(encoding="utf-8")
        .startswith("allowed-tools: mcp__open-notebook")
    )
    assert ["C:/bin/claude.exe", "mcp", "remove", "open-notebook", "-s", "user"] in (
        runner.calls
    )
    assert inst.mcp_add_argv("C:/bin/claude.exe", p) in runner.calls
    report_files = list(p.lv_dir.glob("install-report-*.txt"))
    assert len(report_files) == 1
    report = report_files[0].read_text(encoding="utf-8")
    assert "wheel sha256：" + inst.sha256_file(installer.wheel) in report
    assert "doctor pass 31／fail 0／warn 0／skipped 7" in report
    assert "重開 Claude Code" in report
    # 密鑰不出現在任何輸出、報告、指令參數
    for blob in (cap.text, report, runner.flat()):
        assert FAKE_TOKEN not in blob
        assert "cid-secret-value" not in blob
        assert "csec-value" not in blob
    # 報告遮蔽家目錄
    assert str(home) not in report
    # episode hook 的隱私提示有出現，預設不裝
    assert "對話原文" in cap.text
    assert "episode hook：未安裝" in report
    assert not p.hooks_dir.exists() and not p.client_env.exists()
    assert not p.settings_json.exists()


def test_rerun_is_idempotent_and_keeps_existing(tmp_path):
    runner = FakeRunner()
    env = {"LORE_VAULT_API_TOKEN": FAKE_TOKEN}
    installer, _ = make_installer(tmp_path, runner, assume_yes=True, environ=env)
    assert installer.install() == 0
    p = installer.paths
    p.mcp_toml.write_text(inst.render_mcp_toml("https://custom.example"), "utf-8")
    first_env = p.mcp_env.read_bytes()

    runner2 = FakeRunner(existing={"lore-vault": "User config"})
    installer2, cap2 = make_installer(
        tmp_path,
        runner2,
        home=p.home,
        kit=installer.kit_dir,
        assume_yes=True,
        environ={},
        base_url=None,
    )
    assert installer2.install() == 0
    # --yes 下既有設定保留
    assert "custom.example" in p.mcp_toml.read_text(encoding="utf-8")
    assert p.mcp_env.read_bytes() == first_env
    # 既有 lore-vault 先移除再新增
    assert ["C:/bin/claude.exe", "mcp", "remove", "lore-vault", "-s", "user"] in (
        runner2.calls
    )
    assert not any(c[1] == "venv" for c in runner2.calls)
    assert FAKE_TOKEN not in cap2.text


def test_yes_without_token_stops_with_resume_hint(tmp_path):
    runner = FakeRunner()
    installer, cap = make_installer(tmp_path, runner, assume_yes=True, environ={})
    with pytest.raises(inst.StepFailed) as exc:
        installer.install()
    assert "LORE_VAULT_API_TOKEN" in exc.value.resume
    assert not installer.paths.mcp_env.exists()
    # 失敗前的步驟已完成，mcp add 尚未執行
    assert not any(c[1:3] == ["mcp", "add"] for c in runner.calls)


def test_non_console_without_env_token_refuses_prompt(tmp_path):
    installer, _ = make_installer(
        tmp_path, FakeRunner(), answers=["n", "y", ""], secret=_no_input
    )
    inst_is_console = inst._stdin_is_console
    inst._stdin_is_console = lambda: False
    try:
        with pytest.raises(inst.StepFailed) as exc:
            installer.install()
    finally:
        inst._stdin_is_console = inst_is_console
    assert "winpty" in exc.value.resume


def test_project_scope_open_notebook_not_removed(tmp_path):
    runner = FakeRunner(existing={"open-notebook": "Project config (.mcp.json)"})
    installer, _ = make_installer(
        tmp_path, runner, assume_yes=True, environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN}
    )
    installer.install()
    assert not any(c[1:4] == ["mcp", "remove", "open-notebook"] for c in runner.calls)


@pytest.mark.parametrize(
    "status,category",
    [
        ({"category": "bearer", "message": "認證失敗（HTTP 401）"}, "bearer"),
        ({"category": "cf_access", "message": "HTTP 403"}, "cf_access"),
        ({"category": "dns", "message": "ConnectError: getaddrinfo failed"}, "dns"),
    ],
)
def test_self_check_failure_classified(tmp_path, status, category):
    runner = FakeRunner(status=status)
    installer, cap = make_installer(
        tmp_path, runner, assume_yes=True, environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN}
    )
    assert installer.install() == 1
    assert inst.CATEGORY_HINTS[category] in cap.text
    report = next(installer.paths.lv_dir.glob("install-report-*.txt")).read_text(
        "utf-8"
    )
    assert f"失敗（{category}）" in report
    assert FAKE_TOKEN not in cap.text + report


def test_report_redacts_token_even_if_echoed(tmp_path):
    runner = FakeRunner(status={"category": "service", "message": FAKE_TOKEN})
    installer, cap = make_installer(
        tmp_path, runner, assume_yes=True, environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN}
    )
    installer.install()
    assert FAKE_TOKEN not in cap.text


def test_update_only_reinstalls(tmp_path):
    runner = FakeRunner()
    installer, _ = make_installer(
        tmp_path, runner, assume_yes=True, environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN}
    )
    installer.install()
    toml_before = installer.paths.mcp_toml.read_bytes()

    runner2 = FakeRunner()
    upd, cap = make_installer(
        tmp_path,
        runner2,
        home=installer.paths.home,
        kit=installer.kit_dir,
        assume_yes=True,
    )
    assert upd.update() == 0
    kinds = [c[1:3] for c in runner2.calls]
    assert ["pip", "install"] in kinds
    assert not any(k[0] == "venv" or k[:2] == ["mcp", "add"] for k in kinds)
    assert installer.paths.mcp_toml.read_bytes() == toml_before
    assert "/mcp" in cap.text


def test_update_without_venv_fails_with_hint(tmp_path):
    upd, _ = make_installer(tmp_path, FakeRunner(), assume_yes=True)
    with pytest.raises(inst.StepFailed) as exc:
        upd.update()
    assert "完整安裝" in exc.value.resume


def test_rollback_restores_backups(tmp_path):
    installer, cap = make_installer(tmp_path, FakeRunner(), answers=["y"])
    p = installer.paths
    p.claude_json.write_text("new", encoding="utf-8")
    inst.backup_path(p.claude_json).write_text("old", encoding="utf-8")
    p.skill.parent.mkdir(parents=True)
    p.skill.write_text("new-skill", encoding="utf-8")
    inst.backup_path(p.skill).write_text("old-skill", encoding="utf-8")
    assert installer.rollback() == 0
    assert p.claude_json.read_text(encoding="utf-8") == "old"
    assert p.skill.read_text(encoding="utf-8") == "old-skill"
    assert "重開 Claude Code" in cap.text


def test_rollback_with_yes_restores_without_prompt(tmp_path):
    """--rollback --yes 要直接還原，不能因確認題預設否而中止。"""
    installer, _ = make_installer(tmp_path, FakeRunner(), assume_yes=True)
    p = installer.paths
    p.claude_json.write_text("new", encoding="utf-8")
    inst.backup_path(p.claude_json).write_text("old", encoding="utf-8")
    assert installer.rollback() == 0
    assert p.claude_json.read_text(encoding="utf-8") == "old"


def test_rollback_interactive_defaults_to_abort(tmp_path):
    installer, _ = make_installer(tmp_path, FakeRunner(), answers=[""])
    p = installer.paths
    p.claude_json.write_text("new", encoding="utf-8")
    inst.backup_path(p.claude_json).write_text("old", encoding="utf-8")
    with pytest.raises(inst.Abort):
        installer.rollback()
    assert p.claude_json.read_text(encoding="utf-8") == "new"


def test_missing_cf_env_blocks_install_only_when_requested(tmp_path):
    home = make_home(tmp_path, cf=False)
    runner = FakeRunner()
    installer, cap = make_installer(
        tmp_path,
        runner,
        home=home,
        assume_yes=True,
        cf_env_file=home / ".cloudflared" / "pm-token.env",
    )
    with pytest.raises(inst.StepFailed):
        installer.install()
    assert "CF Access 憑證檔不存在" in cap.text
    assert not (home / ".lore-vault").exists()


def test_install_without_cf_omits_cf_key(tmp_path):
    """CF Access 為選配：沒有憑證檔也能裝，mcp.toml 不寫 cf_access_env_file。"""
    import tomllib

    home = make_home(tmp_path, cf=False)
    installer, _ = make_installer(
        tmp_path,
        FakeRunner(),
        home=home,
        assume_yes=True,
        environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN},
    )
    assert installer.install() == 0
    data = tomllib.loads(installer.paths.mcp_toml.read_text(encoding="utf-8"))["mcp"]
    assert "cf_access_env_file" not in data
    assert data["base_url"] == BASE_URL


def test_yes_without_base_url_stops_with_hint(tmp_path):
    installer, _ = make_installer(
        tmp_path,
        FakeRunner(),
        assume_yes=True,
        base_url=None,
        environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN},
    )
    with pytest.raises(inst.StepFailed) as exc:
        installer.install()
    assert "--base-url" in exc.value.resume
    assert not installer.paths.mcp_toml.exists()


def test_main_dry_run_cli(tmp_path, capsys):
    home = make_home(tmp_path)
    kit = make_kit(tmp_path)
    runner = FakeRunner()
    code = inst.main(
        ["--dry-run", "--home", str(home), "--kit-dir", str(kit)],
        runner=runner,
        environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN},
    )
    out = capsys.readouterr().out
    assert code in (0, 1)  # 本機 PATH 可能沒有 uv／claude；重點是不寫檔
    assert not (home / ".lore-vault").exists()
    assert FAKE_TOKEN not in out


def test_self_check_parse_ignores_noise():
    data = inst.parse_self_check('warn\n{"category": "ok", "ok": true}\n')
    assert data["category"] == "ok"
    assert inst.parse_self_check("")["category"] == "internal"


def test_self_check_code_compiles():
    compile(inst.SELF_CHECK_CODE, "<self-check>", "exec")


# ── 自檢程式實跑（本機假服務，不連外）──


SPACE_REQUIRED = {"error": {"code": "space_required", "message": "space 必填"}}


def _status_rule(raw: bytes, status_code: int, body: dict) -> tuple[int, dict]:
    """模擬服務端 `/v1/status`：無 body 是純健康檢查；有 body 就必須帶 space。"""
    if raw.strip():
        try:
            req = json.loads(raw)
        except ValueError:
            req = None
        if not isinstance(req, dict) or "space" not in req:
            return 400, SPACE_REQUIRED
    return status_code, body


def _serve(status_code: int, body: dict):
    import http.server
    import threading

    seen: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.update({k.lower(): v for k, v in self.headers.items()})
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            code, payload = _status_rule(raw, status_code, body)
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _run_self_check(tmp_path, base_url):
    import subprocess

    cfg = tmp_path / "mcp.toml"
    cf = tmp_path / "pm-token.env"
    cf.write_text(
        "CF_ACCESS_CLIENT_ID=cid-secret-value\nCF_ACCESS_CLIENT_SECRET=csec-value\n"
    )
    cfg.write_text(
        inst.render_mcp_toml(
            base_url,
            cf_access_env_file=str(cf).replace("\\", "/"),
            snapshot_dir=str(tmp_path / "snap").replace("\\", "/"),
            timeout=5,
        ),
        encoding="utf-8",
    )
    env = tmp_path / "mcp.env"
    env.write_bytes(inst.render_mcp_env(FAKE_TOKEN))
    argv = [sys.executable, "-c", inst.SELF_CHECK_CODE, str(cfg), str(env)]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={
            k: v
            for k, v in __import__("os").environ.items()
            if not k.startswith(("LORE_VAULT", "CF_ACCESS"))
        },
    )
    return proc


@pytest.mark.parametrize(
    "code,body,category",
    [
        (
            200,
            {
                "ok": True,
                "schema": {"version": 13, "expected": 13},
                "doctor": {"summary": {"pass": 3, "fail": 0}, "checks": []},
            },
            "ok",
        ),
        (401, {"error": {"code": "unauthorized", "message": "x"}}, "bearer"),
        (403, {}, "cf_access"),
        (302, {}, "cf_access"),
        (500, {"error": {"code": "storage_error", "message": "boom"}}, "service"),
        (530, {}, "unreachable"),
    ],
)
def test_self_check_code_against_local_service(tmp_path, code, body, category):
    server, seen = _serve(code, body)
    try:
        proc = _run_self_check(tmp_path, f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    data = inst.parse_self_check(proc.stdout)
    assert data["category"] == category, (proc.stdout, proc.stderr)
    # 請求確實帶 bearer 與 CF header，但輸出不含任何密鑰
    assert seen["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert seen["cf-access-client-id"] == "cid-secret-value"
    for secret in (FAKE_TOKEN, "cid-secret-value", "csec-value"):
        assert secret not in proc.stdout + proc.stderr
    if category == "ok":
        assert data["schema_version"] == 13
        assert data["doctor"] == {"pass": 3, "fail": 0}


def test_self_check_code_connection_refused(tmp_path):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = _run_self_check(tmp_path, f"http://127.0.0.1:{port}")
    assert inst.parse_self_check(proc.stdout)["category"] in ("connect", "unreachable")


# ── 服務位址與 HTTP 模式 ──


def test_normalize_base_url():
    assert inst.normalize_base_url(" https://a.example/mcp/ ") == "https://a.example"
    assert inst.normalize_base_url("http://127.0.0.1:5056/") == "http://127.0.0.1:5056"
    for bad in ("", "vault.example.com", "ftp://a.example"):
        with pytest.raises(inst.StepFailed):
            inst.normalize_base_url(bad)


def test_plaintext_remote_detection():
    assert inst.is_plaintext_remote("http://10.0.0.2:5056")
    assert not inst.is_plaintext_remote("http://127.0.0.1:5056")
    assert not inst.is_plaintext_remote("http://localhost:5056")
    assert not inst.is_plaintext_remote("https://vault.example.com")


def test_mcp_add_http_argv_puts_headers_last():
    # --header 是可變長度選項，放在名稱與網址之前會吞掉位置參數
    argv = inst.mcp_add_http_argv(
        "claude", "https://a.example/mcp", {"Authorization": "Bearer X"}
    )
    assert argv == [
        "claude",
        "mcp",
        "add",
        "--transport",
        "http",
        "-s",
        "user",
        "lore-vault",
        "https://a.example/mcp",
        "--header",
        "Authorization: Bearer X",
    ]


def _ok_status(*_args):
    return {
        "category": "ok",
        "ok": True,
        "schema_version": 14,
        "schema_expected": 14,
        "doctor": {"pass": 1, "fail": 0, "warn": 0, "skipped": 0},
        "doctor_fails": [],
    }


def test_http_mode_full_flow(tmp_path):
    runner = FakeRunner(existing={"open-notebook": "User config"})
    checks: list[tuple[str, dict]] = []

    def fake_check(url, headers, timeout):
        checks.append((url, dict(headers)))
        return _ok_status()

    home = make_home(tmp_path)
    installer, cap = make_installer(
        tmp_path,
        runner,
        home=home,
        assume_yes=True,
        mode="http",
        cf_env_file=home / ".cloudflared" / "pm-token.env",
        environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN},
        http_check=fake_check,
    )
    # HTTP 模式不需要 wheel
    next(installer.kit_dir.glob(inst.WHEEL_GLOB)).unlink()
    assert installer.install() == 0

    add = next(c for c in runner.calls if c[1:3] == ["mcp", "add"])
    assert add[3:5] == ["--transport", "http"]
    assert add[7:9] == ["lore-vault", BASE_URL + "/mcp"]
    assert f"Authorization: Bearer {FAKE_TOKEN}" in add
    assert "CF-Access-Client-Id: cid-secret-value" in add
    assert "CF-Access-Client-Secret: csec-value" in add
    assert ["C:/bin/claude.exe", "mcp", "remove", "open-notebook", "-s", "user"] in (
        runner.calls
    )
    assert not any(c[1] in ("venv", "pip") for c in runner.calls)
    assert checks[0][0] == BASE_URL
    assert checks[0][1]["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert installer.paths.skill.read_bytes() == _skill_lf()
    assert not installer.paths.mcp_toml.exists()

    report = next(installer.paths.lv_dir.glob("install-report-*.txt")).read_text(
        "utf-8"
    )
    assert "HTTP" in report
    # HTTP 模式沒有 wheel，status 行不列 wheel schema
    status_line = next(
        line for line in report.splitlines() if line.startswith("status：")
    )
    assert "wheel" not in status_line
    for blob in (cap.text, report):
        for secret in (FAKE_TOKEN, "cid-secret-value", "csec-value"):
            assert secret not in blob


def test_http_mode_check_failure_stops_before_register(tmp_path):
    runner = FakeRunner()
    installer, _ = make_installer(
        tmp_path,
        runner,
        assume_yes=True,
        mode="http",
        environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN},
        http_check=lambda *a: {"category": "bearer", "message": "HTTP 401"},
    )
    with pytest.raises(inst.StepFailed) as exc:
        installer.install()
    assert inst.CATEGORY_HINTS["bearer"] in exc.value.message
    assert not any(c[1:3] == ["mcp", "add"] for c in runner.calls)


def test_auto_mode_picks_http_without_shell_config(tmp_path):
    installer, _ = make_installer(tmp_path, FakeRunner(), assume_yes=True, mode="auto")
    assert installer.resolve_mode() == "http"
    installer.paths.mcp_toml.parent.mkdir(parents=True)
    installer.paths.mcp_toml.write_text("", encoding="utf-8")
    installer.mode = "auto"
    assert installer.resolve_mode() == "shell"


def test_main_dry_run_http_without_base_url(tmp_path, capsys):
    home = make_home(tmp_path, cf=False)
    kit = make_kit(tmp_path)
    runner = FakeRunner()
    inst.main(
        ["--dry-run", "--home", str(home), "--kit-dir", str(kit)],
        runner=runner,
        environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN},
    )
    out = capsys.readouterr().out
    assert "HTTP" in out
    # 本機 PATH 可能沒有 claude，此時停在環境檢查；有的話 dry-run 只顯示遮蔽後的 header
    assert "Bearer ***" in out or "找不到 claude" in out
    assert FAKE_TOKEN not in out
    assert not (home / ".lore-vault").exists()


# ── HTTP 模式自檢實跑（本機假服務，不連外）──


def _serve_http(status_code: int, body: dict, location: str | None = None):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            code, payload = _status_rule(raw, status_code, body)
            data = json.dumps(payload).encode()
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # 自檢若跟隨轉址，登入頁會回 200；用來證明沒有跟過去
        def do_GET(self):  # noqa: N802
            data = b"<html>login</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize(
    "code,body,location,category",
    [
        (
            200,
            {
                "ok": True,
                "schema": {"version": 14, "expected": 14},
                "doctor": {"summary": {"pass": 2}, "checks": []},
            },
            None,
            "ok",
        ),
        (401, {"error": {"code": "unauthorized", "message": "x"}}, None, "bearer"),
        (302, {}, "/login", "cf_access"),
        (403, {}, None, "cf_access"),
        (530, {}, None, "unreachable"),
        (500, {"error": {"code": "storage_error", "message": "boom"}}, None, "service"),
    ],
)
def test_http_self_check_against_local_service(code, body, location, category):
    server = _serve_http(code, body, location)
    try:
        data = inst.http_self_check(
            f"http://127.0.0.1:{server.server_port}",
            {"Authorization": f"Bearer {FAKE_TOKEN}"},
            5,
        )
    finally:
        server.shutdown()
    assert data["category"] == category, data
    assert FAKE_TOKEN not in json.dumps(data, ensure_ascii=False)
    if category == "ok":
        assert data["schema_version"] == 14


def test_http_self_check_connection_refused():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data = inst.http_self_check(f"http://127.0.0.1:{port}", {}, 5)
    assert data["category"] in ("connect", "unreachable")


# ── episode hook（D13）──


USER_HOOKS = {
    "SessionStart": [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": "python persona.py"}],
        }
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": "notify-send done"}],
        }
    ],
}


def _user_settings() -> dict:
    return {"model": "opus", "hooks": json.loads(json.dumps(USER_HOOKS))}


def _ours(settings: dict, root: Path) -> list[tuple[str, dict]]:
    return [
        (event, hook)
        for event, hook in inst._iter_hooks(settings.get("hooks", {}))
        if inst.hook_points_under(hook, root)
    ]


def _episode_installer(tmp_path, *, mode="http", check=None, runner=None, **kw):
    checks: list[tuple[str, dict]] = []

    def fake_check(url, headers, timeout):
        checks.append((url, dict(headers)))
        return check or {"category": "ok"}

    kw.setdefault("environ", {"LORE_VAULT_API_TOKEN": FAKE_TOKEN})
    installer, cap = make_installer(
        tmp_path,
        runner or FakeRunner(),
        assume_yes=True,
        mode=mode,
        episodes=True,
        http_check=_ok_status,
        episode_check=fake_check,
        **kw,
    )
    return installer, cap, checks


def test_render_client_env_is_read_by_hook_settings(tmp_path):
    from lore_vault.hooks.client_env import load_client_settings

    snap = tmp_path / "snapshot" / "concepts.json"
    data = inst.render_client_env(
        BASE_URL + "/",
        FAKE_TOKEN,
        {"CF_ACCESS_CLIENT_ID": "cid", "CF_ACCESS_CLIENT_SECRET": "csec"},
        snap,
    )
    assert not data.startswith(b"\xef\xbb\xbf")
    path = tmp_path / "client.env"
    path.write_bytes(data)
    settings = load_client_settings(path, environ={})
    assert settings.push_configured, settings.describe()
    assert settings.url == BASE_URL
    assert settings.token.reveal() == FAKE_TOKEN
    assert [s.reveal() for s in settings.cf_access] == ["cid", "csec"]
    assert settings.concept_snapshot == snap
    plain = inst.render_client_env(BASE_URL, FAKE_TOKEN)
    assert b"CF_ACCESS" not in plain and b"CONCEPT_SNAPSHOT" not in plain
    with pytest.raises(inst.StepFailed):
        inst.render_client_env(BASE_URL, "bad\ntoken")


def test_merge_hooks_keeps_user_hooks_and_is_idempotent(tmp_path):
    root = tmp_path / ".lore-vault" / "hooks"
    wanted = [
        ("Stop", "", inst.hook_entry("C:/sys/python.exe", root / inst.STOP_SCRIPT)),
        (
            "PreToolUse",
            inst.PRETOOLUSE_MATCHER,
            inst.hook_entry("C:/sys/python.exe", root / inst.PRETOOLUSE_SCRIPT),
        ),
    ]
    original = _user_settings()
    once = inst.merge_hooks(original, root, wanted)
    twice = inst.merge_hooks(once, root, wanted)
    assert once == twice
    assert original == _user_settings()  # 不改傳入物件
    assert once["model"] == "opus"
    assert once["hooks"]["SessionStart"] == USER_HOOKS["SessionStart"]
    # 使用者原本的 Stop 群組原樣保留，我們的另成一組
    assert once["hooks"]["Stop"][0] == USER_HOOKS["Stop"][0]
    assert once["hooks"]["Stop"][1] == {"matcher": "", "hooks": [wanted[0][2]]}
    assert once["hooks"]["PreToolUse"] == [
        {"matcher": inst.PRETOOLUSE_MATCHER, "hooks": [wanted[1][2]]}
    ]
    assert len(_ours(twice, root)) == 2
    # Python 路徑變了：舊登記被換掉，不會兩份並存
    moved = inst.merge_hooks(
        once,
        root,
        [("Stop", "", inst.hook_entry("D:/py/python.exe", root / inst.STOP_SCRIPT))],
    )
    ours = _ours(moved, root)
    assert [(e, h["command"]) for e, h in ours] == [("Stop", "D:/py/python.exe")]
    assert "PreToolUse" not in moved["hooks"]
    # 全部移除：回到原狀
    assert inst.merge_hooks(once, root, []) == _user_settings()


def test_remove_hooks_keeps_mixed_group_members(tmp_path):
    root = tmp_path / "hooks"
    ours = inst.hook_entry("py", root / inst.STOP_SCRIPT)
    theirs = {"type": "command", "command": "echo hi"}
    hooks = {"Stop": [{"matcher": "", "hooks": [theirs, ours]}]}
    assert inst.remove_hooks_under(hooks, root) == {
        "Stop": [{"matcher": "", "hooks": [theirs]}]
    }
    # 前綴相似的其他目錄不算我們的
    sibling = inst.hook_entry("py", tmp_path / "hooks2" / "hook_stop.py")
    assert not inst.hook_points_under(sibling, root)


def test_foreign_registration_detects_host_hook(tmp_path):
    root = tmp_path / ".lore-vault" / "hooks"
    hooks = {
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "C:\\py\\python.exe",
                        "args": ["C:\\repo\\agent_memory_spike\\hook_stop.py"],
                    }
                ],
            }
        ]
    }
    assert inst.foreign_registrations(hooks, root, "hook_stop.py") == ["Stop"]
    assert inst.foreign_registrations(hooks, root, "hook_pretooluse.py") == []
    ours = inst.merge_hooks(
        {}, root, [("Stop", "", inst.hook_entry("py", root / inst.STOP_SCRIPT))]
    )["hooks"]
    assert inst.foreign_registrations(ours, root, "hook_stop.py") == []


def test_http_mode_installs_stop_hook_only(tmp_path):
    installer, cap, checks = _episode_installer(tmp_path)
    p = installer.paths
    p.settings_json.parent.mkdir(parents=True)
    original = json.dumps(_user_settings(), indent=4)
    p.settings_json.write_text(original, encoding="utf-8")

    assert installer.install() == 0

    # hook 檔案照 manifest 複製
    for rel, data in HOOK_FILES.items():
        assert (p.hooks_dir / rel).read_bytes() == data
    assert (p.hooks_dir / "VERSION.json").is_file()
    # client.env：UTF-8 無 BOM、只有服務位址與 token（HTTP 模式沒有快照鍵）
    raw = p.client_env.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert f"LORE_VAULT_URL={BASE_URL}\n" in text
    assert f"LORE_VAULT_API_TOKEN={FAKE_TOKEN}\n" in text
    assert "CONCEPT_SNAPSHOT" not in text and "CF_ACCESS" not in text
    if os.name != "nt":
        assert stat.S_IMODE(p.client_env.stat().st_mode) == 0o600
    # settings.json：先備份原檔，只合併 Stop，使用者其他設定保留
    assert inst.backup_path(p.settings_json).read_text(encoding="utf-8") == original
    merged = json.loads(p.settings_json.read_text(encoding="utf-8"))
    assert merged["model"] == "opus"
    assert merged["hooks"]["SessionStart"] == USER_HOOKS["SessionStart"]
    ours = _ours(merged, p.hooks_dir)
    assert [e for e, _ in ours] == ["Stop"]
    assert ours[0][1] == {
        "type": "command",
        "command": "C:/sys/python.exe",
        "args": [str(p.hooks_dir / "spike" / "hook_stop.py")],
        "timeout": 10,
    }
    # 收料檢查帶 bearer
    assert checks == [(BASE_URL, {"Authorization": f"Bearer {FAKE_TOKEN}"})]
    report = next(p.lv_dir.glob("install-report-*.txt")).read_text("utf-8")
    assert "episode hook：Stop" in report
    assert "收料：服務已開啟" in report
    for blob in (cap.text, report):
        assert FAKE_TOKEN not in blob


def test_episode_rerun_is_idempotent_and_keeps_first_backup(tmp_path):
    installer, _, _ = _episode_installer(tmp_path)
    p = installer.paths
    p.settings_json.parent.mkdir(parents=True)
    p.settings_json.write_text(json.dumps(_user_settings()), encoding="utf-8")
    assert installer.install() == 0
    bak = inst.backup_path(p.settings_json)
    bak.write_text("ORIGINAL", encoding="utf-8")
    after_first = p.settings_json.read_bytes()
    env_first = p.client_env.read_bytes()

    again, cap, _ = _episode_installer(tmp_path, home=p.home, kit=installer.kit_dir)
    assert again.install() == 0
    assert p.settings_json.read_bytes() == after_first
    assert bak.read_text(encoding="utf-8") == "ORIGINAL"
    assert p.client_env.read_bytes() == env_first
    merged = json.loads(p.settings_json.read_text(encoding="utf-8"))
    assert len(_ours(merged, p.hooks_dir)) == 1
    statuses = {r.name: r.status for r in again.records}
    assert statuses["episode hook 檔案"] == "skip"
    assert statuses["寫 client.env"] == "skip"
    assert statuses["登記 hook"] == "skip"

    # 系統 Python 換了：登記更新（仍只有一筆），最早的備份不被覆蓋
    moved = FakeRunner(hook_python={"C:/py/python.exe": _probe(executable="D:/py.exe")})
    third, _, _ = _episode_installer(
        tmp_path, runner=moved, home=p.home, kit=installer.kit_dir
    )
    assert third.install() == 0
    merged = json.loads(p.settings_json.read_text(encoding="utf-8"))
    assert [h["command"] for _, h in _ours(merged, p.hooks_dir)] == ["D:/py.exe"]
    assert bak.read_text(encoding="utf-8") == "ORIGINAL"


def test_new_kit_replaces_installed_hooks(tmp_path):
    installer, _, _ = _episode_installer(tmp_path)
    assert installer.install() == 0
    p = installer.paths
    (p.hooks_dir / "spike" / "stale.py").write_text("x", encoding="utf-8")
    kit2 = tmp_path / "kit2"
    kit2.mkdir()
    (kit2 / "SKILL.md").write_bytes(SKILL.read_bytes())
    make_hooks(kit2, {**HOOK_FILES, "spike/hook_stop.py": b"# v2\n"}, commit="def5678")
    again, _, _ = _episode_installer(tmp_path, home=p.home, kit=kit2)
    assert again.install() == 0
    assert (p.hooks_dir / "spike" / "hook_stop.py").read_bytes() == b"# v2\n"
    # 整目錄換上：舊版殘留的檔案不留
    assert not (p.hooks_dir / "spike" / "stale.py").exists()
    assert not p.hooks_dir.with_name("hooks.new").exists()
    assert not p.hooks_dir.with_name("hooks.old").exists()


def test_shell_mode_registers_pretooluse_with_concept_snapshot(tmp_path):
    installer, _, checks = _episode_installer(tmp_path, mode="shell")
    home = installer.paths.home
    cf = home / ".cloudflared" / "pm-token.env"
    installer.cf_env_file = cf
    assert installer.install() == 0
    p = installer.paths
    text = p.client_env.read_text(encoding="utf-8")
    assert f"LORE_VAULT_CONCEPT_SNAPSHOT={p.concept_snapshot.as_posix()}" in text
    assert "CF_ACCESS_CLIENT_ID=cid-secret-value" in text
    merged = json.loads(p.settings_json.read_text(encoding="utf-8"))
    ours = _ours(merged, p.hooks_dir)
    assert sorted(e for e, _ in ours) == ["PreToolUse", "Stop"]
    group = merged["hooks"]["PreToolUse"][0]
    assert group["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    assert checks[0][1]["CF-Access-Client-Id"] == "cid-secret-value"
    # 安裝前沒有 settings.json：沒有備份可言
    assert not inst.backup_path(p.settings_json).exists()


def test_shell_mode_reuses_kept_token_and_toml(tmp_path):
    """第二次只加裝 hook：token 取自既有 mcp.env、服務位址取自既有 mcp.toml。"""
    first, _ = make_installer(
        tmp_path,
        FakeRunner(),
        assume_yes=True,
        environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN},
    )
    assert first.install() == 0
    p = first.paths
    assert not p.client_env.exists()
    again, cap, checks = _episode_installer(
        tmp_path,
        mode="shell",
        home=p.home,
        kit=first.kit_dir,
        environ={},
        base_url=None,
    )
    assert again.install() == 0
    text = p.client_env.read_text(encoding="utf-8")
    assert f"LORE_VAULT_URL={BASE_URL}\n" in text
    assert f"LORE_VAULT_API_TOKEN={FAKE_TOKEN}\n" in text
    assert checks[0][1]["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert FAKE_TOKEN not in cap.text


def test_ingest_disabled_is_hint_not_failure(tmp_path):
    installer, cap, _ = _episode_installer(
        tmp_path, check={"category": "disabled", "http_status": 403}
    )
    assert installer.install() == 0
    rec = next(r for r in installer.records if r.name == "收料檢查")
    assert rec.status == "skip"
    assert "服務未開啟收料（UI 設定頁可開）" in cap.text
    report = next(installer.paths.lv_dir.glob("install-report-*.txt")).read_text(
        "utf-8"
    )
    assert "UI 設定頁可開" in report
    # hook 照樣裝好，紀錄會留在本機 spool 等服務開啟
    assert installer.paths.client_env.exists()


def test_ingest_check_auth_failure_marks_install_failed(tmp_path):
    installer, cap, _ = _episode_installer(
        tmp_path, check={"category": "bearer", "message": "HTTP 401"}
    )
    assert installer.install() == 1
    assert inst.CATEGORY_HINTS["bearer"] in cap.text


def test_no_episodes_touches_nothing(tmp_path):
    installer, cap = make_installer(
        tmp_path,
        FakeRunner(),
        assume_yes=True,
        mode="http",
        episodes=False,
        http_check=_ok_status,
        environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN},
    )
    assert installer.install() == 0
    p = installer.paths
    assert not p.hooks_dir.exists()
    assert not p.client_env.exists()
    assert not p.settings_json.exists()
    assert "未安裝（加 --episodes 可安裝）" in cap.text


def test_yes_defaults_to_no_episodes(tmp_path):
    runner = FakeRunner()
    installer, _ = make_installer(
        tmp_path,
        runner,
        assume_yes=True,
        mode="http",
        http_check=_ok_status,
        environ={"LORE_VAULT_API_TOKEN": FAKE_TOKEN},
    )
    assert installer.install() == 0
    assert installer.episodes is False
    assert not installer.paths.client_env.exists()
    # 沒選 hook 就不探測 Python
    assert not any(inst.HOOK_PYTHON_PROBE in c for c in runner.calls)


@pytest.mark.parametrize(
    "found",
    [
        {},  # 什麼都找不到
        {"C:/py/python.exe": _probe(version=(3, 10, 4))},  # 版本太舊
        {"C:/py/python.exe": _probe(venv=True)},  # venv 不算系統 Python
    ],
)
def test_no_system_python_stops_before_any_write(tmp_path, found):
    home = make_home(tmp_path)
    before = _snapshot_tree(home)
    runner = FakeRunner(hook_python=found)
    installer, cap, _ = _episode_installer(tmp_path, runner=runner, home=home)
    with pytest.raises(inst.StepFailed):
        installer.install()
    assert "找不到合格的系統 Python" in cap.text
    assert "--no-episodes" in cap.text
    assert _snapshot_tree(home) == before
    assert not any(c[1:3] == ["mcp", "add"] for c in runner.calls)


def test_hook_python_prefers_base_over_venv_and_uses_reported_executable(tmp_path):
    runner = FakeRunner(
        hook_python={
            "C:/base/python.exe": _probe(executable="C:/Real/python.exe"),
            "C:/bin/python.exe": _probe(),
        }
    )
    installer, _, _ = _episode_installer(
        tmp_path,
        runner=runner,
        python_in_venv=True,
        base_python="C:/base/python.exe",
    )
    assert installer.detect_hook_python() == "C:/Real/python.exe"
    # 安裝器自己的 venv python 不在候選內
    assert "C:/py/python.exe" not in installer.hook_python_candidates()


def test_explicit_hook_python_allows_venv_but_checks_version(tmp_path):
    runner = FakeRunner(hook_python={"D:/v/python.exe": _probe(venv=True)})
    installer, _, _ = _episode_installer(
        tmp_path, runner=runner, hook_python="D:/v/python.exe"
    )
    assert installer.detect_hook_python() == "C:/sys/python.exe"
    runner.hook_python["D:/v/python.exe"] = _probe(version=(3, 11, 0))
    assert installer.detect_hook_python() is None


def test_invalid_settings_json_is_not_overwritten(tmp_path):
    home = make_home(tmp_path)
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ // 使用者手寫註解\n}", encoding="utf-8")
    installer, cap, _ = _episode_installer(tmp_path, home=home)
    with pytest.raises(inst.StepFailed):
        installer.install()
    assert settings.read_text(encoding="utf-8") == "{ // 使用者手寫註解\n}"
    assert "不會覆寫" in cap.text


def test_kit_without_hooks_blocks_episode_install(tmp_path):
    kit = make_kit(tmp_path)
    import shutil

    shutil.rmtree(kit / "hooks")
    installer, cap, _ = _episode_installer(tmp_path, kit=kit)
    with pytest.raises(inst.StepFailed):
        installer.install()
    assert "VERSION.json" in cap.text


def test_tampered_kit_hook_is_rejected(tmp_path):
    kit = make_kit(tmp_path)
    (kit / "hooks" / "spike" / "hook_stop.py").write_bytes(b"# changed\n")
    with pytest.raises(inst.StepFailed, match="內容不符"):
        inst.load_hooks_manifest(kit / "hooks")


def test_host_with_existing_spike_hooks_is_not_double_registered(tmp_path):
    home = make_home(tmp_path)
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    host = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "C:/py/python.exe",
                            "args": ["C:/repo/agent_memory_spike/hook_stop.py"],
                        }
                    ],
                }
            ]
        }
    }
    settings.write_text(json.dumps(host), encoding="utf-8")
    installer, cap, _ = _episode_installer(tmp_path, home=home)
    assert installer.install() == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == host
    assert "不重複登記" in cap.text


def test_dry_run_with_episodes_writes_nothing(tmp_path):
    home = make_home(tmp_path)
    before = _snapshot_tree(home)
    installer, cap, checks = _episode_installer(tmp_path, home=home, dry_run=True)
    assert installer.install() == 0
    assert _snapshot_tree(home) == before
    assert checks == []
    assert "空批次" in cap.text


def test_rollback_restores_settings_backup(tmp_path):
    installer, _, _ = _episode_installer(tmp_path)
    p = installer.paths
    p.settings_json.parent.mkdir(parents=True)
    p.settings_json.write_text(json.dumps(_user_settings()), encoding="utf-8")
    original = p.settings_json.read_bytes()
    assert installer.install() == 0
    assert p.settings_json.read_bytes() != original

    rb, cap = make_installer(
        tmp_path, FakeRunner(), home=p.home, kit=installer.kit_dir, assume_yes=True
    )
    assert rb.rollback() == 0
    assert p.settings_json.read_bytes() == original
    assert "settings.json 也會整份還原" in cap.text
    assert "client.env 含 token" in cap.text


def test_rollback_without_backup_strips_only_our_hooks(tmp_path):
    installer, _, _ = _episode_installer(tmp_path, mode="shell")
    assert installer.install() == 0
    p = installer.paths
    # 安裝後使用者又加了自己的 hook
    data = json.loads(p.settings_json.read_text(encoding="utf-8"))
    data["hooks"].setdefault("Stop", []).append(USER_HOOKS["Stop"][0])
    data["theme"] = "dark"
    p.settings_json.write_text(json.dumps(data), encoding="utf-8")

    rb, _ = make_installer(
        tmp_path, FakeRunner(), home=p.home, kit=installer.kit_dir, assume_yes=True
    )
    assert rb.rollback() == 0
    left = json.loads(p.settings_json.read_text(encoding="utf-8"))
    assert left == {"hooks": {"Stop": USER_HOOKS["Stop"]}, "theme": "dark"}


def test_cli_episode_flags():
    parser = inst.build_parser()
    assert parser.parse_args([]).episodes is None
    assert parser.parse_args(["--episodes"]).episodes is True
    assert parser.parse_args(["--no-episodes"]).episodes is False
    with pytest.raises(SystemExit):
        parser.parse_args(["--episodes", "--no-episodes"])
    args = parser.parse_args(["--hook-python", "C:/py/python.exe"])
    assert args.hook_python == "C:/py/python.exe"


# ── 收料開關檢查實跑（本機假服務，不連外）──


def _serve_episodes(code: int, body: dict):
    import http.server
    import threading

    seen: list[bytes] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.mark.parametrize(
    "code,body,category",
    [
        (200, {"accepted": 0, "duplicates": 0, "results": []}, "ok"),
        (
            403,
            {"error": {"code": "episode_ingest_disabled", "message": "未開啟"}},
            "disabled",
        ),
        (403, {}, "cf_access"),
        (401, {"error": {"code": "unauthorized", "message": "x"}}, "bearer"),
        (200, {"unexpected": True}, "service"),
    ],
)
def test_episode_ingest_check_sends_empty_batch(code, body, category):
    server, seen = _serve_episodes(code, body)
    try:
        data = inst.episode_ingest_check(
            f"http://127.0.0.1:{server.server_port}",
            {"Authorization": f"Bearer {FAKE_TOKEN}"},
            5,
        )
    finally:
        server.shutdown()
    assert data["category"] == category, data
    # 只送空批次：不會在正式服務寫入假 episode
    assert [json.loads(b) for b in seen] == [{"episodes": []}]
    assert FAKE_TOKEN not in json.dumps(data, ensure_ascii=False)
