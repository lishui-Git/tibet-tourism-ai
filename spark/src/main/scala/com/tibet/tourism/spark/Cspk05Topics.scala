package com.tibet.tourism.spark

import org.apache.spark.ml.clustering.{LDA, LDAModel}
import org.apache.spark.sql.functions._
import org.apache.spark.sql.{DataFrame, SparkSession}

import scala.collection.mutable.ListBuffer

/**
 * C-SPK-05 · LDA 主题发现组件
 *
 * 设计职责（详细设计 §4.8、§8.2）：
 *   用 LDA 做主题发现，结果写入 `topic` 与 `topic_word`：
 *     · `topic`      ：范围（global/spot）+ 主题序号 + 主题占比 + 参与训练样本量 + 模型版本
 *     · `topic_word` ：每个主题的 Top10 主题词与权重
 *
 * ── 输入为什么是「词频」而不是 TF-IDF ──
 *   LDA 的生成过程建立在**词的计数**上（文档由主题混合生成、主题由词分布生成），
 *   喂 TF-IDF 会破坏其概率假设。因此本组件使用 C-SPK-03 里 `CountVectorizer`
 *   产出的 `countFeatures`，而 C-SPK-04（朴素贝叶斯）使用同一套词表的 TF-IDF 特征。
 *   同一套分词结果、两种特征表达——这正是 C-SPK-03 被设计为"共同前置"的体现。
 *
 * ── 范围与样本（设计未细化，本文件采用的取值与理由）──
 *   · **global**：对全部建模评论训练一次（已过滤低信息量与重复正文，约 4.7 万条）。
 *   · **spot**  ：只对 **评论量 ≥100 的 57 个景点**分别训练。
 *     理由：`has_full_evaluation=1` 正是设计定义的"可做完整评价"门槛（BR-02）；
 *     样本过少的景点做主题建模没有统计意义，与 BR-04「样本不足不出结论」口径一致。
 *   · 小样本验证（--limit）时**自动跳过景点级**，避免在有限样本上堆出不可信的主题模型。
 *
 * ── 主题数 ──
 *   设计未规定主题数。本实现取 `Config.LDA_TOPICS`（默认 5），并在 README 与开发日报中
 *   如实说明这是**可配置的建模选择**、不是设计硬性规定；主题词取 Top10
 *   （依据 `topic_word.rank_no` 注释「取 Top10」）。
 *
 * 幂等：`topic` 的唯一键是 `(scope_type, scope_id, topic_index)`。由于 `topic_id` 是自增主键，
 *   采用「先删该范围的旧主题词与旧主题、再插入」的方式（Db.deleteTopicScope），
 *   保证重跑结果干净、可复现，也不会留下孤立主题词。
 */
object Cspk05Topics {

  /** 模型类型标识：写入 `analysis_task.model_type`（约定取值 lda）。 */
  val MODEL_TYPE = "lda"

  /** 模型版本：写入 topic.model_version 与 analysis_task.model_version。 */
  val MODEL_VERSION = "lda-v1"

  /** topic_word 保留的主题词数（与 topic_word.rank_no 注释「取 Top10」一致）。 */
  val TOP_WORDS: Int = 10

  /** 主题级产出（写库前在 Driver 端构造）。 */
  case class TopicOut(
      scopeType: String,
      spotName: Option[String],
      topicIndex: Int,
      topicRate: BigDecimal,
      sampleSize: Long
  )

  /** 主题词产出。 */
  case class TopicWordOut(
      scopeType: String,
      spotName: Option[String],
      topicIndex: Int,
      word: String,
      weight: BigDecimal,
      rankNo: Int
  )

  case class LdaResult(
      globalTopics: Seq[TopicOut],
      globalWords: Seq[TopicWordOut],
      spotTopics: Seq[TopicOut],
      spotWords: Seq[TopicWordOut],
      trainingDocs: Long,
      numTopics: Int
  )

  def run(
      spark: SparkSession,
      loaded: Cspk01DataLoad.Loaded,
      features: Cspk03Features.Features,
      skipDbWrite: Boolean
  ): LdaResult = {
    val smallSample = features.modelingInput.modelingRows < 10000L
    if (smallSample) {
      println("  [C-SPK-05] 小样本模式：只做 global 主题建模，跳过 57 个景点级建模")
    }
    val result = train(spark, loaded, features, includeSpot = !smallSample)
    report(result)

    if (skipDbWrite) {
      println("  [C-SPK-05] --skip-db-write 已启用：只训练、不写库。")
    } else {
      write(result, loaded.spotIdByName)
    }
    result
  }

