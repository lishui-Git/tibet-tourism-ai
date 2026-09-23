package com.tibet.tourism.spark

import java.util.concurrent.TimeUnit

import org.apache.spark.sql.SparkSession

/**
 * Spark 离线分析模块统一入口（C4 容器「Spark 离线分析」）。
 *
 * 对应组件：
 *   C-SPK-01 数据加载   → Cspk01DataLoad
 *   C-SPK-02 统计聚合   → Cspk02Stats      → stat_spot / stat_time / stat_ip
 *   C-SPK-03 特征工程   → Cspk03Features   （分词/停用词/TF-IDF；共同前置，不单独产出表）
 *   C-SPK-04 情感基线   → Cspk04Sentiment  → sentiment(method='mllib') + analysis_task 指标
 *   C-SPK-05 LDA 主题   → Cspk05Topics     → topic / topic_word
 *
 * 两条硬约束（答辩要点）：
 *   1. 本容器**不发起任何 HTTP 调用**，即不接触 DeepSeek（C4容器图 §3）；
 *   2. 与 Python 侧通过 MySQL 解耦，任一侧可单独重跑。
 *
 * 用法见 MainArgs.USAGE；典型调用（spark-submit 由 scripts/run_spark.ps1 封装）。
 */
object Main {

  def main(argv: Array[String]): Unit = {
    val args = try {
      MainArgs.parse(argv)
    } catch {
      case e: IllegalArgumentException =>
        System.err.println(s"[参数错误] ${e.getMessage}")
        sys.exit(2)
    }

    Config.setExplicitConfig(args.config)

    println("=" * 78)
    println(" Spark 离线分析（C-SPK-01~05）· 基于 DeepSeek 的西藏旅游景点智能评价与分析系统")
    println("=" * 78)
    println(s"  项目根目录 : ${Config.projectRoot}")
    println(s"  配置文件   : ${Config.loadedFrom}")
    println(s"  数据库     : ${Config.dbSummary}（口令不打印）")
    println(s"  数据源     : ${args.source.name}" + args.limit.map(n => s"（小样本前 $n 条）").getOrElse("（全量）"))
    println(s"  执行组件   : ${args.stages.mkString("C-SPK-0", " / C-SPK-0", "")}")
    println(s"  写库       : ${if (args.skipDbWrite) "否（--skip-db-write）" else "是"}")
    println(s"  统计版本   : ${Config.STAT_VERSION}")
    println("-" * 78)

    val startedAt = System.currentTimeMillis()
    val spark = SparkEnv.create(s"TibetTourism-Spark-OfflineAnalysis-${args.source.name}")
    var exitCode = 0

    try {
      // ---------------- C-SPK-01 数据加载 ----------------
      val loaded = Cspk01DataLoad.load(spark, args.source, args.limit)

      // 在 Driver 端缓存一份「景点数」供结果表使用
      val spots = loaded.reviews.select("景点名称").distinct().count()

      // ---------------- C-SPK-02 统计聚合 ----------------
      if (args.stages.contains(2)) {
        section("C-SPK-02 · 统计聚合（stat_spot / stat_time / stat_ip）")
        Cspk02Stats.run(spark, loaded, args.skipDbWrite)
      }

      // ---------------- C-SPK-03 特征工程 ----------------
      // 04 与 05 都以它为前置，因此只在需要时构建一次，避免重复分词。
      val needFeatures = args.stages.exists(s => s == 3 || s == 4 || s == 5)
      val features = if (needFeatures) {
        section("C-SPK-03 · 特征工程（分词 / 停用词过滤 / TF-IDF）")
        Some(Cspk03Features.build(spark, loaded, args))
      } else None

      if (args.stages.contains(3) && features.isDefined) {
        section("C-SPK-03 · 特征验证（词表规模、样例词、TF-IDF 维度）")
        Cspk03Features.report(features.get)
      }

      // ---------------- C-SPK-04 情感基线 ----------------
      if (args.stages.contains(4)) {
        section("C-SPK-04 · MLlib 情感基线（三分类训练与批量推理）")
        Cspk04Sentiment.run(spark, loaded, features.get, args.skipDbWrite)
      }

      // ---------------- C-SPK-05 LDA 主题 ----------------
      if (args.stages.contains(5)) {
        section("C-SPK-05 · LDA 主题发现（topic / topic_word）")
        Cspk05Topics.run(spark, loaded, features.get, args.skipDbWrite)
      }

      // ---------------- 结果自检 ----------------
      if (!args.skipDbWrite) {
        section("结果表写入自检")
        SelfCheck.run(spark)
      } else {
        println("\n[提示] --skip-db-write 已启用：本次只计算、不写库，故跳过结果表自检。")
        println(s"        涉及景点数（Driver 端统计）：$spots")
      }

    } catch {
      case e: Throwable =>
        System.err.println(s"\n[失败] Spark 离线分析中断：${e.getClass.getSimpleName}: ${e.getMessage}")
        e.printStackTrace()
        exitCode = 3
    } finally {
      SparkEnv.stop(spark)
      val cost = TimeUnit.MILLISECONDS.toSeconds(System.currentTimeMillis() - startedAt)
      println("\n" + "=" * 78)
      println(s" 执行结束：${if (exitCode == 0) "成功" else "失败"} · 耗时 ${cost} 秒")
      println("=" * 78)
    }

    sys.exit(exitCode)
  }

  private def section(title: String): Unit = {
    println("\n" + "-" * 78)
    println(s" $title")
    println("-" * 78)
  }
}
