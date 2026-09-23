package com.tibet.tourism.spark

import java.nio.charset.StandardCharsets

import com.huaban.analysis.jieba.JiebaSegmenter
import org.apache.spark.ml.PipelineModel
import org.apache.spark.ml.feature.{CountVectorizer, CountVectorizerModel, IDF, IDFModel, StopWordsRemover}
import org.apache.spark.sql.functions._
import org.apache.spark.sql.{DataFrame, SparkSession}

import scala.collection.JavaConverters._
import scala.io.Source

/**
 * C-SPK-03 · 特征工程组件（中文分词 / 停用词过滤 / TF-IDF 向量化）
 *
 * 设计职责（详细设计 §4.8）：
 *   `C-SPK-03` 是 `C-SPK-04`（MLlib 情感）与 `C-SPK-05`（LDA 主题）的**共同前置**，
 *   **不单独产出数据表**（§4.8「说明」栏原文）。
 *
 * 因此本组件的产物只有两类，都不落库：
 *   ① 一个新的 DataFrame：在评论明细上增加了 token 列与向量列；
 *   ② 两个可持久化的模型：`CountVectorizerModel` 与 `IDFModel`
 *      （持久化到 `data/spark/models/`，用于「同一套词表可复现」的说明）。
 *
 * ── 三个文本处理环节做了什么、为什么 ──
 *   1. **中文分词**：用 jieba（结巴）分词。Spark 3.3.1 自带的 `RegexTokenizer` 只按正则
 *      切分，给不出中文词边界（会把「布达拉宫」切成整串或单字），所以必须引入分词库。
 *      分词以 UDF 形式逐行调用，这是 Spark 处理中文文本的常规做法。
 *   2. **停用词过滤**：用 Spark 内置 `StopWordsRemover`，词表来自
 *      `src/main/resources/stopwords_zh.txt`（项目自建精简表，随 jar 打包）。
 *      同时过滤长度 < 2 的 token——单字大多是虚词或标点，对情感/主题无区分度。
 *   3. **TF-IDF 向量化**：`CountVectorizer`（词频）→ `IDF`（逆文档频率）。
 *      **TF-IDF 专供 C-SPK-04**（朴素贝叶斯需要数值特征向量）；
 *      **C-SPK-05 的 LDA 用的是 CountVectorizer 输出的「词频」，不是 TF-IDF**——
 *      LDA 的生成过程建立在词计数上，喂 TF-IDF 会破坏其概率假设。
 *      两者共用同一个 CountVectorizer 词表，保证"同一套分词结果、两种特征表达"。
 *
 * ── 参与建模的评论范围（重要口径）──
 *   与 BR-05 / BR-06 的双口径一致（数据处理流程图 §3 关键设计点 5）：
 *     · **统计类**功能用全量（已在 C-SPK-02 完成）；
 *     · **文本建模类**（本组件及其下游 04/05）过滤掉：
 *         - `is_low_info = 1`（正文 ≤10 字，BR-05：默认不进入模型调用）
 *         - `is_dup_content = 1`（正文完全重复，BR-06：只处理一次、复用结果）
 *       这一范围由 `buildModelingInput` 统一实施，04/05 不再各自过滤，避免口径分散。
 */
object Cspk03Features {

  /**
   * 文本建模输入：过滤后的明细 + token 数组列。
   *
   * 为什么把「过滤 + 分词」合并成一步产出：
   *   04 与 05 都需要同一份建模输入，若各自过滤/分词会造成口径与算力双浪费。
   */
  case class ModelingInput(df: DataFrame, inputRows: Long, modelingRows: Long, droppedLowInfo: Long, droppedDup: Long)

  /**
   * 特征工程产物。
   *
   * @param modelingInput 建模用输入（含 tokens 列、sentence 列）
   * @param tfidfDf       含 TF-IDF 向量列 `features`，供 C-SPK-04 使用
   * @param countDf       含词频向量列 `countFeatures`，供 C-SPK-05 的 LDA 使用
   * @param vocab         词表（CountVectorizer 的 vocabulary，index → word）
   * @param cvModel       CountVectorizer 模型（可持久化）
   * @param idfModel      IDF 模型（可持久化）
   * @param vocabSize     词表规模
   */
  case class Features(
      modelingInput: ModelingInput,
      tfidfDf: DataFrame,
      countDf: DataFrame,
      vocab: Array[String],
      cvModel: CountVectorizerModel,
      idfModel: IDFModel,
      vocabSize: Int
  )

  // ---------------------------------------------------------------------------
  // 分词
  // ---------------------------------------------------------------------------

