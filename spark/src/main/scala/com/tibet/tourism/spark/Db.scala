package com.tibet.tourism.spark

import java.sql.{Connection, DriverManager, PreparedStatement, ResultSet}

import scala.collection.mutable.ListBuffer

/**
 * 数据库访问层（Spark 侧**唯一**的 JDBC 出口）。
 *
 * 设计依据：
 *   · 详细设计 §2：数据访问职责集中，不允许在业务组件里到处复制连接代码
 *   · 详细设计 §8.2：各结果表都有明确的幂等键，重跑不得产生重复行
 *   · 项目现状分析.md §8.4 K3：JDBC 连接**不要跨 Spark 闭包传递**，
 *     所以本模块只在 Driver 端（collect 之后）使用，不在 executors 里连库
 *
 * 使用方式（全部在 Driver 端）：
 *   Db.withConnection { conn => ... }
 *
 * 幂等策略（与 §8.2 的幂等键一一对应）：
 *   · sentiment    PK(comment_id, method)                → INSERT ... ON DUPLICATE KEY UPDATE
 *   · stat_spot    PK(spot_id)                           → 同上（重跑覆盖同一景点）
 *   · stat_time    UK(period_type, period, scope_type, scope_id) → 同上
 *   · stat_ip      UK(scope_type, scope_id, ip_province) → 同上
 *   · topic        UK(scope_type, scope_id, topic_index) → 先删同 scope 旧行再插（见 deleteTopicScope）
 */
object Db {

  // MySQL 驱动类名（8.x 为 com.mysql.cj.jdbc.Driver）
  private val DRIVER = "com.mysql.cj.jdbc.Driver"

  // 每批提交的行数：太大占内存，太小往返多；1000 是常见的折中值
  private val BATCH_SIZE = 1000

  Class.forName(DRIVER)

  /** 新建连接（调用方负责关闭；推荐用 withConnection）。 */
  def open(): Connection = {
    Config.requirePassword()
    val conn = DriverManager.getConnection(Config.jdbcUrl, Config.dbUser, Config.dbPassword)
    // JDBC 默认 autocommit=true，此时调用 commit() 会抛
    // "Can't call commit when autocommit=true"；本模块采用显式事务，故必须关闭自动提交。
    conn.setAutoCommit(false)
    conn
  }

  /** 连接上下文：正常提交、异常回滚、最后必关。 */
  def withConnection[A](f: Connection => A): A = {
    val conn = open()
    try {
      val result = f(conn)
      conn.commit()
      result
    } catch {
      case e: Throwable =>
        try conn.rollback() catch { case _: Throwable => () }
        throw e
    } finally {
      try conn.close() catch { case _: Throwable => () }
    }
  }

  /**
   * 批量执行一条带参数的写语句（自动分批 + 分批计数）。
   *
   * 计数口径说明（实测踩到过）：MySQL 驱动在批量执行时经常返回
   * `Statement.SUCCESS_NO_INFO`（-2）表示"成功但不报告影响行数"。
   * 若按"只统计 >= 0"来计算，就会把成功的批次统计成 0 行，
   * 打印出"已写入 0 行"这种与实际不符的日志（数据其实已经写入）。
   * 因此这里把 -2 也计入成功数，只在出现真正的异常（EXECUTE_FAILED = -3）时不计。
   *
   * @param sql  含 ? 占位符的 SQL
   * @param rows 参数行集合；每个元素与 ? 一一对应
   * @param bind 把一行绑定到 PreparedStatement 上
   * @return 累计影响行数（含 SUCCESS_NO_INFO 的批次估算值）
   */
  def batchUpdate[A](conn: Connection, sql: String, rows: Iterable[A])(bind: (PreparedStatement, A) => Unit): Int = {
    if (rows.isEmpty) return 0
    var total = 0
    val ps = conn.prepareStatement(sql)
    try {
      var inBatch = 0
      rows.foreach { row =>
        bind(ps, row)
        ps.addBatch()
        inBatch += 1
        if (inBatch >= BATCH_SIZE) {
          total += countBatch(ps.executeBatch())
          inBatch = 0
        }
      }
      if (inBatch > 0) total += countBatch(ps.executeBatch())
      total
    } finally {
      try ps.close() catch { case _: Throwable => () }
    }
  }

  /** 统计一次 executeBatch 的成功行数：>=0 取实际值，-2（SUCCESS_NO_INFO）计 1，-3 失败不计。 */
  private def countBatch(results: Array[Int]): Int =
    results.count(r => r >= 0 || r == java.sql.Statement.SUCCESS_NO_INFO)

