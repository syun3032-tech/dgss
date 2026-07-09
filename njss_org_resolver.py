"""NJSS発注機関 → 公式サイトURL・調達ページの解決と「追加できない理由」の分類。

njss_org_scraper.py が取得した機関リスト（research/njss_orgs_114.csv 等）を入力に、
  1) 既存の監視機関（agencies テーブル=クライアント提供シート由来）のURLを流用
  2) 親法人マッピング（例: 国立病院機構◯◯病院 → hosp.go.jp）で公式サイトを決定
  3) 公式サイトを実際に取得して 調達・入札ページ を自動探索
を行い、
  - agencies へ取り込める行（research/njss_dokuho_agencies.csv）
  - クライアント向け「追加可否・理由」リスト（research/njss_dokuho_report.csv）
を出力する。ネットワークに出るのはローカル実行時のみ（本番ビルドでは使わない）。

CLI:
  .venv/bin/python njss_org_resolver.py --in research/njss_orgs_114.csv
"""

from __future__ import annotations

import csv
import re
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 独立行政法人（国立研究開発法人・行政執行法人含む）の親法人 → 公式サイト。
# ここに無い/間違っている分は検証フェッチで弾かれ、理由リストに落ちる。
PARENT_SITES: dict[str, str] = {
    "日本原子力研究開発機構": "https://www.jaea.go.jp/",
    "国際協力機構": "https://www.jica.go.jp/",
    "産業技術総合研究所": "https://www.aist.go.jp/",
    "量子科学技術研究開発機構": "https://www.qst.go.jp/",
    "情報通信研究機構": "https://www.nict.go.jp/",
    "理化学研究所": "https://www.riken.jp/",
    "国立がん研究センター": "https://www.ncc.go.jp/",
    "農業・食品産業技術総合研究機構": "https://www.naro.go.jp/",
    "水産研究・教育機構": "https://www.fra.go.jp/",
    "物質・材料研究機構": "https://www.nims.go.jp/",
    "鉄道建設・運輸施設整備支援機構": "https://www.jrtt.go.jp/",
    "国立印刷局": "https://www.npb.go.jp/",
    "都市再生機構": "https://www.ur-net.go.jp/",
    "新エネルギー・産業技術総合開発機構": "https://www.nedo.go.jp/",
    "日本私立学校振興・共済事業団": "https://www.shigaku.go.jp/",
    "宇宙航空研究開発機構": "https://www.jaxa.jp/",
    "国立病院機構": "https://nho.hosp.go.jp/",
    "労働者健康安全機構": "https://www.johas.go.jp/",
    "地域医療機能推進機構": "https://www.jcho.go.jp/",
    "海洋研究開発機構": "https://www.jamstec.go.jp/",
    "防災科学技術研究所": "https://www.bosai.go.jp/",
    "国立環境研究所": "https://www.nies.go.jp/",
    "土木研究所": "https://www.pwri.go.jp/",
    "建築研究所": "https://www.kenken.go.jp/",
    "海上・港湾・航空技術研究所": "https://www.mpat.go.jp/",
    "交通安全環境研究所": "https://www.ntsel.go.jp/",
    "国立長寿医療研究センター": "https://www.ncgg.go.jp/",
    "国立国際医療研究センター": "https://www.ncgm.go.jp/",
    "国立精神・神経医療研究センター": "https://www.ncnp.go.jp/",
    "国立循環器病研究センター": "https://www.ncvc.go.jp/",
    "国立成育医療研究センター": "https://www.ncchd.go.jp/",
    "医薬基盤・健康・栄養研究所": "https://www.nibiohn.go.jp/",
    "日本医療研究開発機構": "https://www.amed.go.jp/",
    "科学技術振興機構": "https://www.jst.go.jp/",
    "日本学術振興会": "https://www.jsps.go.jp/",
    "森林研究・整備機構": "https://www.ffpri.go.jp/",
    "港湾空港技術研究所": "https://www.pari.go.jp/",
    "海上技術安全研究所": "https://www.nmri.go.jp/",
    "電子航法研究所": "https://www.enri.go.jp/",
    "医薬品医療機器総合機構": "https://www.pmda.go.jp/",
    "農林水産消費安全技術センター": "https://www.famic.go.jp/",
    "家畜改良センター": "https://www.nlbc.go.jp/",
    "農畜産業振興機構": "https://www.alic.go.jp/",
    "日本貿易振興機構": "https://www.jetro.go.jp/",
    "情報処理推進機構": "https://www.ipa.go.jp/",
    "中小企業基盤整備機構": "https://www.smrj.go.jp/",
    "日本スポーツ振興センター": "https://www.jpnsport.go.jp/",
    "日本芸術文化振興会": "https://www.ntj.jac.go.jp/",
    "国立科学博物館": "https://www.kahaku.go.jp/",
    "国立美術館": "https://www.artmuseums.go.jp/",
    "国立文化財機構": "https://www.nich.go.jp/",
    "教職員支援機構": "https://www.nits.go.jp/",
    "大学入試センター": "https://www.dnc.ac.jp/",
    "国立青少年教育振興機構": "https://www.niye.go.jp/",
    "国立女性教育会館": "https://www.nwec.go.jp/",
    "日本学生支援機構": "https://www.jasso.go.jp/",
    "大学改革支援・学位授与機構": "https://www.niad.ac.jp/",
    "国立高等専門学校機構": "https://www.kosen-k.go.jp/",
    "放送大学学園": "https://www.ouj.ac.jp/",
    "石油天然ガス・金属鉱物資源機構": "https://www.jogmec.go.jp/",
    "エネルギー・金属鉱物資源機構": "https://www.jogmec.go.jp/",
    "製品評価技術基盤機構": "https://www.nite.go.jp/",
    "工業所有権情報・研修館": "https://www.inpit.go.jp/",
    "経済産業研究所": "https://www.rieti.go.jp/",
    "自動車技術総合機構": "https://www.naltec.go.jp/",
    "自動車事故対策機構": "https://www.nasva.go.jp/",
    "水資源機構": "https://www.water.go.jp/",
    "国際観光振興機構": "https://www.jnto.go.jp/",
    "住宅金融支援機構": "https://www.jhf.go.jp/",
    "勤労者退職金共済機構": "https://www.taisyokukin.go.jp/",
    "高齢・障害・求職者雇用支援機構": "https://www.jeed.go.jp/",
    "福祉医療機構": "https://www.wam.go.jp/",
    "国立重度知的障害者総合施設のぞみの園": "https://www.nozomi.go.jp/",
    "労働政策研究・研修機構": "https://www.jil.go.jp/",
    "国立公文書館": "https://www.archives.go.jp/",
    "北方領土問題対策協会": "https://www.hoppou.go.jp/",
    "国民生活センター": "https://www.kokusen.go.jp/",
    "郵便貯金簡易生命保険管理・郵便局ネットワーク支援機構": "https://www.yuchokampo.go.jp/",
    "農業者年金基金": "https://www.nounen.go.jp/",
    "環境再生保全機構": "https://www.erca.go.jp/",
    "駐留軍等労働者労務管理機構": "https://www.lmo.go.jp/",
    "統計センター": "https://www.nstac.go.jp/",
    "造幣局": "https://www.mint.go.jp/",
    "酒類総合研究所": "https://www.nrib.go.jp/",
    "航空大学校": "https://www.kouku-dai.ac.jp/",
    "海技教育機構": "https://www.jmets.ac.jp/",
    "国立特別支援教育総合研究所": "https://www.nise.go.jp/",
    "年金積立金管理運用独立行政法人": "https://www.gpif.go.jp/",
    "農林漁業信用基金": "https://www.jaffic.go.jp/",
    "奄美群島振興開発基金": "https://www.amami.go.jp/",
    "日本高速道路保有・債務返済機構": "https://www.jehdra.go.jp/",
    "国際交流基金": "https://www.jpf.go.jp/",
    "日本貿易保険": "https://www.nexi.go.jp/",
    "空港周辺整備機構": "https://www.oeia.or.jp/",
    "日本原子力研究開発機構 敦賀廃止措置実証本部": "https://www.jaea.go.jp/",
    "国立国語研究所": "https://www.ninjal.ac.jp/",
    "人間文化研究機構": "https://www.nihu.jp/",
    "国際農林水産業研究センター": "https://www.jircas.go.jp/",
    "輸出入・港湾関連情報処理センター": "https://www.naccs.jp/",
    "年金積立金管理運用": "https://www.gpif.go.jp/",
    "海上災害防止センター": "https://www.mdpc.or.jp/",
    "国立健康危機管理研究機構": "https://www.jihs.go.jp/",
    "男女共同参画機構": "https://www.jgepa.go.jp/",
}

