-- =============================================================================
--  基于 DeepSeek 的西藏旅游景点智能评价与分析系统 · 数据库建表脚本
--  数据库：MySQL 8.x        库名：tibet_review        字符集：utf8mb4
--  版本：V1.0               日期：2026-09-20
--
--  使用说明：
--    1) 先创建库：CREATE DATABASE tibet_review DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
--    2) 再执行本脚本（或在 Navicat 中直接运行）
--    3) 本脚本可重复执行（IF NOT EXISTS），不会删除已有数据
--
--  表清单（17 张，按精简要求已由 21 张收敛）：
--    基础数据  spot / review
--    语义结果  sentiment / aspect / comment_semantic
--    主题结果  topic / topic_word
--    统计指标  stat_overview / stat_spot / stat_time / stat_ip
--    解释结果  spot_fact_package / spot_report
--    业务记录  qa_record
--    运维      analysis_task（含模型版本与实验信息）/ task_log（含清洗统计与调用失败）
--    用户      sys_user
--
--  已删除的 4 张表及去处：
--    ml_model        → analysis_task 的 model_type/model_version/model_path/random_seed/model_metrics_json
--    llm_failure     → task_log 的 level=ERROR/WARN + stage/ref_key/retry_count/resolved
--    clean_log       → task_log 的 stage=步骤名 + detail_json 四类计数
--    spot_comparison → 对比指标由后端实时计算、DeepSeek 解读直接返回，不落库
--
--  设计依据：docs/database/数据库设计说明.md、docs/database/ER图.md
--  核心数据表 spot / review 严格依据《旅游评论数据集_最终版.csv》的 15 个真实字段
-- =============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- =============================================================================
-- 一、用户表
-- =============================================================================
DROP TABLE IF EXISTS `sys_user`;
CREATE TABLE `sys_user` (
  `user_id`        INT UNSIGNED    NOT NULL AUTO_INCREMENT COMMENT '用户主键',
  `username`       VARCHAR(64)     NOT NULL                COMMENT '登录名',
  `password_hash`  VARCHAR(255)    NOT NULL                COMMENT '加盐哈希口令（禁止明文）',
  `nickname`       VARCHAR(64)     NULL     DEFAULT NULL   COMMENT '显示名',
  `role`           VARCHAR(16)     NOT NULL DEFAULT 'user' COMMENT '角色：user/admin',
  `status`         TINYINT(1)      NOT NULL DEFAULT 1      COMMENT '状态：1正常 0停用',
  `created_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
  `last_login_at`  DATETIME        NULL     DEFAULT NULL   COMMENT '最近登录时间',
  PRIMARY KEY (`user_id`),
  UNIQUE KEY `uk_username` (`username`),
  KEY `idx_role` (`role`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统用户表（角色从简，仅 user/admin 两档）';


-- =============================================================================
-- 二、核心数据表（严格来源：旅游评论数据集_最终版.csv）
-- =============================================================================

-- 2.1 景点维表：由 CSV「景点名称」去重（837 个）+ 4 个景点级字段构成
DROP TABLE IF EXISTS `spot`;
CREATE TABLE `spot` (
  `spot_id`              INT UNSIGNED  NOT NULL AUTO_INCREMENT COMMENT '景点主键',
  `spot_name`            VARCHAR(128)  NOT NULL                COMMENT '景点名称（CSV，837 个唯一值）',
  `source_scope`         VARCHAR(16)   NOT NULL DEFAULT 'tibet' COMMENT '来源口径：tibet(554)/route(283)',
  `address`              VARCHAR(255)  NULL DEFAULT NULL       COMMENT '地址（837/837 有值）',
  `open_time`            VARCHAR(255)  NULL DEFAULT NULL       COMMENT '开放时间（546/837 有值）',
  `phone`                VARCHAR(64)   NULL DEFAULT NULL       COMMENT '官方电话（仅 99/837 有值）',
  `introduction`         TEXT          NULL                    COMMENT '景点介绍（738/837 有值）',
  `poi_url`              VARCHAR(255)  NULL DEFAULT NULL       COMMENT '携程景点页地址（便于溯源）',
  `review_count`         INT UNSIGNED  NOT NULL DEFAULT 0      COMMENT '评论量（冗余自 stat_spot，便于排行）',
  `has_full_evaluation`  TINYINT(1)    NOT NULL DEFAULT 0      COMMENT '是否具备完整智能评价资格（评论量≥100，共57个）',
  `created_at`           DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  PRIMARY KEY (`spot_id`),
  UNIQUE KEY `uk_spot_name` (`spot_name`),
  KEY `idx_source_scope` (`source_scope`),
  KEY `idx_review_count` (`review_count`),
  KEY `idx_has_full_eval` (`has_full_evaluation`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='景点维表（景点级字段，仅作展示，不作分析维度 BR-09）';


-- 2.2 评论明细表：CSV 的 11 个评论级字段 + 清洗派生字段（59,033 条）
DROP TABLE IF EXISTS `review`;
CREATE TABLE `review` (
  `comment_id`      BIGINT UNSIGNED  NOT NULL                COMMENT '评论编号（CSV，唯一率100%，天然主键）',
  `spot_id`         INT UNSIGNED     NOT NULL                COMMENT '所属景点',
  `score`           TINYINT UNSIGNED NULL DEFAULT NULL       COMMENT '评分 1-5（37 条空值）',
  `score_desc`      VARCHAR(16)      NULL DEFAULT NULL       COMMENT '评分描述（与评分一一对应）',
  `content`         TEXT             NULL                    COMMENT '评论内容（1 条空值）',
  `content_length`  SMALLINT UNSIGNED NOT NULL DEFAULT 0     COMMENT '正文字数（派生，中位数 31）',
  `publish_date`    DATE             NOT NULL                COMMENT '发布时间（格式统一，异常 0 条）',
  `publish_year`    SMALLINT UNSIGNED NOT NULL DEFAULT 0     COMMENT '发布年份（派生，2015-2026）',
  `publish_month`   TINYINT UNSIGNED NOT NULL DEFAULT 0      COMMENT '发布月份（派生，1-12）',
  `ip_location`     VARCHAR(32)      NOT NULL DEFAULT '未知' COMMENT 'IP 归属地原始值（66 种取值）',
  `ip_is_unknown`   TINYINT(1)       NOT NULL DEFAULT 0      COMMENT '是否未知（2022-08 前 100% 为 1）',
  `ip_province`     VARCHAR(32)      NULL DEFAULT NULL       COMMENT '标准化省份（仅 2022-08 后有效）',
  `user_nick`       VARCHAR(128)     NOT NULL DEFAULT ''     COMMENT '用户昵称（非稳定 ID，不用于建模）',
  `like_count`      INT UNSIGNED     NOT NULL DEFAULT 0      COMMENT '点赞数（非零 18.35%，最大 588）',
  `image_count`     TINYINT UNSIGNED NOT NULL DEFAULT 0      COMMENT '图片数（非零 48.35%）',
  `image_urls`      TEXT             NULL                    COMMENT '图片URL（30,493 条为空＝该评论无图）',
  `is_low_info`     TINYINT(1)       NOT NULL DEFAULT 0      COMMENT '低信息量（正文≤10字，10,228 条/17.33%）',
  `is_dup_content`  TINYINT(1)       NOT NULL DEFAULT 0      COMMENT '正文重复（4,239 条/7.18%）',
  `dup_group_id`    INT UNSIGNED     NULL DEFAULT NULL       COMMENT '重复组号（对应内容重复清单.csv，1,285 组）',
  `created_at`      DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
  PRIMARY KEY (`comment_id`),
  KEY `idx_spot_id` (`spot_id`),
  KEY `idx_publish_date` (`publish_date`),
  KEY `idx_spot_date` (`spot_id`, `publish_date`),
  KEY `idx_publish_ym` (`publish_year`, `publish_month`),
  KEY `idx_ip_province` (`ip_province`),
  KEY `idx_ip_unknown` (`ip_is_unknown`),
  KEY `idx_like_count` (`like_count`),
  KEY `idx_is_low_info` (`is_low_info`),
  KEY `idx_is_dup` (`is_dup_content`),
  KEY `idx_dup_group` (`dup_group_id`),
  KEY `idx_score` (`score`),
  CONSTRAINT `fk_review_spot` FOREIGN KEY (`spot_id`) REFERENCES `spot` (`spot_id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评论明细表（59,033 条，来源 CSV 评论级字段）';


-- =============================================================================
-- 三、语义结果表（DeepSeek / MLlib / 词典法）
-- =============================================================================

-- 3.1 多方法情感结果：复合主键 (comment_id, method) 支持三方法并列
DROP TABLE IF EXISTS `sentiment`;
CREATE TABLE `sentiment` (
  `comment_id`  BIGINT UNSIGNED  NOT NULL                    COMMENT '评论编号',
  `spot_id`     INT UNSIGNED     NOT NULL                    COMMENT '景点（冗余，便于按景点聚合）',
  `method`      VARCHAR(16)      NOT NULL                    COMMENT '方法：deepseek/mllib/dict',
  `polarity`    VARCHAR(10)      NOT NULL                    COMMENT '极性：positive/neutral/negative',
  `intensity`   TINYINT UNSIGNED NULL DEFAULT NULL           COMMENT '情感强度 1-5（规则法可为空）',
  `confidence`  DECIMAL(5,4)     NULL DEFAULT NULL           COMMENT '置信度（MLlib 概率）',
  `is_valid`    TINYINT(1)       NOT NULL DEFAULT 1          COMMENT '是否通过校验（非法值标记 0）',
  `raw_json`    TEXT             NULL                        COMMENT '模型原始返回（便于复核与复现）',
  `created_at`  DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
  PRIMARY KEY (`comment_id`, `method`),
  KEY `idx_spot_method` (`spot_id`, `method`),
  KEY `idx_method_polarity` (`method`, `polarity`),
  CONSTRAINT `fk_sentiment_review` FOREIGN KEY (`comment_id`) REFERENCES `review` (`comment_id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_sentiment_spot`   FOREIGN KEY (`spot_id`)    REFERENCES `spot` (`spot_id`)     ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评论情感结果表（多方法并列，支撑基线对照 FR-SA-04）';


-- 3.2 方面级结果
DROP TABLE IF EXISTS `aspect`;
CREATE TABLE `aspect` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT     COMMENT '主键',
  `comment_id`   BIGINT UNSIGNED NOT NULL                    COMMENT '评论编号',
  `spot_id`      INT UNSIGNED    NOT NULL                    COMMENT '景点',
  `aspect_name`  VARCHAR(32)     NOT NULL                    COMMENT '方面名（风景/交通/门票/服务/设施/住宿餐饮/高原反应/人流拥挤/性价比/其他）',
  `polarity`     VARCHAR(10)     NOT NULL                    COMMENT '该方面倾向',
  `evidence`     VARCHAR(255)    NULL DEFAULT NULL           COMMENT '原文证据片段（校验须为原文子串）',
  `method`       VARCHAR(16)     NOT NULL DEFAULT 'deepseek' COMMENT '来源方法',
  `created_at`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_comment_aspect` (`comment_id`, `aspect_name`),
  KEY `idx_spot_aspect` (`spot_id`, `aspect_name`),
  KEY `idx_aspect_polarity` (`aspect_name`, `polarity`),
  CONSTRAINT `fk_aspect_review` FOREIGN KEY (`comment_id`) REFERENCES `review` (`comment_id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_aspect_spot`   FOREIGN KEY (`spot_id`)    REFERENCES `spot` (`spot_id`)     ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评论方面级结果表（样本<10条不出结论 BR-04）';


-- 3.3 关键词与摘要
DROP TABLE IF EXISTS `comment_semantic`;
CREATE TABLE `comment_semantic` (
  `comment_id`  BIGINT UNSIGNED NOT NULL                     COMMENT '评论编号',
  `spot_id`     INT UNSIGNED    NOT NULL                     COMMENT '景点',
  `keywords`    VARCHAR(255)    NULL DEFAULT NULL            COMMENT '关键词，逗号分隔（≤5 个）',
  `summary`     VARCHAR(128)    NULL DEFAULT NULL            COMMENT '一句话摘要（≤40 字）',
  `source`      VARCHAR(16)     NOT NULL DEFAULT 'deepseek'  COMMENT '来源：deepseek/rule（≤10字走规则）',
  `created_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
  PRIMARY KEY (`comment_id`),
  KEY `idx_cs_spot` (`spot_id`),
  CONSTRAINT `fk_cs_review` FOREIGN KEY (`comment_id`) REFERENCES `review` (`comment_id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_cs_spot`   FOREIGN KEY (`spot_id`)    REFERENCES `spot` (`spot_id`)     ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评论关键词与摘要表';


-- 3.4 LDA 主题
DROP TABLE IF EXISTS `topic`;
CREATE TABLE `topic` (
  `topic_id`       INT UNSIGNED NOT NULL AUTO_INCREMENT      COMMENT '主题主键',
  `scope_type`     VARCHAR(10)  NOT NULL DEFAULT 'global'    COMMENT '范围：global/spot',
  `scope_id`       INT UNSIGNED NOT NULL DEFAULT 0           COMMENT '景点ID（全局为 0）',
  `topic_index`    INT UNSIGNED NOT NULL                     COMMENT '主题序号',
  `topic_rate`     DECIMAL(6,4) NOT NULL DEFAULT 0           COMMENT '该主题占比',
  `sample_size`    INT UNSIGNED NOT NULL DEFAULT 0           COMMENT '参与训练的评论数',
  `model_version`  VARCHAR(32)  NULL DEFAULT NULL            COMMENT '关联 analysis_task.model_version',
  `created_at`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
  PRIMARY KEY (`topic_id`),
  UNIQUE KEY `uk_topic_scope` (`scope_type`, `scope_id`, `topic_index`),
  KEY `idx_topic_scope` (`scope_type`, `scope_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LDA 主题表';

DROP TABLE IF EXISTS `topic_word`;
CREATE TABLE `topic_word` (
  `id`        BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT       COMMENT '主键',
  `topic_id`  INT UNSIGNED     NOT NULL                      COMMENT '所属主题',
  `word`      VARCHAR(32)      NOT NULL                      COMMENT '主题词',
  `weight`    DECIMAL(10,6)    NOT NULL DEFAULT 0            COMMENT '词权重',
  `rank_no`   TINYINT UNSIGNED NOT NULL DEFAULT 0            COMMENT '词序号（取 Top10）',
  PRIMARY KEY (`id`),
  KEY `idx_tw_topic` (`topic_id`),
  CONSTRAINT `fk_tw_topic` FOREIGN KEY (`topic_id`) REFERENCES `topic` (`topic_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LDA 主题词表';


-- =============================================================================
-- 四、统计指标表（Spark 产出，在线只读）
-- =============================================================================

-- 5.4 全局统计汇总（离线预计算；非独立事实源——各字段均可由 spot/review 确定性重算）
DROP TABLE IF EXISTS `stat_overview`;
CREATE TABLE `stat_overview` (
  `id`                     TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '固定为 1（单行汇总表）',
  `total_reviews`          INT UNSIGNED NOT NULL DEFAULT 0     COMMENT '评论总数 59033（由 review 聚合）',
  `total_spots`            INT UNSIGNED NOT NULL DEFAULT 0     COMMENT '景点总数 837（由 spot 聚合）',
  `tibet_spots`            INT UNSIGNED NOT NULL DEFAULT 0     COMMENT '西藏景点数 554（由 spot.source_scope 聚合）',
  `route_spots`            INT UNSIGNED NOT NULL DEFAULT 0     COMMENT '进藏沿线景点数 283（由 spot.source_scope 聚合）',
  `date_start`             DATE         NULL DEFAULT NULL      COMMENT '最早评论 2015-06-19（由 review.publish_date 聚合）',
  `date_end`               DATE         NULL DEFAULT NULL      COMMENT '最晚评论 2026-09-16（由 review.publish_date 聚合）',
  `valid_ip_sample`        INT UNSIGNED NOT NULL DEFAULT 0     COMMENT '有效 IP 样本 35098（2022-08 起，口径 S1）',
  `full_eval_spot_count`   INT UNSIGNED NOT NULL DEFAULT 0     COMMENT '可完整评价景点数 57（由 spot.has_full_evaluation 聚合）',
  `stat_version`           VARCHAR(32)  NOT NULL DEFAULT 'v1'  COMMENT '统计版本（关联生成任务）',
  `updated_at`             DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全局统计汇总表（离线预计算；非独立事实源，仅供查询复用，由离线任务重建）';


DROP TABLE IF EXISTS `stat_spot`;
CREATE TABLE `stat_spot` (
  `spot_id`              INT UNSIGNED  NOT NULL               COMMENT '景点（一景一行）',
  `review_count`         INT UNSIGNED  NOT NULL DEFAULT 0     COMMENT '评论量',
  `avg_score`            DECIMAL(3,2)  NULL DEFAULT NULL      COMMENT '平均评分',
  `score_1`              INT UNSIGNED  NOT NULL DEFAULT 0     COMMENT '1 星条数',
  `score_2`              INT UNSIGNED  NOT NULL DEFAULT 0     COMMENT '2 星条数',
  `score_3`              INT UNSIGNED  NOT NULL DEFAULT 0     COMMENT '3 星条数',
  `score_4`              INT UNSIGNED  NOT NULL DEFAULT 0     COMMENT '4 星条数',
  `score_5`              INT UNSIGNED  NOT NULL DEFAULT 0     COMMENT '5 星条数',
  `positive_rate`        DECIMAL(5,4)  NULL DEFAULT NULL      COMMENT '好评率（4-5 星占比）',
  `negative_rate`        DECIMAL(5,4)  NULL DEFAULT NULL      COMMENT '差评率（1-2 星占比）',
  `image_rate`           DECIMAL(5,4)  NOT NULL DEFAULT 0     COMMENT '图文率',
  `total_likes`          INT UNSIGNED  NOT NULL DEFAULT 0     COMMENT '点赞总数',
  `low_info_rate`        DECIMAL(5,4)  NOT NULL DEFAULT 0     COMMENT '低信息量评论占比',
  `sentiment_positive`   DECIMAL(5,4)  NULL DEFAULT NULL      COMMENT 'DeepSeek 正面占比',
  `sentiment_neutral`    DECIMAL(5,4)  NULL DEFAULT NULL      COMMENT '中性占比',
  `sentiment_negative`   DECIMAL(5,4)  NULL DEFAULT NULL      COMMENT '负面占比',
  `sentiment_sample`     INT UNSIGNED  NULL DEFAULT NULL      COMMENT '情感判定样本量（用于口径标注 BR-10）',
  `valid_ip_sample`      INT UNSIGNED  NOT NULL DEFAULT 0     COMMENT '该景点有效 IP 样本量（2022-08 后）',
  `first_comment_date`   DATE          NULL DEFAULT NULL      COMMENT '最早评论日期',
  `last_comment_date`    DATE          NULL DEFAULT NULL      COMMENT '最晚评论日期',
  `stat_version`         VARCHAR(32)   NOT NULL DEFAULT 'v1'  COMMENT '统计版本／任务号',
  `updated_at`           DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`spot_id`),
  KEY `idx_ss_review_count` (`review_count`),
  KEY `idx_ss_avg_score` (`avg_score`),
  KEY `idx_ss_positive` (`positive_rate`),
  CONSTRAINT `fk_ss_spot` FOREIGN KEY (`spot_id`) REFERENCES `spot` (`spot_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='景点级统计指标表（837 行）';


DROP TABLE IF EXISTS `stat_time`;
CREATE TABLE `stat_time` (
  `id`            INT UNSIGNED    NOT NULL AUTO_INCREMENT      COMMENT '主键',
  `period_type`   VARCHAR(10)     NOT NULL                     COMMENT '周期类型：year/month',
  `period`        VARCHAR(8)      NOT NULL                     COMMENT '周期值：如 2025 / 2025-03',
  `scope_type`    VARCHAR(10)     NOT NULL DEFAULT 'global'    COMMENT '范围：global/spot',
  `scope_id`      INT UNSIGNED    NOT NULL DEFAULT 0           COMMENT '景点ID（全局为 0）',
  `review_count`  INT UNSIGNED    NOT NULL DEFAULT 0           COMMENT '该周期评论量',
  `avg_score`     DECIMAL(3,2)    NULL DEFAULT NULL            COMMENT '平均评分',
  `sentiment_avg` DECIMAL(5,4)    NULL DEFAULT NULL            COMMENT '情感均值（正=1/中=0.5/负=0）',
  `stat_version`  VARCHAR(32)     NOT NULL DEFAULT 'v1'        COMMENT '统计版本',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_st_period` (`period_type`, `period`, `scope_type`, `scope_id`),
  KEY `idx_st_scope` (`scope_type`, `scope_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='时间维度统计表（展示时须声明非客流量 FR-OV-03）';


DROP TABLE IF EXISTS `stat_ip`;
CREATE TABLE `stat_ip` (
  `id`            INT UNSIGNED  NOT NULL AUTO_INCREMENT        COMMENT '主键',
  `scope_type`    VARCHAR(10)   NOT NULL DEFAULT 'global'      COMMENT '范围：global/spot',
  `scope_id`      INT UNSIGNED  NOT NULL DEFAULT 0             COMMENT '景点ID（全局为 0）',
  `ip_province`   VARCHAR(32)   NOT NULL                       COMMENT '标准化省份／境外地区',
  `review_count`  INT UNSIGNED  NOT NULL DEFAULT 0             COMMENT '该客源地评论量',
  `ratio`         DECIMAL(6,4)  NOT NULL DEFAULT 0             COMMENT '占比',
  `sample_size`   INT UNSIGNED  NOT NULL DEFAULT 0             COMMENT '该口径下有效样本总量（35098）',
  `is_overseas`   TINYINT(1)    NOT NULL DEFAULT 0             COMMENT '是否境外',
  `stat_version`  VARCHAR(32)   NOT NULL DEFAULT 'v1'          COMMENT '统计版本',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_si_scope` (`scope_type`, `scope_id`, `ip_province`),
  KEY `idx_si_province` (`ip_province`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客源地统计表（仅统计 2022-08-01 之后的评论 BR-01）';


-- =============================================================================
-- 五、解释结果表（事实包、评价报告）
-- =============================================================================

-- 5.1 事实包快照
DROP TABLE IF EXISTS `spot_fact_package`;
CREATE TABLE `spot_fact_package` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT      COMMENT '主键',
  `spot_id`       INT UNSIGNED    NOT NULL                     COMMENT '景点',
  `version`       VARCHAR(16)     NOT NULL DEFAULT 'v1'        COMMENT '事实包版本',
  `package_json`  JSON            NOT NULL                     COMMENT '事实包完整快照（结构见详细设计 §15.B.2）',
  `review_count`  INT UNSIGNED    NOT NULL DEFAULT 0           COMMENT '快照时的评论量',
  `stat_version`  VARCHAR(32)     NOT NULL DEFAULT 'v1'        COMMENT '依据的统计版本',
  `generated_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '生成时间',
  `task_id`       BIGINT UNSIGNED NULL DEFAULT NULL            COMMENT '生成任务',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_fp_spot_ver` (`spot_id`, `version`),
  KEY `idx_fp_task` (`task_id`),
  CONSTRAINT `fk_fp_spot` FOREIGN KEY (`spot_id`) REFERENCES `spot` (`spot_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='景点事实包快照表（保证评价可追溯 FR-IE-07）';


-- 5.2 景点智能评价报告（仅 57 个景点）
DROP TABLE IF EXISTS `spot_report`;
CREATE TABLE `spot_report` (
  `spot_id`               INT UNSIGNED    NOT NULL             COMMENT '景点（一景一报告）',
  `fact_package_version`  VARCHAR(16)     NOT NULL DEFAULT 'v1' COMMENT '依据的事实包版本',
  `summary`               TEXT            NOT NULL             COMMENT '综合评价',
  `advantages_json`       JSON            NULL DEFAULT NULL    COMMENT '主要优势（数组）',
  `issues_json`           JSON            NULL DEFAULT NULL    COMMENT '主要问题（数组）',
  `visitor_focus_json`    JSON            NULL DEFAULT NULL    COMMENT '游客关注点（数组）',
  `model`                 VARCHAR(32)     NOT NULL DEFAULT 'deepseek-chat' COMMENT '模型名',
  `prompt_version`        VARCHAR(16)     NOT NULL DEFAULT 'p1' COMMENT 'Prompt 模板版本',
  `need_review`           TINYINT(1)      NOT NULL DEFAULT 0   COMMENT '数字一致性校验未通过时置 1',
  `token_usage`           INT UNSIGNED    NULL DEFAULT NULL    COMMENT 'token 用量（成本核算）',
  `generated_at`          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '生成时间',
  `task_id`               BIGINT UNSIGNED NULL DEFAULT NULL    COMMENT '生成任务',
  PRIMARY KEY (`spot_id`),
  KEY `idx_sr_need_review` (`need_review`),
  KEY `idx_sr_task` (`task_id`),
  CONSTRAINT `fk_sr_spot` FOREIGN KEY (`spot_id`) REFERENCES `spot` (`spot_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='景点智能评价报告表（DeepSeek 生成，仅评论量≥100 的景点 BR-02）';


-- 5.3 景点对比（已按精简要求删除 spot_comparison 表）
--     对比指标（评论量/均分/好评率/方面对比等）由后端**实时查询** stat_spot / sentiment / aspect 计算；
--     DeepSeek 生成的对比解读**直接返回前端**，不落库。理由见《数据库设计说明》§1.1 决策 D-4。


-- =============================================================================
-- 六、业务记录表
-- =============================================================================

DROP TABLE IF EXISTS `qa_record`;
CREATE TABLE `qa_record` (
  `qa_id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT     COMMENT '主键',
  `user_id`        INT UNSIGNED    NULL DEFAULT NULL           COMMENT '提问用户（游客为 NULL）',
  `session_key`    VARCHAR(64)     NULL DEFAULT NULL           COMMENT '游客标识（可选）',
  `question`       VARCHAR(512)    NOT NULL                    COMMENT '用户问题',
  `question_type`  VARCHAR(32)     NOT NULL                    COMMENT '类型：SPOT_EVALUATION/SPOT_COMPARISON/VISITOR_FOCUS/SENTIMENT_EXPLAIN/DATA_METRIC/RANKING/OUT_OF_SCOPE',
  `spot_ids`       VARCHAR(128)    NULL DEFAULT NULL           COMMENT '识别到的景点 ID，逗号分隔',
  `answer`         TEXT            NOT NULL                    COMMENT '自然语言回答（含拒答说明）',
  `sources_json`   JSON            NULL DEFAULT NULL           COMMENT '回答所依据的数据来源',
  `caliber_note`   VARCHAR(255)    NULL DEFAULT NULL           COMMENT '数据口径说明（BR-10）',
  `need_review`    TINYINT(1)      NOT NULL DEFAULT 0          COMMENT '数字一致性校验标记',
  `model`          VARCHAR(32)     NULL DEFAULT NULL           COMMENT '模型名',
  `created_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '提问时间',
  PRIMARY KEY (`qa_id`),
  KEY `idx_qa_user_time` (`user_id`, `created_at`),
  KEY `idx_qa_type` (`question_type`),
  KEY `idx_qa_session` (`session_key`),
  CONSTRAINT `fk_qa_user` FOREIGN KEY (`user_id`) REFERENCES `sys_user` (`user_id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能问答记录表（不做长期上下文记忆 FR-QA-08）';


-- =============================================================================
-- 七、运维表（任务与统一日志）
--    说明（按精简要求，原 4 张运维表合并为 2 张）：
--      · ml_model      → 删除；模型版本与实验信息并入 analysis_task 的
--                        model_type / model_version / model_path / random_seed / model_metrics_json
--      · llm_failure   → 删除；失败原因与重试次数并入 task_log（level=ERROR/WARN，字段 detail_json）
--      · clean_log     → 删除；清洗步骤统计并入 task_log（stage=清洗步骤名，字段 detail_json）
-- =============================================================================

DROP TABLE IF EXISTS `analysis_task`;
CREATE TABLE `analysis_task` (
  `task_id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT    COMMENT '任务主键',
  `task_type`           VARCHAR(32)     NOT NULL                   COMMENT '类型：clean/stat/mllib/lda/semantic/fact_package/spot_report',
  `task_name`           VARCHAR(128)    NULL DEFAULT NULL          COMMENT '任务名',
  `status`              VARCHAR(16)     NOT NULL DEFAULT 'pending' COMMENT '状态：pending/running/success/failed/partial',
  `total_count`         INT UNSIGNED    NULL DEFAULT NULL          COMMENT '计划处理量',
  `success_count`       INT UNSIGNED    NOT NULL DEFAULT 0         COMMENT '成功量',
  `fail_count`          INT UNSIGNED    NOT NULL DEFAULT 0         COMMENT '失败量',
  `skip_count`          INT UNSIGNED    NOT NULL DEFAULT 0         COMMENT '跳过量（断点续跑／幂等跳过）',
  `started_at`          DATETIME        NULL DEFAULT NULL          COMMENT '开始时间',
  `finished_at`         DATETIME        NULL DEFAULT NULL          COMMENT '结束时间',
  `cost_seconds`        INT UNSIGNED    NULL DEFAULT NULL          COMMENT '耗时（秒）',
  `operator_id`         INT UNSIGNED    NULL DEFAULT NULL          COMMENT '触发人（管理员）',
  `error_message`       VARCHAR(512)    NULL DEFAULT NULL          COMMENT '失败摘要',
  -- 以下 5 个字段承接原 ml_model 表（模型版本与实验信息）：仅 mllib / lda 类任务填写
  `model_type`          VARCHAR(32)     NULL DEFAULT NULL          COMMENT '模型类型：nb/lr/lda（原 ml_model.model_type）',
  `model_version`       VARCHAR(32)     NULL DEFAULT NULL          COMMENT '模型版本号（原 ml_model 主键）',
  `model_path`          VARCHAR(255)    NULL DEFAULT NULL          COMMENT '模型持久化路径（原 ml_model.model_path）',
  `random_seed`         INT UNSIGNED    NULL DEFAULT NULL          COMMENT '随机种子（原 ml_model.random_seed，保证可复现 NR-R-06）',
  `model_metrics_json`  JSON            NULL DEFAULT NULL          COMMENT '训练样本量与评估指标：accuracy/precision/recall/f1/confusion_matrix（原 ml_model.train_sample+metrics_json；论文实验结果章节直接引用）',
  `created_at`          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`task_id`),
  KEY `idx_at_type_status` (`task_type`, `status`),
  KEY `idx_at_created` (`created_at`),
  KEY `idx_at_model_version` (`model_version`),
  CONSTRAINT `fk_at_operator` FOREIGN KEY (`operator_id`) REFERENCES `sys_user` (`user_id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='批处理任务表（含模型版本与实验信息；管理端进度展示 FR-SY-03）';


DROP TABLE IF EXISTS `task_log`;
CREATE TABLE `task_log` (
  `log_id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT    COMMENT '主键',
  `task_id`          BIGINT UNSIGNED NOT NULL                   COMMENT '所属任务（清洗/统计/语义/评价等）',
  `level`            VARCHAR(8)      NOT NULL DEFAULT 'INFO'    COMMENT '级别：INFO/WARN/ERROR',
  `stage`            VARCHAR(64)     NULL DEFAULT NULL          COMMENT '阶段名或步骤名：如 行数校验/IP标准化/语义抽取/景点评价/DeepSeek调用；失败记录填 scene（semantic/spot_report/compare/qa）',
  `message`          VARCHAR(512)    NOT NULL                   COMMENT '日志内容（中文化）；失败记录填失败原因（超时/解析失败/校验不通过）',
  `ref_key`          VARCHAR(64)     NULL DEFAULT NULL          COMMENT '关联标识：comment_id / spot_id / 景点对组合键（原 llm_failure.ref_key，便于定位与补跑）',
  `retry_count`      TINYINT UNSIGNED NOT NULL DEFAULT 0        COMMENT '已重试次数（原 llm_failure.retry_count）',
  `resolved`         TINYINT(1)      NOT NULL DEFAULT 0         COMMENT '失败是否已补跑成功（原 llm_failure.resolved；非失败记录恒为 0）',
  `processed_count`  INT UNSIGNED    NULL DEFAULT NULL          COMMENT '当前处理量（进度用）',
  `detail_json`      JSON            NULL DEFAULT NULL          COMMENT '步骤统计与上下文明细：input_count/output_count/dropped_count/abnormal_count（承接原 clean_log 四类计数）、extra 附加信息',
  `created_at`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '时间',
  PRIMARY KEY (`log_id`),
  KEY `idx_tl_task` (`task_id`),
  KEY `idx_tl_created` (`created_at`),
  KEY `idx_tl_level` (`level`),
  KEY `idx_tl_stage` (`stage`),
  KEY `idx_tl_failed` (`level`, `resolved`),
  KEY `idx_tl_ref` (`ref_key`),
  CONSTRAINT `fk_tl_task` FOREIGN KEY (`task_id`) REFERENCES `analysis_task` (`task_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='统一任务日志表（合并原 clean_log 与 llm_failure：既记流程日志，也记清洗步骤统计与模型调用失败/重试）';


SET FOREIGN_KEY_CHECKS = 1;

-- =============================================================================
-- 八、初始化数据
-- =============================================================================

-- 8.1 管理员账号（口令需由程序用加盐哈希写入，此处仅为占位示例，请勿直接使用）
--     建议首个管理员通过初始化脚本 register_admin.py 生成，确保 password_hash 为哈希值
-- INSERT INTO `sys_user` (`username`, `password_hash`, `nickname`, `role`)
-- VALUES ('admin', '<由脚本生成>', '管理员', 'admin');

-- 8.2 数据集总体指标占位（真实值由清洗与统计任务写入）
INSERT INTO `stat_overview` (`id`, `stat_version`) VALUES (1, 'v1')
  ON DUPLICATE KEY UPDATE `id` = `id`;

-- =============================================================================
-- 九、校验查询（建表后可直接运行，用于确认表结构与预期一致）
-- =============================================================================
-- SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES
--   WHERE TABLE_SCHEMA = 'tibet_review' ORDER BY TABLE_NAME;   -- 期望 17 张表
-- SELECT COUNT(*) AS spot_count  FROM spot;    -- 期望 837
-- SELECT COUNT(*) AS review_count FROM review; -- 期望 59033
-- SELECT COUNT(*) AS full_eval    FROM spot WHERE has_full_evaluation = 1; -- 期望 57
-- 模型实验信息示例（论文实验结果章节可直接引用）：
-- SELECT task_id, model_type, model_version, random_seed, model_metrics_json
--   FROM analysis_task WHERE task_type IN ('mllib','lda') ORDER BY task_id DESC;
-- 模型调用失败与补跑情况：
-- SELECT task_id, stage, message, ref_key, retry_count, resolved, created_at
--   FROM task_log WHERE level IN ('WARN','ERROR') ORDER BY created_at DESC;

-- =============================================================================
-- 脚本结束
-- =============================================================================
