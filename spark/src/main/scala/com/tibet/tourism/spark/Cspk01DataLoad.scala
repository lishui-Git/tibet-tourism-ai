package com.tibet.tourism.spark

import java.nio.file.Files

import org.apache.spark.sql.functions._
import org.apache.spark.sql.types._
import org.apache.spark.sql.{DataFrame, SparkSession}

/**
 * C-SPK-01 · 数据加载组件
 *
 * 设计职责（详细设计 §4.8、§8.2）：
 *   从**清洗后的评论明细**读取数据，产出一份供 C-SPK-02~05 共用的评论 DataFrame，
 *   并把「景点名称 → spot_id」维表一并准备好。
 *
 * 数据源（两条路，字段与口径完全一致；由命令行 --source 选择）：
 *   ① mysql（默认）—— 读库 `tibet_review` 的 `review` 表。
 *      依据：详细设计 §8.1「MySQL ├─③ Spark 统计聚合」、§8.2「③ 统计 输入=review」、
 *      C4容器图 §3「Spark 与 Python 通过 MySQL 解耦」。
 *   ② csv —— 读阶段二产出的清洗版文件（59,033 行 × 23 列，UTF-8 with BOM）。
 *
 * ── 一个刻意的设计决定：评论 DataFrame 里**不放 spot_id** ──
 *   结果表（`stat_spot`、`sentiment`）的外键指向 `spot.spot_id`，所以必须有 spot_id。
 *   但两条数据源拿到 spot_id 的方式不同（MySQL 直接有列，CSV 只有景点名称）。
 *   解决办法不是加 UDF 去查映射（在 executors 里查 Driver 的 Map 既费性能，
 *   又会在名称意外缺失时静默产生 null），而是：
 *     维度统一用「景点名称」，**在写库前于 Driver 端一次性映射为 spot_id**。
 *   这样做的好处：两条数据源的 DataFrame 列集合完全相同，下游组件无需分支；
 *   映射发生在写库这一必经步骤上，且映射失败会立即报错而不是写入脏数据。
 *
 * 关键处理：
 *   · **显式给出 schema**，不让 Spark 推断类型：`评分` 列有 37 条空值，若按字符串推断
 *     会得到 StringType，后续 sum/avg 会静默出错。显式 IntegerType 让空值成为 null，
 *     聚合函数按 SQL 语义忽略 null——与 Python 侧「缺失即 NULL」的口径一致。
 *   · 行数校验：全量必须为 59,033（NR-R-03），不符即终止，避免脏数据进入结果表。
 *   · 分层标记（is_low_info / is_dup_content / dup_group_id / ip_province 等）
 *     **已在阶段二算好**，本模块直接使用，不重复实现清洗规则（职责不重叠）。
 */
object Cspk01DataLoad {

  /**
   * 评论明细统一 schema —— **必须与清洗版 CSV 的 23 列逐列对应**。
   *
   * 为什么强调这一点：阶段二产出的清洗版 CSV 列顺序是
   *   原 15 列（景点名称 … 景点介绍，含 4 个景点级字段）
   *   + 8 个派生列（content_length … dup_group_id）
   * 若 schema 漏掉中间的「地址/开放时间/官方电话/景点介绍」4 列，
   * Spark 会把后面的列**整体左移**读取：`is_low_info` 会读到 `publish_month`、
   * `is_dup_content` 会读到 `dup_group_id`，而且**不会报错**，
   * 只会让下游过滤条件全部失真（本项目实测踩到过，症状是"建模输入行数为 0"）。
   * 因此这里显式声明全部 23 列，并用 `mode=FAILFAST` 保证列数/类型不符时立即失败。
   */
  val ReviewSchema: StructType = StructType(Seq(
    // ---- 原 15 列（评论级 11 + 景点级 4）----
    StructField("景点名称", StringType, nullable = false),
    StructField("评论编号", LongType, nullable = false),
    StructField("评分", IntegerType, nullable = true),
    StructField("评分描述", StringType, nullable = true),
    StructField("评论内容", StringType, nullable = true),
    StructField("发布时间", DateType, nullable = true),
    StructField("IP归属地", StringType, nullable = true),
    StructField("用户昵称", StringType, nullable = true),
    StructField("点赞数", IntegerType, nullable = true),
    StructField("图片数", IntegerType, nullable = true),
    StructField("图片URL", StringType, nullable = true),
    // 景点级 4 字段：只作辅助展示，不作分析维度（BR-09）
    StructField("地址", StringType, nullable = true),
    StructField("开放时间", StringType, nullable = true),
    StructField("官方电话", StringType, nullable = true),
    StructField("景点介绍", StringType, nullable = true),
    // ---- 阶段二派生的 8 个规范化字段 ----
    StructField("content_length", IntegerType, nullable = true),
    StructField("publish_year", IntegerType, nullable = true),
    StructField("publish_month", IntegerType, nullable = true),
    StructField("ip_province", StringType, nullable = true),
    StructField("ip_is_unknown", IntegerType, nullable = true),
    StructField("is_low_info", IntegerType, nullable = true),
    StructField("is_dup_content", IntegerType, nullable = true),
    StructField("dup_group_id", IntegerType, nullable = true)
  ))