# 国の機関（府省庁・立法・司法）→ 公式サイト。機関名の先頭一致でルーティングする。
# 地方支分部局（地方整備局・労働局・税関等）は省庁名プレフィクスで親に寄せる。
MINISTRY_SITES: dict[str, str] = {
    "内閣官房": "https://www.cas.go.jp/",
    "内閣府": "https://www.cao.go.jp/",
    "内閣法制局": "https://www.clb.go.jp/",
    "宮内庁": "https://www.kunaicho.go.jp/",
    "公正取引委員会": "https://www.jftc.go.jp/",
    "警察庁": "https://www.npa.go.jp/",
    "個人情報保護委員会": "https://www.ppc.go.jp/",
    "金融庁": "https://www.fsa.go.jp/",
    "消費者庁": "https://www.caa.go.jp/",
    "こども家庭庁": "https://www.cfa.go.jp/",
    "デジタル庁": "https://www.digital.go.jp/",
    "復興庁": "https://www.reconstruction.go.jp/",
    "総務省": "https://www.soumu.go.jp/",
    "消防庁": "https://www.fdma.go.jp/",
    "法務省": "https://www.moj.go.jp/",
    "出入国在留管理庁": "https://www.moj.go.jp/isa/",
    "公安調査庁": "https://www.moj.go.jp/psia/",
    "検察庁": "https://www.kensatsu.go.jp/",
    "外務省": "https://www.mofa.go.jp/mofaj/",
    "財務省": "https://www.mof.go.jp/",
    "国税庁": "https://www.nta.go.jp/",
    "税関": "https://www.customs.go.jp/",
    "文部科学省": "https://www.mext.go.jp/",
    "スポーツ庁": "https://www.mext.go.jp/sports/",
    "文化庁": "https://www.bunka.go.jp/",
    "厚生労働省": "https://www.mhlw.go.jp/",
    "農林水産省": "https://www.maff.go.jp/",
    "林野庁": "https://www.rinya.maff.go.jp/",
    "水産庁": "https://www.jfa.maff.go.jp/",
    "経済産業省": "https://www.meti.go.jp/",
    "資源エネルギー庁": "https://www.enecho.meti.go.jp/",
    "特許庁": "https://www.jpo.go.jp/",
    "中小企業庁": "https://www.chusho.meti.go.jp/",
    "国土交通省": "https://www.mlit.go.jp/",
    "観光庁": "https://www.mlit.go.jp/kankocho/",
    "気象庁": "https://www.jma.go.jp/",
    "海上保安庁": "https://www.kaiho.mlit.go.jp/",
    "環境省": "https://www.env.go.jp/",
    "原子力規制委員会": "https://www.nra.go.jp/",
    "防衛省": "https://www.mod.go.jp/",
    "防衛装備庁": "https://www.mod.go.jp/atla/",
    "会計検査院": "https://www.jbaudit.go.jp/",
    "人事院": "https://www.jinji.go.jp/",
    "最高裁判所": "https://www.courts.go.jp/",
    "裁判所": "https://www.courts.go.jp/",
    "衆議院": "https://www.shugiin.go.jp/",
    "参議院": "https://www.sangiin.go.jp/",
    "国立国会図書館": "https://www.ndl.go.jp/",
}

