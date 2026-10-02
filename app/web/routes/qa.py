# -*- coding: utf-8 -*-
"""M5 智能问答接口（C4 组件 `C-API-11`，详细设计 §6.2 第 21–22 项、§15.E）。

    POST /api/qa/ask        提问并获取回答（21，否）
    GET  /api/qa/history    问答历史（22，需登录）

【成本与架构要点】
    · `POST /api/qa/ask` 是**唯一**可能需要在线调用模型的接口，由 `APP_QA_LIVE` 控制，
      **默认关闭**；关闭时仍返回完整的数据依据（分类、景点、检索到的事实），
      并明确 `reached_model=false`，所以默认配置下反复提问也不会产生任何费用。
    · 设计内置三条"不调用模型"的分支（超范围拒答／未识别到景点／检索无数据），
      即使开启开关，这三类问题也不会调用模型。

【越权防护（§13.1）】历史查询**强制以当前会话 user_id 过滤**，
    不接受前端传入 user_id——避免"改个参数就看别人问过什么"。
"""

from __future__ import annotations

from flask import Blueprint, request

from app.db import DatabaseError, connection
from app.web.data_access import ParamError, fetch_all, parse_pagination
from app.web.qa import answer_question, should_save_record
from app.web.response import (
    CODE_DB_ERROR,
    CODE_INTERNAL_ERROR,
    CODE_PARAM_INVALID,
    fail,
    ok,
)
from app.web.routes._auth_helpers import current_user, login_required
from app.web.routes._helpers import respond

bp = Blueprint("qa", __name__, url_prefix="/api/qa")

QUESTION_MAX_CHARS = 200  # §13.2 输入校验


@bp.post("/ask")
def ask():
    """提问并获取回答（接口 21）。"""
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    question = str(payload.get("question") or "").strip()
    if not question:
        return fail(CODE_PARAM_INVALID, "问题不能为空")
    if len(question) > QUESTION_MAX_CHARS:
        return fail(CODE_PARAM_INVALID, f"问题长度不得超过 {QUESTION_MAX_CHARS} 字")

    try:
        result = answer_question(question)
    except ParamError as exc:
        return fail(CODE_PARAM_INVALID, str(exc))
    except DatabaseError as exc:
        return fail(CODE_DB_ERROR, f"数据库访问失败：{exc}")
    except Exception as exc:
        return fail(CODE_INTERNAL_ERROR, f"服务内部错误：{type(exc).__name__}")

    # 落库：qa_record（设计 §15.E.4 的"保存"，仅在"确实处理过提问"时写，见 should_save_record）
    if should_save_record(result):
        try:
            qa_id = _save_record(question, result)
            result["qa_id"] = qa_id
        except DatabaseError as exc:
            # 落库失败不影响本次回答（但如实告知，不假装保存成功）
            result["qa_id"] = None
            result["save_error"] = f"问答记录保存失败：{exc}"
    else:
        result["qa_id"] = None
        result["saved"] = False
        result["save_note"] = "该问题为拒答或追问提示，按设计不写入 qa_record（§15.E.1）。"

    return ok(result)


def _save_record(question: str, result: dict) -> int:
    """写入 `qa_record`（登录用户记 user_id；游客记 session_key，可为空）。"""
    import json

    user = current_user()
    spot_ids = ",".join(str(spot["spot_id"]) for spot in result.get("spots") or []) or None
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO qa_record
                    (user_id, session_key, question, question_type, spot_ids, answer,
                     sources_json, caliber_note, need_review, model, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    user.user_id if user else None,
                    None,
                    question[:512],
                    result["question_type"],
                    spot_ids,
                    # answer 为 NOT NULL：未生成回答时写入说明文字，保证记录可读
                    (result.get("answer") or result.get("message_text") or "（未生成自然语言回答）")[:65535],
                    json.dumps({"sources": result.get("sources") or [], "facts": result.get("facts") or []}, ensure_ascii=False),
                    (result.get("caliber_note") or "")[:255] or None,
                    int(result.get("need_review") or 0),
                    result.get("model"),
                ),
            )
            return int(cur.lastrowid)


@bp.get("/history")
@login_required
def history():
    """问答历史（接口 22）：**只返回当前登录用户自己的记录**（§13.1 越权防护）。"""
    user = current_user()
    if user is None:
        return fail(CODE_PARAM_INVALID, "未登录")

    def action():
        page, page_size = parse_pagination(request.args)
        total_row = fetch_all(
            "SELECT COUNT(*) AS n FROM qa_record WHERE user_id = %s", (user.user_id,)
        )
        total = int(total_row[0]["n"]) if total_row else 0
        items = fetch_all(
            """
            SELECT qa_id, question, question_type, spot_ids, answer, sources_json,
                   caliber_note, need_review, model, created_at
              FROM qa_record
             WHERE user_id = %s
             ORDER BY qa_id DESC
             LIMIT %s OFFSET %s
            """,
            (user.user_id, page_size, (page - 1) * page_size),
        )
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "caliber_note": "历史记录仅返回当前登录用户本人的提问（以服务端会话的 user_id 过滤，不接受前端传参）。",
            "sample_size": total,
        }

    return respond(action)


__all__ = ["bp"]