  /** 数据源标识。 */
  sealed trait Source { def name: String }
  case object FromMysql extends Source { val name = "mysql" }
  case object FromCsv extends Source { val name = "csv" }

  def parseSource(s: String): Source = s.trim.toLowerCase match {
    case "mysql" | "db" => FromMysql
    case "csv"          => FromCsv
    case other          => throw new IllegalArgumentException(s"未知数据源：$other（可选 mysql / csv）")
  }

  /** 加载结果：评论明细 + 景点维表（名称 → spot_id）。 */
  case class Loaded(reviews: DataFrame, spotIdByName: Map[String, Int], source: Source) {
    def spotCount: Int = spotIdByName.size
  }

  /**
   * 加载评论明细。
   *
   * @param limit 小样本行数（None = 全量）。**只用于验证阶段**，全量运行传 None。
   */
  def load(spark: SparkSession, source: Source, limit: Option[Int] = None): Loaded = {
    val base = source match {
      case FromMysql => loadFromMysql(spark)
      case FromCsv   => loadFromCsv(spark)
    }

    // 小样本模式：**必须随机取样，不能顺序取前 N 行**。
    // 原因：清洗版 CSV 按景点聚集（前 200 行只覆盖 7 个景点），顺序取样的语料过窄，
    // 会让 C-SPK-03 的词表在 minDF=2 下为空、下游 TF-IDF/LDA 无特征可用。
    // 用固定种子保证「同样的 --limit 得到同样的样本」，仍然可复现（NR-R-06）。
    val limited = limit match {
      case Some(n) =>
        val fraction = math.min(1.0, n.toDouble / math.max(1L, base.count()).toDouble)
        println(s"  [C-SPK-01] 小样本模式  : 随机抽样比例 ${"%.4f".format(fraction)}（种子 ${Config.RANDOM_SEED}，非顺序取前 N 行）")
        base.sample(withReplacement = false, fraction = fraction, seed = Config.RANDOM_SEED).limit(n)
      case None => base
    }

    val spotIdByName = loadSpotIds()

    // ---------- 入口把关（C-SPK-01 的校验职责）----------
    val total = base.count()
    println(s"  [C-SPK-01] 配置文件     : ${Config.loadedFrom}")
    println(s"  [C-SPK-01] 数据源       : ${source.name}")
    println(s"  [C-SPK-01] 评论明细行数 : $total")
    println(s"  [C-SPK-01] 景点维表     : ${spotIdByName.size} 个景点（来自 MySQL spot 表）")

    if (limit.isEmpty && total != Config.EXPECTED_REVIEW_ROWS) {
      throw new IllegalStateException(
        s"行数校验未通过：实测 $total 行，设计基准 ${Config.EXPECTED_REVIEW_ROWS} 行（NR-R-03）。已终止分析。"
      )
    }
    if (limit.isEmpty && spotIdByName.size != Config.EXPECTED_SPOT_ROWS) {
      throw new IllegalStateException(
        s"景点数校验未通过：实测 ${spotIdByName.size}，设计基准 ${Config.EXPECTED_SPOT_ROWS}。已终止分析。"
      )
    }

    // 景点名称必须都能对上 spot_id，否则写库时外键必然失败
    // 注意：把 Dataset[String] 取成 Set 需要 Spark 的 String 编码器，故在方法内导入 implicits
    val namesInReviews = {
      import spark.implicits._
      limited.select("景点名称").distinct().as[String].collect().toSet
    }
    val missing = namesInReviews.diff(spotIdByName.keySet.toSet)
    if (missing.nonEmpty) {
      throw new IllegalStateException(
        s"有 ${missing.size} 个景点名称在 spot 表中找不到对应记录，示例：${missing.take(5).mkString("、")}"
      )
    }
    println(s"  [C-SPK-01] 明细涉及景点 : ${namesInReviews.size} 个（全部可关联 spot_id）")

    Loaded(limited, spotIdByName, source)
  }