# 都道府県庁 → 公式サイト（標準パターン＋既知の例外。実フェッチ検証で誤りは弾かれる）
PREF_SITES: dict[str, str] = {
    "北海道庁": "https://www.pref.hokkaido.lg.jp/",
    "青森県庁": "https://www.pref.aomori.lg.jp/",
    "岩手県庁": "https://www.pref.iwate.jp/",
    "宮城県庁": "https://www.pref.miyagi.jp/",
    "秋田県庁": "https://www.pref.akita.lg.jp/",
    "山形県庁": "https://www.pref.yamagata.jp/",
    "福島県庁": "https://www.pref.fukushima.lg.jp/",
    "茨城県庁": "https://www.pref.ibaraki.jp/",
    "栃木県庁": "https://www.pref.tochigi.lg.jp/",
    "群馬県庁": "https://www.pref.gunma.jp/",
    "埼玉県庁": "https://www.pref.saitama.lg.jp/",
    "千葉県庁": "https://www.pref.chiba.lg.jp/",
    "東京都庁": "https://www.metro.tokyo.lg.jp/",
    "神奈川県庁": "https://www.pref.kanagawa.jp/",
    "新潟県庁": "https://www.pref.niigata.lg.jp/",
    "富山県庁": "https://www.pref.toyama.jp/",
    "石川県庁": "https://www.pref.ishikawa.lg.jp/",
    "福井県庁": "https://www.pref.fukui.lg.jp/",
    "山梨県庁": "https://www.pref.yamanashi.jp/",
    "長野県庁": "https://www.pref.nagano.lg.jp/",
    "岐阜県庁": "https://www.pref.gifu.lg.jp/",
    "静岡県庁": "https://www.pref.shizuoka.jp/",
    "愛知県庁": "https://www.pref.aichi.jp/",
    "三重県庁": "https://www.pref.mie.lg.jp/",
    "滋賀県庁": "https://www.pref.shiga.lg.jp/",
    "京都府庁": "https://www.pref.kyoto.jp/",
    "大阪府庁": "https://www.pref.osaka.lg.jp/",
    "兵庫県庁": "https://web.pref.hyogo.lg.jp/",
    "奈良県庁": "https://www.pref.nara.jp/",
    "和歌山県庁": "https://www.pref.wakayama.lg.jp/",
    "鳥取県庁": "https://www.pref.tottori.lg.jp/",
    "島根県庁": "https://www.pref.shimane.lg.jp/",
    "岡山県庁": "https://www.pref.okayama.jp/",
    "広島県庁": "https://www.pref.hiroshima.lg.jp/",
    "山口県庁": "https://www.pref.yamaguchi.lg.jp/",
    "徳島県庁": "https://www.pref.tokushima.lg.jp/",
    "香川県庁": "https://www.pref.kagawa.lg.jp/",
    "愛媛県庁": "https://www.pref.ehime.jp/",
    "高知県庁": "https://www.pref.kochi.lg.jp/",
    "福岡県庁": "https://www.pref.fukuoka.lg.jp/",
    "佐賀県庁": "https://www.pref.saga.lg.jp/",
    "長崎県庁": "https://www.pref.nagasaki.jp/",
    "熊本県庁": "https://www.pref.kumamoto.jp/",
    "大分県庁": "https://www.pref.oita.jp/",
    "宮崎県庁": "https://www.pref.miyazaki.lg.jp/",
    "鹿児島県庁": "https://www.pref.kagoshima.jp/",
    "沖縄県庁": "https://www.pref.okinawa.jp/",
}

