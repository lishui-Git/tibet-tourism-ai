package com.tibet.tourism.spark

import java.sql.Timestamp
import java.time.LocalDateTime

import org.apache.spark.ml.classification.NaiveBayes
import org.apache.spark.ml.evaluation.MulticlassClassificationEvaluator
import org.apache.spark.sql.functions._
import org.apache.spark.sql.{Row, SparkSession}

import scala.collection.mutable.ListBuffer

/**
 * C-SPK-04 · MLlib 情感基线组件（三分类训练与批量推理）
 *
 * 设计职责（详细设计 §4.8、§8.1、§8.2）：
 *   · 用评论**星级作为弱标签**训练三分类模型，并对全量评论批量推理；
 *   · 结果写入 `sentiment` 表，`method = 'mllib'`（PK 为 `(comment_id, method)`）；
 *   · **模型版本与实验指标写入 `analysis_task`**：
 *     `model_type` / `model_version` / `model_path` / `random_seed` / `model_metrics_json`；
 *   · 固定随机种子（NR-R-06），保证可复现。
 *
 * ── 标签怎么来（答辩必问）──
 *   数据集没有人工情感标注，因此采用**星级弱标签**：
 *     1–2 星 → negative，3 星 → neutral，4–5 星 → positive
 *   这是"弱监督"做法，不是真实情感标注。它的意义在于：
 *     · 提供一个**可量化的基线**，与阶段五 DeepSeek 的细粒度语义结果做对比实验；
 *     · 论文里可如实说明其局限（高星集中、无法区分方面级情感）。
 *   评分空值（37 条）**不参与训练也不参与推理**——没有标签就没有监督信号。
 *
 * ── 为什么选朴素贝叶斯 ──
 *   · 文本 TF-IDF 特征维度高、样本量中等，朴素贝叶斯在该场景下训练快、基线稳；
 *   · 设计只要求"三分类 + 固定种子 + 记录指标"，并未指定算法族，
 *     选 NB 属于**在冻结范围内的实现选择**，已在 spark/README.md 与开发日报中说明。
 *
 * ── 与其他组件的关系 ──
 *   · 输入特征来自 C-SPK-03（TF-IDF 向量 `features`），不重复分词；
 *   · 输出只进 `sentiment` 表（method=mllib）。`stat_spot.sentiment_positive/neutral/negative`
 *     三列的口径是 **DeepSeek** 占比（建表脚本注释），本组件**不写**那三列。
 */
object Cspk04Sentiment {

  /** 方法标识：与 `sentiment.method` 的取值约定一致（deepseek/mllib/dict）。 */
  val METHOD = "mllib"

  /** 模型类型标识：写入 `analysis_task.model_type`（约定取值 nb/lr/lda）。 */
  val MODEL_TYPE = "nb"

  /** 模型版本号：写入 `analysis_task.model_version`，也作为模型目录名。 */
  val MODEL_VERSION = "mllib-nb-v1"

  /** 训练/测试划分比例（测试集用于产出可写入论文的实验指标）。 */
  private val TRAIN_RATIO = 0.8

  /** 结果行（写库前在 Driver 端构造）。 */
  case class SentimentRow(
      commentId: Long,
      spotId: Int,
      polarity: String,
      confidence: Option[BigDecimal],
      isSpotKnown: Boolean
  )

  /** 一次运行的结果汇总。 */
  case class SentimentResult(
      trainRows: Long,
      testRows: Long,
      predictedRows: Long,
      labelDistribution: Map[String, Long],
      metricsJson: String,
      accuracy: Double,
      modelPath: String,
      predictions: org.apache.spark.sql.DataFrame // comment_id + polarity，供 C-SPK-02 补 sentiment_avg
  )

