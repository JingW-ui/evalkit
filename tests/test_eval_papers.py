#!/usr/bin/env python3
"""test_eval_papers.py — 题库试卷化：papers 装载 / tasks 新字段 / 置信下界 / veto / 答辩 invalid。"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_store import EvalStore
from task_gen import load_papers


def _tmp_db():
    return os.path.join(tempfile.mkdtemp(), "t.db")


def test_load_papers():
    ts = load_papers()
    assert len(ts) == 23, f"expected 23, got {len(ts)}"
    by_id = {t["task_id"]: t for t in ts}
    assert "L3_g66_deploy" in by_id
    assert by_id["L3_g66_deploy"]["expected_answer"]["result"]  # result 必填
    assert by_id["L4_lock_screen"]["accept_criteria"]["veto"] is True
    assert by_id["L4_lock_screen"]["prep"]  # 锁屏题带 prep
    print("load_papers OK")


def test_roundtrip_new_fields():
    s = EvalStore(_tmp_db())
    try:
        n = len(s.import_papers())
        assert n == 23, n
        t = s.get_task("L3_g66_deploy")
        assert isinstance(t["expected_answer"], dict)
        assert isinstance(t["tools_required"], list)
        assert isinstance(t["accept_criteria"], dict)
        assert isinstance(t["success_condition"], dict)
        assert t["enabled"] == 1
        print("import+roundtrip OK")
    finally:
        s.close()


def test_wilson():
    lo5 = EvalStore._wilson_lower(5, 5)
    assert lo5 is not None and 0.55 < lo5 < 0.58, lo5
    assert EvalStore._wilson_lower(0, 0) is None
    print("wilson OK")


def test_stats_ci_and_veto():
    s = EvalStore(_tmp_db())
    try:
        s.import_papers()
        # g66 L3（1 成功 1 失败）→ n=2, sr=0.5, veto=False（L3 非 veto）
        for i, ok in enumerate((True, False)):
            s.upsert({
                "session_id": f"s_{i}", "task_id": "L3_g66_deploy", "run_idx": i + 1,
                "agent": "claude", "model": "m", "skill_expected": "g66",
                "level": "L3", "success": 1 if ok else 0, "_at": i,
            })
        rows = {r["task_id"]: r for r in s.stats()["rows"]}
        r = rows["L3_g66_deploy"]
        assert r["n"] == 2 and r["sr"] == 0.5
        assert r["ci_lower"] is not None
        assert r["veto"] is False and r["veto_hit"] is False

        # L4 一票否决（1 成功 1 失败）→ veto=True, veto_hit=True
        for i, ok in enumerate((True, False)):
            s.upsert({
                "session_id": f"l4_{i}", "task_id": "L4_lock_screen", "run_idx": i + 1,
                "agent": "claude", "model": "m", "skill_expected": "uu_remote",
                "level": "L4", "success": 1 if ok else 0, "_at": 100 + i,
            })
        rows = {r["task_id"]: r for r in s.stats()["rows"]}
        r = rows["L4_lock_screen"]
        assert r["veto"] is True and r["veto_hit"] is True
        print("stats ci/veto OK")
    finally:
        s.close()


def test_review_defense():
    s = EvalStore(_tmp_db())
    try:
        s.upsert({"session_id": "x1", "task_id": "L1_device_list", "level": "L1",
                  "success": 0, "success_auto": 0, "_at": 1})
        r = s.review("x1", defense="pass")
        assert r["success"] == 1 and r["review_status"] == "corrected"
        r = s.review("x1", defense="invalid")
        assert r["review_status"] == "invalid"
        # 统计排除 invalid
        s.upsert({"session_id": "x2", "task_id": "L1_device_list", "level": "L1",
                  "success": 1, "success_auto": 1, "_at": 2})
        rows = {r["task_id"]: r for r in s.stats()["rows"]}
        assert rows["L1_device_list"]["n"] == 1  # x1(invalid) 被排除
        print("review defense OK")
    finally:
        s.close()


if __name__ == "__main__":
    test_load_papers()
    test_roundtrip_new_fields()
    test_wilson()
    test_stats_ci_and_veto()
    test_review_defense()
    print("ALL PASSED")
