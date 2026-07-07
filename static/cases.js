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
})();