  /** 执行一条无参/带参写语句，返回影响行数。 */
  def execute(conn: Connection, sql: String, params: Seq[Any] = Nil): Int = {
    val ps = conn.prepareStatement(sql)
    try {
      params.zipWithIndex.foreach { case (p, i) => setParam(ps, i + 1, p) }
      ps.executeUpdate()
    } finally {
      try ps.close() catch { case _: Throwable => () }
    }
  }

  /** 查询单个整数（COUNT 等）。 */
  def queryLong(conn: Connection, sql: String, params: Seq[Any] = Nil): Long = {
    val ps = conn.prepareStatement(sql)
    try {
      params.zipWithIndex.foreach { case (p, i) => setParam(ps, i + 1, p) }
      val rs = ps.executeQuery()
      try { if (rs.next()) rs.getLong(1) else 0L } finally rs.close()
    } finally {
      try ps.close() catch { case _: Throwable => () }
    }
  }

  /** 查询多行（每行为 Map[列名, 值]），用于结果复核与自检打印。 */
  def queryRows(conn: Connection, sql: String, params: Seq[Any] = Nil): Seq[Map[String, Any]] = {
    val ps = conn.prepareStatement(sql)
    try {
      params.zipWithIndex.foreach { case (p, i) => setParam(ps, i + 1, p) }
      val rs = ps.executeQuery()
      try {
        val meta = rs.getMetaData
        val n = meta.getColumnCount
        val out = ListBuffer[Map[String, Any]]()
        while (rs.next()) {
          val m = (1 to n).map { i => meta.getColumnLabel(i) -> (rs.getObject(i): Any) }.toMap
          out += m
        }
        out.toSeq
      } finally rs.close()
    } finally {
      try ps.close() catch { case _: Throwable => () }
    }
  }

  /** 参数绑定：把 Scala 值映射为 JDBC 类型（NULL 用 setNull 显式处理，避免误写 0）。 */
  private def setParam(ps: PreparedStatement, idx: Int, value: Any): Unit = value match {
    case null                    => ps.setNull(idx, java.sql.Types.NULL)
    case v: Int                  => ps.setInt(idx, v)
    case v: Long                 => ps.setLong(idx, v)
    case v: Double               => ps.setDouble(idx, v)
    case v: BigDecimal           => ps.setBigDecimal(idx, v.bigDecimal)
    case v: Boolean              => ps.setBoolean(idx, v)
    case v: String               => ps.setString(idx, v)
    case v: java.sql.Date        => ps.setDate(idx, v)
    case v: java.sql.Timestamp   => ps.setTimestamp(idx, v)
    case opt: Option[_]          => opt match {
      case Some(x) => setParam(ps, idx, x)
      case None    => ps.setNull(idx, java.sql.Types.NULL)
    }
    case other                   => ps.setObject(idx, other)
  }

  /** 读一个 ResultSet 的某列为 Option[String]（供内部使用）。 */
  private[spark] def optString(rs: ResultSet, col: String): Option[String] = Option(rs.getString(col))

  // ---------------------------------------------------------------------------
  // 类型转换辅助
  //
  // 为什么需要：MySQL 的 BIGINT UNSIGNED 经 JDBC/Spark 取回后可能是
  // java.math.BigDecimal（而不是 Long），直接 getAs[Long] 会抛
  // "ClassCastException: java.math.BigDecimal cannot be cast to java.lang.Long"。
  // 这是本项目全量运行时实际踩到的坑，故统一在这里做兼容转换。
  // ---------------------------------------------------------------------------

  /** 把数值型取值安全转为 Long（Long / Int / BigDecimal / String 均可）。 */
  def toLong(v: Any): Long = v match {
    case null            => throw new IllegalArgumentException("toLong: 取值为 null")
    case l: Long         => l
    case i: Int          => i.toLong
    case s: Short        => s.toLong
    case d: java.math.BigDecimal => d.longValue()
    case b: java.lang.Number     => b.longValue()
    case s: String       => s.trim.toLong
    case other           => other.toString.trim.toLong
  }

  /** 把数值型取值安全转为 Int。 */
  def toInt(v: Any): Int = v match {
    case null            => throw new IllegalArgumentException("toInt: 取值为 null")
    case i: Int          => i
    case l: Long         => l.toInt
    case d: java.math.BigDecimal => d.intValue()
    case b: java.lang.Number     => b.intValue()
    case s: String       => s.trim.toDouble.toInt
    case other           => other.toString.trim.toDouble.toInt
  }

