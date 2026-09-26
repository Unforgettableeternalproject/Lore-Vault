"""MCP `upload` 的 Windows 路徑形式檢查（磁碟代號相對路徑、根目錄相對、UNC、
裝置前綴、NTFS 替代資料流）。

`windows_path_problem` 是純函式（`PureWindowsPath`），任何平台都測；
`resolve_upload_path` 的實際解析只在 Windows 上測。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import lore_vault.mcp.upload as upload_mod
from lore_vault.mcp.upload import (
    UploadPathError,
    read_upload,
    resolve_upload_path,
    windows_path_problem,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows 路徑語意")


@pytest.mark.parametrize(
    "raw",
    [
        "C:foo.txt",
        "c:docs\\a.md",
        "\\foo.txt",
        "/foo.txt",
        "\\\\server\\share\\a.md",
        "//server/share/a.md",
        "\\\\?\\C:\\docs\\a.md",
        "\\\\.\\C:\\docs\\a.md",
        "//?/C:/docs/a.md",
        "\\\\.\\PhysicalDrive0",
        "file.txt:stream",
        "docs\\a.md::$DATA",
        "C:\\docs\\a.md:secret",
    ],
)
def test_windows_problem_forms(raw):
    assert windows_path_problem(raw) is not None


@pytest.mark.parametrize(
    "raw", ["C:\\docs\\a.md", "C:/docs/a.md", "docs\\a.md", "a.md", "~\\a.md"]
)
def test_windows_ordinary_forms_pass(raw):
    assert windows_path_problem(raw) is None


def test_gate_follows_platform():
    # POSIX 絕對路徑在 PureWindowsPath 下是「有 root 無 drive」，
    # 所以容器（Linux）上必須關閉這組檢查
    assert windows_path_problem("/home/user/a.md") is not None
    assert upload_mod.WINDOWS_PATHS == (os.name == "nt")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "設定.md").write_text("# 設定\n內容", encoding="utf-8")
    return root


def _rejected(raw: str, project: Path) -> str:
    with pytest.raises(UploadPathError) as info:
        resolve_upload_path(raw, [project], cwd=str(project))
    return info.value.code


@windows_only
def test_drive_relative_is_rejected(project):
    drive = Path(project).drive
    assert _rejected(f"{drive}docs\\設定.md", project) == "path_not_allowed"


@windows_only
def test_root_relative_without_drive_is_rejected(project):
    no_drive = str(project / "docs" / "設定.md")[len(Path(project).drive) :]
    assert no_drive.startswith("\\")
    assert _rejected(no_drive, project) == "path_not_allowed"


@windows_only
@pytest.mark.parametrize("prefix", ["\\\\?\\", "\\\\.\\"])
def test_device_prefixes_are_rejected(project, prefix):
    raw = prefix + str(project / "docs" / "設定.md")
    assert _rejected(raw, project) == "path_not_allowed"


@windows_only
def test_unc_is_rejected(project):
    target = project / "docs" / "設定.md"
    drive = Path(project).drive.rstrip(":")
    raw = f"\\\\localhost\\{drive}$" + str(target)[2:]
    assert _rejected(raw, project) == "path_not_allowed"


@windows_only
def test_alternate_data_stream_is_rejected(project):
    target = project / "docs" / "設定.md"
    try:
        with open(str(target) + ":hidden", "w", encoding="utf-8") as fh:
            fh.write("藏在替代資料流的內容")
    except OSError as exc:  # 非 NTFS
        pytest.skip(f"無法建立替代資料流：{exc}")
    for raw in (
        str(target) + ":hidden",
        "docs\\設定.md:hidden",
        "docs/設定.md::$DATA",
    ):
        assert _rejected(raw, project) == "path_not_allowed"


@windows_only
def test_ordinary_paths_still_resolve(project):
    target = (project / "docs" / "設定.md").resolve()
    for raw in ("docs\\設定.md", "docs/設定.md", str(target)):
        assert resolve_upload_path(raw, [project], cwd=str(project)) == target
    local = read_upload("docs/設定.md", [project], cwd=str(project), max_bytes=1024)
    assert local.name == "設定.md"


def test_posix_absolute_path_passes_when_gate_off(project, monkeypatch):
    """容器（gate 關閉）上一般絕對路徑照常解析。"""
    monkeypatch.setattr(upload_mod, "WINDOWS_PATHS", False)
    target = (project / "docs" / "設定.md").resolve()
    assert resolve_upload_path(str(target), [project], cwd="/") == target
