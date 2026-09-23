package com.tibet.tourism.spark

import org.apache.spark.sql.functions._
import org.apache.spark.sql.{Column, DataFrame, SparkSession}

import scala.collection.mutable.ListBuffer

/**
 * C-SPK-02 · 统计聚合组件
 *
 * 设计职责（详细设计 §4.8、§8.1、§8.2）：
 *   用 Spark SQL 做多维聚合，产出三张统计结果表：
 *     · `stat_spot`  一景一行：评论量／均分／评分分布／好评率／差评率／图文率／点赞数／
 *                    低信息量占比／有效 IP 样本量／首末评论日期
 *     · `stat_time`  时间维度：year 与 month 两种周期 × global 与 spot 两种范围
 *     · `stat_ip`    客源地维度：**仅用 2022-08-01 及之后的评论**（BR-01）
 *
 * 设计文档没有明说的几处，本文件采用的取值与理由（答辩会被问到）：
 *   1. **时间粒度取 year 与 month**：`stat_time.period_type` 注释写「周期类型：year/month」，
 *      未提日粒度，故不自行增加 day。
 *   2. **景点级时间只做 year，不做 month**：837 个景点 × 上百个月会产生大量
 *      样本量极小（多为 1–2 条）的行，既无展示价值也容易误导趋势判断。
 *   3. **stat_time.sentiment_avg 本组件不填**：该列语义是「情感均值（正=1/中=0.5/负=0）」，
 *      其数据来源是 C-SPK-04 的 MLlib 判定结果。为保持组件单一职责，
 *      本组件不依赖 04 的输出；该列留 NULL，由后续阶段补写。
 *
 * 关键口径（全部来自设计文档，不得在此处"顺手"改动）：
 *   · 好评率 positive_rate = 4–5 星占比          （BRD FR-SA-02 原文）
 *   · 差评率 negative_rate = 1–2 星占比          （BRD FR-SA-02 原文）
 *   · 图文率 image_rate    = 有图评论数 / 评论量  （image_count > 0 记有图）
 *   · 低信息量占比 low_info_rate = is_low_info=1 占比（BR-05，阈值 10 字，阶段二已标记）
 *   · 客源地 sample_size   = 该范围内 2022-08 后的有效样本量（全局应为 35,098）
 *   · 评分空值（37 条）**不计入好评/差评分子，也不计入分母**，避免把"未评分"当成差评
 *
 * 幂等：三张表都用 INSERT ... ON DUPLICATE KEY UPDATE（§8.2 给出的幂等键），
 *       重跑覆盖同一主键/唯一键的行，不产生重复数据。
 *
 * 关于 stat_spot 的 sentiment_* 三列：其口径是 **DeepSeek 正面/中性/负面占比**
 *   （建表脚本注释原文），本阶段不写、保持 NULL，留给阶段五语义抽取；
 *   MLlib 的情感结果只进 `sentiment` 表（C-SPK-04），两者并列、互不覆盖。
 */
object Cspk02Stats {

  /** stat_spot 一行的业务字段（写库时再补 spot_id 与 stat_version）。 */
  case class StatSpotRow(
      spotName: String,
      reviewCount: Long,
      avgScore: Option[BigDecimal],
      score1: Long,
      score2: Long,
      score3: Long,
      score4: Long,
      score5: Long,
      positiveRate: Option[BigDecimal],
      negativeRate: Option[BigDecimal],
      imageRate: BigDecimal,
      totalLikes: Long,
      lowInfoRate: BigDecimal,
      validIpSample: Long,
      firstCommentDate: Option[java.sql.Date],
      lastCommentDate: Option[java.sql.Date]
  )

  /** stat_time 一行；`spotName` 仅在 scopeType=spot 时有值，写库时映射为 scope_id。 */
  case class StatTimeRow(
      periodType: String,
      period: String,
      scopeType: String,
      spotName: Option[String],
      reviewCount: Long,
      avgScore: Option[BigDecimal]
  )