  /**
   * jieba 分词器持有者。
   *
   * 为什么用 object（单例）而不是每次 new：
   *   `JiebaSegmenter` 初始化时要加载词典，开销较大；在每个 executor JVM 上
   *   只需一个实例复用即可。object 是 JVM 级单例，天然满足这一点。
   *   它不会进入闭包序列化（UDF 内是对 object 的静态引用）。
   */
  private object Tokenizer {
    private lazy val segmenter = new JiebaSegmenter()

    /** 分词：返回词序列。空文本返回空数组，避免下游遇到 null。 */
    def cut(text: String): Seq[String] = {
      if (text == null || text.trim.isEmpty) Seq.empty
      else {
        segmenter.process(text, JiebaSegmenter.SegMode.SEARCH).asScala
          .map(_.word.trim)
          .filter(w => w.nonEmpty && !isPunctuationOnly(w))
          .toSeq
      }
    }

    /** 纯标点/空白 token 直接丢弃（分词后仍可能残留）。 */
    private def isPunctuationOnly(w: String): Boolean =
      w.forall(ch => !Character.isLetterOrDigit(ch))
  }

  /** 从 classpath 读停用词表（`src/main/resources/stopwords_zh.txt`）。 */
  lazy val stopWords: Array[String] = {
    val stream = Option(getClass.getClassLoader.getResourceAsStream(Config.STOPWORDS_RESOURCE))
      .getOrElse(throw new IllegalStateException(
        s"停用词表未找到：${Config.STOPWORDS_RESOURCE}（应位于 src/main/resources 下并随 jar 打包）"))
    val src = Source.fromInputStream(stream, StandardCharsets.UTF_8.name())
    try {
      src.getLines()
        .map(_.replace("\uFEFF", "").trim) // 去 BOM 与首尾空白
        .filter(l => l.nonEmpty && !l.startsWith("#"))
        .flatMap(_.split("\\s+"))           // 允许一行写多个词
        .filter(_.nonEmpty)
        .toArray
        .distinct
    } finally src.close()
  }

  /** 分词 UDF（返回数组类型的列）。 */
  private val cutUdf = udf((text: String) => Tokenizer.cut(text))

  /** 供诊断/验证使用：把分词能力以列函数形式暴露（与 cutUdf 同一实现，不重复逻辑）。 */
  def cutForDiag(textCol: org.apache.spark.sql.Column): org.apache.spark.sql.Column = cutUdf(textCol)

  // ---------------------------------------------------------------------------
  // 特征构建
  // ---------------------------------------------------------------------------

  /**
   * 构建特征工程产物。
   *
   * @param args 命令行参数（仅用于打印口径说明，不改变处理逻辑）
   */
  def build(spark: SparkSession, loaded: Cspk01DataLoad.Loaded, args: MainArgs): Features = {
    val modeling = buildModelingInput(spark, loaded)

    println(s"  [C-SPK-03] 停用词表     : ${stopWords.length} 个词（${Config.STOPWORDS_RESOURCE}）")
    println(s"  [C-SPK-03] 建模输入行数 : ${modeling.modelingRows} / 全量 ${modeling.inputRows}" +
      s"（剔除低信息量 ${modeling.droppedLowInfo}、重复正文 ${modeling.droppedDup}）")

    // ---- Stage 1：计数向量化（词频）----
    // minDF=2：至少在 2 条评论中出现，滤掉一次性生僻词，控制词表规模。
    // 但小样本运行时词表可能为空（样本太窄），此时自动降级为 1 并给出提示，
    // 否则下游 TF-IDF / LDA 会因为「没有任何文档」直接失败。
    def fitCountVectorizer(minDf: Int): CountVectorizerModel =
      new CountVectorizer()
        .setInputCol("filteredTokens")
        .setOutputCol("countFeatures")
        .setMinDF(minDf)
        .setMinTF(1.0)
        .fit(modeling.df)

    var minDfUsed = 2
    var cvModel = fitCountVectorizer(minDfUsed)
    if (cvModel.vocabulary.isEmpty) {
      minDfUsed = 1
      cvModel = fitCountVectorizer(minDfUsed)
      println(s"  [C-SPK-03][提示] minDF=2 时词表为空（小样本语料过窄），已自动降级为 minDF=1")
    }
    val vocab = cvModel.vocabulary

    val withCount = cvModel.transform(modeling.df)

    // ---- Stage 2：TF-IDF（供 C-SPK-04）----
    val idf = new IDF().setInputCol("countFeatures").setOutputCol("features")
    val idfModel = idf.fit(withCount)
    val tfidfDf = idfModel.transform(withCount)

    println(s"  [C-SPK-03] 词表规模     : ${vocab.length} 个词（minDF=$minDfUsed）")
    println(s"  [C-SPK-03] TF-IDF 维度  : ${tfidfDf.select("features").head().getAs[org.apache.spark.ml.linalg.Vector](0).size}")

    Features(
      modelingInput = modeling,
      tfidfDf = tfidfDf,
      countDf = withCount,
      vocab = vocab,
      cvModel = cvModel,
      idfModel = idfModel,
      vocabSize = vocab.length
    )
  }