# 国立大学法人・主要公立大学 → 公式サイト（実フェッチ検証で誤りは弾かれる）
UNIV_SITES: dict[str, str] = {
    "北海道大学": "https://www.hokudai.ac.jp/", "北海道教育大学": "https://www.hokkyodai.ac.jp/",
    "室蘭工業大学": "https://muroran-it.ac.jp/", "小樽商科大学": "https://www.otaru-uc.ac.jp/",
    "帯広畜産大学": "https://www.obihiro.ac.jp/", "旭川医科大学": "https://www.asahikawa-med.ac.jp/",
    "北見工業大学": "https://www.kitami-it.ac.jp/", "弘前大学": "https://www.hirosaki-u.ac.jp/",
    "岩手大学": "https://www.iwate-u.ac.jp/", "東北大学": "https://www.tohoku.ac.jp/",
    "宮城教育大学": "https://www.miyakyo-u.ac.jp/", "秋田大学": "https://www.akita-u.ac.jp/",
    "山形大学": "https://www.yamagata-u.ac.jp/", "福島大学": "https://www.fukushima-u.ac.jp/",
    "茨城大学": "https://www.ibaraki.ac.jp/", "筑波大学": "https://www.tsukuba.ac.jp/",
    "筑波技術大学": "https://www.tsukuba-tech.ac.jp/", "宇都宮大学": "https://www.utsunomiya-u.ac.jp/",
    "群馬大学": "https://www.gunma-u.ac.jp/", "埼玉大学": "https://www.saitama-u.ac.jp/",
    "千葉大学": "https://www.chiba-u.ac.jp/", "東京大学": "https://www.u-tokyo.ac.jp/",
    "東京科学大学": "https://www.isct.ac.jp/", "東京医科歯科大学": "https://www.isct.ac.jp/",
    "東京工業大学": "https://www.isct.ac.jp/", "東京外国語大学": "https://www.tufs.ac.jp/",
    "東京学芸大学": "https://www.u-gakugei.ac.jp/", "東京農工大学": "https://www.tuat.ac.jp/",
    "東京藝術大学": "https://www.geidai.ac.jp/", "東京芸術大学": "https://www.geidai.ac.jp/",
    "東京海洋大学": "https://www.kaiyodai.ac.jp/", "お茶の水女子大学": "https://www.ocha.ac.jp/",
    "電気通信大学": "https://www.uec.ac.jp/", "一橋大学": "https://www.hit-u.ac.jp/",
    "横浜国立大学": "https://www.ynu.ac.jp/", "新潟大学": "https://www.niigata-u.ac.jp/",
    "長岡技術科学大学": "https://www.nagaokaut.ac.jp/", "上越教育大学": "https://www.juen.ac.jp/",
    "富山大学": "https://www.u-toyama.ac.jp/", "金沢大学": "https://www.kanazawa-u.ac.jp/",
    "福井大学": "https://www.u-fukui.ac.jp/", "山梨大学": "https://www.yamanashi.ac.jp/",
    "信州大学": "https://www.shinshu-u.ac.jp/", "岐阜大学": "https://www.gifu-u.ac.jp/",
    "静岡大学": "https://www.shizuoka.ac.jp/", "浜松医科大学": "https://www.hama-med.ac.jp/",
    "名古屋大学": "https://www.nagoya-u.ac.jp/", "愛知教育大学": "https://www.aichi-edu.ac.jp/",
    "名古屋工業大学": "https://www.nitech.ac.jp/", "豊橋技術科学大学": "https://www.tut.ac.jp/",
    "三重大学": "https://www.mie-u.ac.jp/", "滋賀大学": "https://www.shiga-u.ac.jp/",
    "滋賀医科大学": "https://www.shiga-med.ac.jp/", "京都大学": "https://www.kyoto-u.ac.jp/",
    "京都教育大学": "https://www.kyokyo-u.ac.jp/", "京都工芸繊維大学": "https://www.kit.ac.jp/",
    "大阪大学": "https://www.osaka-u.ac.jp/", "大阪教育大学": "https://osaka-kyoiku.ac.jp/",
    "兵庫教育大学": "https://www.hyogo-u.ac.jp/", "神戸大学": "https://www.kobe-u.ac.jp/",
    "奈良教育大学": "https://www.nara-edu.ac.jp/", "奈良女子大学": "https://www.nara-wu.ac.jp/",
    "和歌山大学": "https://www.wakayama-u.ac.jp/", "鳥取大学": "https://www.tottori-u.ac.jp/",
    "島根大学": "https://www.shimane-u.ac.jp/", "岡山大学": "https://www.okayama-u.ac.jp/",
    "広島大学": "https://www.hiroshima-u.ac.jp/", "山口大学": "https://www.yamaguchi-u.ac.jp/",
    "徳島大学": "https://www.tokushima-u.ac.jp/", "鳴門教育大学": "https://www.naruto-u.ac.jp/",
    "香川大学": "https://www.kagawa-u.ac.jp/", "愛媛大学": "https://www.ehime-u.ac.jp/",
    "高知大学": "https://www.kochi-u.ac.jp/", "福岡教育大学": "https://www.fukuoka-edu.ac.jp/",
    "九州大学": "https://www.kyushu-u.ac.jp/", "九州工業大学": "https://www.kyutech.ac.jp/",
    "佐賀大学": "https://www.saga-u.ac.jp/", "長崎大学": "https://www.nagasaki-u.ac.jp/",
    "熊本大学": "https://www.kumamoto-u.ac.jp/", "大分大学": "https://www.oita-u.ac.jp/",
    "宮崎大学": "https://www.miyazaki-u.ac.jp/", "鹿児島大学": "https://www.kagoshima-u.ac.jp/",
    "鹿屋体育大学": "https://www.nifs-k.ac.jp/", "琉球大学": "https://www.u-ryukyu.ac.jp/",
    "政策研究大学院大学": "https://www.grips.ac.jp/", "総合研究大学院大学": "https://www.soken.ac.jp/",
    "北陸先端科学技術大学院大学": "https://www.jaist.ac.jp/", "奈良先端科学技術大学院大学": "https://www.naist.jp/",
    "東海国立大学機構": "https://www.thers.ac.jp/", "奈良国立大学機構": "https://www.nub.ac.jp/",
    "北海道国立大学機構": "https://www.hokkaido-nuc.ac.jp/",
    "札幌医科大学": "https://web.sapmed.ac.jp/", "東京都立大学": "https://www.tmu.ac.jp/",
    "横浜市立大学": "https://www.yokohama-cu.ac.jp/", "名古屋市立大学": "https://www.nagoya-cu.ac.jp/",
    "大阪公立大学": "https://www.omu.ac.jp/", "兵庫県立大学": "https://www.u-hyogo.ac.jp/",
    "京都府立大学": "https://www.kpu.ac.jp/", "京都府立医科大学": "https://www.kpu-m.ac.jp/",
    "北九州市立大学": "https://www.kitakyu-u.ac.jp/", "福島県立医科大学": "https://www.fmu.ac.jp/",
    "和歌山県立医科大学": "https://www.wakayama-med.ac.jp/", "奈良県立医科大学": "https://www.naramed-u.ac.jp/",
}