  /** stat_ip 一行；`spotName` 仅在 scopeType=spot 时有值。 */
  case class StatIpRow(
      scopeType: String,
      spotName: Option[String],
      ipProvince: String,
      reviewCount: Long,
      ratio: BigDecimal,
      sampleSize: Long,
      isOverseas: Int
  )

  /** 统计结果（纯数据，便于小样本验证时不写库也能核对）。 */
  case class StatsResult(
      spotRows: Seq[StatSpotRow],
      timeRows: Seq[StatTimeRow],
      ipRows: Seq[StatIpRow],
      globalReviewCount: Long,
      validReviewCount: Long,
      globalValidIp: Long,
      lowInfoCount: Long
  )

  /** 中国 31 个省级行政区简称（数据集中已标准化为该口径；其余记为境外/港澳台）。 */
  private[spark] val DOMESTIC_PROVINCES: Set[String] = Set(
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西",
    "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆"
  )

  /** 有效 IP 的过滤条件：ip_province 非空即「2022-08 后且归属地已知」（BR-01 已在阶段二落实）。 */
  private val VIP_FILTER: Column =
    col("ip_province").isNotNull && length(trim(col("ip_province"))) > 0

  /** 统计范围开关：景点级 IP 明细（837 景点 × 省级）行数较多，小样本验证时可关闭。 */
  @volatile private var includeSpotIp: Boolean = true

  def setIncludeSpotIp(v: Boolean): Unit = includeSpotIp = v

  /**
   * 执行统计聚合（计算 + 打印 + 可选落库）。
   *
   * @param skipDbWrite true 时只计算并打印，不写数据库（验证阶段使用）
   */
  def run(spark: SparkSession, loaded: Cspk01DataLoad.Loaded, skipDbWrite: Boolean): StatsResult = {
    val result = compute(loaded)
    report(result)

    if (skipDbWrite) {
      println("  [C-SPK-02] --skip-db-write 已启用：只计算、不写库。")
    } else {
      write(result, loaded.spotIdByName)
    }
    result
  }