  /**
   * 构造文本建模输入：过滤 + 分词 + 去停用词。
   *
   * 过滤规则（BR-05 / BR-06 的"文本建模口径"）：
   *   · 剔除正文为空的行（无文本可建模）
   *   · 剔除 is_low_info = 1（正文 ≤10 字）
   *   · 剔除 is_dup_content = 1（正文完全重复，保留每组的第一条即可，
   *     这里选择整体剔除——因为重复组的判定已在阶段二完成，
   *     统计类功能仍使用全量，建模类避免重复样本放大权重）
   */
  def buildModelingInput(spark: SparkSession, loaded: Cspk01DataLoad.Loaded): ModelingInput = {
    import spark.implicits._

    val all = loaded.reviews
    val inputRows = all.count()

    val lowInfo = all.filter(col("is_low_info") === 1).count()
    val dupCount = all.filter(col("is_dup_content") === 1).count()

    val filtered = all
      .filter(col("评论内容").isNotNull && length(trim(col("评论内容"))) > 0)
      .filter(col("is_low_info") === 0)
      .filter(col("is_dup_content") === 0)

    val modelingRows = filtered.count()

    // 分词 → 过滤单字 → 去停用词
    val tokenized = filtered
      .withColumn("tokens", cutUdf(col("评论内容")))
      .withColumn("tokens", expr("filter(tokens, t -> length(t) >= 2)")) // 去掉单字 token
      .filter(size(col("tokens")) > 0)

    val remover = new StopWordsRemover()
      .setInputCol("tokens")
      .setOutputCol("filteredTokens")
      .setStopWords(stopWords)

    val out = remover.transform(tokenized)

    ModelingInput(out, inputRows, modelingRows, lowInfo, dupCount)
  }

  // ---------------------------------------------------------------------------
  // 验证报告（--stage 3 单独执行时打印）
  // ---------------------------------------------------------------------------
  def report(features: Features): Unit = {
    import features.modelingInput.df.sparkSession.implicits._

    val totalTokens = features.modelingInput.df
      .select(expr("sum(size(filteredTokens))").as("n"))
      .head().getAs[Long]("n")

    println(s"  [C-SPK-03] 有效 token 总数 : $totalTokens")
    println(s"  [C-SPK-03] 平均每行 token 数: ${"%.1f".format(totalTokens.toDouble / math.max(1, features.modelingInput.modelingRows))}")

    println("  [C-SPK-03] 词表前 20 个词（按文档频率降序，CountVectorizer 的排序规则）：")
    println("      " + features.vocab.take(20).mkString(" / "))

    println("  [C-SPK-03] 分词样例（前 3 条，用于人工核对中文切分是否正确）：")
    features.modelingInput.df
      .select(col("评论内容"), col("filteredTokens"))
      .limit(3)
      .collect()
      .foreach { r =>
        val content = Option(r.getAs[String]("评论内容")).getOrElse("").take(40)
        val tokens = r.getAs[Seq[String]]("filteredTokens").take(12).mkString(" ")
        println(s"      原文：$content…")
        println(s"      分词：$tokens")
      }

    // 停用词确实被剔除的抽查：验证词表与分词结果里都不含停用词
    val stopSet = stopWords.toSet
    val leaked = features.vocab.filter(stopSet.contains)
    println(s"  [C-SPK-03] 停用词泄漏检查: ${if (leaked.isEmpty) "通过（词表中无停用词）" else s"发现 ${leaked.length} 个：" + leaked.take(10).mkString("、")}")
  }

  /**
   * 持久化特征模型（可选调用）。
   *
   * 为什么持久化：`CountVectorizerModel` 决定了"哪些词构成特征维度"，
   * 保存它才能说明「同一套词表、可复现」；也便于后续阶段复用同一向量空间。
   */
  def persist(features: Features): Unit = {
    val dir = Config.outputDir.resolve("models")
    java.nio.file.Files.createDirectories(dir)
    val cvPath = dir.resolve("count_vectorizer").toString
    val idfPath = dir.resolve("idf").toString
    features.cvModel.write.overwrite().save(cvPath)
    features.idfModel.write.overwrite().save(idfPath)
    println(s"  [C-SPK-03] 已持久化模型：$cvPath")
    println(s"  [C-SPK-03] 已持久化模型：$idfPath")
  }
}
