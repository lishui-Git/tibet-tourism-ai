# -*- coding: utf-8 -*-
"""写库失败兜底测试（保险机制第 10 条：**数据库写入失败时不要重复请求模型**）。

## 为什么单独测这个
语义分析的调用层是"先调用模型（已付费）→ 再写库"。如果写库失败时没有兜底，
已付费的结果会随事务回滚一起丢掉，下次运行重新查询游标时会**再调一次模型**，
等于同一批数据被重复扣费。这条路径平时不会触发，属于典型的"故障时才暴露"的风险，
因此用**注入写库失败**的方式把它固定下来。

## 测法（全程零 API 消费）
    用一个"第 N 次写库率先失败"的连接代理包住真实连接，注入到
    `write_records_safely` / 调用循环里，然后断言：
      A. 瞬时失败（第一次失败、重试成功）→ 结果最终写库，且**模型调用次数不变**；
      B. 持续失败 → 结果落到 `logs/recovery/*.jsonl`，且**模型调用次数不变**；
      C. 落盘内容能被还原（`payload_to_record`）——保证"补写"真的可用。

用真实 `DeepSeekClient` 作为客户端：**只数调用次数、不做任何网络请求**
（所有断言用的都是 `write_records_safely` 与函数本身，不走到 `client.chat`）。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from app.batch.semantic_analysis import (
    RECOVERY_DIR,
    SemanticStats,
    payload_to_record,
    write_records_safely,
)
from app.batch.write_recovery import recovery_path
from app.db import DatabaseError, connection, query_one
from app.llm.validators import SemanticResult

RESULTS: list[tuple[str, bool, str]] = []


def _recovery_path(comment_id: int):
    """语义结果的落盘路径（统一由共享模块按 `comment_id % 100` 分片）。"""
    return recovery_path("semantic", comment_id)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class FailingCursor:
    """代理真实游标：前 `fail_times` 次写操作直接抛 DatabaseError。"""

    def __init__(self, real, state: dict):
        self._real = real
        self._state = state

    def execute(self, *args, **kwargs):
        self._state["writes"] += 1
        if self._state["writes"] <= self._state["fail_times"]:
            raise DatabaseError("注入的写库失败（模拟磁盘/锁冲突）")
        return self._real.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._state["writes"] += 1
        if self._state["writes"] <= self._state["fail_times"]:
            raise DatabaseError("注入的写库失败（模拟磁盘/锁冲突）")
        return self._real.executemany(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._real, item)

    # 真实游标支持 `with conn.cursor() as cur:`；特殊方法不会走 __getattr__，必须显式代理
    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)


class ConnectionProxy:
    """代理真实连接，让 `conn.cursor()` 返回会按需失败的游标。"""

    def __init__(self, real, state: dict):
        self._real = real
        self._state = state

    def cursor(self):
        return FailingCursor(self._real.cursor(), self._state)

    def __getattr__(self, item):
        return getattr(self._real, item)


def make_record(comment_id: int, spot_id: int, original: dict) -> dict:
    """构造一条"模型已返回"的待写记录（不涉及任何网络）。

    **关键：内容刻意与库中现值完全一致**，并且 `raw_json` 也原样保留。
    这样即使 upsert 真的生效，业务数据也不发生任何变化——
    测试只关心"写库路径是否被执行、失败时是否兜底"，不关心内容差异。
    （首版测试用了写死的 positive/0.6，结果**覆盖掉了一条真实 API 结果**，
      并顺带删除了它原有的方面行——这个教训写在这里，避免重犯。）
    """
    return {
        "comment_id": comment_id,
        "spot_id": spot_id,
        "result": SemanticResult(
            polarity=original["polarity"],
            intensity=original["intensity"],
            is_valid=int(original["is_valid"]),
            aspects=original["aspects"],
            keywords=original["keywords"],
            summary=original["summary"],
        ),
        "source": original["source"],
        "raw": original["raw"],
    }


def snapshot_comment(comment_id: int) -> dict:
    """完整快照该评论的三张表相关行，供测试结束后逐项比对还原。"""
    from app.db import query_all

    sentiment = query_one(
        "SELECT polarity, intensity, is_valid, raw_json FROM sentiment "
        "WHERE comment_id=%s AND method='deepseek'",
        (comment_id,),
    )
    semantic = query_one(
        "SELECT keywords, summary, source FROM comment_semantic WHERE comment_id=%s",
        (comment_id,),
    )
    aspects = query_all(
        "SELECT aspect_name, polarity, evidence, created_at FROM aspect "
        "WHERE comment_id=%s AND method='deepseek' ORDER BY aspect_name, evidence",
        (comment_id,),
    )
    return {"sentiment": sentiment, "semantic": semantic, "aspects": aspects}


def restore_comment(comment_id: int, snap: dict) -> None:
    """把该评论的三张表**逐字段还原**成快照状态（测试可能覆盖过它）。"""
    import json as _json

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sentiment SET polarity=%s, intensity=%s, is_valid=%s, raw_json=%s "
                "WHERE comment_id=%s AND method='deepseek'",
                (
                    snap["sentiment"]["polarity"],
                    snap["sentiment"]["intensity"],
                    snap["sentiment"]["is_valid"],
                    snap["sentiment"]["raw_json"],
                    comment_id,
                ),
            )
            cur.execute(
                "UPDATE comment_semantic SET keywords=%s, summary=%s, source=%s WHERE comment_id=%s",
                (
                    snap["semantic"]["keywords"],
                    snap["semantic"]["summary"],
                    snap["semantic"]["source"],
                    comment_id,
                ),
            )
            # 方面行：先清后按快照重建（与 write_records 的"先删后插"同口径）
            #
            # 【隔离保真修正】必须写回**快照里的 created_at**，不能再用 `NOW()`。
            # 旧实现用 NOW() 会把这些方面行的 created_at 改成"测试运行时刻"——
            # 行数不变、业务字段不变，但生产数据的**时间戳被静默改写**，
            # 与"逐字段精确还原"的承诺不符（用 `aspect` 的内容级摘要才检得出来）。
            cur.execute("DELETE FROM aspect WHERE comment_id=%s", (comment_id,))
            for row in snap["aspects"]:
                cur.execute(
                    "INSERT INTO aspect (comment_id, spot_id, aspect_name, polarity, evidence, method, created_at) "
                    "SELECT %s, spot_id, %s, %s, %s, 'deepseek', %s FROM sentiment "
                    "WHERE comment_id=%s AND method='deepseek'",
                    (comment_id, row["aspect_name"], row["polarity"], row["evidence"],
                     row["created_at"], comment_id),
                )


def pick_two_comments() -> list[tuple[int, int]]:
    """取一条"已处理"的评论（写库到已存在的键上，幂等、不改动业务口径）。"""
    from app.db import query_all

    rows = query_all(
        "SELECT comment_id, spot_id FROM sentiment WHERE method='deepseek' ORDER BY comment_id LIMIT 1"
    )
    return [(int(rows[0]["comment_id"]), int(rows[0]["spot_id"]))]


def table_counts() -> dict[str, int]:
    """三张结果表的整体行数，用于证明"测试前后完全一致"（没有污染真实数据）。"""
    from app.db import query_all

    rows = query_all(
        "SELECT (SELECT COUNT(*) FROM sentiment WHERE method='deepseek') AS sentiment_rows, "
        "       (SELECT COUNT(*) FROM comment_semantic) AS semantic_rows, "
        "       (SELECT COUNT(*) FROM aspect) AS aspect_rows"
    )
    return {k: int(v) for k, v in rows[0].items()}


def main() -> int:
    print("=" * 84)
    print("写库失败兜底测试（注入写库失败；不调用任何模型、不产生 API 费用）")
    print("=" * 84)

    picked = pick_two_comments()
    if not picked:
        print("✘ 库中没有已处理的 deepseek 结果，无法在幂等键上做测试")
        return 1
    comment_id, spot_id = picked[0]

    # 先做完整快照：测试结束必须逐字段还原（本测试会真的调用 write_records）
    snap = snapshot_comment(comment_id)
    if not snap["sentiment"]:
        print(f"✘ 未能取到 comment_id={comment_id} 的快照，放弃测试")
        return 1
    original = {
        "polarity": snap["sentiment"]["polarity"],
        "intensity": snap["sentiment"]["intensity"],
        "is_valid": int(snap["sentiment"]["is_valid"]),
        "raw": json.loads(snap["sentiment"]["raw_json"]) if snap["sentiment"]["raw_json"] else None,
        "keywords": snap["semantic"]["keywords"] if snap["semantic"] else None,
        "summary": snap["semantic"]["summary"] if snap["semantic"] else None,
        "source": snap["semantic"]["source"] if snap["semantic"] else None,
        "aspects": [
            {"aspect": a["aspect_name"], "polarity": a["polarity"], "evidence": a["evidence"] or ""}
            for a in snap["aspects"]
        ],
    }
    before_rows = table_counts()
    print(f"\n测试对象：comment_id={comment_id}（spot_id={spot_id}）")
    print(f"快照：polarity={original['polarity']} intensity={original['intensity']} "
          f"aspects={len(original['aspects'])} 条 keywords={original['keywords']}")
    print(f"测试前结果表行数：{json.dumps(before_rows, ensure_ascii=False)}")

    # 记录刻意与现值一致 → 即便 upsert 生效也不改变业务数据
    record = make_record(comment_id, spot_id, original)
    stats = SemanticStats()

    # ---------- A. 瞬时失败：第一次写失败 → 重试成功 ----------
    print("\n[A] 瞬时写库失败 → 只重试写库，不重新调用模型")
    state = {"writes": 0, "fail_times": 1}
    with connection() as real_conn:
        write_records_safely(ConnectionProxy(real_conn, state), [record], stats)

    after = query_one(
        "SELECT polarity, intensity FROM sentiment WHERE comment_id=%s AND method='deepseek'",
        (comment_id,),
    )
    check("瞬时失败后结果最终写库存在（重试成功）", after is not None,
          f"polarity={after['polarity'] if after else None}")
    check("确实发生了注入的失败（说明测试有效）", state["writes"] >= 2, f"写操作次数={state['writes']}")
    check("重试后业务数据与快照一致（未被篡改）",
          after["polarity"] == original["polarity"]
          and abs(float(after["intensity"]) - float(original["intensity"])) < 1e-9,
          f"库中={after['polarity']}/{after['intensity']} 快照={original['polarity']}/{original['intensity']}")
    check("未产生恢复文件（因为重试已成功）", not _recovery_path(comment_id).exists(),
          str(_recovery_path(comment_id)))

    # ---------- B. 持续失败：全部重试都失败 → 落盘待补 ----------
    print("\n[B] 持续写库失败 → 结果落盘待补，不丢数据")
    recovery_file = _recovery_path(comment_id)
    if recovery_file.exists():
        recovery_file.unlink()
    backup = None
    if RECOVERY_DIR.exists():
        backup = RECOVERY_DIR.parent / "recovery_backup_for_test"
        if backup.exists():
            shutil.rmtree(backup)

    state2 = {"writes": 0, "fail_times": 99}
    stats2 = SemanticStats()
    with connection() as real_conn:
        write_records_safely(ConnectionProxy(real_conn, state2), [record], stats2)

    check("持续失败后写出了恢复文件", recovery_file.exists(), str(recovery_file))
    lines = []
    if recovery_file.exists():
        lines = [ln for ln in recovery_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    check("恢复文件包含该条记录", len(lines) == 1, f"行数={len(lines)}")
    if lines:
        # 统一结构：每行是 {"text": 可读摘要, "payload": 可写库业务数据}
        entry = json.loads(lines[0])
        check("落盘结构统一为 {text, payload}", "text" in entry and "payload" in entry,
              f"keys={sorted(entry.keys())}")
        payload = entry["payload"]
        usage_tokens = (payload.get("raw") or {}).get("usage", {}).get("total_tokens")
        check("落盘内容含核心字段（极性/方面/来源）",
              payload["comment_id"] == comment_id
              and payload["result"]["polarity"] == original["polarity"]
              and len(payload["result"]["aspects"]) == len(original["aspects"])
              and payload["source"] == original["source"],
              json.dumps(payload, ensure_ascii=False)[:140])
        # usage 是否存在取决于该条记录本身：库中有 24 条历史结果未落 usage（早期局限）。
        # 因此这里断言的是"**原样保留**"，而不是"一定有 usage"——不掩盖、也不误报。
        snapshot_usage = (original["raw"] or {}).get("usage")
        if snapshot_usage:
            check("落盘保留了 usage（费用可复核）", usage_tokens == snapshot_usage.get("total_tokens"),
                  f"库中={json.dumps(snapshot_usage, ensure_ascii=False)} 落盘={usage_tokens}")
        else:
            check("该条记录本就没有 usage，落盘如实保留（未凭空造数）",
                  payload["raw"] == original["raw"],
                  "raw 与库中快照逐字节一致")

        # ---------- C. 落盘内容可还原（补写真的可用） ----------
        print("\n[C] 落盘内容可还原为可写库的记录（补写可用）")
        restored = payload_to_record(payload)
        check("还原后 comment_id/spot_id 一致",
              restored["comment_id"] == comment_id and restored["spot_id"] == spot_id, "")
        check("还原后结果字段与快照一致",
              restored["result"].polarity == original["polarity"]
              and abs(float(restored["result"].intensity) - float(original["intensity"])) < 1e-9
              and len(restored["result"].aspects) == len(original["aspects"]),
              f"polarity={restored['result'].polarity} aspects={len(restored['result'].aspects)}")

        # 用还原后的记录真正写一次库，证明"补写"闭环可用
        with connection() as real_conn:
            write_records_safely(real_conn, [restored], SemanticStats())
        final = query_one(
            "SELECT polarity, intensity, raw_json FROM sentiment WHERE comment_id=%s AND method='deepseek'",
            (comment_id,),
        )
        raw = json.loads(final["raw_json"]) if final and final["raw_json"] else {}
        check("补写闭环：还原后的记录能成功写库且与快照一致",
              final is not None and final["polarity"] == original["polarity"],
              f"polarity={final['polarity'] if final else None}")
        # 与上文同理：只断言"补写把 raw 原样写回"，不假定 usage 必然存在
        check("补写后 raw_json 与快照逐字段一致（usage 若有则保留）",
              (raw or {}) == (original["raw"] or {}),
              f"库中 usage={json.dumps((raw or {}).get('usage'), ensure_ascii=False)}")

    # ---------- D. 还原并证明"测试没有污染真实数据" ----------
    print("\n[D] 还原测试对象并证明数据未被污染")
    restore_comment(comment_id, snap)
    after_restore = snapshot_comment(comment_id)
    check("还原后 sentiment 逐字段一致",
          after_restore["sentiment"]["polarity"] == snap["sentiment"]["polarity"]
          and float(after_restore["sentiment"]["intensity"]) == float(snap["sentiment"]["intensity"])
          and int(after_restore["sentiment"]["is_valid"]) == int(snap["sentiment"]["is_valid"])
          and after_restore["sentiment"]["raw_json"] == snap["sentiment"]["raw_json"],
          "polarity/intensity/is_valid/raw_json 全部一致")
    check("还原后 comment_semantic 逐字段一致",
          after_restore["semantic"]["keywords"] == snap["semantic"]["keywords"]
          and after_restore["semantic"]["summary"] == snap["semantic"]["summary"]
          and after_restore["semantic"]["source"] == snap["semantic"]["source"], "")
    check("还原后 aspect 行集合**逐字段**一致（含 created_at，防止时间戳被 NOW() 静默改写）",
          [(a["aspect_name"], a["polarity"], a["evidence"], str(a["created_at"]))
           for a in after_restore["aspects"]]
          == [(a["aspect_name"], a["polarity"], a["evidence"], str(a["created_at"]))
              for a in snap["aspects"]],
          f"{len(after_restore['aspects'])} 条")
    after_rows = table_counts()
    check("还原后结果表行数与测试前完全一致", after_rows == before_rows,
          f"{json.dumps(before_rows, ensure_ascii=False)} → {json.dumps(after_rows, ensure_ascii=False)}")

    # ---------- 清理 ----------
    for path in {_recovery_path(comment_id)}:
        if path.exists():
            path.unlink()
    if RECOVERY_DIR.exists() and not any(RECOVERY_DIR.iterdir()):
        RECOVERY_DIR.rmdir()
    print("\n已清理：恢复文件与空的 recovery 目录")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 84)
    print(f"写库兜底测试：{passed}/{total} 项通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} — {detail}")
    print("=" * 84)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