  /** 纯计算部分（不落库）。 */
  def compute(loaded: Cspk01DataLoad.Loaded): StatsResult = {
    val reviews = loaded.reviews

    val totalCount = reviews.count()
    val validReviews = reviews.filter(col("评分").isNotNull)

    // ---------------------------------------------------------------------
    // 1) stat_spot —— 按景点名称聚合
    // ---------------------------------------------------------------------
    val spotAgg = reviews
      .groupBy(col("景点名称"))
      .agg(
        count(lit(1)).as("review_count"),
        avg(col("评分")).as("avg_score"),
        sum(when(col("评分") === 1, 1).otherwise(0)).as("score_1"),
        sum(when(col("评分") === 2, 1).otherwise(0)).as("score_2"),
        sum(when(col("评分") === 3, 1).otherwise(0)).as("score_3"),
        sum(when(col("评分") === 4, 1).otherwise(0)).as("score_4"),
        sum(when(col("评分") === 5, 1).otherwise(0)).as("score_5"),
        // 好评/差评的分母只算"有评分"的评论
        sum(when(col("评分") >= 4, 1).otherwise(0)).as("pos_count"),
        sum(when(col("评分") <= 2, 1).otherwise(0)).as("neg_count"),
        sum(when(col("图片数") > 0, 1).otherwise(0)).as("image_pos"),
        sum(col("点赞数")).as("total_likes"),
        sum(when(col("is_low_info") === 1, 1).otherwise(0)).as("low_info_count"),
        sum(when(VIP_FILTER, 1).otherwise(0)).as("valid_ip_sample"),
        min(col("发布时间")).as("first_comment_date"),
        max(col("发布时间")).as("last_comment_date")
      )

    // 每个景点的"有评分评论数"，用于好评率/差评率分母
    val scoredBySpot: Map[String, Long] = validReviews
      .groupBy(col("景点名称"))
      .agg(count(lit(1)).as("scored"))
      .collect()
      .map(r => r.getAs[String]("景点名称") -> r.getAs[Long]("scored"))
      .toMap

    val spotRows = spotAgg.collect().map { r =>
      val name = r.getAs[String]("景点名称")
      val reviewCount = r.getAs[Long]("review_count")
      val scored = scoredBySpot.getOrElse(name, 0L)
      StatSpotRow(
        spotName = name,
        reviewCount = reviewCount,
        avgScore = decimal(r.getAs[java.lang.Double]("avg_score")),
        score1 = r.getAs[Long]("score_1"),
        score2 = r.getAs[Long]("score_2"),
        score3 = r.getAs[Long]("score_3"),
        score4 = r.getAs[Long]("score_4"),
        score5 = r.getAs[Long]("score_5"),
        positiveRate = ratio(r.getAs[Long]("pos_count"), scored),
        negativeRate = ratio(r.getAs[Long]("neg_count"), scored),
        imageRate = ratioOrZero(r.getAs[Long]("image_pos"), reviewCount),
        totalLikes = Option(r.getAs[java.lang.Long]("total_likes")).map(_.longValue()).getOrElse(0L),
        lowInfoRate = ratioOrZero(r.getAs[Long]("low_info_count"), reviewCount),
        validIpSample = r.getAs[Long]("valid_ip_sample"),
        firstCommentDate = Option(r.getAs[java.sql.Date]("first_comment_date")),
        lastCommentDate = Option(r.getAs[java.sql.Date]("last_comment_date"))
      )
    }.sortBy(_.spotName)

    // ---------------------------------------------------------------------
    // 2) stat_time —— year / month × global / spot
    // ---------------------------------------------------------------------
    val timeRows = ListBuffer[StatTimeRow]()

    /** 全局时间聚合（按年、按月）。 */
    def globalTime(periodExpr: Column, alias: String, periodType: String): Unit = {
      val df = reviews
        .withColumn(alias, periodExpr)
        .filter(col(alias).isNotNull)
        .groupBy(col(alias))
        .agg(count(lit(1)).as("review_count"), avg(col("评分")).as("avg_score"))
      df.collect().foreach { r =>
        timeRows += StatTimeRow(
          periodType = periodType,
          period = r.getAs[String](alias),
          scopeType = "global",
          spotName = None,
          reviewCount = r.getAs[Long]("review_count"),
          avgScore = decimal(r.getAs[java.lang.Double]("avg_score"))
        )
      }
    }

    globalTime(year(col("发布时间")).cast("string"), "period", "year")
    globalTime(date_format(col("发布时间"), "yyyy-MM"), "period", "month")

    // 景点级：只做年（理由见文件头说明 2）
    val spotTimeDf = reviews
      .withColumn("period", year(col("发布时间")).cast("string"))
      .filter(col("period").isNotNull)
      .groupBy(col("景点名称"), col("period"))
      .agg(count(lit(1)).as("review_count"), avg(col("评分")).as("avg_score"))
    spotTimeDf.collect().foreach { r =>
      timeRows += StatTimeRow(
        periodType = "year",
        period = r.getAs[String]("period"),
        scopeType = "spot",
        spotName = Some(r.getAs[String]("景点名称")),
        reviewCount = r.getAs[Long]("review_count"),
        avgScore = decimal(r.getAs[java.lang.Double]("avg_score"))
      )
    }

    // ---------------------------------------------------------------------
    // 3) stat_ip —— 仅 2022-08 之后（BR-01）
    // ---------------------------------------------------------------------
    val validIp = reviews.filter(VIP_FILTER)
    val globalValidIp = validIp.count()

    val globalIpRows = validIp
      .groupBy(col("ip_province"))
      .agg(count(lit(1)).as("review_count"))
      .collect()
      .map { r =>
        val province = r.getAs[String]("ip_province")
        val c = r.getAs[Long]("review_count")
        StatIpRow("global", None, province, c, ratioOrZero(c, globalValidIp), globalValidIp, overseas(province))
      }.toSeq

    val spotIpRows = if (!includeSpotIp) Seq.empty else {
      // 先算「景点 × 省份」的评论量，再用窗口函数取该景点的有效样本总量作为分母
      val bySpotProvince = validIp
        .groupBy(col("景点名称"), col("ip_province"))
        .agg(count(lit(1)).as("review_count"))
      val withSample = bySpotProvince
        .withColumn("sample_size", sum(col("review_count")).over(
          org.apache.spark.sql.expressions.Window.partitionBy(col("景点名称"))
        ))
      withSample.collect().map { r =>
        val province = r.getAs[String]("ip_province")
        val c = r.getAs[Long]("review_count")
        val sample = r.getAs[Long]("sample_size")
        StatIpRow(
          scopeType = "spot",
          spotName = Some(r.getAs[String]("景点名称")),
          ipProvince = province,
          reviewCount = c,
          ratio = ratioOrZero(c, sample),
          sampleSize = sample,
          isOverseas = overseas(province)
        )
      }.toSeq
    }

    StatsResult(
      spotRows = spotRows,
      timeRows = timeRows.toSeq,
      ipRows = globalIpRows ++ spotIpRows,
      globalReviewCount = totalCount,
      validReviewCount = validReviews.count(),
      globalValidIp = globalValidIp,
      lowInfoCount = reviews.filter(col("is_low_info") === 1).count()
    )
  }