# 民営化法人・特殊法人・認可法人など → 公式サイト
EXTRA_ORG_SITES: dict[str, str] = {
    "東日本高速道路": "https://www.e-nexco.co.jp/",
    "中日本高速道路": "https://www.c-nexco.co.jp/",
    "西日本高速道路": "https://www.w-nexco.co.jp/",
    "首都高速道路": "https://www.shutoko.co.jp/",
    "阪神高速道路": "https://www.hanshin-exp.co.jp/",
    "本州四国連絡高速道路": "https://www.jb-honshi.co.jp/",
    "北海道旅客鉄道": "https://www.jrhokkaido.co.jp/",
    "東日本旅客鉄道": "https://www.jreast.co.jp/",
    "東海旅客鉄道": "https://jr-central.co.jp/",
    "西日本旅客鉄道": "https://www.westjr.co.jp/",
    "四国旅客鉄道": "https://www.jr-shikoku.co.jp/",
    "九州旅客鉄道": "https://www.jrkyushu.co.jp/",
    "日本貨物鉄道": "https://www.jrfreight.co.jp/",
    "日本郵政": "https://www.japanpost.jp/",
    "日本郵便": "https://www.post.japanpost.jp/",
    "日本電信電話": "https://group.ntt/",
    "東日本電信電話": "https://www.ntt-east.co.jp/",
    "西日本電信電話": "https://www.ntt-west.co.jp/",
    "日本たばこ産業": "https://www.jti.co.jp/",
    "成田国際空港": "https://www.naa.jp/",
    "中部国際空港": "https://www.centrair.jp/",
    "関西エアポート": "https://www.kansai-airports.co.jp/",
    "新関西国際空港": "https://www.nkiac.co.jp/",
    "日本政策金融公庫": "https://www.jfc.go.jp/",
    "日本政策投資銀行": "https://www.dbj.jp/",
    "国際協力銀行": "https://www.jbic.go.jp/",
    "商工組合中央金庫": "https://www.shokochukin.co.jp/",
    "日本銀行": "https://www.boj.or.jp/",
    "日本放送協会": "https://www.nhk.or.jp/",
    "日本年金機構": "https://www.nenkin.go.jp/",
    "全国健康保険協会": "https://www.kyoukaikenpo.or.jp/",
    "日本赤十字社": "https://www.jrc.or.jp/",
    "日本中央競馬会": "https://www.jra.go.jp/",
    "日本下水道事業団": "https://www.jswa.go.jp/",
    "預金保険機構": "https://www.dic.go.jp/",
    "日本司法支援センター": "https://www.houterasu.or.jp/",
    "警視庁": "https://www.keishicho.metro.tokyo.lg.jp/",
    "沖縄振興開発金融公庫": "https://www.okinawakouko.go.jp/",
    "原子力損害賠償・廃炉等支援機構": "https://www.ndf.go.jp/",
    "使用済燃料再処理": "https://www.nuro.or.jp/",
    "日本原燃": "https://www.jnfl.co.jp/",
    "電源開発": "https://www.jpower.co.jp/",
    "日本アルコール産業": "https://www.j-alco.com/",
    "沖縄科学技術大学院大学": "https://www.oist.jp/",
    "放送大学": "https://www.ouj.ac.jp/",
    "済生会": "https://www.saiseikai.or.jp/",
    "恩賜財団済生会": "https://www.saiseikai.or.jp/",
    "地方公共団体情報システム機構": "https://www.j-lis.go.jp/",
    "東京都公園協会": "https://www.tokyo-park.or.jp/",
    "東京都都市づくり公社": "https://www.toshizukuri.or.jp/",
    "東京都道路整備保全公社": "https://www.tmpc.or.jp/",
    "東京都住宅供給公社": "https://www.to-kousya.or.jp/",
    "東京都下水道サービス": "https://www.tgs-sw.co.jp/",
    "東京都立病院機構": "https://www.tmhp.jp/",
    "大阪府立病院機構": "https://www.opho.jp/",
    "埼玉県立病院機構": "https://www.saitama-pho.jp/",
    "神奈川県立病院機構": "https://kanagawa-pho.jp/",
    "堺市立病院機構": "https://www.sakai-city-hospital.jp/",
    "埼玉県住宅供給公社": "https://www.saijk.or.jp/",
    "神奈川県住宅供給公社": "https://www.kanagawa-jk.or.jp/",
}

LOCALGOV_JSON = "research/localgovjp.json"  # 全国市区町村の公式URL（code4fukui/localgovjp）
PUBLIC_UNIV_JSON = "research/public_univ_sites.json"  # 公立大学の公式URL（公立大学協会の会員校一覧より）


def load_public_univs(path: str = PUBLIC_UNIV_JSON) -> dict[str, str]:
    import json
    import os
    if not os.path.exists(path):
        return {}
    return json.load(open(path, encoding="utf-8"))


