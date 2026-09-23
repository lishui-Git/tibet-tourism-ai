package com.tibet.tourism.spark

/**
 * 结果表写入自检（不属于 C-SPK-01~05 组件，是作业收尾的**复核步骤**）。
 *
 * 为什么需要它：Spark 作业"成功结束"不等于"结果正确"。
 *   本对象在写库完成后，用 SQL 直接查结果表核对关键数量与口径，
 *   把"可解释的验证证据"打在终端上，便于答辩与开发日报留痕。
 *
 * 复核内容：
 *   1. 核心表未被破坏：spot = 837、review = 59,033、完整评价资格 = 57
 *   2. stat_spot 行数 = 837（一景一行），且评论量合计 = review 行数、逐景点一致
 *   3. 好评率/差评率是否落在 [0,1]
 *   4. stat_ip 的全局分母 = 34,578、占比合计 ≈ 1
 *   5. 各结果表行数汇总
 *   6. analysis_task 中 mllib/lda 任务的模型版本与指标（建表脚本 §9 的复核 SQL）
 *   7. global 主题词抽样（人工判读主题是否可解释）
 */
object SelfCheck {

  def run(spark: org.apache.spark.sql.SparkSession): Unit = {
    Db.withConnection { conn =>
      def one(sql: String): Long = Db.queryLong(conn, sql)

      println("  [自检] ---- 核心表是否被破坏（必须与阶段一/二一致）----")
      val spots = one("SELECT COUNT(*) FROM spot")
      val reviews = one("SELECT COUNT(*) FROM review")
      check("spot 行数", spots, Config.EXPECTED_SPOT_ROWS)
      check("review 行数", reviews, Config.EXPECTED_REVIEW_ROWS)
      check("具备完整评价资格的景点数", one("SELECT COUNT(*) FROM spot WHERE has_full_evaluation = 1"), 57L)

      println("  [自检] ---- 本阶段写入的结果表 ----")
      val statSpot = one("SELECT COUNT(*) FROM stat_spot")
      println(s"      stat_spot   = $statSpot")
      println(s"      stat_time   = ${one("SELECT COUNT(*) FROM stat_time")}")
      println(s"      stat_ip     = ${one("SELECT COUNT(*) FROM stat_ip")}")
      println(s"      sentiment(method=${Cspk04Sentiment.METHOD}) = " +
        s"${one(s"SELECT COUNT(*) FROM sentiment WHERE method='${Cspk04Sentiment.METHOD}'")}")
      println(s"      topic       = ${one("SELECT COUNT(*) FROM topic")}")
      println(s"      topic_word  = ${one("SELECT COUNT(*) FROM topic_word")}")
      check("stat_spot 行数", statSpot, Config.EXPECTED_SPOT_ROWS)

      println("  [自检] ---- 一致性交叉核对 ----")
      check("stat_spot.review_count 合计", one("SELECT COALESCE(SUM(review_count),0) FROM stat_spot"), reviews)
      val mismatch = one(
        """SELECT COUNT(*) FROM (
          |  SELECT s.spot_id, s.review_count, COUNT(r.comment_id) AS actual
          |    FROM stat_spot s LEFT JOIN review r ON r.spot_id = s.spot_id
          |   GROUP BY s.spot_id, s.review_count
          |  HAVING s.review_count <> actual
          |) t""".stripMargin
      )
      println(s"      stat_spot 与 review 评论量不一致的景点数 = $mismatch（应为 0）")
      if (mismatch != 0) println("      [警告] 存在评论量不一致的景点，请检查聚合逻辑")

      val badRate = one(
        "SELECT COUNT(*) FROM stat_spot WHERE positive_rate < 0 OR positive_rate > 1 " +
          "OR negative_rate < 0 OR negative_rate > 1"
      )
      println(s"      好评率/差评率越界的景点数 = $badRate（应为 0）")

      check(
        "stat_ip 全局分母",
        one("SELECT COALESCE(MAX(sample_size),0) FROM stat_ip WHERE scope_type='global'"),
        Config.EXPECTED_IP_SAMPLE_SIZE
      )
      val ratioSum = asDouble(
        Db.queryRows(conn, "SELECT COALESCE(SUM(ratio),0) AS s FROM stat_ip WHERE scope_type='global'").head.get("s")
      )
      println(f"      stat_ip 全局占比合计 = $ratioSum%.4f（应≈1.0）")

      println("  [自检] ---- 模型实验信息（建表脚本 §9 复核 SQL）----")
      val tasks = Db.queryRows(
        conn,
        """SELECT task_id, task_type, status, model_type, model_version, random_seed, cost_seconds,
          |       LEFT(COALESCE(model_metrics_json,''), 110) AS metrics
          |  FROM analysis_task
          | WHERE task_type IN ('mllib','lda')
          | ORDER BY task_id DESC LIMIT 5""".stripMargin
      )
      if (tasks.isEmpty) {
        println("      [警告] 未找到 mllib/lda 任务记录（模型指标应写入 analysis_task）")
      } else {
        tasks.foreach { t =>
          println(s"      task_id=${str(t.get("task_id"))} ${str(t.get("task_type"))} " +
            s"${str(t.get("status"))} model=${str(t.get("model_type"))} " +
            s"ver=${str(t.get("model_version"))} seed=${str(t.get("random_seed"))} " +
            s"cost=${str(t.get("cost_seconds"))}s")
          println(s"        metrics=${str(t.get("metrics"))}")
        }
      }

      println("  [自检] ---- global 主题词抽样（人工判读主题是否可解释）----")
      val sample = Db.queryRows(
        conn,
        """SELECT t.topic_index, t.topic_rate, w.word, w.rank_no
          |  FROM topic t JOIN topic_word w ON w.topic_id = t.topic_id
          | WHERE t.scope_type = 'global' AND w.rank_no <= 5
          | ORDER BY t.topic_index, w.rank_no""".stripMargin
      )
      sample
        .groupBy(r => str(r.get("topic_index")))
        .toSeq
        .sortBy(_._1)
        .foreach { case (idx, rows) =>
          val words = rows
            .sortBy(r => asDouble(r.get("rank_no")).toInt)
            .map(r => str(r.get("word")))
            .mkString(" / ")
          println(f"      主题 $idx%2s  占比 ${str(rows.head.get("topic_rate"))}%8s  $words")
        }

      println("  [自检] ---- 景点级主题覆盖情况 ----")
      val spotTopicScopes = one("SELECT COUNT(DISTINCT scope_id) FROM topic WHERE scope_type='spot'")
      println(s"      有景点级主题的景点数 = $spotTopicScopes（预期 52：可建模评论数 ≥100 的景点）")
      val spotTopicRows = one("SELECT COUNT(*) FROM topic WHERE scope_type='spot'")
      println(s"      景点级主题行数       = $spotTopicRows（预期 52 × K = ${52 * Config.LDA_TOPICS}）")
    }
  }

  private def check(name: String, actual: Long, expected: Long): Unit = {
    val ok = actual == expected
    val mark = if (ok) "OK " else "不符"
    println(f"      [$mark] $name%-28s 实测 $actual%7d / 期望 $expected%7d")
  }

  /** 把 Db.queryRows 取出的 Any 值转为可打印字符串（解包 Option 与 null）。 */
  private def str(v: Any): String = v match {
    case null    => ""
    case Some(x) => str(x)
    case None    => ""
    case other   => other.toString
  }

  /** 把 Db.queryRows 取出的 Any 值转为 Double（兼容 BigDecimal / Option / 字符串）。 */
  private def asDouble(v: Any): Double = v match {
    case null                    => 0.0
    case Some(x)                 => asDouble(x)
    case None                    => 0.0
    case d: java.math.BigDecimal => d.doubleValue()
    case n: java.lang.Number     => n.doubleValue()
    case s: String               => s.trim.toDouble
    case other                   => other.toString.trim.toDouble
  }
}