  // ---------------------------------------------------------------------------
  // 报告
  // ---------------------------------------------------------------------------
  private def report(result: StatsResult): Unit = {
    val spotRows = result.spotRows
    val globalIp = result.ipRows.count(_.scopeType == "global")
    val spotIp = result.ipRows.count(_.scopeType == "spot")

    println(s"  [C-SPK-02] stat_spot      : ${spotRows.size} 个景点")
    println(s"  [C-SPK-02] stat_time      : ${result.timeRows.size} 行" +
      s"（global ${result.timeRows.count(_.scopeType == "global")} + spot ${result.timeRows.count(_.scopeType == "spot")}）")
    println(s"  [C-SPK-02] stat_ip        : ${result.ipRows.size} 行（global $globalIp + spot $spotIp）")
    println(s"  [C-SPK-02] 评论总数       : ${result.globalReviewCount}")
    println(s"  [C-SPK-02] 有评分评论数   : ${result.validReviewCount}")
    println(s"  [C-SPK-02] stat_ip 分母   : ${result.globalValidIp}" +
      s"（2022-08 后且归属地已知，基准 ${Config.EXPECTED_IP_SAMPLE_SIZE}）")
    println(s"  [C-SPK-02] 低信息量评论数 : ${result.lowInfoCount}（占 ${"%.2f".format(
      if (result.globalReviewCount > 0) result.lowInfoCount.toDouble / result.globalReviewCount * 100 else 0.0)}%）")

    // 口径自检（两个指标不要混淆，实测已核对）：
    //   stat_ip.sample_size          = 34,578（2022-08 后且归属地已知）
    //   stat_overview.valid_ip_sample = 35,098（2022-08 后全部，含"未知" 520 条）
    if (result.globalReviewCount == Config.EXPECTED_REVIEW_ROWS
        && result.globalValidIp != Config.EXPECTED_IP_SAMPLE_SIZE) {
      println(s"  [C-SPK-02][警告] stat_ip 分母与基准不符：实测 ${result.globalValidIp}，" +
        s"期望 ${Config.EXPECTED_IP_SAMPLE_SIZE}（= ${Config.EXPECTED_VALID_IP_SAMPLE} − " +
        s"${Config.EXPECTED_IP_UNKNOWN_AFTER_CUTOFF} 条未知）")
    }

