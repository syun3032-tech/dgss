/**
 * 官公需情報ポータルAPI → Google スプレッドシート 自動巡回スクリプト（完全無料）
 *
 * できること:
 *   - 毎日（時間トリガー）官公需API(kkj.go.jp)から電気工事の入札公告を取得
 *   - 既にシートに無い案件だけを「🆕新着」として追記（重複は自動スキップ）
 *   - 前回の🆕は自動で消えるので、常に「今回の新着」だけが🆕表示
 *
 * 使い方（5分・無料）:
 *   1. 対象のGoogleスプレッドシートを開く
 *   2. 拡張機能 → Apps Script を開く
 *   3. このコードを全部貼り付けて保存
 *   4. 上部の関数選択で「初期設定」を選び ▶実行（初回だけ権限承認）
 *   5. これで毎日自動更新される（時計アイコン=トリガーに「毎日」が登録される）
 *   ※ 手動で今すぐ更新したい時は「更新」を ▶実行
 */

// ===== 設定 =====
const QUERY = '電気工事';        // 検索キーワード
const CATEGORY = '2';            // 1=物品 2=工事 3=役務
const COUNT = '1000';           // 最大件数（最大1000）
// 関西だけに絞りたい時は下を有効化（滋賀26… 大阪27 等のJISコード, カンマ区切り）
// const LG_CODE = '25,26,27,28,29,30';
const LG_CODE = '';             // 空=全国
const SHEET_NAME = '案件';       // 書き込むシート名
const TZ = 'Asia/Tokyo';

const PROC = { '1': '一般競争入札', '2': '簡易公募型競争入札', '3': '簡易公募型指名競争入札' };
const HEADER = ['取得日', '新着', '公告日', '都道府県', '発注機関', '案件名',
                '入札方式', '締切(開札)', '仕様書', '公告URL', 'Key'];

/** 初回セットアップ：毎日の自動実行トリガーを登録し、すぐ1回更新する */
function 初期設定() {
  // 既存の同名トリガーを消してから1つ登録（毎朝7時台）
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === '更新') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('更新').timeBased().everyDays(1).atHour(7).create();
  更新();
}

/** 官公需APIを取得してシートに新着追記（手動でも毎日トリガーでも実行） */
function 更新() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(SHEET_NAME);
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADER);
    sheet.setFrozenRows(1);
  }

  // 既存Key（重複防止）と、前回の🆕セルをクリア
  const last = sheet.getLastRow();
  const existing = {};
  if (last > 1) {
    const keys = sheet.getRange(2, 11, last - 1, 1).getValues(); // K列=Key
    keys.forEach((r, i) => { if (r[0]) existing[r[0]] = true; });
    sheet.getRange(2, 2, last - 1, 1).clearContent(); // B列=新着 をクリア
  }

  // API取得
  let url = 'https://www.kkj.go.jp/api/?Query=' + encodeURIComponent(QUERY)
          + '&Category=' + CATEGORY + '&Count=' + COUNT;
  if (LG_CODE) url += '&LG_Code=' + LG_CODE;
  const xml = UrlFetchApp.fetch(url, { muteHttpExceptions: true }).getContentText();
  const root = XmlService.parse(xml).getRootElement();
  const results = root.getChild('SearchResults');
  if (!results) { Logger.log('結果なし/エラー: ' + xml.slice(0, 200)); return; }

  const today = Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd');
  const get = (sr, t) => { const c = sr.getChild(t); return c ? c.getText() : ''; };
  const rows = [];
  results.getChildren('SearchResult').forEach(sr => {
    const key = get(sr, 'Key') || get(sr, 'ResultId');
    if (!key || existing[key]) return; // 既出はスキップ
    existing[key] = true;
    const att = sr.getChild('Attachments');
    const spec = (att && att.getChild('Attachment')) ? '取得可' : '未判定';
    rows.push([today, '🆕', get(sr, 'CftIssueDate'), get(sr, 'PrefectureName'),
      get(sr, 'OrganizationName'), get(sr, 'ProjectName'),
      PROC[get(sr, 'ProcedureType')] || get(sr, 'ProcedureType'),
      get(sr, 'OpeningTendersEvent'), spec, get(sr, 'ExternalDocumentURI'), key]);
  });

  if (rows.length) {
    // 新着を先頭（ヘッダ直下）に挿入して、上に新しい案件が来るように
    sheet.insertRowsAfter(1, rows.length);
    sheet.getRange(2, 1, rows.length, HEADER.length).setValues(rows);
  }
  Logger.log('新着 ' + rows.length + ' 件を追加');
}
