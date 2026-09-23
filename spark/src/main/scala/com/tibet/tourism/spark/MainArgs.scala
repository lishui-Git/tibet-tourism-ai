package com.tibet.tourism.spark

import java.nio.file.Paths

/**
 * 命令行参数（C-SPK-01~05 共用的统一解析）。
 *
 * 设计考虑：本模块只有一个入口，用 `--stage` 选择要跑的组件，
 * 这样答辩演示时不必记 5 条不同的命令，也便于"只重跑某一环"。
 *
 * 用法示例：
 *   spark-submit ... --class com.tibet.tourism.spark.Main spark-offline-analysis-1.0.0.jar --stage 2
 *   ... --stage 2 --source csv --limit 2000      # 小样本验证
 *   ... --stage all                              # 全量跑 02→05
 */
case class MainArgs(
    source: Cspk01DataLoad.Source = Cspk01DataLoad.FromMysql,
    stages: Seq[Int] = Seq(2, 3, 4, 5),
    limit: Option[Int] = None,
    config: Option[java.nio.file.Path] = None,
    skipDbWrite: Boolean = false,
    dryRun: Boolean = false
)

object MainArgs {

  val USAGE: String =
    """用法：--source <mysql|csv> [--stage <n|all>] [--limit <n>] [--config <file>]
      |      [--skip-db-write] [--dry-run]
      |
      |  --source  数据源：mysql（默认，读 review 表）/ csv（读阶段二清洗版文件）
      |  --stage   要执行的组件：2/3/4/5，可逗号分隔，all=2,3,4,5（默认）
      |              2 = C-SPK-02 统计聚合（stat_spot / stat_time / stat_ip）
      |              3 = C-SPK-03 特征工程（分词/停用词/TF-IDF，不落库，仅验证与供 04/05 使用）
      |              4 = C-SPK-04 MLlib 情感基线（sentiment(method=mllib) + analysis_task 指标）
      |              5 = C-SPK-05 LDA 主题（topic / topic_word）
      |  --limit   只取前 N 条评论（小样本验证用；**不要用于最终结果**）
      |  --config  显式指定配置文件（默认依次找 spark/config.env、项目根 .env）
      |  --skip-db-write  只计算不写库（验证阶段使用，确保不动数据库）
      |  --dry-run 等价于 --skip-db-write --limit 2000
      |""".stripMargin

  def parse(argv: Array[String]): MainArgs = {
    var args = MainArgs()
    var i = 0
    while (i < argv.length) {
      argv(i) match {
        case "--source" =>
          i += 1; args = args.copy(source = Cspk01DataLoad.parseSource(argv(i)))
        case "--stage" =>
          i += 1
          val raw = argv(i).trim.toLowerCase
          val stages =
            if (raw == "all") Seq(2, 3, 4, 5)
            else raw.split(",").map(_.trim).filter(_.nonEmpty).map { s =>
              val n = s.toInt
              require(Seq(2, 3, 4, 5).contains(n), s"--stage 只支持 2/3/4/5 或 all，收到：$s")
              n
            }.toSeq
          args = args.copy(stages = stages)
        case "--limit" =>
          i += 1; args = args.copy(limit = Some(argv(i).toInt))
        case "--config" =>
          i += 1; args = args.copy(config = Some(Paths.get(argv(i))))
        case "--skip-db-write" =>
          args = args.copy(skipDbWrite = true)
        case "--dry-run" =>
          args = args.copy(dryRun = true, skipDbWrite = true, limit = args.limit.orElse(Some(2000)))
        case "-h" | "--help" =>
          println(USAGE); sys.exit(0)
        case other =>
          throw new IllegalArgumentException(s"未知参数：$other\n\n$USAGE")
      }
      i += 1
    }
    args
  }
}