  /**
   * 训练 LDA（global + 可选 spot）。
   *
   * @param includeSpot 是否对 57 个具备完整评价资格的景点分别训练
   */
  def train(
      spark: SparkSession,
      loaded: Cspk01DataLoad.Loaded,
      features: Cspk03Features.Features,
      includeSpot: Boolean
  ): LdaResult = {
    val df = features.countDf

    // ---- global ----
    val vocab = features.vocab
    val (globalTopics, globalWords, docs) = fitOne(df, scopeType = "global", spotName = None, vocab)

    // ---- spot：仅 has_full_evaluation = 1 的 57 个景点 ----
    val (spotTopics, spotWords) = if (!includeSpot) {
      (Seq.empty[TopicOut], Seq.empty[TopicWordOut])
    } else {
      // 注意：Db.queryRows 的值是 Any（多为 Option 包装），必须解包后再用，
      // 否则会得到字符串 "Some(布达拉宫)"，用它去 filter 会匹配到 0 行（实测踩到过）。
      val fullEval = Db.withConnection { conn =>
        Db.queryRows(conn, "SELECT spot_name FROM spot WHERE has_full_evaluation = 1")
          .map(r => str(r.get("spot_name")))
          .filter(_.nonEmpty)
          .toSet
      }
      println(s"  [C-SPK-05] 景点级建模范围: ${fullEval.size} 个具备完整评价资格的景点（BR-02）")

      val tBuf = ListBuffer[TopicOut]()
      val wBuf = ListBuffer[TopicWordOut]()
      var computed = 0
      var skipped = 0
      var maxCount = 0L
      var minCount = Long.MaxValue
      fullEval.toSeq.sorted.foreach { name =>
        val c = df.filter(col("景点名称") === name).count()
        maxCount = math.max(maxCount, c)
        minCount = math.min(minCount, c)
        if (c >= Config.FULL_EVAL_THRESHOLD) {
          computed += 1
          val (t, w, _) = fitOne(df.filter(col("景点名称") === name), scopeType = "spot", spotName = Some(name), vocab)
          tBuf ++= t
          wBuf ++= w
        } else {
          skipped += 1
        }
      }
      println(s"  [C-SPK-05] 景点级建模结果: 完成 $computed 个，跳过（可建模评论 < " +
        s"${Config.FULL_EVAL_THRESHOLD}）$skipped 个")
      println(s"  [C-SPK-05] 景点可建模评论数范围: $minCount ~ $maxCount")
      (tBuf.toSeq, wBuf.toSeq)
    }

    LdaResult(globalTopics, globalWords, spotTopics, spotWords, docs, Config.LDA_TOPICS)
  }

