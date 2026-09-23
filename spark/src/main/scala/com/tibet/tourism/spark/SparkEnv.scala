package com.tibet.tourism.spark

import org.apache.spark.sql.SparkSession

/**
 * SparkSession 工厂（全局唯一入口）。
 *
 * 设计依据（spark/README.md §2，技术栈已冻结）：
 *   · 运行模式 local[*]（单机，使用本机全部核心）
 *   · JDK 1.8 下必须用 -Xms512m -Xmx2g 风格的 JVM 参数（K2：不要照抄 JDK17 风格）
 *   · 时区固定 Asia/Shanghai，与 JDBC 连接串保持一致，避免统计按 UTC 落到前一天
 *
 * 为什么用 local[*] 而不是集群：数据集仅 59,033 行、单机内存充裕，
 * 引入集群不产生业务价值，反而增加答辩时的解释成本（项目现状分析.md §7.3）。
 */
object SparkEnv {

  /** 会话时区：必须与 JDBC 的 serverTimezone 一致，否则按日期聚合会出现 ±1 天偏差。 */
  private val TIMEZONE = "Asia/Shanghai"

  /**
   * 创建 SparkSession。
   *
   * @param appName 应用名（会显示在 Spark UI 上，便于答辩演示时区分任务）
   * @param master  master 地址，默认 local[*]
   * @param shufflePartitions shuffle 分区数；单机小数据量下调小可减少任务开销
   */
  def create(
      appName: String,
      master: String = "local[*]",
      shufflePartitions: Int = 8
  ): SparkSession = {
    val builder = SparkSession
      .builder()
      .appName(appName)
      .master(master)
      // ---- 时区与本地化 ----
      .config("spark.sql.session.timeZone", TIMEZONE)
      // ---- 单机小数据量的合理默认值（不是"性能调优"，只是避免 200 个空分区 ----
      .config("spark.sql.shuffle.partitions", shufflePartitions.toString)
      .config("spark.default.parallelism", shufflePartitions.toString)
      // ---- Windows 本地运行：显式给出 hadoop 家目录，避免 winutils 相关报错 ----
      .config("spark.driver.extraJavaOptions", "-Dfile.encoding=UTF-8")

    sys.env.get("HADOOP_HOME").foreach(h => builder.config("spark.hadoop.hadoop.home.dir", h))

    // 默认关闭 Spark 的 INFO 噪音日志（只保留 WARN 及以上），便于在终端看清本项目的输出。
    // 不引入 log4j 配置文件，避免多一个需要解释的文件。
    val spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    spark
  }

  /** 停止会话（作业结束或异常时统一调用）。 */
  def stop(spark: SparkSession): Unit = {
    try spark.stop() catch { case _: Throwable => () }
  }
}
