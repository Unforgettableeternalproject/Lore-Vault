"""doctor `concept_scope.anchor_agreement`：scope 與自己的 anchors 是否一致。

2026-10-02 事故：蒸餾把一組跨兩個子專案的候選的 scope 都填成不存在的
上位名稱 'JSAI'，anchors 卻明確指向 JSAI-Functions／JSAI-API，推送整批
被服務端以 vault_unresolved 拒收。這裡驗證「拿掉這項檢查」會讓事故重現時
看起來一切正常（doctor 全綠）——也就是這項檢查本身的保護效力。

全部在 tmp_path 建 spike 資料目錄，不碰 ~/.lore-vault。
"""

from __future__ import annotations

import json
from pathlib import Path

from lore_vault.doctor import DoctorContext, Status, default_registry


def _home(tmp_path: Path, concepts: list[dict]) -> Path:
    home = tmp_path / "spike"
    home.mkdir()
    (home / "concepts.json").write_text(json.dumps(concepts), encoding="utf-8")
    return home


def _check(settings: dict):
    report = default_registry().run(
        DoctorContext(settings=settings), categories=["concept_scope"]
    )
    (outcome,) = [o for o in report.outcomes if o.name == "concept_scope.anchor_agreement"]
    return outcome.result


def test_registered_under_its_own_category():
    names = {c.name: c.category for c in default_registry().checks}
    assert names["concept_scope.anchor_agreement"] == "concept_scope"


def test_pass_when_scope_matches_anchor_head(tmp_path):
    home = _home(
        tmp_path,
        [{"id": "c-1", "scope": "JSAI-API", "anchors": ["JSAI-API/src/x.ts"]}],
    )
    result = _check({"spike_home": str(home)})
    assert result.status is Status.PASS, result.summary
    assert result.counts == {"checked": 1, "mismatched": 0}


def test_pass_when_scope_is_none_or_missing(tmp_path):
    """None／沒有 scope 是明確表態為通用，不是這項檢查的範圍。"""
    home = _home(
        tmp_path,
        [
            {"id": "c-1", "scope": None, "anchors": ["JSAI-API/src/x.ts"]},
            {"id": "c-2", "anchors": ["JSAI-API/src/x.ts"]},
        ],
    )
    result = _check({"spike_home": str(home)})
    assert result.status is Status.PASS
    assert result.counts["checked"] == 0


def test_pass_when_anchors_have_no_path(tmp_path):
    """anchors 沒有路徑型（函式名、欄位名）時無法驗證，不誤判。"""
    home = _home(
        tmp_path,
        [{"id": "c-1", "scope": "Eternity", "anchors": ["container.replace"]}],
    )
    assert _check({"spike_home": str(home)}).status is Status.PASS


# JSAI-Functions／JSAI-API 要被當作「已知合法 vault」，池子裡至少要有兩個
# 不同候選組（source_candidate）掛過各自的名字——否則同一次錯誤產出的兩條
# 重複 scope 會互相撐出一個假的「共識」。這裡的其他正常記錄就是在撐這個門檻。
_ESTABLISHED_SUBPROJECTS = [
    {"id": "c-1", "scope": "JSAI-Functions", "source_candidate": "cand-a",
     "anchors": ["JSAI-Functions/src/old.ts"]},
    {"id": "c-2", "scope": "JSAI-Functions", "source_candidate": "cand-b",
     "anchors": ["JSAI-Functions/src/other.ts"]},
    {"id": "c-3", "scope": "JSAI-API", "source_candidate": "cand-c",
     "anchors": ["JSAI-API/src/old.ts"]},
    {"id": "c-4", "scope": "JSAI-API", "source_candidate": "cand-d",
     "anchors": ["JSAI-API/src/other.ts"]},
]