  /**
   * 对给定数据集训练一个 LDA 模型，产出主题与主题词。
   *
   * 主题占比 `topic_rate` 的口径：**该主题作为"文档主导主题"的文档比例**
   *   （对每篇文档取最大主题概率的序号，再统计各序号的出现频率）。
   *   为什么用这个口径：LDA 输出的是每篇文档的主题分布，需要聚合成"该主题占多少"；
   *   取主导主题比取平均概率更直观、也更容易在页面上解释（各主题占比之和为 1）。
   */
  private def fitOne(
      df: DataFrame,
      scopeType: String,
      spotName: Option[String],
      vocabulary: Array[String]
  ): (Seq[TopicOut], Seq[TopicWordOut], Long) = {
    val sample = df.cache()
    val docs = sample.count()
    val k = Config.LDA_TOPICS

    val lda = new LDA()
      .setK(k)
      .setMaxIter(20)
      .setSeed(Config.RANDOM_SEED) // NR-R-06：固定种子保证可复现
      .setFeaturesCol("countFeatures")
      .setOptimizer("em")

    val model: LDAModel = lda.fit(sample)
    val topicWords = model.describeTopics(TOP_WORDS).collect()
    // Spark 3.3.1 的 describeTopics 返回**词表下标**（termIndices），且 LDAModel 上没有
    // vocabulary 属性（该属性是更高版本才加的）。因此词表由调用方从
    // C-SPK-03 的 CountVectorizerModel 传入——保证"特征工程产出的词表"与"主题词"同源。

    // 主导主题统计。
    // 注意：`argmax` 是 Spark 3.4+ 才提供的内置函数，本项目锁定 Spark 3.3.1，
    // 因此在 3.3.1 上必须用 UDF 求"概率最大的主题下标"（不能照抄新版文档）。
    val dominantUdf = udf((v: org.apache.spark.ml.linalg.Vector) => {
      var bestIdx = 0
      var bestVal = Double.NegativeInfinity
      var i = 0
      while (i < v.size) {
        val x = v(i)
        if (x > bestVal) { bestVal = x; bestIdx = i }
        i += 1
      }
      bestIdx
    })

    val dominant = model
      .transform(sample)
      .select(dominantUdf(col("topicDistribution")).as("dominant"))
      .groupBy(col("dominant")).agg(count(lit(1)).as("c"))
      .collect()
      .map(r => Db.toInt(r.getAs[Any]("dominant")) -> Db.toLong(r.getAs[Any]("c")))
      .toMap

    val topics = (0 until k).map { i =>
      val c = dominant.getOrElse(i, 0L)
      TopicOut(
        scopeType = scopeType,
        spotName = spotName,
        topicIndex = i,
        topicRate =
          if (docs > 0) BigDecimal(c.toDouble / docs).setScale(4, BigDecimal.RoundingMode.HALF_UP)
          else BigDecimal(0).setScale(4),
        sampleSize = docs
      )
    }

    val words = topicWords.flatMap { row =>
      val idx = row.getAs[Int]("topic")
      // Spark 返回的数组列在运行时可能是 mutable.ArraySeq，而 Scala 2.13 的 Seq 是
      // immutable.Seq，直接 getAs[Seq[...]] 会抛 ClassCastException（类型擦除陷阱）。
      // 统一通过 asInts / asDoubles 取值，兼容两种表示。
      val indices = asInts(row.getAs[Any]("termIndices"))
      val weights = asDoubles(row.getAs[Any]("termWeights"))
      indices.zip(weights).zipWithIndex.map { case ((termIdx, wt), rank) =>
        TopicWordOut(
          scopeType = scopeType,
          spotName = spotName,
          topicIndex = idx,
          word = vocabulary(termIdx),
          weight = BigDecimal(wt).setScale(6, BigDecimal.RoundingMode.HALF_UP),
          rankNo = rank + 1
        )
      }
    }.toSeq

    sample.unpersist()
    (topics, words, docs)
  }

  // ---------------------------------------------------------------------------
  // 取值辅助：兼容 Spark 运行时数组的两种表示
  // ---------------------------------------------------------------------------

  /** 把 Db.queryRows 取出的 Any 值解包为字符串（Option / null 兼容）。 */
  private def str(v: Any): String = v match {
    case null    => ""
    case Some(x) => str(x)
    case None    => ""
    case other   => other.toString
  }

  /** 把 Row 取出的数组列转为 Array[Int]（兼容 WrappedArray / mutable.ArraySeq / Array）。 */
  private def asInts(v: Any): Array[Int] = v match {
    case null                  => Array.emptyIntArray
    case a: Array[Int]         => a
    case s: scala.collection.Seq[_] => s.iterator.map(x => x.toString.toInt).toArray
    case arr: Array[_]         => arr.map(x => x.toString.toInt)
    case other                 => throw new IllegalStateException(s"termIndices 取值类型不支持：${other.getClass}")
  }

  /** 把 Row 取出的数组列转为 Array[Double]。 */
  private def asDoubles(v: Any): Array[Double] = v match {
    case null                  => Array.emptyDoubleArray
    case a: Array[Double]      => a
    case s: scala.collection.Seq[_] => s.iterator.map(x => x.toString.toDouble).toArray
    case arr: Array[_]         => arr.map(x => x.toString.toDouble)
    case other                 => throw new IllegalStateException(s"termWeights 取值类型不支持：${other.getClass}")
  }

  private def report(result: LdaResult): Unit = {    println(s"  [C-SPK-05] 训练文档数   : ${result.trainingDocs}（global）")
    println(s"  [C-SPK-05] 主题数 K     : ${result.numTopics}（可通过 SPARK_LDA_TOPICS 配置）")
    println(s"  [C-SPK-05] global 主题  : ${result.globalTopics.size} 个，主题词 ${result.globalWords.size} 条")
    println(s"  [C-SPK-05] spot   主题  : ${result.spotTopics.size} 个，主题词 ${result.spotWords.size} 条")
    println("  [C-SPK-05] global 主题词（答辩核对用）：")
    result.globalTopics.foreach { t =>
      val ws = result.globalWords.filter(_.topicIndex == t.topicIndex)
        .sortBy(_.rankNo).map(_.word).mkString(" / ")
      println(f"      主题 ${t.topicIndex}%2d  占比 ${t.topicRate.toDouble * 100}%6.2f%%  $ws")
    }
  }