def load_localgov(path: str = LOCALGOV_JSON) -> dict[str, str]:
    """『都道府県|市区町村名』と『市区町村名』→ 公式URL の辞書。

    同名自治体（府中市=東京都/広島県 等）があるため都道府県付きキーを優先で引く。
    """
    import json
    import os
    if not os.path.exists(path):
        return {}
    data = json.load(open(path, encoding="utf-8"))
    table: dict[str, str] = {}
    for x in data:
        pref = (x.get("pref") or "").strip()
        city = (x.get("city") or "").strip().replace(" ", "")
        url = (x.get("url") or "").strip()
        if city and url:
            table[f"{pref}|{city}"] = url
            table.setdefault(city, url)
    return table


def match_localgov(name: str, address: str, lg: dict[str, str]) -> str:
    """『尼崎市役所』『安芸郡芸西村役場』『大阪市役所 水道局』等 → 自治体公式URL。"""
    m = re.match(r"(.+?[市町村区])(?:役所|役場)", normalize(name))
    if not m:
        return ""
    base = m.group(1)
    nogun = re.sub(r"^.+?郡", "", base)  # NJSSは町村に郡名を付ける（台帳は郡なし）
    nopref = re.sub(r"^.+?[都道府県]", "", base)  # 同名市の区別で県名が付くことがある
    for key in (f"{address}|{base}", f"{address}|{nogun}", f"{address}|{nopref}",
                base, nogun, nopref):
        if lg.get(key):
            return lg[key]
    return ""


# 自動探索で見つからない/誤りやすい法人の調達ページ（実アクセスで確認済みの確定値）
KNOWN_BID_PAGES: dict[str, str] = {
    "日本原子力研究開発機構": "https://tenkai.jaea.go.jp/agreement/",
    "国立がん研究センター": "https://www.ncc.go.jp/jp/chotatsu/index.html",
    "海洋研究開発機構": "https://www.jamstec.go.jp/j/about/procurement/",
    "自動車事故対策機構": "https://www.nasva.go.jp/tyoutatsu/",
    "水資源機構": "https://www.water.go.jp/honsya/honsya/keiyaku/",
    "空港周辺整備機構": "https://www.oeia.or.jp/nyusatu/one.cgi",
    "国立科学博物館": "https://www.kahaku.go.jp/info/chotatsu.html",
    "国立国語研究所": "https://www.ninjal.ac.jp/info/disclosure/procurement/",
    "大学入試センター": "https://www.dnc.ac.jp/disclosure/choutatsu_jouhou/index.html",
    "福祉医療機構": "https://www.wam.go.jp/hp/cat/chotatsujoho/",
    "国立病院機構": "https://nho.hosp.go.jp/bid/index.html",
}

# 調達・入札ページらしさのキーワード（リンクテキスト用）
BID_WORDS = ("入札", "調達", "契約情報", "契約に関する", "公募情報", "発注情報", "公告")


def normalize(name: str) -> str:
    """機関名の比較用正規化: 括弧内略称・空白・法人種別接頭辞を除く。"""
    s = re.sub(r"[（(].*?[)）]", "", name or "")
    s = re.sub(r"[〈<].*?[〉>]", "", s)
    s = re.sub(r"^(独立行政法人|国立研究開発法人|行政執行法人)", "", s)
    return re.sub(r"\s+", "", s)


class _LinkParser(HTMLParser):
    """トップページから <a href>テキスト</a> を集める簡易パーサ。"""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if self._href:
                self.links.append((self._href, text))
            self._href = None


