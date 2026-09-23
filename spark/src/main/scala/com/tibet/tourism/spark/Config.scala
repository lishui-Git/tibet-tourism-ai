package com.tibet.tourism.spark

import java.io.File
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Path, Paths}
import scala.collection.mutable
import scala.jdk.CollectionConverters._

/**
 * 配置读取（Spark 侧唯一读配置文件的地方）。
 *
 * 设计依据：
 *   · 详细设计 §2 / NR-M-01：配置集中管理，代码内**不得出现明文口令**
 *   · spark/README.md §5 K5：MySQL 8 连接串必须带 allowPublicKeyRetrieval / useSSL / serverTimezone
 *
 * 读文件顺序（先找到先用，找到即停止）：
 *   1) 命令行显式指定的 --config 文件
 *   2) 环境变量 SPARK_APP_CONFIG 指向的文件
 *   3) <项目根>/spark/config.env      —— Spark 侧自己的配置（已 gitignore，不提交）
 *   4) <项目根>/.env                  —— 复用 Python 侧已有配置（同样已 gitignore）
 *
 * 项目根目录的判定：从本 class 的运行时位置向上找到含 app/ 目录的那一级。
 * 这样无论从 spark/target/classes 还是从打包后的 jar 运行都能定位到根目录，
 * **不需要在代码里写死任何绝对路径**。
 *
 * 支持的键（与 Python 侧 .env 同名，故可直接复用同一个 .env）：
 *   DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME / DB_CHARSET
 */
object Config {

  /** Spark 侧配置文件（可选）：spark/config.env */
  val SPARK_CONFIG_FILE = "spark/config.env"

  /**
   * 从运行时位置向上探测项目根目录。
   *
   * 判据：某一级目录下同时存在 `app`（Python 后端包）目录与 `spark` 目录。
   * 为什么不用 `System.getProperty("user.dir")`：user.dir 取决于"从哪个目录调用
   * spark-submit"，不稳定；而 class 文件位置由 Maven 固定，更可靠。
   */
  lazy val projectRoot: Path = {
    val starts = mutable.ListBuffer[Path]()
    // ① class 文件所在位置（target/classes 或 jar 内）
    val here = Paths.get(Config.getClass.getProtectionDomain.getCodeSource.getLocation.toURI)
    starts += here
    // ② 当前工作目录（兜底）
    starts += Paths.get(System.getProperty("user.dir")).toAbsolutePath

    val found = starts.iterator
      .flatMap(p => Iterator.iterate(p.toAbsolutePath)(_.getParent).takeWhile(_ != null))
      .find(dir => Files.isDirectory(dir.resolve("app")) && Files.isDirectory(dir.resolve("spark")))

    found.getOrElse {
      throw new IllegalStateException(
        "无法定位项目根目录（未找到同时含 app/ 与 spark/ 的目录）。" +
          "请用 --config 显式指定配置文件，或设置环境变量 SPARK_APP_CONFIG。"
      )
    }
  }

  /** 命令行显式指定的配置文件（由 Main 在解析参数后注入）。 */
  @volatile private var explicitConfig: Option[Path] = None

  def setExplicitConfig(path: Option[Path]): Unit = explicitConfig = path

  /** 解析后的键值对（小写键，值已去空白）。 */
  private lazy val values: Map[String, String] = {
    val candidates: Seq[Option[Path]] = Seq(
      explicitConfig,
      sys.env.get("SPARK_APP_CONFIG").map(Paths.get(_)),
      Some(projectRoot.resolve(SPARK_CONFIG_FILE)),
      Some(projectRoot.resolve(".env"))
    )
    val firstExisting = candidates.flatten.find(p => Files.isRegularFile(p))
    firstExisting.map(parse).getOrElse(Map.empty)
  }

  /** 已加载的配置文件路径（用于启动日志打印，便于排查"读的哪个配置"）。 */
  lazy val loadedFrom: String = {
    val candidates: Seq[Option[Path]] = Seq(
      explicitConfig,
      sys.env.get("SPARK_APP_CONFIG").map(Paths.get(_)),
      Some(projectRoot.resolve(SPARK_CONFIG_FILE)),
      Some(projectRoot.resolve(".env"))
    )
    candidates.flatten.find(p => Files.isRegularFile(p)).map(_.toString).getOrElse("<未找到配置文件>")
  }

  /** 极简 .env 解析：KEY=VALUE，忽略空行与 # 注释，兼容「值内含等号」的情况。 */
  private def parse(path: Path): Map[String, String] = {
    val lines = Files.readAllLines(path, StandardCharsets.UTF_8).asScala
    val m = mutable.LinkedHashMap[String, String]()
    lines.foreach { raw =>
      val line = raw.trim
      if (line.nonEmpty && !line.startsWith("#") && line.contains("=")) {
        val idx = line.indexOf('=')
        val key = line.substring(0, idx).trim
        // 去掉可能存在的行内注释与首尾引号
        val value = line.substring(idx + 1).trim.replaceAll("^\"|\"$", "")
        if (key.nonEmpty) m(key) = value
      }
    }
    m.toMap
  }

  /**
   * 取值：先查配置文件，再查真实进程环境变量（后者优先，便于 CI/临时覆盖）。
   */
  def get(key: String, default: String = ""): String =
    sys.env.getOrElse(key, values.getOrElse(key, default))

  // ---------------------------------------------------------------------------
  // 数据库配置（与 Python 侧 app/config.py 的 DatabaseSettings 同名同义）
  // ---------------------------------------------------------------------------

