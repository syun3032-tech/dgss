/* 入札・工程＆協力会社 管理 — bid-next-eta（川野電気システム）のクライアントアプリを
   DGSS(Flask)へ忠実移植したもの。サーバ(applications/companies)をデータ源とし、
   揮発DB対策で localStorage にミラー保存する。 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { var d = document.createElement("div"); d.textContent = (s == null ? "" : s); return d.innerHTML; };
  var read = function (id) { try { return JSON.parse($(id).textContent); } catch (e) { return null; } };

  var CFG = read("cfgData") || {};
  var CASES = read("casesData") || [];
  var COMPANIES = read("companiesData") || [];
  var TODAY = new Date((CFG.today || "") + "T00:00:00");
  var STATUSES = CFG.statuses || [];                       // 公共シートの状況 [{id,accent}]
  var STATUSES_PRIVATE = CFG.statuses_private || [];       // 民間シートの状況 [{id,accent}]
  var ACCENT = {};
  STATUSES.forEach(function (s) { ACCENT[s.id] = s.accent; });
  STATUSES_PRIVATE.forEach(function (s) { ACCENT[s.id] = s.accent; });
  // シート区分(公共/民間)に応じた状況リストを返す。民間だけ見積フローの列を使う。
  // 公共役務(役務)は公共と同じ入札フロー。
  function statusesFor(sector) { return sector === "民間" ? STATUSES_PRIVATE : STATUSES; }

  /* ---------- シート定義（川野さん要望 2026-07: 公共を建設業/役務に分割） ----------
     公共ページは【建設業】【役務】の2シート、民間は独立ページ（混在させない）。 */
  var SHEETS = [
    { id: "公共",     label: "公共案件【建設業】", col: "#2563eb" },
    { id: "公共役務", label: "公共案件【役務】",   col: "#0d9488" },
    { id: "民間",     label: "民間 管理シート",     col: "#d97706" }
  ];
  function sheetDef(id) { return SHEETS.filter(function (s) { return s.id === id; })[0] || SHEETS[0]; }
  // 協力会社の区分は公共/民間の2値のまま（役務シートは公共の会社リストを共用）
  function coSectorOf(sheetOrSector) { return sheetOrSector === "民間" ? "民間" : "公共"; }
  var ASSIGNEES = CFG.assignees || [];                     // [{id,color}]
  var ACOLOR = {}; ASSIGNEES.forEach(function (a) { ACOLOR[a.id] = a.color; });
  var WORK = CFG.works || {};                              // {name:color}
  var METHODS = CFG.submit_methods || [];

  /* ---------- 担当者の追加（ローカル保存・サーバ既定に積み増し） ----------
     ASSIGNEES の既定はサーバ(db.ASSIGNEES)。揮発ホストでも消えないよう、画面から
     足した担当者は localStorage に保存し、起動時に既定へマージする。
     案件側の assignee は自由文字列で保存されるため、サーバ改修は不要。 */
  var DEFAULT_ASSIGNEE_IDS = ASSIGNEES.map(function (a) { return a.id; });
  var ADD_OPT = "__add_assignee__";
  var ASSIGNEE_PALETTE = ["#16a34a", "#2563eb", "#ea580c", "#7c3aed", "#db2777",
    "#0891b2", "#ca8a04", "#4338ca", "#b45309", "#0d9488", "#65a30d", "#9333ea"];
  function pickAssigneeColor() {
    var used = {}; ASSIGNEES.forEach(function (a) { used[a.color] = 1; });
    for (var i = 0; i < ASSIGNEE_PALETTE.length; i++) if (!used[ASSIGNEE_PALETTE[i]]) return ASSIGNEE_PALETTE[i];
    return ASSIGNEE_PALETTE[ASSIGNEES.length % ASSIGNEE_PALETTE.length];
  }
  function insertAssignee(a) {
    // 「未割当」は常に末尾。新規担当者はその直前に差し込む。
    ACOLOR[a.id] = a.color;
    var idx = -1;
    for (var i = 0; i < ASSIGNEES.length; i++) if (ASSIGNEES[i].id === "未割当") { idx = i; break; }
    if (idx >= 0) ASSIGNEES.splice(idx, 0, a); else ASSIGNEES.push(a);
  }
  function saveCustomAssignees() {
    var custom = ASSIGNEES.filter(function (a) { return DEFAULT_ASSIGNEE_IDS.indexOf(a.id) < 0; })
      .map(function (a) { return { id: a.id, color: a.color }; });
    try { localStorage.setItem("kawanoAssignees", JSON.stringify(custom)); } catch (e) {}
  }
  function loadCustomAssignees() {
    var raw; try { raw = JSON.parse(localStorage.getItem("kawanoAssignees")); } catch (e) { return; }
    if (!Array.isArray(raw)) return;
    raw.forEach(function (a) {
      if (!a || !a.id || a.id === "未割当") return;
      if (ASSIGNEES.some(function (x) { return x.id === a.id; })) return;
      insertAssignee({ id: a.id, color: a.color || pickAssigneeColor() });
    });
  }
  // 担当者を1人追加（名前を尋ねる）。追加/既存ならその名前を、中止なら null を返す。
  function addAssigneePrompt() {
    var name = (window.prompt("追加する担当者の名前を入力してください（例: 田中さん）") || "").trim();
    if (!name) return null;
    if (name === "未割当") { alert("「未割当」は予約名のため追加できません。"); return null; }
    if (ASSIGNEES.some(function (a) { return a.id === name; })) { alert("「" + name + "」は既に登録されています。"); return name; }
    insertAssignee({ id: name, color: pickAssigneeColor() });
    saveCustomAssignees();
    return name;
  }
  // 担当者セレクトの <option> 群（末尾に「＋ 追加」を付ける）。両モーダル共通。
  function assigneeOptions(sel) {
    return ASSIGNEES.map(function (a) {
      return '<option value="' + esc(a.id) + '"' + (a.id === sel ? " selected" : "") + ">" + esc(a.id) + "</option>";
    }).join("") + '<option value="' + ADD_OPT + '">＋ 新しい担当者を追加…</option>';
  }

  /* ---------- 共通ヘルパー（reference の es/en/er/ed/ei/ec と同等） ---------- */
  var fmtMan = function (y) { return Math.round((Number(y) || 0) / 1e4).toLocaleString("ja-JP") + "万"; };
  var toYen = function (s) { return Number(String(s == null ? "" : s).replace(/[^\d]/g, "")) || 0; };
  var dUntil = function (d) { if (!d) return null; var t = new Date(d + "T00:00:00"); if (isNaN(t)) return null; return Math.round((t - TODAY) / 864e5); };
  var md = function (d) { var t = new Date(d + "T00:00:00"); return isNaN(t) ? "" : (t.getMonth() + 1) + "/" + t.getDate(); };
  var workColor = function (w) { return WORK[w] || "#94a3b8"; };

  // 次の締切マイルストーン（ei）
  function milestone(c) {
    // 民間シートは「見積提出期限」(apply_deadline を流用)を締切マイルストーンにする。
    if ((c.sector || "公共") === "民間")
      return c.apply_deadline ? { label: "見積提出", date: c.apply_deadline } : null;
    var apply = c.apply_deadline || c.deadline || "";
    if (c.status === "参加申請準備前") return apply ? { label: "参加申請", date: apply } : null;
    if (["入札参加申請済み", "協力会社探し中", "見積取得"].indexOf(c.status) >= 0)
      return c.bid_deadline ? { label: "入札書提出", date: c.bid_deadline } : null;
    if (c.status === "入札書提出済み") return c.open_date ? { label: "開札", date: c.open_date } : null;
    return null;
  }
  // 残日数→色帯（ec）
  function band(days) {
    if (days == null) return { bg: "#f1f5f9", fg: "#94a3b8" };
    if (days <= 7) return { bg: "#fee2e2", fg: "#dc2626" };
    if (days <= 14) return { bg: "#fef3c7", fg: "#b45309" };
    return { bg: "#ecfdf5", fg: "#047857" };
  }
  function daysLabel(days) {
    if (days == null) return "";
    if (days < 0) return Math.abs(days) + "日超";
    if (days === 0) return "本日";
    return "あと" + days + "日";
  }

  /* ---------- 保存フィードバック（トースト） ---------- */
  var toastTimer = null;
  function toast(msg) {
    var el = document.getElementById("appToast");
    if (!el) { el = document.createElement("div"); el.id = "appToast"; document.body.appendChild(el); }
    el.textContent = msg; el.className = "show";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = ""; }, 2200);
  }

  /* ---------- localStorage ミラー ---------- */
  function mirrorCase(c, mtime) {
    var KEY = "kawanoApplications", map;
    try { map = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { map = {}; }
    if (!c.external_id) return;
    map[c.external_id] = {
      // 世代(編集時刻)。restoreはこれとサーバ側 client_mtime を比べ、新しい方だけ残す。
      mtime: mtime || Date.now(),
      external_id: c.external_id, status: c.status, assignee: c.assignee,
      flag: c.flag, applied_date: c.applied_date || "", note: c.note || "",
      apply_deadline: c.apply_deadline || "", bid_deadline: c.bid_deadline || "",
      open_date: c.open_date || "", submit_method: c.submit_method || "",
      work: c.work || "", materials: c.materials || "",
      needs_check: c.needs_check ? 1 : 0, award_called: c.award_called ? 1 : 0,
      bid_plan: c.bid_plan || 0, win_amount: c.win_amount || 0,
      win_company: c.win_company || "", cost_items: c.cost_items || [],
      partner: c.partner || "", partners: c.partners || [],
      // 手動案件(manual:)は案件本体がDB再生成で消えるため、復元用に案件情報も保持する。
      title: c.title || "", agency: c.agency || "", sector: c.sector || "公共",
      source: c.source || ""
    };
    try { localStorage.setItem(KEY, JSON.stringify(map)); } catch (e) {}
  }
  function mirrorCompanies() {
    try { localStorage.setItem("kawanoCompanies", JSON.stringify(COMPANIES)); } catch (e) {}
  }

  /* ---------- ステート ---------- */
  // sheet=どの管理シートか(公共/民間・完全分離)。tab=シート内のタブ(案件/会社/もうけ/レポート)。
  var state = { sheet: "公共", tab: "案件", assignee: null, sector: null, statuses: [], q: "", coTag: null, coPartner: false, coSort: false };

  // フィルタ・並び替え・タブの選択を localStorage に保持する。
  // 案件保存やNG移動は location.reload() を伴うため、保持しないと毎回初期化されてしまう。
  // ページを離れて戻ってきたときも直前の絞り込みを復元する。
  var UI_KEY = "kawanoUiState";
  function saveState() {
    try { localStorage.setItem(UI_KEY, JSON.stringify(state)); } catch (e) {}
  }
  function loadState() {
    var s; try { s = JSON.parse(localStorage.getItem(UI_KEY)); } catch (e) { return; }
    if (!s || typeof s !== "object") return;
    ["sheet", "tab", "assignee", "sector", "statuses", "q", "coTag", "coPartner", "coSort"].forEach(function (k) {
      if (s[k] !== undefined && s[k] !== null) state[k] = s[k];
    });
    // 復元値の型ガード。壊れた保存状態（型違い）で描画ごと落ちないようにする。
    if (!Array.isArray(state.statuses)) state.statuses = [];
    if (typeof state.q !== "string") state.q = "";
    if (typeof state.assignee !== "string") state.assignee = null;
    if (typeof state.coTag !== "string") state.coTag = null;
    state.coPartner = !!state.coPartner; state.coSort = !!state.coSort;
    // 旧版はタブがシートを兼ねていた（公共/民間/会社/もうけ/レポート）。新構造へ読み替える。
    if (state.tab === "公共" || state.tab === "民間") { state.sheet = state.tab; state.tab = "案件"; }
    if (["案件", "会社", "もうけ", "レポート"].indexOf(state.tab) < 0) state.tab = "案件";
    if (["公共", "公共役務", "民間"].indexOf(state.sheet) < 0) state.sheet = "公共";
  }

  /* ============================================================
     タブバー
  ============================================================ */
  function renderTabs() {
    // 公共・民間は完全分離の管理シート。上段でシートを選び、下段のタブ
    // （案件・協力会社・利益計算・月次レポート）は選択中シートの内容だけを出す。
    // 公共ページは【建設業】【役務】の2枚だけを出し、民間ページには民間だけを出す
    // （公共管理シートに民間を混ぜない・川野さん要望 2026-07）。
    var byS = {};
    CASES.forEach(function (c) { var s = c.sector || "公共"; byS[s] = (byS[s] || 0) + 1; });
    var isPriv = state.sheet === "民間";
    var sheets = SHEETS.filter(function (s) { return (s.id === "民間") === isPriv; });
    var sheetBtns = sheets.map(function (s) {
      var on = state.sheet === s.id;
      return '<button class="sheettab" data-sheet="' + s.id + '" style="display:flex;align-items:center;gap:6px;' +
        'padding:8px 18px;border-radius:10px;cursor:pointer;font-weight:700;font-size:14px;' +
        (on ? "background:" + s.col + ";color:#fff;border:1px solid " + s.col
            : "background:" + s.col + "14;color:" + s.col + ";border:1px solid " + s.col + "44") + '">' +
        s.label + ' <span style="font-size:11px;font-weight:600;opacity:.85">' + (byS[s.id] || 0) + "件</span></button>";
    }).join("");
    var coN = COMPANIES.filter(function (co) { return (co.sector || "公共") === coSectorOf(state.sheet); }).length;
    var tabs = [
      { id: "案件", label: "案件・工程", sub: (byS[state.sheet] || 0) + "件" },
      { id: "会社", label: "協力会社", sub: coN + "社" },
      { id: "もうけ", label: "利益計算", sub: "落札−発注" },
      { id: "レポート", label: "月次レポート", sub: "月別" }
    ];
    var tabBtns = tabs.map(function (t) {
      return '<button class="bidtab' + (state.tab === t.id ? " active" : "") + '" data-tab="' + t.id + '">' +
        '<span class="bt-label">' + t.label + '</span><span class="bt-sub">' + t.sub + "</span></button>";
    }).join("");
    $("bidTabs").innerHTML = '<div style="width:100%">' +
      '<div style="display:flex;gap:8px;margin:0 0 10px">' + sheetBtns + "</div>" +
      '<div style="display:flex;gap:8px">' + tabBtns + "</div></div>";
    Array.prototype.forEach.call($("bidTabs").querySelectorAll(".sheettab"), function (b) {
      b.addEventListener("click", function () {
        state.sheet = b.getAttribute("data-sheet");
        state.statuses = [];   // 公共/民間で状況が違うため、シート切替時は状況フィルタを解除
        render();
      });
    });
    Array.prototype.forEach.call($("bidTabs").querySelectorAll(".bidtab"), function (b) {
      b.addEventListener("click", function () {
        state.tab = b.getAttribute("data-tab");
        render();
      });
    });
  }

  /* ============================================================
     タブ1: 案件・工程（カンバン）
  ============================================================ */
  function renderKanban(sector) {
    // この管理シート(公共/民間)に属する案件だけを母集団にする。タブ自体が区分を兼ねる。
    var sectorCases = CASES.filter(function (c) { return (c.sector || "公共") === sector; });
    var SL = statusesFor(sector);   // このシートの状況リスト（公共/民間で列が異なる）
    // このシートに無い状況の絞り込みが残っていると列が1本も表示されず
    // 「データが消えた」ように見えるため、シートに合わない選択は自動で外す
    // （例: 公共でNG絞り込み→サイドバーから民間へ移動した時）。
    if (state.statuses.length) {
      var validSt = {}; SL.forEach(function (s) { validSt[s.id] = 1; });
      state.statuses = state.statuses.filter(function (id) { return validSt[id]; });
    }
    // 担当者で絞り込み
    var rows = state.assignee ? sectorCases.filter(function (c) { return (c.assignee || "未割当") === state.assignee; }) : sectorCases;

    // 担当者チップ件数（この区分内で数える）
    var counts = {}; ASSIGNEES.forEach(function (a) { counts[a.id] = 0; });
    sectorCases.forEach(function (c) { var a = c.assignee || "未割当"; counts[a] = (counts[a] || 0) + 1; });

    // サマリー
    var active = rows.filter(function (c) { return ["自社落札", "他社落札", "NG", "見積集まらず"].indexOf(c.status) < 0; });
    var soon = [], over = 0;
    rows.forEach(function (c) {
      // 期限超過は従来どおり「今の状況に対応する次の期限」で数える
      var ms = milestone(c);
      if (ms) { var d0 = dUntil(ms.date); if (d0 != null && d0 < 0) over++; }
      // 直近アラートは【参加申請】【入札書提出】【開札】の3種それぞれで出す（要望⑯）。
      // 完了・NGの案件は対象外。民間は見積提出のみ。
      if (["自社落札", "他社落札", "NG", "見積集まらず"].indexOf(c.status) >= 0) return;
      var kinds = (c.sector || "公共") === "民間"
        ? [{ label: "見積提出", date: c.apply_deadline || c.deadline }]
        : [{ label: "参加申請", date: c.apply_deadline || c.deadline },
           { label: "入札書提出", date: c.bid_deadline },
           { label: "開札", date: c.open_date }];
      kinds.forEach(function (k) {
        if (!k.date) return;
        var d = dUntil(k.date); if (d == null) return;
        if (d >= 0 && d <= 7) soon.push({ c: c, ms: k, d: d });
      });
    });
    soon.sort(function (a, b) { return a.d - b.d; });

    // チップは最大2行に収めて縦スクロール（横に長く広がらないよう .chips-wrap で内包）。
    var chips = '<div class="chips"><span class="chips-label">担当で絞る:</span><div class="chips-wrap">' +
      '<button class="chip2' + (!state.assignee ? " on" : "") + '" data-as="">全員 <b>' + sectorCases.length + "</b></button>" +
      ASSIGNEES.map(function (a) {
        return '<button class="chip2' + (state.assignee === a.id ? " on" : "") + '" data-as="' + esc(a.id) + '" style="--c:' + a.color + '">' +
          '<span class="dot" style="background:' + a.color + '"></span>' + esc(a.id) + " <b>" + (counts[a.id] || 0) + "</b></button>";
      }).join("") +
      '<button class="chip2" data-addas="1" title="担当者を追加" style="--c:#64748b;border-style:dashed">＋ 担当者</button>' +
      "</div></div>";

    // 状態（ステータス）で絞る。担当の絞り込み後(rows)の件数で表示。
    var scnt = {}; rows.forEach(function (c) { scnt[c.status] = (scnt[c.status] || 0) + 1; });
    var chipsStatus = '<div class="chips"><span class="chips-label">状態で絞る:</span><div class="chips-wrap">' +
      '<button class="chip2' + (!state.statuses.length ? " on" : "") + '" data-st="">すべて <b>' + rows.length + "</b></button>" +
      SL.map(function (s) {
        var on = state.statuses.indexOf(s.id) >= 0;
        return '<button class="chip2' + (on ? " on" : "") + '" data-st="' + esc(s.id) + '" style="--c:' + s.accent + '">' +
          '<span class="dot" style="background:' + s.accent + '"></span>' + esc(s.id) + " <b>" + (scnt[s.id] || 0) + "</b></button>";
      }).join("") + "</div></div>";
    chips += chipsStatus;

    var cards = '<div class="sumrow">' +
      sumCard(active.length, "進行中の案件") +
      sumCard(soon.length, "1週間以内の締切", soon.length ? "warn" : "") +
      sumCard(over, "期限超過・要対応", over ? "danger" : "") +
      '<button class="addcase" id="addCaseBtn" data-sec="' + sector + '">＋ 案件を追加<small>' + sheetDef(sector).label + 'の案件をその場で</small></button>' +
      "</div>";

    var warn = "";
    if (soon.length) {
      warn = '<div class="warnbar"><span class="wb-title">直近の締切（入札1週間前〜）</span>' +
        soon.slice(0, 9).map(function (x) {
          var b = band(x.d);
          // クリックで該当案件を開く（要望⑩）。どの案件のアラートか分かるよう案件名も添える。
          return '<button type="button" class="wb-item" data-case="' + x.c.case_id + '" title="クリックで案件を開く" style="background:' + b.bg + ";color:" + b.fg + '">' +
            esc(x.ms.label) + " " + md(x.ms.date) + "・" + daysLabel(x.d) + ' <b class="wb-name">' + esc(x.c.title) + "</b></button>";
        }).join("") + "</div>";
    }

    // カンバン列（状態フィルタ時は選んだ状態の列だけ表示・複数可）
    var shownStatuses = state.statuses.length
      ? SL.filter(function (s) { return state.statuses.indexOf(s.id) >= 0; })
      : SL;
    var cols = shownStatuses.map(function (s) {
      var list = rows.filter(function (c) { return c.status === s.id; });
      return '<div class="kcol">' +
        '<div class="kcol-head" style="border-top:3px solid ' + s.accent + '">' +
          '<span>' + esc(s.id) + '</span><span class="kcnt">' + list.length + "</span></div>" +
        '<div class="kcol-body">' + (list.map(card).join("") || '<div class="kempty">—</div>') + "</div></div>";
    }).join("");

    // 安全網: このシートの列に無い状況の案件も必ず表示する。
    // （例: 民間フロー化以前に登録された民間案件は「参加申請準備前」等の公共状況の
    //   ままで、どの列にも一致せず“消えて”いた。開いて状況を選び直せば正しい列に入る）
    var knownSt = {}; SL.forEach(function (s) { knownSt[s.id] = 1; });
    var stray = rows.filter(function (c) { return !knownSt[c.status]; });
    if (stray.length && !state.statuses.length) {
      cols += '<div class="kcol">' +
        '<div class="kcol-head" style="border-top:3px solid #dc2626">' +
          '<span>状況の選び直し待ち</span><span class="kcnt">' + stray.length + "</span></div>" +
        '<div class="kcol-note" style="font-size:11px;color:#b45309;padding:4px 8px">旧シートの状況のままの案件です。開いて状況を選び直すと各列に入ります。</div>' +
        '<div class="kcol-body">' + stray.map(card).join("") + "</div></div>";
    }

    var html = chips + cards + warn + '<div class="kanban">' + cols + "</div>";
    if (!sectorCases.length) {
      var emptyMsg = sector === "民間"
        ? 'まだ民間の管理案件がありません。<b>＋案件を追加</b>で民間案件をその場で登録できます。'
        : sector === "公共役務"
        ? 'まだ役務（点検・保守・委託など）の管理案件がありません。<b>＋案件を追加</b>で役務案件をその場で登録できます。'
        : 'まだ公共の管理案件がありません。<b>＋案件を追加</b>で公共案件をその場で登録するか、<a href="/">案件を探す</a>から公共案件を開いて登録できます。';
      html = chips + cards + '<p class="empty">' + emptyMsg + "</p>";
    }
    return html;
  }

  function sumCard(num, label, cls) {
    return '<div class="sumc ' + (cls || "") + '"><span class="n">' + num + '</span><span class="l">' + label + "</span></div>";
  }

  // 案件カードの区分バッジ（公共=青/役務=青緑/民間=橙）。CSS追加不要のインライン指定。
  function secBadge(sec) {
    sec = sec || "公共";
    var col = sec === "民間" ? "#d97706" : sec === "公共役務" ? "#0d9488" : "#2563eb";
    var label = sec === "公共役務" ? "役務" : sec;
    return '<span class="kc-sec" style="font-size:10px;line-height:1;padding:2px 5px;border-radius:8px;color:' +
      col + ';border:1px solid ' + col + '55;background:' + col + '14">' + label + "</span>";
  }

  function card(c) {
    var ms = milestone(c), d = ms ? dUntil(ms.date) : null, b = band(d);
    var qreq = (c.partners || []).length, qrep = (c.partners || []).filter(function (p) { return p.replied; }).length;
    return '<div class="kcard" data-id="' + c.case_id + '">' +
      '<div class="kc-top"><span class="wdot" style="background:' + workColor(c.work_eff) + '"></span>' +
        secBadge(c.sector) +
        '<span class="kc-tag">' + esc(c.agency || c.work_eff || "") + "</span>" +
        '<span class="kc-as" style="background:' + (ACOLOR[c.assignee || "未割当"] || "#a8a29e") + '">' + esc(c.assignee || "未割当") + "</span></div>" +
      '<div class="kc-title">' + esc(c.title) + "</div>" +
      (ms ? '<div class="kc-dl" style="background:' + b.bg + ";color:" + b.fg + '">' + esc(ms.label) + " " + md(ms.date) + " ・ " + daysLabel(d) + "</div>" : "") +
      (qreq ? '<div class="kc-q">見積 依頼' + qreq + "社/回答" + qrep + "</div>" : "") +
      (c.flag ? '<div class="kc-flag">' + esc(c.flag) + "</div>" : "") +
      '<div class="kc-acts" onclick="event.stopPropagation()">' +
        '<a class="kc-link" href="/case/' + c.case_id + '" target="_blank" rel="noopener">詳細・公告</a>' +
        (c.detail_url ? '<a class="kc-link" href="' + esc(c.detail_url) + '" target="_blank" rel="noopener">NJSS</a>' : "") +
        '<a class="kc-link" href="/case/' + c.case_id + '#aiRun" target="_blank" rel="noopener">AI</a>' +
        (c.status !== "NG" ? '<button type="button" class="kc-ng" data-ng="' + c.case_id + '">NGへ</button>' : "") +
      "</div>" +
      "</div>";
  }

  /* ============================================================
     タブ2: 協力会社
  ============================================================ */
  function renderCompanies() {
    var q = state.q.trim();
    // 協力業者リストは公共/民間で完全分離。選択中シートの会社だけを出す。
    var list = COMPANIES.filter(function (c) {
      if ((c.sector || "公共") !== coSectorOf(state.sheet)) return false;
      if (state.coPartner && !c.partner) return false;
      if (state.coTag && (c.tags || []).indexOf(state.coTag) < 0) return false;
      if (!q) return true;
      var hay = [c.name, c.area, c.note].concat(c.tags || []).join(" ");
      return hay.indexOf(q) >= 0;
    });
    if (state.coSort) list = list.slice().sort(function (a, b) { return (b.rating || 0) - (a.rating || 0); });
    var tagChips = Object.keys(WORK).slice(0, 10).map(function (t) {
      var on = state.coTag === t;
      return '<button class="cochip co-tagf' + (on ? " on" : "") + '" data-t="' + esc(t) + '" style="--c:' + workColor(t) + '">' + esc(t) + (on ? " ✕" : "") + "</button>";
    }).join("");
    var head = '<div class="co-head"><input id="coSearch" class="co-search" placeholder="会社名・エリア・工事内容でさがす" value="' + esc(state.q) + '">' +
      '<button class="btn primary" id="coAdd">＋ 会社を追加</button></div>' +
      '<div class="co-filters">' +
      '<button class="cochip co-pf' + (state.coPartner ? " on" : "") + '">★よく頼む会社</button>' +
      '<button class="cochip co-sf' + (state.coSort ? " on" : "") + '">評価が高い順</button><span class="co-sep"></span>' + tagChips + "</div>";
    var cards = list.map(function (c) {
      var stars = "★★★★★".slice(0, c.rating || 0) + "☆☆☆☆☆".slice(0, 5 - (c.rating || 0));
      return '<div class="cocard" data-id="' + c.id + '">' +
        '<div class="co-row1">' + (c.partner ? '<span class="co-fav">★よく頼む</span>' : "") +
          '<span class="co-name">' + esc(c.name) + "</span>" +
          '<span class="co-stars">' + stars + "</span></div>" +
        '<div class="co-tags">' + (c.tags || []).map(function (t) {
          return '<span class="co-tag" style="background:' + workColor(t) + '22;color:' + workColor(t) + '">' + esc(t) + "</span>";
        }).join("") + "</div>" +
        '<div class="co-meta">' + (c.area ? esc(c.area) : "") + (c.tel ? " ・ TEL " + esc(c.tel) : "") +
          (c.url ? ' ・ <a href="' + esc(c.url) + '" target="_blank" rel="noopener">サイト</a>' : "") + "</div>" +
        (c.note ? '<div class="co-note">' + esc(c.note) + "</div>" : "") +
        ((c.reviews || []).length ? '<div class="co-revs">' + c.reviews.map(function (r) { return "<div>・" + esc(r) + "</div>"; }).join("") + "</div>" : "") +
        '<div class="co-acts"><button class="btn ghost small co-edit">編集</button><button class="btn ghost small co-del">削除</button></div>' +
        "</div>";
    }).join("");
    return head + '<div class="cogrid">' + (cards ||
      '<p class="empty">' + state.sheet + 'シートの協力会社がまだありません。「会社を追加」から登録してください。</p>') + "</div>";
  }

  /* ============================================================
     タブ3: 利益計算
  ============================================================ */
  function partnerCost(c) {
    var sel = (c.partners || []).filter(function (p) { return p.selected; });
    if (sel.length) return sel.reduce(function (s, p) { return s + toYen(p.amount); }, 0);
    return 0;
  }
  function costTotal(c) {
    return (c.cost_items || []).reduce(function (s, it) { return s + (toYen(it.amount) || 0); }, 0);
  }
  function renderProfit() {
    // 自社落札の案件だけで集計する（要望⑭: 他社落札や未落札は利益計算に入れない）。
    // 利益＝落札額−(自社原価＋依頼先への発注)（要望⑮の式に統一）。
    var rows = CASES.filter(function (c) {
      return (c.sector || "公共") === state.sheet && c.status === "自社落札";
    });
    var byCo = {};
    var body = rows.map(function (c) {
      var cost = partnerCost(c), own = costTotal(c), win = c.win_amount || 0;
      var profit = win - cost - own;
      var rate = win ? Math.round(profit / win * 100) : 0;
      if (c.partner) { byCo[c.partner] = byCo[c.partner] || { order: 0, profit: 0, cnt: 0 }; byCo[c.partner].order += cost; byCo[c.partner].profit += profit; byCo[c.partner].cnt++; }
      return "<tr><td>" + esc(c.title) + "</td><td>" + esc(c.partner || "—") + "</td>" +
        '<td class="num">' + (cost ? fmtMan(cost) : "—") + "</td>" +
        '<td class="num">' + (own ? fmtMan(own) : "—") + "</td>" +
        '<td class="num">' + (win ? fmtMan(win) : "—") + "</td>" +
        '<td class="num" style="color:' + (profit < 0 ? "#dc2626" : "#16a34a") + '">' + fmtMan(profit) + "</td>" +
        '<td class="num">' + rate + "%</td></tr>";
    }).join("");
    var totWin = rows.reduce(function (s, c) { return s + (c.win_amount || 0); }, 0);
    var totCost = rows.reduce(function (s, c) { return s + partnerCost(c); }, 0);
    var totOwn = rows.reduce(function (s, c) { return s + costTotal(c); }, 0);
    var totProfit = totWin - totCost - totOwn;
    var coRows = Object.keys(byCo).map(function (k) {
      var v = byCo[k];
      return "<tr><td>" + esc(k) + "</td><td class=num>" + v.cnt + "</td><td class=num>" + fmtMan(v.order) + "</td><td class=num>" + fmtMan(v.profit) + "</td></tr>";
    }).join("");
    return '<div class="sumrow">' + sumCard(fmtMan(totWin), "落札 合計") + sumCard(fmtMan(totCost), "発注 合計") +
        sumCard(fmtMan(totOwn), "自社原価 合計") +
        sumCard(fmtMan(totProfit), "利益 合計", totProfit < 0 ? "danger" : "") + "</div>" +
      "<h3>案件ごとの内訳（自社落札のみ）</h3>" +
      '<table class="list"><thead><tr><th>案件名</th><th>発注先</th><th>発注</th><th>自社原価</th><th>落札</th><th>利益</th><th>利益率</th></tr></thead><tbody>' +
        (body || '<tr><td colspan="7" class="dim">自社落札の案件がまだありません（状況を「自社落札」にして落札額を入力）。</td></tr>') + "</tbody></table>" +
      (coRows ? '<h3>会社ごとの発注・利益</h3><table class="list"><thead><tr><th>協力会社</th><th>件数</th><th>発注</th><th>利益</th></tr></thead><tbody>' + coRows + "</tbody></table>" : "");
  }

  /* ============================================================
     タブ4: 月次レポート
  ============================================================ */
  // NG理由のAI集計（打合せ合意: メモはフリー記述のまま、AIが固定カテゴリに分類・集計）
  var ngSum = null, ngBusy = false;
  function ngPanelHtml() {
    var ngCases = CASES.filter(function (c) { return (c.sector || "公共") === state.sheet && c.status === "NG"; });
    var h = '<div class="rep rep-ng"><div class="rep-h"><b>NG理由の集計（AI）</b><span>' + ngCases.length + "件のNG案件</span></div>";
    if (!ngCases.length) {
      return h + '<p class="dim">NG（不参加）にした案件がまだありません。</p></div>';
    }
    if (ngBusy) {
      return h + '<p class="dim">AIがNG理由のメモを読んで集計中…（10〜30秒ほどお待ちください）</p></div>';
    }
    if (ngSum && ngSum.sheet === state.sheet) {
      if (ngSum.enabled === false) {
        h += '<p class="dim">' + esc(ngSum.reason || "AI集計はAIプラン（GEMINI_API_KEY設定）で使えます。") + "</p>";
      } else if (ngSum.error) {
        h += '<p class="dim">集計に失敗しました。時間をおいて再度お試しください。</p>' +
          '<div class="rep-save"><button type="button" class="btn primary small" id="ngSumBtn">もう一度集計</button></div>';
      } else {
        var cats = (ngSum.categories || []).filter(function (x) { return (x.count || 0) > 0; });
        cats.sort(function (a, b) { return (b.count || 0) - (a.count || 0); });
        var maxC = cats.reduce(function (m, x) { return Math.max(m, x.count || 0); }, 1);
        h += cats.map(function (x) {
          var w = Math.max(4, Math.round(((x.count || 0) / maxC) * 100));
          // 要望⑰: 何の資格が不足か・どの地域が何件かの内訳を数字で出す
          var bd = (x.breakdown || []).filter(function (b) { return b && b.item && (b.count || 0) > 0; });
          var bdHtml = bd.length
            ? '<div class="ngr-bd">内訳: ' + bd.map(function (b) {
                return "<b>" + esc(b.item) + "</b> " + b.count + "件";
              }).join(" ／ ") + "</div>"
            : "";
          return '<div class="ngr"><div class="ngr-h"><b>' + esc(x.reason || "") + "</b><span>" + (x.count || 0) + "件</span></div>" +
            '<div class="ngr-bar"><i style="width:' + w + '%"></i></div>' +
            bdHtml +
            ((x.examples || []).length ? '<div class="ngr-ex">例: ' + x.examples.slice(0, 2).map(esc).join(" ／ ") + "</div>" : "") +
            "</div>";
        }).join("");
        if (ngSum.insight) h += '<p class="ngr-insight">💡 ' + esc(ngSum.insight) + "</p>";
        h += '<div class="rep-save"><button type="button" class="btn ghost small" id="ngSumBtn" data-refresh="1">再集計</button></div>';
      }
      return h + "</div>";
    }
    h += '<p class="dim">NG案件の理由メモをAIが読み、「実績不足」「地域要件」などの固定カテゴリで件数を集計します。' +
      "（例: 実績なしだけで年間100件逃しているなら、実績づくりを計画する材料になります）</p>" +
      '<div class="rep-save"><button type="button" class="btn primary small" id="ngSumBtn">AIでNG理由を集計</button></div>';
    return h + "</div>";
  }

  function renderReports() {
    // 選択中シートの案件だけを月別集計。民間は開札日が無いので見積提出期限で束ねる。
    var months = {};
    CASES.forEach(function (c) {
      if ((c.sector || "公共") !== state.sheet) return;
      var d = state.sheet === "民間" ? (c.open_date || c.apply_deadline || c.deadline) : c.open_date;
      var m = (d || "").slice(0, 7); if (!m) return;
      (months[m] = months[m] || []).push(c);
    });
    var keys = Object.keys(months).sort();
    var saved = [];
    try { saved = JSON.parse(localStorage.getItem("kawanoReports")) || []; } catch (e) {}
    var blocks = keys.map(function (m) {
      var list = months[m];
      var plan = list.reduce(function (s, c) { return s + (c.bid_plan || 0); }, 0);
      var cost = list.reduce(function (s, c) { return s + partnerCost(c); }, 0);
      return '<div class="rep"><div class="rep-h"><b>' + m + "</b><span>" + list.length + "件・想定利益 " + fmtMan(plan - cost) + "</span></div>" +
        '<ul class="rep-list">' + list.map(function (c) { return "<li>" + esc(c.title) + ' <span class="dim">' + esc(c.status) + "</span></li>"; }).join("") + "</ul>" +
        '<div class="rep-save"><input class="rep-memo" placeholder="この月のメモ（例：照明改修は安定。次月は空調強化…）"><button class="btn ghost small rep-btn" data-m="' + m + '" data-plan="' + (plan - cost) + '" data-cnt="' + list.length + '">この月を保存</button></div></div>';
    }).join("");
    var savedHtml = saved.length ? '<h3>保存したレポート</h3>' + saved.map(function (r) {
      return '<div class="rep-saved"><b>' + esc(r.month) + "</b>（" + r.cnt + "件・想定利益 " + fmtMan(r.plan) + "）<div>" + esc(r.memo || "") + "</div></div>";
    }).join("") : "";
    var emptyRep = state.sheet === "民間"
      ? '<p class="empty">見積提出期限が入った民間案件がまだありません（編集で期限を入力すると月別に集計されます）。</p>'
      : '<p class="empty">開札日が入った案件がまだありません（編集で開札日を入力すると月別に集計されます）。</p>';
    return ngPanelHtml() + (blocks || emptyRep) + savedHtml;
  }

  /* ============================================================
     レンダリング統合 + イベント
  ============================================================ */
  function render() {
    saveState();   // 絞り込み・並び替え・タブの選択を保持（reload/遷移で初期化しない）
    renderTabs();
    var panel = $("tabPanel"), html;
    if (state.tab === "会社") html = renderCompanies();
    else if (state.tab === "もうけ") html = renderProfit();
    else if (state.tab === "レポート") html = renderReports();
    else html = renderKanban(state.sheet);
    panel.innerHTML = html;
    bind();
  }

  function bind() {
    // 担当チップ
    Array.prototype.forEach.call(document.querySelectorAll(".chip2"), function (b) {
      b.addEventListener("click", function () {
        if (b.hasAttribute("data-addas")) {            // ＋担当者を追加
          var added = addAssigneePrompt();
          if (added) state.assignee = added;            // 追加した担当で絞り込む
          render(); return;
        }
        if (b.hasAttribute("data-st")) {
          var v = b.getAttribute("data-st");
          if (!v) state.statuses = [];                       // 「すべて」で解除
          else {
            var i = state.statuses.indexOf(v);
            if (i >= 0) state.statuses.splice(i, 1);          // 選択中なら外す
            else state.statuses.push(v);                       // 未選択なら追加
          }
        } else {
          state.assignee = b.getAttribute("data-as") || null;
        }
        render();
      });
    });
    // カードクリック→編集。開けない時は黙って無反応にせず、原因を画面に出す。
    function safeOpenCase(c) {
      if (!c) return;
      try { openCaseModal(c); }
      catch (e) {
        alert("案件を開く途中でエラーが発生しました。\n" +
          (e && e.message ? e.message : String(e)) +
          "\n\nお手数ですが、この表示のスクリーンショットを松本までお送りください。");
      }
    }
    Array.prototype.forEach.call(document.querySelectorAll(".kcard"), function (el) {
      el.addEventListener("click", function () {
        var c = CASES.filter(function (x) { return String(x.case_id) === el.getAttribute("data-id"); })[0];
        safeOpenCase(c);
      });
    });
    // カードのワンクリック「NGへ」（クリックはkc-actsでstopPropagation済み）
    Array.prototype.forEach.call(document.querySelectorAll(".kc-ng"), function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var c = CASES.filter(function (x) { return String(x.case_id) === btn.getAttribute("data-ng"); })[0];
        if (c && confirm("「" + c.title + "」をNG（不参加）に移動しますか？")) {
          var prev = c.status;
          c.status = "NG";
          saveCase(c, function (ok) { if (!ok) { c.status = prev; render(); } });
        }
      });
    });
    // 直近の締切アラート→クリックで該当案件を開く（要望⑩）
    Array.prototype.forEach.call(document.querySelectorAll(".wb-item"), function (b) {
      b.addEventListener("click", function () {
        var c = CASES.filter(function (x) { return String(x.case_id) === b.getAttribute("data-case"); })[0];
        safeOpenCase(c);
      });
    });
    // 案件を追加（開いている管理シートの区分を初期値にする）
    var ac = $("addCaseBtn"); if (ac) ac.addEventListener("click", function () { openNewCaseModal(ac.getAttribute("data-sec") || "公共"); });
    // 協力会社
    // 会社検索: 日本語入力(IME)の変換中に再描画すると1文字目がアルファベットのまま
    // 確定されてしまうため、変換中は再描画せず、確定(compositionend)でまとめて反映する。
    var s = $("coSearch");
    if (s) {
      var syncCo = function () { state.q = s.value; var p = s.selectionStart; render(); var n = $("coSearch"); if (n) { n.focus(); try { n.setSelectionRange(p, p); } catch (e) {} } };
      s.addEventListener("input", function (e) { if (e.isComposing) return; syncCo(); });
      s.addEventListener("compositionend", syncCo);
    }
    var pf = document.querySelector(".co-pf"); if (pf) pf.addEventListener("click", function () { state.coPartner = !state.coPartner; render(); });
    var sf = document.querySelector(".co-sf"); if (sf) sf.addEventListener("click", function () { state.coSort = !state.coSort; render(); });
    Array.prototype.forEach.call(document.querySelectorAll(".co-tagf"), function (b) {
      b.addEventListener("click", function () { var t = b.getAttribute("data-t"); state.coTag = state.coTag === t ? null : t; render(); });
    });
    var add = $("coAdd"); if (add) add.addEventListener("click", function () { openCompanyModal(null); });
    Array.prototype.forEach.call(document.querySelectorAll(".cocard"), function (el) {
      var id = el.getAttribute("data-id");
      el.querySelector(".co-edit").addEventListener("click", function () { openCompanyModal(COMPANIES.filter(function (c) { return String(c.id) === id; })[0]); });
      el.querySelector(".co-del").addEventListener("click", function () {
        if (!confirm("この協力会社を削除しますか？")) return;
        fetch("/companies/" + id + "/delete", { method: "POST" }).then(function (r) { return r.json(); })
          .then(function (d) { COMPANIES = d.companies || []; mirrorCompanies(); render(); });
      });
    });
    // NG理由のAI集計（月次レポート）
    var ngb = $("ngSumBtn");
    if (ngb) ngb.addEventListener("click", function () {
      var refresh = ngb.getAttribute("data-refresh") === "1";
      ngBusy = true; render();
      fetch("/reports/ng-reasons?sheet=" + encodeURIComponent(state.sheet) + (refresh ? "&refresh=1" : ""), { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (d) { d = d || {}; d.sheet = state.sheet; ngSum = d; ngBusy = false; render(); })
        .catch(function () { ngSum = { error: true, sheet: state.sheet }; ngBusy = false; render(); });
    });
    // 月次レポート保存
    Array.prototype.forEach.call(document.querySelectorAll(".rep-btn"), function (b) {
      b.addEventListener("click", function () {
        var memo = b.parentNode.querySelector(".rep-memo").value;
        var saved = []; try { saved = JSON.parse(localStorage.getItem("kawanoReports")) || []; } catch (e) {}
        saved = saved.filter(function (r) { return r.month !== b.getAttribute("data-m"); });
        saved.push({ month: b.getAttribute("data-m"), plan: Number(b.getAttribute("data-plan")), cnt: Number(b.getAttribute("data-cnt")), memo: memo });
        try { localStorage.setItem("kawanoReports", JSON.stringify(saved)); } catch (e) {}
        render();
      });
    });
  }

  /* ---------- 見積の状態/最安（bid-next の qState/cheapest 準拠） ---------- */
  function qState(q) {
    if (!q.requested) return { label: "未依頼", color: "#94a3b8" };
    if (!q.replied) return { label: "返事待ち", color: "#d97706" };
    if (q.feasible === -1) return { label: "不可（断り）", color: "#dc2626" };
    if (q.feasible === 1 && !toYen(q.amount)) return { label: "可・金額待ち", color: "#0891b2" };
    if (q.feasible === 1 && toYen(q.amount) > 0) return { label: "回答あり", color: "#16a34a" };
    return { label: "返信あり", color: "#2563eb" };
  }
  function cheapestQ(qs) {
    var ans = qs.filter(function (q) { return q.feasible === 1 && toYen(q.amount) > 0; });
    if (!ans.length) return null;
    return ans.reduce(function (m, q) { return toYen(q.amount) < toYen(m.amount) ? q : m; });
  }

  /* ---------- 案件 編集モーダル（情報/見積の2タブ・bid-next 準拠） ---------- */
  function openCaseModal(c0) {
    var c = JSON.parse(JSON.stringify(c0));
    c.partners = c.partners || [];
    if (!c.win_amount) c.win_amount = toYen(c0.win_price) || 0;
    var mtab = "情報";
    var chOpen = false, chQ = "", chPartner = false;
    var aiResult = null, aiBusy = false;   // AI応募可否判定の結果
    var summaryResult = null, summaryBusy = false;   // AI案件概要（要望⑦STEP2）
    var opt = function (arr, sel) { return arr.map(function (v) { return '<option value="' + esc(v) + '"' + (v === sel ? " selected" : "") + ">" + esc(v) + "</option>"; }).join(""); };

    // 要望⑦STEP2: AI案件概要パネル（公告を読み、工事内容・規模・要求資格を要約）
    function summaryPanel() {
      if (summaryBusy) return '<div class="m-ai busy"><div class="ai-loading">' +
        '<div class="ai-loading-msg">AIが公告を読み込み、案件概要を作成中<span class="ai-dots"><i>.</i><i>.</i><i>.</i></span></div>' +
        '<div class="ai-bar"></div><div class="ai-loading-sub">10〜30秒ほどかかります。</div></div></div>';
      if (!summaryResult) return '<div class="m-ai"><button type="button" class="btn small" id="sumBtn">AIで案件概要を作成</button>' +
        '<small>公告を読み、工事内容・規模・要求資格などを要約します（協力会社の選定にも活用）</small></div>';
      if (summaryResult.enabled === false) return '<div class="m-ai dim">AI概要はAIプラン（GEMINI_API_KEYとアカウント許可）で使えます。</div>';
      if (summaryResult.error) return '<div class="m-ai dim">' + esc(summaryResult.error) + ' <button type="button" class="btn ghost small" id="sumRedo">再作成</button></div>';
      var ov = (summaryResult.overview || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("");
      var kd = (summaryResult.key_dates || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("");
      var sc = (summaryResult.suited_categories || []).map(function (x) { return '<span class="sum-cat">' + esc(x) + "</span>"; }).join("");
      return '<div class="m-ai res sum"><div class="m-ai-top"><b>AI案件概要</b><button type="button" class="btn ghost small" id="sumRedo">再作成</button></div>' +
        (summaryResult.scope ? '<p class="sum-scope">' + esc(summaryResult.scope) + "</p>" : "") +
        (ov ? '<ul class="m-ai-rs">' + ov + "</ul>" : "") +
        (kd ? '<div class="sum-sub">重要日程</div><ul class="m-ai-rs">' + kd + "</ul>" : "") +
        (sc ? '<div class="sum-sub">この工事に適した工事カテゴリ（見積タブの依頼先選びに反映）</div><div class="sum-cats">' + sc + "</div>" : "") +
        "</div>";
    }

    // AI応募可否判定パネル（公告を読み、自社の等級・資格と照合してOK/NGを出す）
    function aiPanel() {
      if (aiBusy) return '<div class="m-ai busy"><div class="ai-loading">'
        + '<div class="ai-loading-msg">AIが公告を読み込み、応募可否（等級など）を判定中'
        + '<span class="ai-dots"><i>.</i><i>.</i><i>.</i></span></div>'
        + '<div class="ai-bar"></div>'
        + '<div class="ai-loading-sub">10〜30秒ほどかかります。そのままお待ちください。</div></div></div>';
      if (!aiResult) return '<div class="m-ai"><button type="button" class="btn primary small" id="aiJudge">AIで応募可否を判定</button>' +
        '<small>公告を読み、自社の保有資格・等級と照合して 〇/△/✕ を出します</small></div>';
      if (aiResult.enabled === false) return '<div class="m-ai dim">AI判定はAIプラン（管理者がGEMINI_API_KEYとアカウント許可を設定）で使えます。</div>';
      if (aiResult.error) return '<div class="m-ai dim">AI判定に失敗しました。' + '<button type="button" class="btn ghost small" id="aiRedo">再判定</button></div>';
      var e = aiResult.eligibility || {}, v = e.verdict || "不明";
      var cls = ({ "〇": "ok", "△": "warn", "✕": "ng" })[v] || "unk";
      var reasons = (e.reasons || []).map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("");
      return '<div class="m-ai res ' + cls + '"><div class="m-ai-top"><b>AI応募可否</b> <span class="ai-v ' + cls + '">' + esc(v) + "</span>" +
        '<button type="button" class="btn ghost small" id="aiRedo">再判定</button></div>' +
        '<ul class="m-ai-rs">' + reasons + "</ul>" +
        (v === "〇" ? '<span class="dim">応募できそうです</span>'
          : '<button type="button" class="btn small ngbtn" id="aiNG">理由を添えてNGに入れる</button>') + "</div>";
    }

    // 要望⑦STEP1: 仕様書の紐付け（URL/添付ファイル）。AI概要の入力にも使う。
    function specSection() {
      var files = c.spec_files || [];
      var items = files.length ? files.map(function (f, i) {
        var icon = f.kind === "url" ? "🔗" : "📄";
        return '<div class="spec-item"><a href="/case/' + c.case_id + "/spec/" + i + '/download" target="_blank" rel="noopener">' +
          icon + " " + esc(f.name || "仕様書") + "</a>" +
          '<button type="button" class="spec-del" data-i="' + i + '" title="外す">×</button></div>';
      }).join("") : '<p class="spec-empty">まだ仕様書は紐付いていません。URLの追加かファイル添付ができます。</p>';
      return '<div class="spec-box"><div class="spec-head">仕様書の紐付け <small>（AI概要の材料にも使われます）</small></div>' +
        '<div class="spec-list">' + items + "</div>" +
        '<div class="spec-acts"><button type="button" class="btn small" id="specUrl">＋ URLを追加</button>' +
        '<button type="button" class="btn small" id="specFileBtn">＋ ファイルを添付</button>' +
        '<input type="file" id="specFile" style="display:none"></div>' +
        '<span class="spec-msg" id="specMsg"></span></div>';
    }

    function infoHtml() {
      // 民間シートは入札フローを持たない（見積〜提出〜結果）。
      var priv = (c.sector || "公共") === "民間";
      var steps = priv ? [
        { label: "公開開始", date: c.announced_date },
        { label: "見積提出期限", date: c.apply_deadline || c.deadline }
      ] : [
        { label: "公開開始", date: c.announced_date },
        { label: "参加申請期限", date: c.apply_deadline || c.deadline },
        { label: "入札書提出期限", date: c.bid_deadline },
        { label: "開札日", date: c.open_date }
      ];
      var tl = steps.map(function (s) {
        var d = dUntil(s.date), b = band(d);
        return '<div class="tl-row"><span class="tl-dot" style="background:' + (d == null ? "#cbd5e1" : b.fg) + '"></span>' +
          '<span class="tl-label">' + s.label + '</span><span class="tl-date">' + (s.date ? md(s.date) : "—") + '</span>' +
          '<span class="tl-days" style="color:' + (d == null ? "#a8a29e" : b.fg) + '">' + (d == null ? "" : daysLabel(d)) + "</span></div>";
      }).join("");
      // 手動追加の案件は案件名を後から編集できる（川野さん要望 2026-07。
      // スクレイプ案件は毎日の更新で上書きされるため対象外）。
      var titleFld = c.source === "manual"
        ? fld("案件名", '<input name="title" value="' + esc(c.title || "") + '" placeholder="案件名（修正可）">', "full")
        : "";
      return summaryPanel() + specSection() + '<div class="m-grid">' +
        titleFld +
        fld("元機関（発注機関）", '<input name="agency_override" value="' + esc(c.agency || "") + '" placeholder="発注機関名（修正可）">', "full") +
        fld("状況", '<select name="status">' + opt(statusesFor(c.sector).map(function (s) { return s.id; }), c.status) + "</select>") +
        fld("担当者", '<select name="assignee" class="assignee-sel" data-prev="' + esc(c.assignee || "未割当") + '">' + assigneeOptions(c.assignee || "未割当") + "</select>") +
        fld("工事カテゴリ", '<select name="work"><option value="">（案件の業種）</option>' + opt(Object.keys(WORK), c.work || c.work_eff) + "</select>") +
        (priv ? "" : fld("入札タイプ", '<select name="submit_method"><option value="">—</option>' + opt(METHODS, c.submit_method) + "</select>")) +
        fld(priv ? "見積提出 期限" : "参加申請 期限", '<input type="date" name="apply_deadline" value="' + esc(c.apply_deadline) + '">') +
        (priv ? "" : fld("入札書提出 期限", '<input type="date" name="bid_deadline" value="' + esc(c.bid_deadline) + '">')) +
        (priv ? "" : fld("開札日", '<input type="date" name="open_date" value="' + esc(c.open_date) + '">')) +
        fld("資料受取", '<input name="materials" value="' + esc(c.materials) + '" placeholder="6/1以降受取 等">') +
        fld("要確認の一言", '<input name="flag" value="' + esc(c.flag) + '" placeholder="資料待ち 等">') +
        fld("メモ", '<textarea name="note" rows="3" class="note-area">' + esc(c.note) + '</textarea>', "full") +
      "</div><div class=\"tl\"><div class=\"tl-h\">工程タイムライン</div>" + tl + "</div>";
    }

    function quoteHtml() {
      var sel = c.partners.filter(function (q) { return q.selected; })[0];
      var ch = cheapestQ(c.partners);
      // 自社原価の内訳（項目＋金額を行で追加→自動合計。メモに埋もれないデータとして残す）
      var costs = c.cost_items || [];
      var costTot = costs.reduce(function (s, it) { return s + (toYen(it.amount) || 0); }, 0);
      var gross = (toYen(c.bid_plan) && costTot) ? toYen(c.bid_plan) - costTot : null;
      // 想定利益＝落札額−(自社原価＋依頼先への工事費)（要望⑮のご指定式）。
      // 依頼先への工事費 = ★採用した依頼先の見積額。落札額が入るまでは「—」。
      var fee = (sel && toYen(sel.amount)) ? toYen(sel.amount) : 0;
      var exp = toYen(c.win_amount) ? toYen(c.win_amount) - costTot - fee : null;
      // 落札額は「自社・他社を問わず案件の最終落札金額」。落札会社名も残してデータ取りに使う。
      var money = '<div class="mq-money">' +
        '<label class="mb"><span>入札予定額</span><input name="bid_plan" value="' + (c.bid_plan || "") + '" placeholder="3000000"></label>' +
        '<label class="mb"><span>落札額(自社・他社問わず)</span><input name="win_amount" value="' + (c.win_amount || "") + '" placeholder="最終落札金額"></label>' +
        '<label class="mb"><span>落札会社名</span><input name="win_company" value="' + esc(c.win_company || "") + '" placeholder="自社 / ○○電気 等"></label>' +
        '<div class="mb exp ' + (exp == null ? "" : exp < 0 ? "neg" : "pos") + '"><span>想定利益</span><b>' + (exp == null ? "—" : fmtMan(exp)) + "</b><small>落札額−(自社原価+依頼先)</small></div></div>";
      var costRows = costs.map(function (it, i) {
        return '<div class="mq-row"><div class="mq-r1">' +
          '<input class="mq-co q-clabel" data-i="' + i + '" value="' + esc(it.label || "") + '" placeholder="例: 照明器具 材料費 / LED 60台">' +
          '<span class="mq-amt">¥<input class="q-camt" data-i="' + i + '" value="' + (it.amount || "") + '" placeholder="金額"></span>' +
          '<button type="button" class="mq-del q-cdel" data-i="' + i + '">×</button></div></div>';
      }).join("");
      var costBox = '<div class="mq-rowshead">自社原価の内訳（機器代・材料費など）' +
        '<button type="button" class="btn ghost small" id="costAdd">＋内訳を追加</button></div>' +
        '<div class="mq-rows">' + (costRows || '<p class="dim">＋内訳を追加 で項目と金額を登録すると自動で合計します。</p>') + "</div>" +
        (costs.length ? '<div class="mq-money"><div class="mb"><span>原価合計</span><b>' + fmtMan(costTot) + "</b></div>" +
          '<div class="mb exp ' + (gross == null ? "" : gross < 0 ? "neg" : "pos") + '"><span>粗利(入札−原価)</span><b>' + (gross == null ? "—" : fmtMan(gross)) + "</b><small>入札予定額−原価合計</small></div></div>" : "");
      var asked = {}; c.partners.forEach(function (q) { asked[q.company] = 1; });
      var work = c.work || c.work_eff;
      // 要望⑦STEP3: 案件の工事カテゴリ・エリア・AI概要の推奨カテゴリと突き合わせて
      // 「この工事に合う会社」を上位に並べる（選定支援）。
      var suited = (summaryResult && summaryResult.suited_categories) || [];
      function coScore(co) {
        var s = (co.rating || 0) * 0.5;
        if (co.partner) s += 2;
        var tags = co.tags || [];
        if (work && tags.indexOf(work) >= 0) s += 3;               // 案件の業種に一致
        suited.forEach(function (cat) { if (tags.indexOf(cat) >= 0) s += 2; });  // AI推奨カテゴリに一致
        var ar = parseArea(co.area);                                // エリア一致（地方＋詳細）
        if (c.region && ar.regions.indexOf(c.region) >= 0) s += 3;
        if (c.prefecture && co.area && co.area.indexOf(c.prefecture) >= 0) s += 3;
        return s;
      }
      // 協力業者リストは公共/民間で分割管理。この案件の区分に属する会社だけ候補に出す
      // （役務案件は公共の会社リストを共用）。
      var caseSec = coSectorOf(c.sector || "公共");
      var POOL = COMPANIES.filter(function (co) { return (co.sector || "公共") === caseSec; });
      var clist = chQ ? POOL.filter(function (co) { return ([co.name, co.area].concat(co.tags || []).join(" ")).indexOf(chQ) >= 0; })
        : POOL.filter(function (co) { return (co.tags || []).indexOf(work) >= 0 || co.partner || (suited.length && (co.tags || []).some(function (t) { return suited.indexOf(t) >= 0; })); });
      if (chPartner) clist = clist.filter(function (co) { return co.partner; });
      clist = clist.slice().sort(function (a, b) { return coScore(b) - coScore(a); });
      var chooser = '<button type="button" class="mq-chtoggle" id="chToggle">＋ ① 依頼先を選ぶ（おすすめ＝この工事に合う会社）</button>';
      if (chOpen) {
        chooser += '<div class="mq-chooser"><input id="chSearch" class="mq-chsearch" placeholder="会社をさがす（空欄＝おすすめ）" value="' + esc(chQ) + '">' +
          '<label class="mq-chpartner"><input type="checkbox" id="chPartner"' + (chPartner ? " checked" : "") + ">★取引先のみ</label>" +
          '<div class="mq-chlist">' + (clist.map(function (co) {
            return '<button type="button" class="mq-chitem' + (asked[co.name] ? " on" : "") + '" data-co="' + esc(co.name) + '"><span class="ck">' + (asked[co.name] ? "選択中" : "") + "</span>" +
              "<span>" + (co.partner ? "[常] " : "") + esc(co.name) + " <small>" + esc(co.area || "") + "</small></span></button>";
          }).join("") || '<p class="dim">該当なし</p>') + "</div></div>";
      }
      var rows = c.partners.length ? c.partners.map(function (q, i) {
        var st = qState(q), isMin = ch && ch === q;
        var co = COMPANIES.filter(function (x) { return x.name === q.company; })[0];
        var tel = q.tel || (co && co.tel) || "";
        // 協力会社マスタに無い会社（手入力）は会社名を直接入力できるようにする（要望⑥）
        var editable = q.manual || !co;
        var nameCell = editable
          ? '<input class="q-co mq-co" data-i="' + i + '" value="' + esc(q.company) + '" placeholder="会社名を入力">'
          : '<span class="mq-co">' + esc(q.company) + (isMin ? ' <span class="mq-min">最安</span>' : "") + "</span>";
        return '<div class="mq-row' + (q.selected ? " sel" : "") + '" data-i="' + i + '"><div class="mq-r1">' +
          '<button type="button" class="mq-star' + (q.selected ? " on" : "") + '" data-act="select"' + (q.feasible === 1 ? "" : " disabled") + ">★</button>" +
          nameCell +
          '<span class="mq-stt" style="background:' + st.color + "1a;color:" + st.color + '">' + st.label + "</span>" +
          (tel ? '<a class="mq-tel" href="tel:' + esc(tel) + '">電話</a>' : "") +
          '<button type="button" class="mq-del" data-act="del">×</button></div><div class="mq-r2">' +
          '<button type="button" class="stp' + (q.requested ? " on" : "") + '" data-act="req">①依頼</button>' +
          '<button type="button" class="stp' + (q.replied ? " on" : "") + '" data-act="rep"' + (q.requested ? "" : " disabled") + ">②返信</button>" +
          (q.replied ? '<span class="yn"><button type="button" class="ynb' + (q.feasible === 1 ? " yes" : "") + '" data-act="yes">可</button><button type="button" class="ynb' + (q.feasible === -1 ? " no" : "") + '" data-act="no">否</button></span>' : "") +
          (q.feasible === 1 ? '<span class="mq-amt">¥<input class="q-amt" data-i="' + i + '" value="' + esc(q.amount) + '" placeholder="金額"></span>' : "") +
          // 入札予定額とこの会社の見積をリンクして差額を行内に出す（会議要望）。
          // ※「想定利益」は⑮で落札額基準に変わったため、行内は「入札との差額」と表記。
          (q.feasible === 1 && toYen(q.amount) && toYen(c.bid_plan)
            ? '<span class="dim">入札との差額 ' + fmtMan(toYen(c.bid_plan) - toYen(q.amount)) + "</span>" : "") +
          // 手入力の新規業者は、その場で協力会社リストへ登録できる（会議要望）
          (editable && (q.company || "").trim()
            ? '<button type="button" class="stp q-reg" data-i="' + i + '" title="協力会社リストへ登録">会社登録→</button>' : "") +
          "</div></div>";
      }).join("") : '<p class="dim">①から依頼先を選ぶか、＋追加で会社を足してください。</p>';
      var call = "";
      if (sel) {
        var co2 = COMPANIES.filter(function (x) { return x.name === sel.company; })[0];
        var tel2 = sel.tel || (co2 && co2.tel) || "";
        call = '<div class="mq-call"><b>落札の連絡</b><p>落札したら <b>' + esc(sel.company) + "</b> に連絡</p>" +
          (tel2 ? '<a class="btn primary small" href="tel:' + esc(tel2) + '">' + esc(tel2) + " に電話</a> " : '<span class="dim">電話番号が未登録</span> ') +
          '<button type="button" class="btn ' + (c.award_called ? "primary" : "ghost") + ' small" id="awardBtn">' + (c.award_called ? "連絡済み" : "連絡したら押す") + "</button></div>";
      }
      return money + costBox + chooser +
        '<div class="mq-rowshead">② 依頼先ごとの進捗（' + c.partners.length + "社）" + (ch ? ' <span class="dim">最安 ' + fmtMan(toYen(ch.amount)) + "</span>" : "") +
          '<button type="button" class="btn ghost small" id="mqAdd">＋手入力</button></div><div class="mq-rows">' + rows + "</div>" + call;
    }

    function bodyHtml() {
      var links = '<div class="m-links">' + (c.case_id ? '<a href="/case/' + c.case_id + '" target="_blank" rel="noopener">案件詳細</a>' : "") +
        (c.detail_url ? ' ・ <a href="' + esc(c.detail_url) + '" target="_blank" rel="noopener">公告ページ</a>' : "") + "</div>";
      var tabs = '<div class="m-tabs"><button type="button" class="m-tab' + (mtab === "情報" ? " on" : "") + '" data-mt="情報">案件情報</button>' +
        '<button type="button" class="m-tab' + (mtab === "見積" ? " on" : "") + '" data-mt="見積">見積もり・発注</button></div>';
      return aiPanel() + links + tabs + (mtab === "情報" ? infoHtml() : quoteHtml());
    }
    function pullInfo(root) {
      // title は手動案件のときだけ入力欄が出る（無ければ querySelector が null で素通り）
      ["status", "assignee", "work", "submit_method", "apply_deadline", "bid_deadline", "open_date", "materials", "flag", "note", "agency_override", "title"].forEach(function (n) {
        var e = root.querySelector('[name="' + n + '"]'); if (e) c[n] = e.value;
      });
    }
    function pullMoney(root) {
      var bp = root.querySelector('[name="bid_plan"]'); if (bp) c.bid_plan = toYen(bp.value);
      var wa = root.querySelector('[name="win_amount"]'); if (wa) c.win_amount = toYen(wa.value);
      var wc = root.querySelector('[name="win_company"]'); if (wc) c.win_company = wc.value.trim();
    }
    function redraw(root) { root.querySelector(".modal-b").innerHTML = bodyHtml(); bindModal(root); }
    function bindModal(root) {
      Array.prototype.forEach.call(root.querySelectorAll(".m-tab"), function (b) {
        b.addEventListener("click", function () {
          if (mtab === "情報") pullInfo(root); else pullMoney(root);
          mtab = b.getAttribute("data-mt"); redraw(root);
        });
      });
      if (mtab === "見積") bindQuote(root);
      if (mtab === "情報") {
        var sb = root.querySelector("#sumBtn"); if (sb) sb.onclick = function () { pullInfo(root); runSummary(root, false); };
        var sr = root.querySelector("#sumRedo"); if (sr) sr.onclick = function () { runSummary(root, true); };
        bindSpec(root);
      }
      bindAi(root);
      root.querySelector(".m-save").onclick = function () {
        var btn = this;
        if (btn.disabled) return;  // 二度押し防止
        if (mtab === "情報") pullInfo(root); else pullMoney(root);
        btn.disabled = true;
        var orig = btn.textContent;
        btn.textContent = "保存中…";
        saveCase(c, function (ok) {
          btn.disabled = false;
          btn.textContent = orig;
          if (ok) closeModal();  // 失敗時は入力を保持したまま開いておく（再試行できる）
        });
      };
    }
    // 要望⑦STEP1: 仕様書の追加/削除/添付を束ねる。
    function bindSpec(root) {
      var msg = root.querySelector("#specMsg");
      function apply(d) {
        if (d && d.spec_files) { c.spec_files = d.spec_files; redraw(root); }
        else if (d && d.error && msg) { msg.className = "spec-msg err"; msg.textContent = d.error; }
      }
      Array.prototype.forEach.call(root.querySelectorAll(".spec-del"), function (b) {
        b.onclick = function () {
          if (!confirm("この仕様書の紐付けを外しますか？")) return;
          fetch("/case/" + c.case_id + "/spec/" + b.getAttribute("data-i") + "/delete", { method: "POST" })
            .then(function (r) { return r.json(); }).then(apply);
        };
      });
      var us = root.querySelector("#specUrl");
      if (us) us.onclick = function () {
        var url = (window.prompt("仕様書のURLを入力してください（http(s)://…）") || "").trim();
        if (!url) return;
        var name = (window.prompt("表示名（任意）", url) || url).trim();
        if (msg) { msg.className = "spec-msg"; msg.textContent = "追加中…"; }
        fetch("/case/" + c.case_id + "/spec", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: url, name: name }) })
          .then(function (r) { return r.json(); }).then(apply)
          .catch(function () { if (msg) { msg.className = "spec-msg err"; msg.textContent = "追加に失敗しました。"; } });
      };
      var fb = root.querySelector("#specFileBtn"), fi = root.querySelector("#specFile");
      if (fb && fi) {
        fb.onclick = function () { fi.click(); };
        fi.onchange = function () {
          if (!fi.files || !fi.files[0]) return;
          var fd = new FormData(); fd.append("file", fi.files[0]);
          if (msg) { msg.className = "spec-msg"; msg.textContent = "アップロード中…"; }
          fetch("/case/" + c.case_id + "/spec", { method: "POST", body: fd })
            .then(function (r) { return r.json(); }).then(apply)
            .catch(function () { if (msg) { msg.className = "spec-msg err"; msg.textContent = "アップロードに失敗しました。"; } });
        };
      }
    }
    function runSummary(root, refresh) {
      summaryBusy = true; redraw(root);
      var url = "/case/" + c.case_id + "/summary" + (refresh ? "?refresh=1" : "");
      fetch(url, { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) { if (r.status === 401 || r.status === 403) return { enabled: false }; return r.json(); })
        .then(function (j) { summaryResult = j || { error: true }; if (j && j.spec_files) c.spec_files = j.spec_files; summaryBusy = false; redraw(root); })
        .catch(function () { summaryResult = { enabled: true, error: "AI概要の生成に失敗しました。" }; summaryBusy = false; redraw(root); });
    }
    function runAi(root, refresh) {
      aiBusy = true; redraw(root);
      var url = "/case/" + c.case_id + "/ai-assist" + (refresh ? "?refresh=1" : "");
      fetch(url, { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) { if (r.status === 401 || r.status === 403) return { enabled: false }; return r.json(); })
        .then(function (j) { aiResult = j || { error: true }; if (j && j.spec_files) c.spec_files = j.spec_files; aiBusy = false; redraw(root); })
        .catch(function () { aiResult = { error: true }; aiBusy = false; redraw(root); });
    }
    function bindAi(root) {
      var j = root.querySelector("#aiJudge"); if (j) j.onclick = function () { if (mtab === "情報") pullInfo(root); else pullMoney(root); runAi(root, false); };
      var rd = root.querySelector("#aiRedo"); if (rd) rd.onclick = function () { runAi(root, true); };
      var ng = root.querySelector("#aiNG"); if (ng) ng.onclick = function () {
        var e = (aiResult && aiResult.eligibility) || {};
        var reasons = (e.reasons || []);
        var first = reasons[0] || "AIが参加資格を満たさないと判定";
        var memo = "【AI判定: 参加不可】" + (reasons.length ? reasons.join(" / ") : first);
        if (mtab === "情報") pullInfo(root); else pullMoney(root);  // フォーム現在値を取り込む
        // 何が足りなかったかを理由メモ(note)に残し、NG列へ振り分ける
        var prev = { status: c.status, flag: c.flag, note: c.note };
        c.status = "NG";
        c.flag = "AI: " + String(first).slice(0, 40);
        c.note = memo + (c.note ? " ／ " + c.note : "");
        saveCase(c, function (ok) {
          if (ok) { closeModal(); return; }
          c.status = prev.status; c.flag = prev.flag; c.note = prev.note;
          render();
        });
      };
    }
    function bindQuote(root) {
      pullMoney(root);
      var ct = root.querySelector("#chToggle"); if (ct) ct.onclick = function () { pullMoney(root); chOpen = !chOpen; redraw(root); };
      // 依頼先を選ぶ内の会社検索も同様にIME変換中の再描画を止める（1文字目バグ対策）
      var cs = root.querySelector("#chSearch");
      if (cs) {
        var syncCh = function () { chQ = cs.value; var p = cs.selectionStart; redraw(root); var n = root.querySelector("#chSearch"); if (n) { n.focus(); try { n.setSelectionRange(p, p); } catch (e) {} } };
        cs.oninput = function (e) { if (e.isComposing) return; syncCh(); };
        cs.addEventListener("compositionend", syncCh);
      }
      var cp = root.querySelector("#chPartner"); if (cp) cp.onclick = function () { chPartner = cp.checked; redraw(root); };
      Array.prototype.forEach.call(root.querySelectorAll(".mq-chitem"), function (b) {
        b.onclick = function () {
          var name = b.getAttribute("data-co");
          if (c.partners.filter(function (q) { return q.company === name; })[0]) c.partners = c.partners.filter(function (q) { return q.company !== name; });
          else { var co = COMPANIES.filter(function (x) { return x.name === name; })[0] || {}; c.partners.push({ company: name, tel: co.tel || "", area: co.area || "", amount: "", requested: 0, replied: 0, feasible: 0, selected: 0 }); }
          redraw(root);
        };
      });
      var add = root.querySelector("#mqAdd"); if (add) add.onclick = function () { c.partners.push({ company: "", manual: 1, amount: "", requested: 0, replied: 0, feasible: 0, selected: 0 }); redraw(root); };
      // 手入力で足した会社は名前を直接編集できる（要望⑥）
      Array.prototype.forEach.call(root.querySelectorAll(".q-co"), function (inp) {
        inp.oninput = function () { c.partners[Number(inp.getAttribute("data-i"))].company = inp.value; };
      });
      var aw = root.querySelector("#awardBtn"); if (aw) aw.onclick = function () { c.award_called = c.award_called ? 0 : 1; redraw(root); };
      Array.prototype.forEach.call(root.querySelectorAll(".q-amt"), function (inp) {
        inp.oninput = function () { c.partners[Number(inp.getAttribute("data-i"))].amount = inp.value; };
        // 金額の入力を確定(blur)したら行内の想定利益を計算し直す
        inp.onchange = function () { pullMoney(root); redraw(root); };
      });
      // 自社原価の内訳行（追加・編集・削除。金額確定で合計と粗利を再計算）
      var ca = root.querySelector("#costAdd");
      if (ca) ca.onclick = function () { pullMoney(root); (c.cost_items = c.cost_items || []).push({ label: "", amount: "" }); redraw(root); };
      Array.prototype.forEach.call(root.querySelectorAll(".q-clabel"), function (inp) {
        inp.oninput = function () { c.cost_items[Number(inp.getAttribute("data-i"))].label = inp.value; };
      });
      Array.prototype.forEach.call(root.querySelectorAll(".q-camt"), function (inp) {
        inp.oninput = function () { c.cost_items[Number(inp.getAttribute("data-i"))].amount = inp.value; };
        inp.onchange = function () { pullMoney(root); redraw(root); };
      });
      Array.prototype.forEach.call(root.querySelectorAll(".q-cdel"), function (b) {
        b.onclick = function () { pullMoney(root); c.cost_items.splice(Number(b.getAttribute("data-i")), 1); redraw(root); };
      });
      // 手入力した新規業者を協力会社リストへその場で登録（この案件の区分・工事カテゴリ付き）
      Array.prototype.forEach.call(root.querySelectorAll(".q-reg"), function (b) {
        b.onclick = function () {
          var q = c.partners[Number(b.getAttribute("data-i"))];
          if (!q || !(q.company || "").trim()) { alert("会社名を入力してください"); return; }
          b.disabled = true; b.textContent = "登録中…";
          var work2 = c.work || c.work_eff;
          fetch("/companies", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: q.company.trim(), tel: q.tel || "", tags: work2 ? [work2] : [], sector: coSectorOf(c.sector || "公共") })
          }).then(function (r) { return r.json(); }).then(function (d) {
            if (d.error) { alert(d.error); b.disabled = false; b.textContent = "会社登録→"; return; }
            COMPANIES = d.companies || COMPANIES; mirrorCompanies();
            pullMoney(root); redraw(root);
          }).catch(function () { b.disabled = false; b.textContent = "会社登録→"; });
        };
      });
      Array.prototype.forEach.call(root.querySelectorAll(".mq-row"), function (rowEl) {
        var i = Number(rowEl.getAttribute("data-i"));
        Array.prototype.forEach.call(rowEl.querySelectorAll("[data-act]"), function (b) {
          b.onclick = function () {
            var q = c.partners[i], act = b.getAttribute("data-act");
            if (act === "del") c.partners.splice(i, 1);
            else if (act === "req") q.requested = q.requested ? 0 : 1;
            else if (act === "rep") q.replied = q.replied ? 0 : 1;
            else if (act === "yes") q.feasible = q.feasible === 1 ? 0 : 1;
            else if (act === "no") { q.feasible = q.feasible === -1 ? 0 : -1; q.selected = 0; }
            else if (act === "select") { var w = !q.selected; c.partners.forEach(function (x, j) { x.selected = (j === i && w) ? 1 : 0; }); if (w) c.partner = q.company; }
            redraw(root);
          };
        });
      });
    }
    showModal(esc(c.title), bodyHtml(), function (root) { bindModal(root); }, {
      danger: {
        label: "申請管理から削除",
        onClick: function () {
          if (!confirm("「" + c.title + "」を申請管理から削除しますか？\n（案件自体は残り、カンバンから外れます）")) return;
          // localStorageのミラーからも消す（自動復元で戻らないように）
          try {
            var KEY = "kawanoApplications", map = JSON.parse(localStorage.getItem(KEY)) || {};
            if (c.external_id && map[c.external_id]) { delete map[c.external_id]; localStorage.setItem(KEY, JSON.stringify(map)); }
          } catch (e) {}
          fetch("/applications/" + c.case_id + "/delete", {
            method: "POST", headers: { "X-Requested-With": "fetch" }
          }).then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            location.reload();
          }).catch(function () {
            alert("削除できませんでした。通信状態を確認して、もう一度お試しください。");
          });
        }
      }
    });
  }
  function saveCase(c, done) {
    // 採用(selected)した見積があり発注先が未入力なら、その会社名を自動セット（擦り合わせ）。
    if (!c.partner) {
      var sel = (c.partners || []).filter(function (p) { return p.selected; })[0];
      if (sel) c.partner = sel.company;
    }
    var mtime = Date.now();
    var MANAGED = "status,assignee,work,submit_method,apply_deadline,bid_deadline,open_date," +
      "materials,partner,flag,note,needs_check,bid_plan,win_amount,win_company,award_called,partners,agency_override,cost_items";
    var fd = new URLSearchParams();
    fd.append("ajax", "1");
    // 手動案件は案件名の変更も一緒に保存する（スクレイプ案件は対象外）
    if (c.source === "manual" && (c.title || "").trim()) {
      MANAGED += ",title";
      fd.append("title", c.title.trim());
    }
    fd.append("managed", MANAGED);
    fd.append("mtime", String(mtime));
    ["status", "assignee", "work", "submit_method", "apply_deadline", "bid_deadline",
      "open_date", "materials", "partner", "flag", "note", "agency_override"].forEach(function (k) { fd.append(k, c[k] || ""); });
    fd.append("bid_plan", c.bid_plan || 0); fd.append("win_amount", c.win_amount || 0);
    fd.append("win_company", c.win_company || "");
    if (c.award_called) fd.append("award_called", "1");
    fd.append("partners", JSON.stringify(c.partners || []));
    fd.append("cost_items", JSON.stringify(c.cost_items || []));
    // 保存の成否を必ず伝える。成功時はリロードせず盤面だけ描き直す（軽い・速い）。
    // 失敗時はミラーも更新しない（未保存の値が「保存済み」として復元されるのを防ぐ）。
    fetch("/case/" + c.case_id + "/apply", {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "fetch" }, body: fd.toString()
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          throw new Error((d && d.error) || ("サーバーエラー (HTTP " + r.status + ")"));
        });
      }
      mirrorCase(c, mtime);
      render();
      toast("保存しました");
      if (done) done(true);
    }).catch(function (e) {
      alert("保存できませんでした。通信状態を確認して、もう一度「保存」を押してください。\n" +
        (e && e.message ? "詳細: " + e.message : ""));
      if (done) done(false);
    });
  }

  /* ---------- 案件を追加（手動・民間/公共）モーダル ---------- */
  function openNewCaseModal(initialSector) {
    var sector = ["公共", "公共役務", "民間"].indexOf(initialSector) >= 0 ? initialSector : "公共";
    var opt = function (arr, sel) { return arr.map(function (v) { return '<option value="' + esc(v) + '"' + (v === sel ? " selected" : "") + ">" + esc(v) + "</option>"; }).join(""); };
    var secPick = SHEETS.map(function (s) {
      return '<button type="button" class="tagpick' + (s.id === sector ? " on" : "") + '" data-s="' + s.id + '" style="--c:' + s.col + '">' + s.label + "</button>";
    }).join("");
    var body = '<div class="m-grid">' +
      fld("案件名 <span style=\"color:#dc2626\">*</span>", '<input name="title" placeholder="例: ○○ビル 電気設備改修工事">', "full") +
      '<div class="m-fld full"><span>区分</span><div class="tagpicks" id="secPick">' + secPick + "</div></div>" +
      fld("発注機関・取引先", '<input name="agency" placeholder="発注者／客先名（任意）">', "full") +
      fld("工事カテゴリ", '<select name="work"><option value="">（未指定）</option>' + opt(Object.keys(WORK), "") + "</select>") +
      fld("担当者", '<select name="assignee" class="assignee-sel" data-prev="未割当">' + assigneeOptions("未割当") + "</select>") +
      // 状況・期限は区分(公共/民間)で内容が変わる。onMount の applySectorUI で切替える。
      '<label class="m-fld"><span>状態</span><select name="status" id="ncStatus"></select></label>' +
      '<label class="m-fld"><span id="ncApplyLbl">参加申請 期限</span><input type="date" name="apply_deadline"></label>' +
      '<label class="m-fld pub-only"><span>入札書提出 期限</span><input type="date" name="bid_deadline"></label>' +
      '<label class="m-fld pub-only"><span>開札日</span><input type="date" name="open_date"></label>' +
      fld("メモ", '<textarea name="note" rows="2" class="note-area"></textarea>', "full") +
      "</div>";
    showModal("案件を追加（民間・公共）", body, function (root) {
      // 区分に応じて状況の選択肢・期限ラベル・公共専用項目の表示を切り替える。
      function applySectorUI(sec) {
        var priv = sec === "民間";
        var sel = root.querySelector("#ncStatus");
        if (sel) sel.innerHTML = opt((priv ? STATUSES_PRIVATE : STATUSES).map(function (s) { return s.id; }), priv ? "見積中" : "参加申請準備前");
        var lbl = root.querySelector("#ncApplyLbl");
        if (lbl) lbl.textContent = priv ? "見積提出 期限" : "参加申請 期限";
        Array.prototype.forEach.call(root.querySelectorAll(".pub-only"), function (e) { e.style.display = priv ? "none" : ""; });
      }
      applySectorUI(sector);
      Array.prototype.forEach.call(root.querySelectorAll("#secPick .tagpick"), function (b) {
        b.addEventListener("click", function () {
          sector = b.getAttribute("data-s");
          Array.prototype.forEach.call(root.querySelectorAll("#secPick .tagpick"), function (x) { x.classList.toggle("on", x === b); });
          applySectorUI(sector);
        });
      });
      root.querySelector(".m-save").addEventListener("click", function () {
        var g = function (n) { var e = root.querySelector('[name="' + n + '"]'); return e ? e.value.trim() : ""; };
        if (!g("title")) { alert("案件名は必須です"); return; }
        var fd = new URLSearchParams();
        ["title", "agency", "work", "assignee", "status", "apply_deadline", "bid_deadline", "open_date", "note"].forEach(function (k) { fd.append(k, g(k)); });
        fd.append("sector", sector);
        fetch("/applications/new", {
          method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "fetch" }, body: fd.toString()
        }).then(function (r) { return r.json(); }).then(function (d) {
          if (d.error) { alert(d.error); return; }
          if (d.case) { CASES.push(d.case); mirrorCase(d.case); }
          closeModal(); render();
        }).catch(function () { alert("追加に失敗しました。時間をおいて再度お試しください。"); });
      });
    });
  }

  /* ---------- 対応エリア（地方＋詳細）ヘルパー（要望⑨②） ----------
     入力形式を「地方（8区分・複数可）＋詳細フリーテキスト」に固定する。
     保存は互換のため既存の area 文字列1本に "地方・地方 ｜ 詳細" 形式で格納。 */
  var REGION_NAMES = ["北海道・東北", "関東", "甲信越・北陸", "東海", "近畿", "中国", "四国", "九州・沖縄"];
  function parseArea(area) {
    area = String(area || "");
    var parts = area.split("｜");
    if (parts.length >= 2) {
      var left = parts[0];
      return { regions: REGION_NAMES.filter(function (r) { return left.indexOf(r) >= 0; }),
               detail: parts.slice(1).join("｜").trim() };
    }
    // 旧データ（自由記入）: 地方名が含まれれば拾い、残りを詳細に回す。
    var regions = REGION_NAMES.filter(function (r) { return area.indexOf(r) >= 0; });
    var detail = area;
    regions.forEach(function (r) { detail = detail.split(r).join(""); });
    detail = detail.replace(/[・,、\s]+/g, " ").trim();
    return { regions: regions, detail: detail };
  }
  function buildArea(regions, detail) {
    var r = regions.join("・");
    detail = (detail || "").trim();
    if (r && detail) return r + " ｜ " + detail;
    return r || detail;
  }

  /* ---------- 協力会社 編集モーダル ---------- */
  function openCompanyModal(c) {
    // 新規登録は今開いているシート（公共/民間）の会社として登録する。
    c = c || { tags: [], reviews: [], rating: 0, sector: coSectorOf(state.sheet) };
    var parsedArea = parseArea(c.area);
    var regionBtns = REGION_NAMES.map(function (r) {
      var on = parsedArea.regions.indexOf(r) >= 0;
      return '<button type="button" class="tagpick regionpick' + (on ? " on" : "") + '" data-r="' + esc(r) + '" style="--c:#2563eb">' + esc(r) + "</button>";
    }).join("");
    var tagBtns = Object.keys(WORK).map(function (t) {
      var on = (c.tags || []).indexOf(t) >= 0;
      return '<button type="button" class="tagpick' + (on ? " on" : "") + '" data-t="' + esc(t) + '" style="--c:' + workColor(t) + '">' + esc(t) + "</button>";
    }).join("");
    var stars = [1, 2, 3, 4, 5].map(function (n) { return '<button type="button" class="starpick' + (n <= (c.rating || 0) ? " on" : "") + '" data-n="' + n + '">★</button>'; }).join("");
    var revs = (c.reviews || []).join("\n");
    // 区分（公共/民間）。協力業者リストは公共・民間で分割管理する（会議要望）。
    var secInit = c.sector === "民間" ? "民間" : "公共";
    var secBtns = ["公共", "民間"].map(function (s) {
      var col = s === "民間" ? "#d97706" : "#2563eb";
      return '<button type="button" class="secpick tagpick' + (s === secInit ? " on" : "") + '" data-s="' + s + '" style="--c:' + col + '">' + s + "</button>";
    }).join("");
    var body = '<div class="m-grid">' +
      fld("会社名", '<input name="name" value="' + esc(c.name) + '">', "full") +
      '<div class="m-fld full"><span>区分（公共/民間）</span><div class="tagpicks" id="secPickCo">' + secBtns + "</div></div>" +
      '<div class="m-fld full"><span>対応エリア（地方）<em style="font-weight:400;color:var(--text-tertiary)"> 複数選択可</em></span><div class="tagpicks" id="regionPick">' + regionBtns + "</div></div>" +
      fld("対応可能エリアの詳細", '<input name="area_detail" value="' + esc(parsedArea.detail) + '" placeholder="例: 大阪府のみ対応可能 / 兵庫県東部 等">', "full") +
      fld("電話", '<input name="tel" value="' + esc(c.tel) + '">') +
      '<label class="m-fld full"><span>URL <em style="font-weight:400;color:var(--text-tertiary)"> 貼り付けて「AIで取得」で会社情報を下書き（要望⑨①）</em></span>' +
        '<span class="url-row"><input name="url" value="' + esc(c.url) + '"><button type="button" class="btn small" id="coExtract">AIで取得</button></span>' +
        '<span class="url-msg" id="coExtractMsg"></span></label>' +
      fld("特徴・メモ", '<input name="note" value="' + esc(c.note) + '">', "full") +
      '<label class="m-check"><input type="checkbox" name="partner"' + (c.partner ? " checked" : "") + "> ★よく頼む</label>" +
      '<div class="m-fld"><span>評価</span><div class="stars" id="starPick">' + stars + "</div></div>" +
      '<div class="m-fld full"><span>工事カテゴリ</span><div class="tagpicks" id="tagPick">' + tagBtns + "</div></div>" +
      fld("口コミ（1行に1件）", '<textarea name="reviews" rows="3">' + esc(revs) + "</textarea>", "full") +
      "</div>";
    showModal(c.id ? "協力会社を編集" : "協力会社を追加", body, function (root) {
      var rating = c.rating || 0;
      Array.prototype.forEach.call(root.querySelectorAll(".starpick"), function (b) {
        b.addEventListener("click", function () {
          rating = Number(b.getAttribute("data-n"));
          Array.prototype.forEach.call(root.querySelectorAll(".starpick"), function (x) { x.classList.toggle("on", Number(x.getAttribute("data-n")) <= rating); });
        });
      });
      Array.prototype.forEach.call(root.querySelectorAll(".tagpick"), function (b) {
        b.addEventListener("click", function () { b.classList.toggle("on"); });
      });
      // 区分（公共/民間）は単一選択。汎用トグルの後に走らせて必ずどちらか1つをONにする。
      Array.prototype.forEach.call(root.querySelectorAll("#secPickCo .secpick"), function (b) {
        b.addEventListener("click", function () {
          Array.prototype.forEach.call(root.querySelectorAll("#secPickCo .secpick"), function (x) { x.classList.toggle("on", x === b); });
        });
      });
      // 要望⑨①: URLからAIで会社情報を取得してフォームに下書きする。
      var ex = root.querySelector("#coExtract");
      if (ex) ex.addEventListener("click", function () {
        var urlEl = root.querySelector('[name="url"]'), msg = root.querySelector("#coExtractMsg");
        var url = urlEl ? urlEl.value.trim() : "";
        if (!url) { if (msg) msg.textContent = "URLを入力してください。"; return; }
        ex.disabled = true; if (msg) { msg.className = "url-msg"; msg.textContent = "AIが取得中…（10〜20秒）"; }
        fetch("/companies/extract", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: url }) })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            ex.disabled = false;
            if (d.enabled === false) { if (msg) { msg.className = "url-msg err"; msg.textContent = d.reason || "AIが無効です。"; } return; }
            if (d.error) { if (msg) { msg.className = "url-msg err"; msg.textContent = d.error; } return; }
            var set = function (n, v) { var e = root.querySelector('[name="' + n + '"]'); if (e && v) e.value = v; };
            set("name", d.name); set("tel", d.tel); set("area_detail", d.area_detail); set("note", d.note);
            // 地方チップ
            (d.regions || []).forEach(function (r) {
              var b = root.querySelector('#regionPick .regionpick[data-r="' + r + '"]'); if (b) b.classList.add("on");
            });
            // 工事カテゴリチップ
            (d.tags || []).forEach(function (t) {
              var b = root.querySelector('#tagPick .tagpick[data-t="' + t + '"]'); if (b) b.classList.add("on");
            });
            if (msg) { msg.className = "url-msg ok"; msg.textContent = "取得しました。内容を確認して保存してください。"; }
          })
          .catch(function () { ex.disabled = false; if (msg) { msg.className = "url-msg err"; msg.textContent = "取得に失敗しました。"; } });
      });
      root.querySelector(".m-save").addEventListener("click", function () {
        var g = function (n) { var e = root.querySelector('[name="' + n + '"]'); return e ? e.value.trim() : ""; };
        if (!g("name")) { alert("会社名は必須です"); return; }
        var regions = Array.prototype.map.call(root.querySelectorAll("#regionPick .regionpick.on"), function (b) { return b.getAttribute("data-r"); });
        var secBtn = root.querySelector("#secPickCo .secpick.on");
        var data = {
          id: c.id, name: g("name"), area: buildArea(regions, g("area_detail")), tel: g("tel"), url: g("url"), note: g("note"),
          sector: secBtn ? secBtn.getAttribute("data-s") : "公共",
          partner: root.querySelector('[name="partner"]').checked, rating: rating,
          // 工事カテゴリのみ拾う（地方チップと混ざらないよう #tagPick に限定）
          tags: Array.prototype.map.call(root.querySelectorAll("#tagPick .tagpick.on"), function (b) { return b.getAttribute("data-t"); }),
          reviews: g("reviews").split("\n").map(function (s) { return s.trim(); }).filter(Boolean)
        };
        fetch("/companies", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) })
          .then(function (r) { return r.json(); }).then(function (d) {
            if (d.error) { alert(d.error); return; }
            COMPANIES = d.companies || []; mirrorCompanies(); closeModal(); render();
          });
      });
    });
  }

  /* ---------- モーダル土台 ---------- */
  function fld(label, inner, cls) { return '<label class="m-fld ' + (cls || "") + '"><span>' + label + "</span>" + inner + "</label>"; }
  function showModal(title, body, onMount, opts) {
    opts = opts || {};
    var del = opts.danger ? '<button type="button" class="btn modal-del">' + esc(opts.danger.label) + '</button>' : '';
    $("modalRoot").innerHTML = '<div class="modal-bg"><div class="modal"><div class="modal-h"><h2>' + title +
      '</h2><button class="modal-x">×</button></div><div class="modal-b">' + body +
      '</div><div class="modal-f">' + del + '<button class="btn ghost modal-x2">閉じる</button><button class="btn primary m-save">保存</button></div></div></div>';
    var root = $("modalRoot");
    var close = function () { closeModal(); };
    root.querySelector(".modal-x").addEventListener("click", close);
    root.querySelector(".modal-x2").addEventListener("click", close);
    root.querySelector(".modal-bg").addEventListener("click", function (e) { if (e.target.classList.contains("modal-bg")) close(); });
    if (opts.danger) { var dl = root.querySelector(".modal-del"); if (dl) dl.addEventListener("click", opts.danger.onClick); }
    if (onMount) onMount(root);
  }
  function closeModal() { $("modalRoot").innerHTML = ""; }

  /* ---------- 協力会社の localStorage 復元（サーバが空のとき） ---------- */
  function restoreCompanies() {
    if (COMPANIES.length) return;
    var raw; try { raw = JSON.parse(localStorage.getItem("kawanoCompanies")); } catch (e) { return; }
    if (!raw || !raw.length) return;
    fetch("/companies/restore", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: raw }) })
      .then(function (r) { return r.json(); }).then(function (d) {
        if (d.restored > 0) { COMPANIES = d.companies || []; render(); }
      }).catch(function () {});
  }

  // 担当者セレクトで「＋ 新しい担当者を追加…」を選んだら、その場で追加する。
  // モーダルは再描画されるため、document へ委譲して一度だけ束ねる。
  document.addEventListener("change", function (e) {
    var sel = e.target;
    if (!sel || !sel.classList || !sel.classList.contains("assignee-sel")) return;
    if (sel.value === ADD_OPT) {
      var prev = sel.getAttribute("data-prev") || "未割当";
      var added = addAssigneePrompt();
      var pick = added || prev;
      sel.innerHTML = assigneeOptions(pick);   // 追加分を反映してオプションを作り直す
      sel.value = pick;
      sel.setAttribute("data-prev", pick);
    } else {
      sel.setAttribute("data-prev", sel.value);
    }
  });

  loadCustomAssignees();   // 画面から足した担当者を既定にマージ
  loadState();   // 直前の絞り込み・並び替え・タブを復元してから描画
  // サイドバーの「公共 管理シート / 民間 管理シート」から来た場合は、
  // URL の ?sheet= で開くシート（タブ）を上書きする。保存状態より優先。
  (function () {
    var m = /[?&]sheet=([^&]+)/.exec(location.search);
    if (!m) return;
    var sheet = decodeURIComponent(m[1]);
    if (["公共", "公共役務", "民間"].indexOf(sheet) >= 0) {
      // シート移動時は状況の絞り込みを解除（シートごとに状況が違うため、残したまま
      // だと移動先で列が0本になり「データが消えた」ように見える）。
      if (state.sheet !== sheet) state.statuses = [];
      state.sheet = sheet; state.tab = "案件";
    }
  })();
  // 描画の安全網: 端末ごとの保存状態が壊れていると画面が真っ白のまま「動かない」ため、
  // 初回描画が落ちたら保存状態を初期化して一度だけ再試行し、それでも駄目なら
  // 黙って死なずエラーを画面に出す（原因の特定もできるように）。
  try {
    render();
  } catch (err) {
    try { localStorage.removeItem(UI_KEY); } catch (x) {}
    state = { sheet: "公共", tab: "案件", assignee: null, sector: null, statuses: [], q: "", coTag: null, coPartner: false, coSort: false };
    try {
      render();
    } catch (err2) {
      var panel2 = $("tabPanel");
      if (panel2) panel2.innerHTML = '<p class="empty">画面の表示でエラーが発生しました。ページを再読み込みしてください。<br>' +
        '<small style="color:#b45309">直らない場合はこの表示のスクリーンショットを松本まで: ' + esc(err2 && err2.message ? err2.message : String(err2)) + "</small></p>";
    }
  }
  restoreCompanies();
})();
