# -*- coding: utf-8 -*-
"""Python 批处理服务（C4 容器「Python 批处理服务」）。

本包承载 C4 组件 `C-BAT-01` ~ `C-BAT-07`：

    C-BAT-01  任务编排与断点续跑组件   → 已实现（clean_dataset.py：7 步 SOP 编排）
    C-BAT-02  数据校验组件             → 已实现（import_dataset.py 行数/表头校验；
                                          clean_dataset.py 行数、表头列数与景点级校验）
    C-BAT-03  清洗与规范化组件         → 已实现（import_dataset.py：字段映射与派生；
                                          clean_dataset.py：评论级/景点级拆分与规范化文件输出）
    C-BAT-04  去重与分层组件           → 已实现（低信息量 BR-05 与重复正文 BR-06 标记，
                                          由 import_dataset.py 构建、clean_dataset.py 统计）
    C-BAT-05  语义抽取组件             → 阶段五
    C-BAT-06  事实包构造组件           → 阶段五
    C-BAT-07  评价生成组件             → 阶段五

两个脚本的分工（不要重复实现）：
    · `import_dataset.py`（阶段一）：把 CSV 写入 `spot` / `review` 两张核心表，是**唯一**写库入口；
    · `clean_dataset.py`（阶段二）：复用前者的规范化函数，产出清洗版文件与统计报告，
      **默认不写核心表**（设计要求的步骤计数可用 `--log-task` 登记到运维表）。

说明：按「先建表、再导数据、后清洗分析」的既定顺序（详细设计 §16），
C-BAT-01~04 分两个阶段落地——阶段一实现「导入所必需」的部分，
阶段二补齐任务编排、校验、拆分与产物输出；尚未实现的 C-BAT-05~07 不建空文件，
避免出现「有文件无实现」的假进度。
"""
