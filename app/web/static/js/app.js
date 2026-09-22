/* =============================================================================
   阶段一骨架页脚本
   约束：展示层不直连数据库（详细设计 §2），一律经 /api/* 接口取数。
   ============================================================================= */
(function () {
  "use strict";

  var btn = document.getElementById("btn-check");
  var out = document.getElementById("check-out");

  /** 统一的接口调用：解析 {code, message, data} 信封。 */
  function callApi(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (resp) {
      return resp.json().then(function (body) {
        return { httpStatus: resp.status, body: body };
      });
    });
  }

  function render(lines, isOk) {
    out.textContent = lines.join("\n");
    out.className = "out " + (isOk ? "ok" : "bad");
  }

  btn.addEventListener("click", function () {
    btn.disabled = true;
    render(["自检中…"], true);

    var lines = [];

    // 1) 进程存活（不查库）
    callApi("/healthz")
      .then(function (r) {
        if (r.body.code === 0) {
          lines.push("[1/2] Flask 进程          OK");
          lines.push("      版本/阶段：" + r.body.data.version + " / " + r.body.data.stage);
          lines.push("      数据库目标：" + r.body.data.db_target + "（不含口令）");
        } else {
          lines.push("[1/2] Flask 进程          FAIL  " + r.body.message);
        }
        return callApi("/api/db-ping");
      })
      .catch(function (err) {
        lines.push("[1/2] Flask 进程          FAIL  " + err);
        render(lines, false);
        btn.disabled = false;
      })
      .then(function (r) {
        if (!r) { return; }
        if (r.body.code === 0) {
          var d = r.body.data;
          lines.push("");
          lines.push("[2/2] MySQL 连接          OK");
          lines.push("      版本        ：" + d.version);
          lines.push("      字符集      ：" + d.charset);
          lines.push("      数据库      ：" + d.database_name);
          lines.push("      已建表数量  ：" + d.table_count + "  （期望 17）");
          lines.push("      外键数量    ：" + d.foreign_key_count + "  （期望 14）");
          var tablesOk = Number(d.table_count) === 17;
          var fkOk = Number(d.foreign_key_count) === 14;
          lines.push("");
          lines.push(tablesOk && fkOk ? "结论：环境就绪。" : "结论：表结构与设计基线不一致，请核对。");
          render(lines, tablesOk && fkOk);
        } else {
          lines.push("");
          lines.push("[2/2] MySQL 连接          FAIL  code=" + r.body.code);
          lines.push("      " + r.body.message);
          lines.push("");
          lines.push("排查：确认 MySQL 服务已启动、.env 中 DB_PASSWORD 正确、库 tibet_review 已建。");
          render(lines, false);
        }
        btn.disabled = false;
      });
  });
})();