  // ---------------------------------------------------------------------------
  // 落库
  // ---------------------------------------------------------------------------
  private def write(result: LdaResult, spotIdByName: Map[String, Int]): Unit = {
    def spotId(name: String): Int = spotIdByName.getOrElse(
      name, throw new IllegalStateException(s"topic 写库失败：景点「$name」不存在")
    )

    Db.withConnection { conn =>
      // ---- 先删旧主题（含主题词），保证重跑幂等 ----
      Db.deleteTopicScope(conn, "global", 0)
      result.spotTopics.map(t => spotId(t.spotName.get)).distinct.foreach { id =>
        Db.deleteTopicScope(conn, "spot", id)
      }

      val topicSql =
        """INSERT INTO topic
          |  (scope_type, scope_id, topic_index, topic_rate, sample_size, model_version)
          |VALUES (?,?,?,?,?,?)""".stripMargin

      def insertTopic(scopeType: String, scopeId: Int, t: TopicOut): Long = {
        val ps = conn.prepareStatement(topicSql, java.sql.Statement.RETURN_GENERATED_KEYS)
        try {
          ps.setString(1, scopeType)
          ps.setInt(2, scopeId)
          ps.setInt(3, t.topicIndex)
          ps.setBigDecimal(4, t.topicRate.bigDecimal)
          ps.setLong(5, t.sampleSize)
          ps.setString(6, MODEL_VERSION)
          ps.executeUpdate()
          val keys = ps.getGeneratedKeys
          try { if (keys.next()) keys.getLong(1) else -1L } finally keys.close()
        } finally ps.close()
      }

      val globalTopicIds = result.globalTopics.map(t => t.topicIndex -> insertTopic("global", 0, t)).toMap

      val spotTopicIds = scala.collection.mutable.Map[(Int, Int), Long]()
      result.spotTopics.foreach { t =>
        val sid = spotId(t.spotName.get)
        spotTopicIds((sid, t.topicIndex)) = insertTopic("spot", sid, t)
      }
      println(s"  [C-SPK-05] 已写入 topic      ：${globalTopicIds.size + spotTopicIds.size} 行" +
        s"（global ${globalTopicIds.size} + spot ${spotTopicIds.size}）")

      // ---- topic_word ----
      val wordSql = "INSERT INTO topic_word (topic_id, word, weight, rank_no) VALUES (?,?,?,?)"

      def bindWord(ps: java.sql.PreparedStatement, topicId: Long, w: TopicWordOut): Unit = {
        ps.setLong(1, topicId)
        ps.setString(2, w.word)
        ps.setBigDecimal(3, w.weight.bigDecimal)
        ps.setInt(4, w.rankNo)
      }

      val globalWordParams = result.globalWords.flatMap(w => globalTopicIds.get(w.topicIndex).map(id => (id, w)))
      val nGw = Db.batchUpdate(conn, wordSql, globalWordParams) { (ps, item) =>
        bindWord(ps, item._1, item._2)
      }

      val spotWordParams = result.spotWords.flatMap { w =>
        val sid = spotId(w.spotName.get)
        spotTopicIds.get((sid, w.topicIndex)).map(id => (id, w))
      }
      val nSw = Db.batchUpdate(conn, wordSql, spotWordParams) { (ps, item) =>
        bindWord(ps, item._1, item._2)
      }
      println(s"  [C-SPK-05] 已写入 topic_word ：${nGw + nSw} 行（global $nGw + spot $nSw）")

      // ---- 任务登记 ----
      val taskId = Db.insertTask(conn, "lda", "Spark LDA 主题发现（C-SPK-05）", result.trainingDocs)
      Db.logStep(
        conn, taskId, "INFO", "LDA 主题发现",
        s"topic ${globalTopicIds.size + spotTopicIds.size} 行 / topic_word ${nGw + nSw} 行（K=${result.numTopics}）",
        Some(
          s"""{"input_count":${result.trainingDocs},"output_count":${globalTopicIds.size + spotTopicIds.size},""" +
            s""""dropped_count":0,"abnormal_count":0,"extra":{"num_topics":${result.numTopics},""" +
            s""""top_words":$TOP_WORDS,"model_version":"$MODEL_VERSION"}}"""
        )
      )
      Db.finishTask(
        conn, taskId, "success", (globalTopicIds.size + spotTopicIds.size).toLong, 0L, 0L,
        modelType = Some(MODEL_TYPE),
        modelVersion = Some(MODEL_VERSION),
        randomSeed = Some(Config.RANDOM_SEED)
      )
      println(s"  [C-SPK-05] 已登记 analysis_task.task_id=$taskId")
    }
  }
}