def _ssl_ctx(legacy: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # 官公庁系は中間証明書不備が散見されるため緩める
    ctx.verify_mode = ssl.CERT_NONE
    if legacy:  # 古い暗号スイートのみのサイト（例: yuchokampo.go.jp）向け
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    return ctx


def fetch_page(url: str, timeout: int = 20) -> str:
    """ページHTMLを取得（失敗は例外のまま呼び出し元へ）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as res:
            raw = res.read()
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(legacy=True)) as res:
            raw = res.read()
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def find_bid_page(top_url: str, html: str) -> str:
    """トップページのリンクから調達・入札ページを探す。見つからなければ ''。"""
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — 壊れたHTMLでも拾えた分だけ使う
        pass
    best, best_score = "", 0
    for href, text in parser.links:
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        t = text or ""
        score = sum(2 if w in t else 0 for w in BID_WORDS[:2])  # 入札/調達を重視
        score += sum(1 for w in BID_WORDS[2:] if w in t)
        if any(k in href.lower() for k in ("nyusatsu", "chotatsu", "procurement", "bid", "keiyaku", "tender")):
            score += 1
        if href.lower().endswith(".pdf"):
            score -= 1  # 単発PDFより一覧ページを優先
        if score > best_score:
            best, best_score = urljoin(top_url, href), score
    return best if best_score >= 1 else ""


# 2階層目を辿る価値のあるリンクテキスト（トップに調達リンクが無いサイト向け）
HUB_WORDS = ("情報公開", "調達", "契約", "公告", "公募", "法人情報", "組織情報", "事業者")


def find_bid_page_deep(top_url: str, html: str, max_hubs: int = 4) -> str:
    """トップで見つからなければ、情報公開系の中間ページを最大 max_hubs 件辿って探す。"""
    bid = find_bid_page(top_url, html)
    if bid:
        return bid
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        pass
    hubs: list[str] = []
    for href, text in parser.links:
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        if any(w in (text or "") for w in HUB_WORDS):
            u = urljoin(top_url, href)
            if u.startswith("http") and not u.lower().endswith((".pdf", ".xlsx", ".doc")) \
                    and u not in hubs and _domain(u) == _domain(top_url):
                hubs.append(u)
    for hub in hubs[:max_hubs]:
        try:
            bid = find_bid_page(hub, fetch_page(hub))
        except Exception:  # noqa: BLE001 — 中間ページの不達は次の候補へ
            continue
        if bid:
            return bid
    return ""


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else ""


def _match_longest_kv(name: str, table: dict[str, str]) -> tuple[str, str]:
    """機関名にマッチする最長キーと値を返す（無ければ ('','')）。"""
    n = normalize(name)
    best_k, best_v = "", ""
    for key, url in table.items():
        k = normalize(key)
        if (n.startswith(k) or k in n) and len(k) > len(normalize(best_k)):
            best_k, best_v = key, url
    return best_k, best_v


def _match_longest(name: str, table: dict[str, str]) -> str:
    """機関名にマッチする最長キーの値を返す（無ければ ''）。"""
    return _match_longest_kv(name, table)[1]


def match_parent(name: str) -> str:
    """機関名から親法人の公式トップURLを引く（最長一致）。"""
    return _match_longest(name, PARENT_SITES)


def match_known_bid(name: str) -> str:
    """機関名から確認済みの調達ページを引く（無ければ ''）。"""
    return _match_longest(name, KNOWN_BID_PAGES)


def resolve(orgs: list[dict], existing: dict[str, dict], workers: int = 12) -> list[dict]:
    """各機関に top_url/bid_url/status/reason を付ける。

    existing: 正規化名 → 既存agencies行（クライアント提供シート由来）
    """
    _LOCALGOV = load_localgov()
    _PUBLIC_UNIVS = load_public_univs()
    # 1) 確定リスト → 既存流用 → 親法人マッピング
    for o in orgs:
        n = normalize(o["name"])
        if "閉鎖" in o["name"]:  # NJSSが［閉鎖］と付けた廃止済み機関
            o.update(top_url="", bid_url="", status="追加不可",
                     reason="廃止・閉鎖済みの機関（NJSS上の過去データのみ・新規案件なし）",
                     source="")
            continue
        known = match_known_bid(o["name"])
        if known:
            note = "" if n in {normalize(k) for k in KNOWN_BID_PAGES} \
                else "※親法人の調達ページを設定（支部・施設個別ページは要人力確認）"
            o.update(top_url=match_parent(o["name"]) or known, bid_url=known,
                     status="追加済み",
                     reason=("調達ページを実アクセスで確認済み " + note).strip(),
                     source="確定リスト")
            continue
        ex = existing.get(n)
        if not ex:  # 部分一致（NJSS側とシート側の表記差の吸収）
            ex = next((v for k, v in existing.items() if (n in k or k in n) and abs(len(k) - len(n)) <= 6), None)
        if ex and (ex.get("top_url") or ex.get("bid_url")):
            top, bid = ex.get("top_url", ""), ex.get("bid_url", "")
            if bid and not bid.lower().endswith(".pdf"):
                o.update(top_url=top, bid_url=bid, status="追加済み",
                         reason="既存の監視機関シートに登録あり（URL流用）",
                         source="既存シート")
                continue
            # シートの bid_url は「NJSS公示リンク先」＝単発の公告PDFのことが多く、
            # 調達ページとしては不適切。トップURLを起点に自前で探し直す。
            base = top or match_parent(o["name"])
            if base:
                o.update(top_url=base, bid_url=bid, status="検証待ち",
                         reason="", source="既存シート(調達ページ再探索)")
                continue
        # 分類ルーティング: 独法親法人 → 都道府県庁 → 市区町村 → 府省庁
        cand, src, base_key = "", "", ""
        k, v = _match_longest_kv(o["name"], PARENT_SITES)
        if v:
            cand, src, base_key = v, "親法人マッピング", k
        else:
            k, v = _match_longest_kv(o["name"], PREF_SITES)
            if v:
                cand, src, base_key = v, "都道府県庁マッピング", k
            else:
                muni = match_localgov(o["name"], o.get("address", ""), _LOCALGOV)
                if muni:
                    m = re.match(r"(.+?[市町村区])(?:役所|役場)", normalize(o["name"]))
                    cand, src = muni, "自治体オープンデータ"
                    base_key = (m.group(0) if m else o["name"])
                else:
                    k, v = _match_longest_kv(o["name"], MINISTRY_SITES)
                    if v:
                        cand, src, base_key = v, "府省庁マッピング", k
                    else:
                        k, v = _match_longest_kv(o["name"], {**UNIV_SITES, **_PUBLIC_UNIVS, **EXTRA_ORG_SITES})
                        if v:
                            cand, src, base_key = v, "法人マッピング", k
                        else:
                            mp = re.match(r"(.+?[都道府県])警察", normalize(o["name"]))
                            if mp and f"{mp.group(1)}庁" in PREF_SITES:
                                # 県警の調達は都道府県の電子入札系に載ることが多い
                                cand, src, base_key = (PREF_SITES[f"{mp.group(1)}庁"],
                                                       "都道府県庁マッピング(県警)", mp.group(0))
        if cand:
            o["_exact"] = normalize(base_key) == n
            o.update(top_url=cand, bid_url="", status="検証待ち", reason="", source=src)
        else:
            o.update(top_url="", bid_url="", status="追加不可",
                     reason="公式サイトURL不明（自動特定できず・要人力確認）", source="")

    # 2) 親法人マッピング分をドメイン単位で実フェッチ検証＋調達ページ探索
    to_check = sorted({o["top_url"] for o in orgs if o["status"] == "検証待ち"})
    results: dict[str, tuple[str, str]] = {}  # top_url -> (verdict, bid_url)

    def check(url: str) -> None:
        try:
            html = fetch_page(url)
        except urllib.error.HTTPError as e:
            if e.code in (403, 406):  # Bot遮断（ブラウザでは閲覧できる）
                results[url] = ("BLOCKED", "")
            else:
                results[url] = (f"到達不可: HTTP {e.code}", "")
            return
        except Exception as e:  # noqa: BLE001 — 到達不可は理由として記録
            results[url] = (f"到達不可: {type(e).__name__}", "")
            return
        results[url] = ("OK", find_bid_page_deep(url, html))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(check, to_check))

    for o in orgs:
        if o["status"] != "検証待ち":
            continue
        verdict, bid = results.get(o["top_url"], ("到達不可: 未検証", ""))
        if verdict == "BLOCKED":
            o.update(status="追加済み(要確認)",
                     reason="公式サイトはあるが自動アクセスが遮断される（ブラウザでは閲覧可・調達ページは要人力確認）")
        elif verdict != "OK":
            o.update(status="追加不可", reason=f"公式サイトに到達できない（{verdict}）")
        elif bid:
            note = "" if o.get("_exact", True) \
                else "※上位組織（本庁・本省・親法人）のサイトを基準に設定（部署個別ページは要人力確認）"
            o.update(bid_url=bid, status="追加済み",
                     reason=(f"公式サイト・調達ページを自動確認 {note}").strip())
        elif o.get("bid_url"):  # シート由来の公告PDFだけは手掛かりとして残す
            o.update(status="追加済み(要確認)",
                     reason="調達ページを自動発見できず（シート記載の公告リンクのみ・要人力確認）")
        else:
            o.update(status="追加済み(要確認)",
                     reason="公式サイトは到達可だがトップから調達・入札ページを自動発見できず（要人力確認）")
    return orgs


def load_existing_agencies(db_path: str = "denki_bid.db") -> dict[str, dict]:
    """クライアント提供シート由来の agencies だけを返す。

    自分（load_extra）が取り込んだ行は sample_url が NJSS機関ページなので除外する。
    含めると前回実行の結果を「既存シート」と誤認して再解決されなくなる（自己汚染）。
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM agencies")]
    conn.close()
    return {normalize(r["name"]): r for r in rows
            if "organizations/proc/" not in (r.get("sample_url") or "")}


def write_outputs(orgs: list[dict], agencies_out: str, report_out: str) -> tuple[int, int]:
    """agencies取り込み用CSVと、クライアント向け理由リストCSVを書き出す。"""
    today = date.today().isoformat()
    ok = [o for o in orgs if o["status"].startswith("追加済み")]
    with open(agencies_out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["name", "njss_count", "top_url", "domain",
                                          "platform_n", "bid_url", "sample_url", "fetched_at"])
        w.writeheader()
        for o in ok:
            w.writerow({
                "name": o["name"],
                "njss_count": o.get("open_count", 0),
                "top_url": o.get("top_url", ""),
                "domain": _domain(o.get("top_url", "")),
                "platform_n": 0,
                "bid_url": o.get("bid_url", ""),
                "sample_url": o.get("njss_url", ""),
                "fetched_at": today,
            })
    with open(report_out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["機関名", "分類", "所在地", "NJSS受付中件数", "NJSS登録案件数",
                                          "追加可否", "理由・備考", "公式サイトURL",
                                          "調達・入札ページURL", "NJSS機関ページ"])
        w.writeheader()
        for o in orgs:
            w.writerow({
                "機関名": o["name"],
                "分類": (o.get("categories") or "").split("|")[-1],
                "所在地": o.get("address", ""),
                "NJSS受付中件数": o.get("open_count", 0),
                "NJSS登録案件数": o.get("total_count", 0),
                "追加可否": o["status"],
                "理由・備考": o["reason"],
                "公式サイトURL": o.get("top_url", ""),
                "調達・入札ページURL": o.get("bid_url", ""),
                "NJSS機関ページ": o.get("njss_url", ""),
            })
    return len(ok), len(orgs)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="NJSS機関の公式URL解決と理由分類")
    ap.add_argument("--in", dest="src", default="research/njss_orgs_114.csv")
    ap.add_argument("--agencies-out", default="research/njss_dokuho_agencies.csv")
    ap.add_argument("--report-out", default="research/njss_dokuho_report.csv")
    args = ap.parse_args()

    with open(args.src, newline="", encoding="utf-8-sig") as f:
        orgs = [dict(r) for r in csv.DictReader(f)]
    for o in orgs:
        for k in ("open_count", "total_count", "result_count"):
            o[k] = int(o.get(k) or 0)

    existing = load_existing_agencies()
    orgs = resolve(orgs, existing)
    n_ok, n_all = write_outputs(orgs, args.agencies_out, args.report_out)
    n_ng = sum(1 for o in orgs if o["status"] == "追加不可")
    print(f"解決結果: 追加可 {n_ok} / 追加不可 {n_ng} / 全 {n_all} 件")
    print(f"  agencies取り込み用: {args.agencies_out}")
    print(f"  クライアント向け理由リスト: {args.report_out}")