    println("  [C-SPK-02] 评论量 Top5（复核用）：")
    spotRows.sortBy(-_.reviewCount).take(5).foreach { s =>
      val avg = s.avgScore.map(_.toString).getOrElse("—")
      val pos = s.positiveRate.map(v => f"${v.toDouble * 100}%.2f%%").getOrElse("—")
      println(f"      ${s.spotName}%-16s 评论 ${s.reviewCount}%6d  均分 $avg%5s  好评率 $pos%8s  有效IP ${s.validIpSample}%6d")
    }
  }

  // ---------------------------------------------------------------------------
  // 落库（全部使用 §8.2 给出的幂等键）
  // ---------------------------------------------------------------------------
  private def write(result: StatsResult, spotIdByName: Map[String, Int]): Unit = {
    def spotId(name: String, ctx: String): Int = spotIdByName.getOrElse(
      name, throw new IllegalStateException(s"$ctx 写库失败：景点「$name」在 spot 表中不存在")
    )

    Db.withConnection { conn =>
      // ---- stat_spot（幂等键 = PK(spot_id)）----
      val spotSql =
        """INSERT INTO stat_spot
          |  (spot_id, review_count, avg_score, score_1, score_2, score_3, score_4, score_5,
          |   positive_rate, negative_rate, image_rate, total_likes, low_info_rate,
          |   valid_ip_sample, first_comment_date, last_comment_date, stat_version)
          |VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          |ON DUPLICATE KEY UPDATE
          |  review_count = VALUES(review_count), avg_score = VALUES(avg_score),
          |  score_1 = VALUES(score_1), score_2 = VALUES(score_2), score_3 = VALUES(score_3),
          |  score_4 = VALUES(score_4), score_5 = VALUES(score_5),
          |  positive_rate = VALUES(positive_rate), negative_rate = VALUES(negative_rate),
          |  image_rate = VALUES(image_rate), total_likes = VALUES(total_likes),
          |  low_info_rate = VALUES(low_info_rate), valid_ip_sample = VALUES(valid_ip_sample),
          |  first_comment_date = VALUES(first_comment_date),
          |  last_comment_date = VALUES(last_comment_date), stat_version = VALUES(stat_version)""".stripMargin

      val n1 = Db.batchUpdate(conn, spotSql, result.spotRows) { (ps, s) =>
        ps.setInt(1, spotId(s.spotName, "stat_spot"))
        ps.setLong(2, s.reviewCount)
        ps.setBigDecimal(3, s.avgScore.map(_.bigDecimal).orNull)
        ps.setLong(4, s.score1); ps.setLong(5, s.score2); ps.setLong(6, s.score3)
        ps.setLong(7, s.score4); ps.setLong(8, s.score5)
        ps.setBigDecimal(9, s.positiveRate.map(_.bigDecimal).orNull)
        ps.setBigDecimal(10, s.negativeRate.map(_.bigDecimal).orNull)
        ps.setBigDecimal(11, s.imageRate.bigDecimal)
        ps.setLong(12, s.totalLikes)
        ps.setBigDecimal(13, s.lowInfoRate.bigDecimal)
        ps.setLong(14, s.validIpSample)
        ps.setDate(15, s.firstCommentDate.orNull)
        ps.setDate(16, s.lastCommentDate.orNull)
        ps.setString(17, Config.STAT_VERSION)
      }
      println(s"  [C-SPK-02] 已写入 stat_spot ：$n1 行")

      // ---- stat_time（幂等键 = UK(period_type, period, scope_type, scope_id)）----
      val timeSql =
        """INSERT INTO stat_time
          |  (period_type, period, scope_type, scope_id, review_count, avg_score, sentiment_avg, stat_version)
          |VALUES (?,?,?,?,?,?,?,?)
          |ON DUPLICATE KEY UPDATE
          |  review_count = VALUES(review_count), avg_score = VALUES(avg_score),
          |  sentiment_avg = VALUES(sentiment_avg), stat_version = VALUES(stat_version)""".stripMargin

      val globalTime = result.timeRows.filter(_.scopeType == "global").map(t => (0, t))
      val spotTime = result.timeRows.filter(_.scopeType == "spot").map { t =>
        (spotId(t.spotName.getOrElse(throw new IllegalStateException("景点级时间行缺少景点名称")), "stat_time"), t)
      }
      val nTime = Db.batchUpdate(conn, timeSql, globalTime ++ spotTime) { (ps, item) =>
        val (scopeId, t) = item
        ps.setString(1, t.periodType)
        ps.setString(2, t.period)
        ps.setString(3, t.scopeType)
        ps.setInt(4, scopeId)
        ps.setLong(5, t.reviewCount)
        ps.setBigDecimal(6, t.avgScore.map(_.bigDecimal).orNull)
        // sentiment_avg 由 C-SPK-04 的 MLlib 结果驱动，本组件不填（见文件头说明 3）
        ps.setBigDecimal(7, null)
        ps.setString(8, Config.STAT_VERSION)
      }
      println(s"  [C-SPK-02] 已写入 stat_time ：$nTime 行（global ${globalTime.size} + spot ${spotTime.size}）")

      // ---- stat_ip（幂等键 = UK(scope_type, scope_id, ip_province)）----
      val ipSql =
        """INSERT INTO stat_ip
          |  (scope_type, scope_id, ip_province, review_count, ratio, sample_size, is_overseas, stat_version)
          |VALUES (?,?,?,?,?,?,?,?)
          |ON DUPLICATE KEY UPDATE
          |  review_count = VALUES(review_count), ratio = VALUES(ratio),
          |  sample_size = VALUES(sample_size), is_overseas = VALUES(is_overseas),
          |  stat_version = VALUES(stat_version)""".stripMargin

      val globalIp = result.ipRows.filter(_.scopeType == "global").map(r => (0, r))
      val spotIp = result.ipRows.filter(_.scopeType == "spot").map { r =>
        (spotId(r.spotName.getOrElse(throw new IllegalStateException("景点级 IP 行缺少景点名称")), "stat_ip"), r)
      }
      val nIp = Db.batchUpdate(conn, ipSql, globalIp ++ spotIp) { (ps, item) =>
        val (scopeId, r) = item
        ps.setString(1, r.scopeType)
        ps.setInt(2, scopeId)
        ps.setString(3, r.ipProvince)
        ps.setLong(4, r.reviewCount)
        ps.setBigDecimal(5, r.ratio.bigDecimal)
        ps.setLong(6, r.sampleSize)
        ps.setInt(7, r.isOverseas)
        ps.setString(8, Config.STAT_VERSION)
      }
      println(s"  [C-SPK-02] 已写入 stat_ip   ：$nIp 行（global ${globalIp.size} + spot ${spotIp.size}）")

      // ---- 登记任务与步骤统计（沿用 Python 侧 stage + detail_json 口径）----
      val taskId = Db.insertTask(conn, "stat", "Spark 统计聚合（C-SPK-02）", result.globalReviewCount)
      Db.logStep(
        conn, taskId, "INFO", "统计聚合",
        s"stat_spot ${result.spotRows.size} 行 / stat_time $nTime 行 / stat_ip $nIp 行",
        Some(
          s"""{"input_count":${result.globalReviewCount},"output_count":$nTime,"dropped_count":0,"abnormal_count":0,""" +
            s""""extra":{"stat_spot":${result.spotRows.size},"stat_time":$nTime,"stat_ip":$nIp,""" +
            s""""valid_ip_sample":${result.globalValidIp},"stat_version":"${Config.STAT_VERSION}"}}"""
        )
      )
      Db.finishTask(conn, taskId, "success", n1.toLong, 0L, 0L)
      println(s"  [C-SPK-02] 已登记 analysis_task.task_id=$taskId")
    }
  }

  // ---------------------------------------------------------------------------
  // 数值辅助
  // ---------------------------------------------------------------------------

  private[spark] def overseas(province: String): Int =
    if (DOMESTIC_PROVINCES.contains(province)) 0 else 1

  /** Double → BigDecimal（保留 2 位，匹配 DECIMAL(3,2)）；null → None。 */
  private def decimal(d: java.lang.Double): Option[BigDecimal] =
    Option(d).map(v => BigDecimal(v).setScale(2, BigDecimal.RoundingMode.HALF_UP))

  /** part/whole 占比，保留 4 位小数（匹配 DECIMAL(5,4)/(6,4)）；分母 ≤0 → None。 */
  private def ratio(part: Long, whole: Long): Option[BigDecimal] =
    if (whole <= 0) None
    else Some(BigDecimal(part.toDouble / whole).setScale(4, BigDecimal.RoundingMode.HALF_UP))

  /** 同上，但分母为 0 时返回 0（用于 NOT NULL 列如 image_rate / low_info_rate）。 */
  private def ratioOrZero(part: Long, whole: Long): BigDecimal =
    ratio(part, whole).getOrElse(BigDecimal(0).setScale(4))
}