  /**
   * 执行训练 + 推理（并按需落库）。
   *
   * @param skipDbWrite true 时只训练与评估，不写库
   */
  def run(
      spark: SparkSession,
      loaded: Cspk01DataLoad.Loaded,
      features: Cspk03Features.Features,
      skipDbWrite: Boolean
  ): SentimentResult = {
    val result = train(spark, loaded, features)
    report(result)

    if (skipDbWrite) {
      println("  [C-SPK-04] --skip-db-write 已启用：只训练与推理、不写库。")
    } else {
      write(result, loaded.spotIdByName)
    }
    result
  }

  /** 训练 + 评估 + 全量推理（不落库）。 */
  def train(
      spark: SparkSession,
      loaded: Cspk01DataLoad.Loaded,
      features: Cspk03Features.Features
  ): SentimentResult = {
    import spark.implicits._

    // ---- 1) 标注：星级 → 三分类标签 ----
    // label 列用 Double：MLlib 的分类器要求标签为 Double
    val labeled = features.tfidfDf
      .filter(col("评分").isNotNull) // 37 条缺评分不参与（无监督信号）
      .withColumn(
        "label",
        when(col("评分") >= 4, lit(2.0))   // positive
          .when(col("评分") === 3, lit(1.0)) // neutral
          .otherwise(lit(0.0))              // negative（1–2 星）
      )

    val labeledCount = labeled.count()
    println(s"  [C-SPK-04] 有评分评论数 : $labeledCount（37 条缺评分不参与训练与推理）")

    // ---- 2) 划分训练集/测试集（固定种子，NR-R-06）----
    val Array(trainDf, testDf) = labeled.randomSplit(
      Array(TRAIN_RATIO, 1 - TRAIN_RATIO),
      seed = Config.RANDOM_SEED
    )
    println(s"  [C-SPK-04] 训练集/测试集: ${trainDf.count()} / ${testDf.count()}（种子 ${Config.RANDOM_SEED}）")

    // ---- 3) 训练朴素贝叶斯三分类 ----
    val nb = new NaiveBayes()
      .setLabelCol("label")
      .setFeaturesCol("features")
      .setModelType("multinomial") // TF-IDF 为非负特征，适用多项模型
      .setSmoothing(1.0)
    val model = nb.fit(trainDf)

    // ---- 4) 评估（产出可写入论文的实验指标）----
    val predictions = model.transform(testDf)
    def evaluate(metric: String): Double =
      new MulticlassClassificationEvaluator()
        .setLabelCol("label").setPredictionCol("prediction").setMetricName(metric)
        .evaluate(predictions)

    val accuracy = evaluate("accuracy")
    val f1 = evaluate("f1")
    // 多分类下 precision/recall 取加权平均（按各类真实样本数加权），避免宏平均被长尾类别扰动
    val weightedPrecision = evaluate("weightedPrecision")
    val weightedRecall = evaluate("weightedRecall")

    // 混淆矩阵：行 = 真实标签(0/1/2)，列 = 预测标签
    val confusion = Array.ofDim[Long](3, 3)
    predictions.select(col("label"), col("prediction")).collect().foreach { r =>
      val a = r.getAs[Double]("label").toInt
      val b = r.getAs[Double]("prediction").toInt
      if (a >= 0 && a < 3 && b >= 0 && b < 3) confusion(a)(b) += 1
    }
    val confusionJson = confusion.map(_.mkString("[", ",", "]")).mkString("[", ",", "]")

    // 标签分布（真实，全量有评分评论）
    val labelDist = labeled
      .groupBy(col("label")).agg(count(lit(1)).as("c"))
      .collect()
      .map(r => labelName(r.getAs[Double]("label").toInt) -> r.getAs[Long]("c"))
      .toMap

    // ---- 5) 全量推理（对"建模口径"的全部评论，不只是测试集）----
    val allPredicted = model.transform(features.tfidfDf)
    val predictedRows = allPredicted.count()

    // ---- 6) 持久化模型 ----
    val modelPath = Config.outputDir.resolve("models").resolve(MODEL_VERSION).toString
    java.nio.file.Files.createDirectories(Config.outputDir.resolve("models"))
    model.write.overwrite().save(modelPath)
    println(s"  [C-SPK-04] 模型已持久化 : $modelPath")

    // 指标 JSON：字段名与建表脚本注释一致（accuracy/precision/recall/f1/confusion_matrix）
    val metricsJson =
      s"""{"train_sample":${trainDf.count()},"test_sample":${testDf.count()},""" +
        s""""accuracy":${round4(accuracy)},"precision":${round4(weightedPrecision)},""" +
        s""""recall":${round4(weightedRecall)},"f1":${round4(f1)},""" +
        s""""confusion_matrix":$confusionJson,"labels":["negative","neutral","positive"],""" +
        s""""seed":${Config.RANDOM_SEED},"model":"multinomial_naive_bayes","smoothing":1.0,""" +
        s""""label_rule":"1-2星=negative, 3星=neutral, 4-5星=positive"}"""

    // 推理结果里只需要 comment_id 与极性，交给 C-SPK-02 计算 sentiment_avg
    val predForStats = allPredicted
      .select(col("评论编号").as("comment_id"), col("prediction"))

    SentimentResult(
      trainRows = trainDf.count(),
      testRows = testDf.count(),
      predictedRows = predictedRows,
      labelDistribution = labelDist,
      metricsJson = metricsJson,
      accuracy = accuracy,
      modelPath = modelPath,
      predictions = predForStats
    )
  }