  // ---------------------------------------------------------------------------
  // 幂等辅助：按 scope 清理旧主题（topic 表用 DELETE+INSERT，避免自增主键错位）
  // ---------------------------------------------------------------------------

  /**
   * 删除指定范围（global 或某景点）的旧 LDA 主题及其主题词。
   *
   * 为什么要删而不是 UPSERT：`topic.topic_id` 是自增主键，主题序号语义在
   * `topic_index` 上。若用 ON DUPLICATE KEY UPDATE，重跑时 topic_id 不变但
   * `topic_word.topic_id` 需要跟着改，反而更复杂且容易留下旧词。
   * 因此按「先删词、再删主题、后插入」处理，保证重跑结果干净且可复现。
   *
   * 注意：topic_word 与 topic 之间**没有外键约束**（只有索引），
   * 所以删除顺序靠本方法自己保证，不能依赖数据库级联。
   */
  def deleteTopicScope(conn: Connection, scopeType: String, scopeId: Int): Unit = {
    execute(
      conn,
      """DELETE w FROM topic_word w
         JOIN topic t ON t.topic_id = w.topic_id
        WHERE t.scope_type = ? AND t.scope_id = ?""",
      Seq(scopeType, scopeId)
    )
    execute(conn, "DELETE FROM topic WHERE scope_type = ? AND scope_id = ?", Seq(scopeType, scopeId))
  }

  /**
   * 登记一条分析任务，返回自增 task_id。
   *
   * 用途：模型版本与实验指标要写入 `analysis_task`（详细设计 §4.8 输出栏），
   * 且建表脚本 §9 给出了直接引用的复核 SQL：
   *   SELECT task_id, model_type, model_version, random_seed, model_metrics_json
   *     FROM analysis_task WHERE task_type IN ('mllib','lda') ORDER BY task_id DESC;
   */
  def insertTask(
      conn: Connection,
      taskType: String,
      taskName: String,
      totalCount: Long,
      status: String = "running"
  ): Long = {
    val ps = conn.prepareStatement(
      """INSERT INTO analysis_task (task_type, task_name, status, total_count, started_at)
        |VALUES (?, ?, ?, ?, NOW())""".stripMargin,
      // RETURN_GENERATED_KEYS 定义在 java.sql.Statement 上，而非 PreparedStatement
      java.sql.Statement.RETURN_GENERATED_KEYS
    )
    try {
      ps.setString(1, taskType)
      ps.setString(2, taskName)
      ps.setString(3, status)
      ps.setLong(4, totalCount)
      ps.executeUpdate()
      val keys = ps.getGeneratedKeys
      try { if (keys.next()) keys.getLong(1) else -1L } finally keys.close()
    } finally {
      try ps.close() catch { case _: Throwable => () }
    }
  }

  /** 收尾：更新任务的结束状态、成功量、耗时与模型信息（可空字段按需填）。 */
  def finishTask(
      conn: Connection,
      taskId: Long,
      status: String,
      successCount: Long,
      failCount: Long,
      costSeconds: Long,
      modelType: Option[String] = None,
      modelVersion: Option[String] = None,
      modelPath: Option[String] = None,
      randomSeed: Option[Int] = None,
      modelMetricsJson: Option[String] = None,
      errorMessage: Option[String] = None
  ): Unit = {
    execute(
      conn,
      """UPDATE analysis_task
        |   SET status = ?, success_count = ?, fail_count = ?, finished_at = NOW(),
        |       cost_seconds = ?, model_type = ?, model_version = ?, model_path = ?,
        |       random_seed = ?, model_metrics_json = ?, error_message = ?
        | WHERE task_id = ?""".stripMargin,
      Seq(
        status, successCount, failCount, costSeconds,
        modelType, modelVersion, modelPath, randomSeed, modelMetricsJson, errorMessage, taskId
      )
    )
  }

  /** 写一条 task_log（沿用 Python 侧的 stage + detail_json 四类计数口径）。 */
  def logStep(
      conn: Connection,
      taskId: Long,
      level: String,
      stage: String,
      message: String,
      detailJson: Option[String] = None
  ): Unit = {
    execute(
      conn,
      """INSERT INTO task_log (task_id, level, stage, message, detail_json)
        |VALUES (?, ?, ?, ?, ?)""".stripMargin,
      Seq(taskId, level, stage, message, detailJson)
    )
  }
}