  lazy val dbHost: String = get("DB_HOST", "127.0.0.1")
  lazy val dbPort: Int = get("DB_PORT", "3306").toInt
  lazy val dbUser: String = get("DB_USER", "root")
  lazy val dbPassword: String = get("DB_PASSWORD", "")
  lazy val dbName: String = get("DB_NAME", "tibet_review")
  lazy val dbCharset: String = get("DB_CHARSET", "utf8mb4")

  /**
   * JDBC 连接串。三个参数缺一不可（项目现状分析.md §8.4 K5/K6/K7）：
   *   useSSL=false                   —— 本地开发，避免 SSL 握手开销与证书问题
   *   allowPublicKeyRetrieval=true   —— MySQL 8 默认 caching_sha2_password 认证需要
   *   serverTimezone=Asia/Shanghai   —— 不加会出现 8 小时时区偏移
   *   characterEncoding=utf8         —— 中文不乱码
   */
  lazy val jdbcUrl: String =
    s"jdbc:mysql://$dbHost:$dbPort/$dbName" +
      "?useSSL=false&allowPublicKeyRetrieval=true" +
      "&serverTimezone=Asia/Shanghai&characterEncoding=utf8&rewriteBatchedStatements=true"

  /** 可安全打印的连接摘要（**绝不包含口令**）。 */
  lazy val dbSummary: String = s"$dbUser@$dbHost:$dbPort/$dbName?charset=$dbCharset"

  def requirePassword(): Unit = {
    if (dbPassword == null || dbPassword.trim.isEmpty) {
      throw new IllegalStateException(
        s"数据库口令未配置：请在 $SPARK_CONFIG_FILE 或项目根 .env 中填写 DB_PASSWORD。" +
          "（配置文件已被 .gitignore 排除，不会提交到仓库）"
      )
    }
  }

  // ---------------------------------------------------------------------------
  // 业务口径常量（全部来自设计文档，禁止在此处"顺手"改动）
  // ---------------------------------------------------------------------------

  /** 数据源：清洗版 CSV（阶段二产物，59,033 行 × 23 列，UTF-8 with BOM）。 */
  lazy val cleanedCsv: Path = {
    val configured = get("SPARK_CLEANED_CSV", "")
    if (configured.nonEmpty) Paths.get(configured)
    else projectRoot.resolve("data").resolve("旅游评论数据集_清洗版_v1.csv")
  }

  /** 强制行数校验基准（NR-R-03：最终数据集必须为 59,033 条）。 */
  val EXPECTED_REVIEW_ROWS: Long = 59033L

  /** 景点数基准。 */
  val EXPECTED_SPOT_ROWS: Long = 837L

  /**
   * `stat_ip.sample_size` 的口径基准。
   *
   * 注意区分设计里的两个不同指标（容易混淆，实测已核对）：
   *   · `stat_overview.valid_ip_sample = 35098`
   *     —— 发布时间 ≥ 2022-08-01 的**全部**评论数（仍包含归属地为"未知"的 520 条）
   *   · `stat_ip.sample_size = 34578`
   *     —— 上述样本中**归属地已知**的部分（35,098 − 520 = 34,578），即真正可做省份分布的分母
   * 本模块计算的是后者（stat_ip 的分母），故期望值取 34,578。
   */
  val EXPECTED_IP_SAMPLE_SIZE: Long = 34578L

  /** `stat_overview.valid_ip_sample` 口径基准（2022-08 后全部评论，含未知）。 */
  val EXPECTED_VALID_IP_SAMPLE: Long = 35098L

  /** 2022-08 后归属地为"未知"的评论数（35,098 − 34,578）。 */
  val EXPECTED_IP_UNKNOWN_AFTER_CUTOFF: Long = 520L

  /** BR-01：客源地统计时间下限。 */
  val IP_VALID_FROM: String = "2022-08-01"

  /** BR-02：完整智能评价门槛（评论量 ≥100 的 57 个景点）。 */
  val FULL_EVAL_THRESHOLD: Int = 100

  /** BR-05：低信息量阈值（正文 ≤10 字）。 */
  val LOW_INFO_MAX_LENGTH: Int = 10

  /** 统计版本号：写入各结果表的 stat_version 字段，便于区分多次运行。 */
  val STAT_VERSION: String = get("SPARK_STAT_VERSION", "v1")

  /** LDA 主题数（设计未指定具体值，取 5 并在 README 与日报中说明为"可配置的建模选择"）。 */
  val LDA_TOPICS: Int = get("SPARK_LDA_TOPICS", "5").toInt

  /** 每个主题保留的主题词数（表结构 topic_word.rank_no 注释为「取 Top10」）。 */
  val LDA_TOP_WORDS: Int = get("SPARK_LDA_TOP_WORDS", "10").toInt

  /** MLlib 随机种子（NR-R-06：固定种子保证可复现）。 */
  val RANDOM_SEED: Int = get("SPARK_RANDOM_SEED", "42").toInt

  /** 停止词表相对路径（在 src/main/resources 下，随 jar 打包）。 */
  val STOPWORDS_RESOURCE: String = "stopwords_zh.txt"

  /** 中间产物目录（仅在运行时需要落盘的中间结果使用，默认 data/spark）。 */
  lazy val outputDir: Path = projectRoot.resolve("data").resolve("spark")

  /** 项目根目录下是否存在指定的 Python 侧文件（用于自检打印）。 */
  def describeRoot(): String = {
    val f = new File(projectRoot.toFile, "app")
    s"$projectRoot（app/ 存在=${f.isDirectory}）"
  }
}