def test_regression_jsai_incident_is_red(tmp_path):
    """根因回歸測試：2026-10-02 事故的原始形狀（scope='JSAI'、
    anchors 指向 JSAI-Functions／JSAI-API）必須被這項檢查擋下。"""
    home = _home(
        tmp_path,
        [
            *_ESTABLISHED_SUBPROJECTS,
            {
                "id": "c-1935",
                "scope": "JSAI",
                "source_candidate": "c-47540ee28590",
                "anchors": [
                    "JSAI-Functions/src/functions/inspection-fill-reminder.ts",
                    "JSAI-Functions/src/functions/inspection-overdue-reminder.ts",
                ],
            },
            {
                "id": "c-1936",
                "scope": "JSAI",
                "source_candidate": "c-47540ee28590",
                "anchors": [
                    "JSAI-API/src/routes/departments/departments.ts",
                    "container.replace",
                    "patch",
                ],
            },
        ],
    )
    result = _check({"spike_home": str(home)})
    assert result.status is Status.FAIL
    assert result.counts == {"checked": 6, "mismatched": 2}
    assert any("c-1935" in d and "JSAI-Functions" in d for d in result.details)
    assert any("c-1936" in d and "JSAI-API" in d for d in result.details)


def test_single_candidate_scope_is_not_treated_as_established(tmp_path):
    """同一次錯誤在同一組候選裡產出兩條重複 scope，不該互相撐出『共識』。

    若單純看 scope 出現次數（不看是不是不同候選組），這個事故本身的兩條
    'JSAI' 會互相印證成『已知名稱』，這項檢查就會對自己要擋的事故視而不見。
    """
    home = _home(
        tmp_path,
        [
            {
                "id": "c-1935",
                "scope": "JSAI",
                "source_candidate": "c-47540ee28590",
                "anchors": ["JSAI-Functions/a.ts"],
            },
            {
                "id": "c-1936",
                "scope": "JSAI",
                "source_candidate": "c-47540ee28590",
                "anchors": ["JSAI-API/b.ts"],
            },
        ],
    )
    result = _check({"spike_home": str(home)})
    # 'JSAI-Functions'／'JSAI-API' 都只各出現一次、不構成已知名單，
    # 所以這裡不誤判（但真正的事故資料──anchors 本身就跨兩個子專案
    # 時歧義——見 test_ambiguous_anchors_do_not_false_positive）
    assert result.status is Status.PASS
    assert result.counts == {"checked": 2, "mismatched": 0}


def test_unestablished_anchor_name_does_not_false_positive(tmp_path):
    """anchor 開頭段落只是個目錄名（例如 'workers'），沒有被當作已知 vault
    用過兩次以上——不當成證據，不誤判為不一致。"""
    home = _home(
        tmp_path,
        [
            {"id": "c-1", "scope": "Eternity", "source_candidate": "cand-a",
             "anchors": ["Eternity/src/x.ts"]},
            {"id": "c-2", "scope": "Eternity", "source_candidate": "cand-b",
             "anchors": ["workers/queue.ts"]},
        ],
    )
    result = _check({"spike_home": str(home)})
    assert result.status is Status.PASS
    assert result.counts == {"checked": 2, "mismatched": 0}


def test_ambiguous_anchors_do_not_false_positive(tmp_path):
    """anchors 本身跨兩個子專案（無法判定哪個對）時不誤判為不一致。"""
    home = _home(
        tmp_path,
        [
            {
                "id": "c-1",
                "scope": "JSAI",
                "anchors": ["JSAI-Functions/a.ts", "JSAI-API/b.ts"],
            }
        ],
    )
    result = _check({"spike_home": str(home)})
    assert result.status is Status.PASS
    assert result.counts == {"checked": 1, "mismatched": 0}


def test_unreadable_concepts_is_red(tmp_path):
    home = tmp_path / "spike"
    home.mkdir()
    (home / "concepts.json").write_text("{not json", encoding="utf-8")
    assert _check({"spike_home": str(home)}).status is Status.FAIL


def test_skipped_without_settings_or_data(tmp_path):
    assert _check({}).status is Status.SKIPPED
    assert _check({"spike_home": str(tmp_path / "missing")}).status is Status.SKIPPED


def test_spool_dir_parent_is_the_fallback_home(tmp_path):
    home = _home(
        tmp_path,
        [
            *_ESTABLISHED_SUBPROJECTS,
            {"id": "c-1935", "scope": "JSAI", "source_candidate": "c-47540ee28590",
             "anchors": ["JSAI-Functions/a.ts"]},
        ],
    )
    result = _check({"spool_dir": str(home / "spool")})
    assert result.status is Status.FAIL