  /**
   * 从 MySQL 的 `review` 表读取（默认数据源）。
   *
   * 通过「join spot 取 spot_name」后再 select 出统一列，
   * 使 MySQL 与 CSV 两条路产出的 DataFrame **列名、列数、类型完全一致**。
   */
  private def loadFromMysql(spark: SparkSession): DataFrame = {
    Config.requirePassword()

    def jdbc(table: String) = spark.read
      .format("jdbc")
      .option("url", Config.jdbcUrl)
      .option("dbtable", table)
      .option("user", Config.dbUser)
      .option("password", Config.dbPassword)
      .option("driver", "com.mysql.cj.jdbc.Driver")

    // 分批读取，避免 5.9 万行全落进单个分区。
    // 注意：所有 option 必须在 load() 之前链式给出（load() 之后已是 DataFrame）。
    val reviews = jdbc("review")
      .option("partitionColumn", "spot_id")
      .option("lowerBound", "1")
      .option("upperBound", "1000")
      .option("numPartitions", "4")
      .option("fetchsize", "1000")
      .load()

    val spots = jdbc("spot").load().select(col("spot_id"), col("spot_name").as("景点名称"))

    val joined = reviews.join(spots, Seq("spot_id"), "left")

    normalizeNulls(
      joined.select(
        col("景点名称").cast(StringType),
        col("comment_id").as("评论编号"),
        col("score").as("评分"),
        col("score_desc").as("评分描述"),
        col("content").as("评论内容"),
        col("publish_date").as("发布时间"),
        col("ip_location").as("IP归属地"),
        col("user_nick").as("用户昵称"),
        col("like_count").as("点赞数"),
        col("image_count").as("图片数"),
        col("image_urls").as("图片URL"),
        col("content_length"),
        col("publish_year"),
        col("publish_month"),
        col("ip_province"),
        // 关键：MySQL 驱动把 TINYINT(1) 映射为 **boolean**，而 CSV 路径读出来是 int。
        // 两条数据源必须产出完全一致的类型，否则下游 coalesce/比较会报
        // "input to function coalesce should all be the same type, but it's [boolean, int]"。
        col("ip_is_unknown").cast(IntegerType),
        col("is_low_info").cast(IntegerType),
        col("is_dup_content").cast(IntegerType),
        col("dup_group_id")
      )
    )
  }

  /**
   * 从清洗版 CSV 读取（`--source csv`）。
   *
   * 编码：文件为 UTF-8 with BOM。Spark 3.x 在 header=true 时会正确处理 BOM，
   * 不会污染第一个列名；此处仍显式指定 UTF-8 以固定行为。
   *
   * `mode=FAILFAST`：列数/类型不符立即失败，不静默丢行。
   * 另外**先读一次表头与 schema 逐列比对**——因为"列数相同但顺序不同"时
   * Spark 不会报错，只会静默错位（这是本项目实际踩过的坑）。
   */
  private def loadFromCsv(spark: SparkSession): DataFrame = {
    val path = Config.cleanedCsv
    if (!Files.isRegularFile(path)) {
      throw new IllegalStateException(
        s"清洗版 CSV 不存在：$path\n  请先运行阶段二：python -m app.batch.clean_dataset"
      )
    }

    // ---- 表头防线：实际表头必须与 schema 列名逐列一致 ----
    val headerLine = {
      val src = scala.io.Source.fromFile(path.toFile, "UTF-8")
      try src.getLines().next() finally src.close()
    }
    val actual = headerLine.replace("\uFEFF", "").split(",", -1).map(_.trim)
    val expected = ReviewSchema.fieldNames
    if (actual.length != expected.length || actual.zip(expected).exists { case (a, e) => a != e }) {
      throw new IllegalStateException(
        s"清洗版 CSV 表头与预期 schema 不一致，已终止（避免列错位导致静默错误）。\n" +
          s"  实际列数 ${actual.length}：${actual.mkString(" | ")}\n" +
          s"  预期列数 ${expected.length}：${expected.mkString(" | ")}"
      )
    }
    println(s"  [C-SPK-01] 表头校验     : 通过（${actual.length} 列与 schema 逐列一致）")

    val df = spark.read
      .option("header", "true")
      .option("encoding", "UTF-8")
      .option("dateFormat", "yyyy-MM-dd")
      .option("nullValue", "")
      .option("mode", "FAILFAST")
      .schema(ReviewSchema)
      .csv(path.toString)

    normalizeNulls(df)
  }

  /**
   * 把计数类派生列的空值归零。
   *
   * 为什么要做：`like_count` / `image_count` / `content_length` 与四个标记列在表中
   * 都是 `NOT NULL DEFAULT 0`。若聚合时出现 null，写库会被 MySQL 拒绝或写入 null，
   * 因此统一 coalesce 为 0——这不是"填补数据"，而是把「没有值」表达为设计规定的默认值。
   * `评分` / `评论内容` **不做**这种处理：它们允许为空，缺失就应如实保留 null。
   */
  private def normalizeNulls(df: DataFrame): DataFrame =
    df.withColumn("点赞数", coalesce(col("点赞数"), lit(0)))
      .withColumn("图片数", coalesce(col("图片数"), lit(0)))
      .withColumn("content_length", coalesce(col("content_length"), lit(0)))
      .withColumn("is_low_info", coalesce(col("is_low_info"), lit(0)))
      .withColumn("is_dup_content", coalesce(col("is_dup_content"), lit(0)))
      .withColumn("ip_is_unknown", coalesce(col("ip_is_unknown"), lit(0)))

  /**
   * 读景点维表：spot_name → spot_id。
   *
   * 为什么必须从 MySQL 读而不是给 CSV 自己编号：
   *   `stat_spot.spot_id` 有外键指向 `spot.spot_id`，`sentiment.spot_id` 同理。
   *   自己编号必然外键失败。所以维表以数据库为唯一准绳。
   */
  private def loadSpotIds(): Map[String, Int] = {
    Db.withConnection { conn =>
      Db.queryRows(conn, "SELECT spot_id, spot_name FROM spot")
        .map(r => r("spot_name").toString -> r("spot_id").toString.toInt)
        .toMap
    }
  }
}