  private def report(result: SentimentResult): Unit = {
    println(s"  [C-SPK-04] 推理评论数   : ${result.predictedRows}")
    println(s"  [C-SPK-04] 标签分布     : " +
      result.labelDistribution.toSeq.sortBy(_._1).map { case (k, v) => s"$k=$v" }.mkString("，"))
    println(f"  [C-SPK-04] 测试集准确率 : ${result.accuracy * 100}%.2f%%")
    println(s"  [C-SPK-04] 实验指标 JSON: ${result.metricsJson}")
    println("  [C-SPK-04][说明] 星级是**弱标签**，非人工情感标注；准确率仅代表"
      + "「模型复现星级划分」的能力，不等同于真实情感识别精度。")
  }

  /**
   * 落库：写 `sentiment`（method='mllib'）与 `analysis_task` 的模型指标。
   *
   * 幂等键 = PK(comment_id, method)，重跑覆盖同一评论的同一方法结果。
   */
  private def write(result: SentimentResult, spotIdByName: Map[String, Int]): Unit = {
    import org.apache.spark.sql.functions._

    // 在 Driver 端把「评论编号 → 景点名称」映射准备好：
    //   预测结果里没有 spot_id，而 sentiment.spot_id 是有外键的必填列。
    //   这里用一次 Spark 读取 review 的 spot_id 并 join spot 取名称，
    //   **不在 executor 里连数据库写结果**（K3：JDBC 连接不跨闭包传递）。
    val spark = result.predictions.sparkSession
    Config.requirePassword()
    val commentToSpotName: Map[Long, String] = {
      val reviewSpot = spark.read.format("jdbc")
        .option("url", Config.jdbcUrl)
        .option("dbtable", "(SELECT comment_id, spot_id FROM review) t")
        .option("user", Config.dbUser)
        .option("password", Config.dbPassword)
        .option("driver", "com.mysql.cj.jdbc.Driver")
        .option("fetchsize", "1000")
        .load()
      val spotName = spark.read.format("jdbc")
        .option("url", Config.jdbcUrl)
        .option("dbtable", "(SELECT spot_id, spot_name FROM spot) t")
        .option("user", Config.dbUser)
        .option("password", Config.dbPassword)
        .option("driver", "com.mysql.cj.jdbc.Driver")
        .load()
      reviewSpot
        .join(spotName, Seq("spot_id"), "left")
        .select(col("comment_id"), col("spot_name"))
        .collect()
        .map(r => Db.toLong(r.getAs[Any]("comment_id")) -> r.getAs[String]("spot_name"))
        .toMap
    }

    val rows = result.predictions
      .select(col("comment_id"), col("prediction"))
      .collect()

    val payload = new ListBuffer[(Long, Int, String)]()
    rows.foreach { r =>
      // comment_id 是 BIGINT UNSIGNED，经 JDBC/Spark 取回可能是 BigDecimal，
      // 故用 Db.toLong 兼容转换（直接 getAs[Long] 会抛 ClassCastException）。
      val commentId = Db.toLong(r.getAs[Any]("comment_id"))
      val pred = Db.toInt(r.getAs[Any]("prediction"))
      for {
        spotName <- commentToSpotName.get(commentId)
        spotId <- spotIdByName.get(spotName)
      } payload += ((commentId, spotId, labelName(pred)))
    }

    val skipped = rows.length - payload.length
    if (skipped > 0) {
      println(s"  [C-SPK-04][警告] 有 $skipped 条预测结果找不到对应 spot_id，已跳过（不写入脏数据）")
    }

    Db.withConnection { conn =>
      val sql =
        s"""INSERT INTO sentiment
           |  (comment_id, spot_id, method, polarity, intensity, confidence, is_valid, raw_json)
           |VALUES (?,?,?,?,?,?,?,?)
           |ON DUPLICATE KEY UPDATE
           |  spot_id = VALUES(spot_id), polarity = VALUES(polarity),
           |  confidence = VALUES(confidence), is_valid = VALUES(is_valid)""".stripMargin

      val n = Db.batchUpdate(conn, sql, payload) { (ps, item) =>
        val (commentId, spotId, polarity) = item
        ps.setLong(1, commentId)
        ps.setInt(2, spotId)
        ps.setString(3, METHOD)
        ps.setString(4, polarity)
        // intensity：本方法不产出 1–5 强度，按设计留 NULL
        ps.setNull(5, java.sql.Types.TINYINT)
        // confidence：朴素贝叶斯可给出概率，但本基线不把概率当"可信度"对外承诺，故留 NULL
        ps.setNull(6, java.sql.Types.DECIMAL)
        ps.setInt(7, 1) // is_valid：本方法无格式校验失败情形，一律 1
        ps.setString(8, null) // raw_json 用于外部模型原始返回，MLlib 无此内容
      }
      println(s"  [C-SPK-04] 已写入 sentiment（method=$METHOD）：$n 行")

      // ---- 任务登记：模型版本与实验指标必须落在 analysis_task ----
      val taskId = Db.insertTask(
        conn, "mllib", "Spark MLlib 情感基线（C-SPK-04）", result.predictedRows
      )
      Db.logStep(
        conn, taskId, "INFO", "情感基线训练与推理",
        s"训练 ${result.trainRows} / 测试 ${result.testRows} / 推理 ${result.predictedRows}",
        Some(s"""{"input_count":${result.predictedRows},"output_count":$n,"dropped_count":$skipped,""" +
          s""""abnormal_count":0,"extra":{"method":"$METHOD","model_type":"$MODEL_TYPE",""" +
          s""""accuracy":${round4(result.accuracy)}}}""")
      )
      Db.finishTask(
        conn, taskId, "success", n.toLong, skipped.toLong, 0L,
        modelType = Some(MODEL_TYPE),
        modelVersion = Some(MODEL_VERSION),
        modelPath = Some(result.modelPath),
        randomSeed = Some(Config.RANDOM_SEED),
        modelMetricsJson = Some(result.metricsJson)
      )
      println(s"  [C-SPK-04] 已登记 analysis_task.task_id=$taskId（含模型指标，可用建表脚本 §9 的 SQL 复核）")
    }
  }

  // ---------------------------------------------------------------------------
  // 辅助
  // ---------------------------------------------------------------------------

  /** 数值标签 → 极性字符串（与 sentiment.polarity 取值一致）。 */
  def labelName(label: Int): String = label match {
    case 2 => "positive"
    case 1 => "neutral"
    case _ => "negative"
  }

  private def round4(d: Double): Double = BigDecimal(d).setScale(4, BigDecimal.RoundingMode.HALF_UP).toDouble
}
