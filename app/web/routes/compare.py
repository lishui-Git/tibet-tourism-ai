# -*- coding: utf-8 -*-
"""M4 景点对比接口（C4 组件 `C-API-10`，详细设计 §6.2 第 20 项、§15.D）。

`GET /api/compare?spot_a=<id>&spot_b=<id>`

与其它接口一致：**对比指标与方面对比由后端实时计算**（不落库、零模型成本），
只有"解读"部分需要模型，且由 `APP_COMPARE_LIVE` 开关控制（默认关闭）。
因此本接口在默认配置下**不会产生任何 API 消费**，返回的 `interpretation.available=false`
会带明确原因，指标与方面数据照常可用（对应设计 §15.D.3 的失败降级路径）。

错误处理（§15.D.1 流程）：
    · spot_a == spot_b            → 1002 参数越界
    · 缺少参数 / 非整数            → 1001 参数错误
    · 景点不存在                   → 3001
    · 解读调用失败                 → 仍返回 200 + code=0，只是 interpretation.available=false
"""

from __future__ import annotations

from flask import Blueprint, request

from app.web.compare import compare_spots
from app.web.data_access import parse_int_arg
from app.web.routes._helpers import respond, respond_one

bp = Blueprint("compare", __name__, url_prefix="/api")


@bp.get("/compare")
def compare():
    """两景点对比与解读（接口 20）：`spot_a`、`spot_b`。"""

    def action():
        spot_a = parse_int_arg(request.args, "spot_a")
        spot_b = parse_int_arg(request.args, "spot_b")
        return compare_spots(int(spot_a), int(spot_b))

    return respond_one(action, "对比的景点不存在（spot_a 或 spot_b 有误）")


__all__ = ["bp"]
