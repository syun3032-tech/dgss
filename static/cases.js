/* 案件を探す 一覧の補助機能（サーバ非改変・localStorage連動）。
   要望②: 管理シート追加済み/NG案件を隠す
   要望④: 検索条件（クエリ）の保持・復元
   要望⑤: 既読案件の色付け＋最近見た案件の履歴 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var readJSON = function (id) { var e = $(id); if (!e) return null; try { return JSON.parse(e.textContent); } catch (x) { return null; } };
  var lsGet = function (k) { try { return JSON.parse(localStorage.getItem(k)); } catch (x) { return null; } };
  var lsSet = function (k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (x) {} };

  /* ---------- 要望④: 検索条件の保持 ---------- */
  // 絞り込んだクエリを画面（案件を探す／新着／1000万↑）ごとに保存し、
  // サイドバーの素のリンクで戻って来たら直近の絞り込みを復元する。
  // 旧実装はサイドバーの「?new=1」等も“絞り込み”として保存を上書きしてしまい、
  // 画面遷移のたびに選択が初期化されていた。プリセットと一致する遷移は復元に回す。
  var FILTER_KEY = "kawanoCaseFilter";      // 旧: 単一キー保存（上記バグの世代）
  var FILTER_NS = "kawanoCaseFilter:";      // 新: 画面別保存
  var SECTIONS = ["cases", "new", "budget"];
  // サイドバー各リンクの「素の」クエリ。これと一致＝フィルタ未指定の遷移とみなす。
  var PRESETS = { cases: "", "new": "new=1", budget: "budget_min=10000000&open=1&sort=budget" };
  function sectionOf(p) {
    if (parseInt(p.get("budget_min") || "0", 10) > 0) return "budget";
    if (p.get("new") === "1" || p.get("fresh")) return "new";
    return "cases";
  }
  // 比較用の正規形（pageは無視・キー順を揃える）
  function normQuery(p) {
    var kv = [];
    p.forEach(function (v, k) { if (k !== "page") kv.push(k + "=" + v); });
    return kv.sort().join("&");
  }
  (function persistFilter() {
    var params = new URLSearchParams(location.search);
    // 旧形式の保存が残っていれば画面別キーへ引き継ぐ
    var legacy = lsGet(FILTER_KEY);
    if (typeof legacy === "string" && legacy.charAt(0) === "?") {
      try { localStorage.removeItem(FILTER_KEY); } catch (x) {}
      var lp = new URLSearchParams(legacy);
      if (!lsGet(FILTER_NS + sectionOf(lp))) lsSet(FILTER_NS + sectionOf(lp), legacy.slice(1));
    }
    var sec = sectionOf(params);
    if (normQuery(params) === PRESETS[sec]) {
      // 素のリンクで来た → この画面で前回絞り込んだ条件を復元
      var saved = lsGet(FILTER_NS + sec);
      if (typeof saved === "string" && saved &&
          normQuery(new URLSearchParams("?" + saved)) !== normQuery(params)) {
        location.replace(location.pathname + "?" + saved);
      }
    } else {
      // フィルタ指定つきで開いた → この画面の条件として保存（pageは持ち越さない）
      params.delete("page");
      lsSet(FILTER_NS + sec, params.toString());
    }
  })();
  // 「クリア」押下時は保持を破棄（全画面分）。これだけが意図的なリセット。
  Array.prototype.forEach.call(document.querySelectorAll(".filterbar .actions a.btn"), function (a) {
    if ((a.textContent || "").indexOf("クリア") >= 0) {
      a.addEventListener("click", function () {
        try { localStorage.removeItem(FILTER_KEY); } catch (x) {}
        SECTIONS.forEach(function (s) { try { localStorage.removeItem(FILTER_NS + s); } catch (x) {} });
      });
    }
  });

  /* ---------- 要望⑤: 閲覧履歴（既読色付け＋最近見た案件） ---------- */
  var VIEW_KEY = "kawanoViewed";
  function viewed() { var v = lsGet(VIEW_KEY); return Array.isArray(v) ? v : []; }
  function recordView(row) {
    var id = row.getAttribute("data-id");
    if (!id) return;
    var list = viewed().filter(function (x) { return String(x.id) !== String(id); });
    list.unshift({ id: id, title: row.getAttribute("data-title") || "", t: Date.now() });
    if (list.length > 50) list = list.slice(0, 50);
    lsSet(VIEW_KEY, list);
  }
  var rows = Array.prototype.slice.call(document.querySelectorAll(".case-row"));
  var viewedIds = {}; viewed().forEach(function (x) { viewedIds[String(x.id)] = 1; });
  rows.forEach(function (row) {
    if (viewedIds[String(row.getAttribute("data-id"))]) row.classList.add("visited");
    // クリック＝閲覧として記録（詳細ページへ遷移する前に保存）
    row.addEventListener("click", function () { recordView(row); });
  });
  // 履歴パネル
  (function renderHistory() {
    var box = $("histList"), cnt = $("cntHist");
    if (!box) return;
    var list = viewed();
    if (cnt) cnt.textContent = list.length ? "(" + list.length + ")" : "";
    if (!list.length) { box.innerHTML = '<p class="lt-empty">まだありません。案件を開くとここに履歴が残ります。</p>'; return; }
    box.innerHTML = "";
    list.slice(0, 20).forEach(function (x) {
      var a = document.createElement("a");
      a.className = "lt-hist-item";
      a.href = "/case/" + x.id;
      a.textContent = x.title || ("案件 #" + x.id);
      box.appendChild(a);
    });
    var clear = document.createElement("button");
    clear.type = "button"; clear.className = "lt-hist-clear"; clear.textContent = "履歴を消す";
    clear.addEventListener("click", function (e) {
      e.preventDefault();
      try { localStorage.removeItem(VIEW_KEY); } catch (x) {}
      rows.forEach(function (r) { r.classList.remove("visited"); });
      renderHistory();
    });
    box.appendChild(clear);
  })();

  /* ---------- 要望②: 管理シート追加済み/NG案件を隠す ---------- */
  // サーバが渡す集合（申請管理に入っている案件の external_id）と、
  // ブラウザ保存のミラー（揮発ホスト対策）の両方をマージして判定する。
  var addedSet = {}, ngSet = {};
  (readJSON("addedEids") || []).forEach(function (e) { if (e) addedSet[e] = 1; });
  (readJSON("ngEids") || []).forEach(function (e) { if (e) ngSet[e] = 1; });
  var mirror = lsGet("kawanoApplications") || {};
  Object.keys(mirror).forEach(function (eid) {
    addedSet[eid] = 1;
    if (mirror[eid] && mirror[eid].status === "NG") ngSet[eid] = 1;
  });
  var nAdded = 0, nNg = 0;
  rows.forEach(function (row) {
    var eid = row.getAttribute("data-eid");
    if (eid && addedSet[eid]) { row.classList.add("is-added"); nAdded++; }
    if (eid && ngSet[eid]) { row.classList.add("is-ng"); nNg++; }
  });
  if ($("cntAdded")) $("cntAdded").textContent = nAdded ? "(" + nAdded + ")" : "";
  if ($("cntNg")) $("cntNg").textContent = nNg ? "(" + nNg + ")" : "";

  var TOOLS_KEY = "kawanoListTools";
  var tools = lsGet(TOOLS_KEY) || {};
  function applyTools() {
    document.body.classList.toggle("hide-added", !!tools.hideAdded);
    document.body.classList.toggle("hide-ng", !!tools.hideNg);
  }
  var ha = $("hideAdded"), hn = $("hideNg");
  if (ha) { ha.checked = !!tools.hideAdded; ha.addEventListener("change", function () { tools.hideAdded = ha.checked; lsSet(TOOLS_KEY, tools); applyTools(); }); }
  if (hn) { hn.checked = !!tools.hideNg; hn.addEventListener("change", function () { tools.hideNg = hn.checked; lsSet(TOOLS_KEY, tools); applyTools(); }); }
  applyTools();

  /* ---------- AIで応募できる案件を探す（一覧バッチ判定） ----------
     表示中の募集中案件を上から順にAI応募可否判定（/case/<id>/ai-assist）にかけ、
     行に〇/△/✕バッジを付けてピックアップする。判定はサーバにキャッシュされ、
     2回目以降・再表示は無料。1件ごとにAI利用料が発生するため実行前に件数を確認する。 */
  (function () {
    var btn = $("aiBatch"), bar = $("aiBatchBar");
    if (!btn || !bar) return;
    var running = false, stopFlag = false;

    function verdictBadge(row, v) {
      var meta = row.querySelector(".cr-meta");
      if (!meta) return;
      var old = meta.querySelector(".ai-verdict"); if (old) old.remove();
      var cls = v === "〇" ? "ok" : v === "△" ? "warn" : v === "✕" ? "ng" : "unk";
      var s = document.createElement("span");
      s.className = "tag ai-verdict " + cls;
      s.textContent = "AI " + v;
      meta.insertBefore(s, meta.firstChild);
      row.setAttribute("data-verdict", v);
    }

    // 判定済み（キャッシュ）をページ表示時に復元（AI呼び出しなし＝無料）
    var idsAll = rows.map(function (r) { return r.getAttribute("data-id"); }).filter(Boolean);
    if (idsAll.length) {
      fetch("/ai/verdicts?ids=" + idsAll.join(","))
        .then(function (r) { return r.ok ? r.json() : {}; })
        .then(function (map) {
          rows.forEach(function (r) { var v = map[r.getAttribute("data-id")]; if (v) verdictBadge(r, v); });
        }).catch(function () {});
    }

    function targets() {
      return rows.filter(function (r) {
        if (r.classList.contains("closed")) return false;                          // 終了
        if (r.classList.contains("is-added") || r.classList.contains("is-ng")) return false;  // 判断済み
        if (r.offsetParent === null) return false;                                 // 非表示（隠す設定）
        return !r.getAttribute("data-verdict");                                    // 既に判定済み
      });
    }
    function setBar(html) { bar.hidden = false; bar.innerHTML = html; }
    function counterHtml(counts) {
      return ' ・ 〇' + counts["〇"] + " △" + counts["△"] + " ✕" + counts["✕"] +
        (counts["？"] ? " ？" + counts["？"] : "");
    }

    btn.addEventListener("click", function () {
      if (running) return;
      var list = targets();
      if (!list.length) {
        alert("判定対象がありません。\n（表示中の募集中案件は、すべて判定済みか管理シートで判断済みです）");
        return;
      }
      if (!confirm("表示中の募集中案件 " + list.length + " 件をAIで応募可否判定します。\n\n" +
        "・1件ごとにAI利用料がかかります（目安 10〜30円/件。判定済みの案件は無料）\n" +
        "・時間は1件あたり10〜30秒ほど。途中でいつでも中止できます\n\n実行しますか？")) return;
      running = true; stopFlag = false; btn.disabled = true;
      var counts = { "〇": 0, "△": 0, "✕": 0, "？": 0 }, done = 0;

      function finish(msg) {
        running = false; btn.disabled = false;
        setBar("<b>" + msg + "</b>" + counterHtml(counts) +
          (counts["〇"] ? ' <label class="lt-check onlyok-label"><input type="checkbox" id="onlyOk"> 〇の案件だけ表示</label>' : ""));
        var only = $("onlyOk");
        if (only) only.addEventListener("change", function () {
          rows.forEach(function (r) {
            r.classList.toggle("not-ok-hidden", only.checked && r.getAttribute("data-verdict") !== "〇");
          });
        });
      }
      function step(i) {
        if (stopFlag) { finish("中止しました（" + done + "/" + list.length + "件）"); return; }
        if (i >= list.length) { finish("完了（" + done + "件を判定）"); return; }
        var row = list[i];
        setBar("AI判定中 " + (i + 1) + "/" + list.length + counterHtml(counts) +
          ' <button type="button" class="btn small" id="aiStop">中止</button>' +
          '<span class="ai-batch-now">' + (row.getAttribute("data-title") || "") + "</span>");
        var st = $("aiStop"); if (st) st.onclick = function () { stopFlag = true; st.disabled = true; st.textContent = "中止します…"; };
        fetch("/case/" + row.getAttribute("data-id") + "/ai-assist",
          { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" } })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (j && j.enabled === false) { stopFlag = true; alert("AIモードが使えない状態です（キー未設定またはアカウント未許可）。"); return; }
            var v = (j && j.eligibility && j.eligibility.verdict) || "？";
            if (!(v in counts)) v = "？";
            counts[v]++; done++;
            verdictBadge(row, v);
          })
          .catch(function () { counts["？"]++; done++; verdictBadge(row, "？"); })
          .then(function () { step(i + 1); });
      }
      step(0);
    });
  })();
})();
