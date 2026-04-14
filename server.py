#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
リベ大流 高配当株選別ツール - バックエンドサーバー
IR BANK から株式データを自動取得してスコア算出します

使い方:
  1. pip install flask flask-cors requests beautifulsoup4
  2. python server.py
  3. ブラウザで http://localhost:5000 を開く
"""

import re
import sys
import os
import webbrowser
import threading

# --------------------------------------------------
# 依存パッケージの自動インストール
# --------------------------------------------------
def install_if_missing(packages):
    import importlib
    for pkg, import_name in packages:
        try:
            importlib.import_module(import_name)
        except ImportError:
            print(f"📦 {pkg} をインストール中...")
            os.system(f'"{sys.executable}" -m pip install {pkg} -q')

install_if_missing([
    ("flask", "flask"),
    ("flask-cors", "flask_cors"),
    ("requests", "requests"),
    ("beautifulsoup4", "bs4"),
])

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==================================================
#  値パーサー  兆・億 → float
# ==================================================
def parse_value(text):
    if not text:
        return None
    text = re.sub(r'[,\s※*]', '', str(text)).strip()
    if text in ['-', '', '予', '—', '－', '--']:
        return None

    m = re.search(r'^(-?[\d.]+)兆$', text)
    if m:
        return float(m.group(1)) * 1e12

    m = re.search(r'^(-?[\d.]+)億$', text)
    if m:
        return float(m.group(1)) * 1e8

    m = re.search(r'^(-?[\d.]+)円$', text)
    if m:
        return float(m.group(1))

    m = re.search(r'^(-?[\d.]+)%$', text)
    if m:
        return float(m.group(1))

    m = re.search(r'^(-?[\d.]+)$', text)
    if m:
        return float(m.group(1))

    return None


# ==================================================
#  トレンド計算
# ==================================================
def calc_trend(pairs, window=8):
    """
    pairs: [(year_str, float_or_None), ...]
    returns: 'up' / 'flat' / 'down' / 'unknown'
    """
    valid = [(y, v) for y, v in pairs if v is not None and v > 0]
    if len(valid) < 3:
        return 'unknown'
    recent = valid[-window:]
    mid = max(1, len(recent) // 2)
    a1 = sum(v for _, v in recent[:mid]) / mid
    a2 = sum(v for _, v in recent[mid:]) / (len(recent) - mid)
    if a1 <= 0:
        return 'unknown'
    ratio = a2 / a1
    if ratio >= 1.05:
        return 'up'
    elif ratio <= 0.90:
        return 'down'
    else:
        return 'flat'


def cf_status(pairs):
    """
    営業CF専用: 赤字チェック + トレンド
    returns: 'up_positive' / 'positive' / 'has_negative' / 'unknown'
    """
    valid = [(y, v) for y, v in pairs if v is not None]
    if not valid:
        return 'unknown'
    recent10 = valid[-10:]
    has_neg = any(v < 0 for _, v in recent10)
    if has_neg:
        return 'has_negative'
    trend = calc_trend(pairs)
    return 'up_positive' if trend == 'up' else 'positive'


def dividend_status(pairs):
    """
    配当金専用: 減配チェック + トレンド
    returns: 'stable_growing' / 'stable' / 'has_cut' / 'unknown'
    """
    valid = [(y, v) for y, v in pairs if v is not None and v > 0]
    if len(valid) < 2:
        return 'unknown'
    recent10 = valid[-10:]
    for i in range(1, len(recent10)):
        if recent10[i][1] < recent10[i - 1][1] * 0.95:   # 5%超の減少
            return 'has_cut'
    trend = calc_trend(pairs)
    return 'stable_growing' if trend == 'up' else 'stable'


# ==================================================
#  テーブルから列データを抽出
# ==================================================
def extract_column(table, col_keyword):
    """
    table: BeautifulSoup table element
    col_keyword: ヘッダーに含まれるキーワード
    returns: [(year_str, float_or_None), ...]  予測行は除外
    """
    thead = table.find('thead')
    if not thead:
        return []
    headers = [th.get_text(strip=True) for th in thead.find_all(['th', 'td'])]

    col_idx = -1
    for i, h in enumerate(headers):
        if col_keyword in h:
            col_idx = i
            break
    if col_idx < 0:
        return []

    tbody = table.find('tbody')
    if not tbody:
        return []

    rows = []
    for row in tbody.find_all('tr'):
        cells = row.find_all(['td', 'th'])
        if len(cells) <= col_idx:
            continue
        year = cells[0].get_text(strip=True)
        # 予測行・注釈行を除外
        if '予' in year or '※' in year or not re.search(r'\d{4}', year):
            continue
        val = parse_value(cells[col_idx].get_text(strip=True))
        rows.append((year, val))
    return rows


def last_valid(pairs):
    """直近の有効値を返す"""
    for _, v in reversed(pairs):
        if v is not None:
            return v
    return None


# ==================================================
#  IR BANK スクレイピング本体
# ==================================================
def scrape_irbank(code):
    code = re.sub(r'\s', '', str(code))
    url = f'https://irbank.net/{code}/results'

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'ja,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://irbank.net/',
    }

    resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    resp.encoding = 'utf-8'

    soup = BeautifulSoup(resp.text, 'html.parser')

    # ---- 銘柄名 ----
    company_name = ''
    h1 = soup.find('h1')
    if h1:
        raw = h1.get_text(strip=True)
        m = re.match(r'^\d+\s+(.+)$', raw)
        company_name = m.group(1) if m else raw

    # ---- テーブル ----
    tables = soup.find_all('table')
    if len(tables) < 4:
        raise ValueError(
            f"テーブルが {len(tables)} 個しか見つかりません。"
            "証券コードを確認してください。"
        )

    t0, t1, t2, t3 = tables[0], tables[1], tables[2], tables[3]

    # ---- 配当利回り（サマリーの dt/dd 構造から取得）----
    # IR BANKの結果ページには <dt>配当 予</dt><dd>3.1%</dd> という形で利回りが記載される
    dividend_yield = None
    for dt in soup.find_all('dt'):
        dt_text = dt.get_text(strip=True)
        if ('配当' in dt_text
                and '配当性向' not in dt_text
                and '配当金' not in dt_text
                and '配当率' not in dt_text):
            dd = dt.find_next_sibling('dd')
            if dd:
                m = re.search(r'([\d.]+)%', dd.get_text(strip=True))
                if m:
                    v = float(m.group(1))
                    if 0 < v < 20:   # 異常値除外
                        dividend_yield = round(v, 2)
                        break

    # ① 売上高（収益）
    sales = extract_column(t0, '収益') or extract_column(t0, '売上')
    sales_trend = calc_trend(sales)

    # ② EPS
    eps = extract_column(t0, 'EPS')
    eps_trend = calc_trend(eps)

    # ③ 営業利益率
    margin = extract_column(t0, '営利率')
    operating_margin = last_valid(margin)
    if operating_margin is not None:
        operating_margin = round(operating_margin, 1)

    # ④ 自己資本比率（IR BANKでは「自己資本比率」列）
    equity = extract_column(t1, '自己資本比率') or extract_column(t1, '株主資本比率')
    equity_ratio = last_valid(equity)
    if equity_ratio is not None:
        equity_ratio = round(equity_ratio, 1)

    # ⑤ 営業CF
    ocf = extract_column(t2, '営業CF')
    operating_cf = cf_status(ocf)

    # ⑥ 現金等
    cash = extract_column(t2, '現金等')
    cash_trend = calc_trend(cash)

    # ⑦ 1株配当金
    div = extract_column(t3, '一株配当')
    dividend_trend = dividend_status(div)

    # ⑧ 配当性向
    payout = extract_column(t3, '配当性向')
    payout_ratio = last_valid(payout)
    if payout_ratio is not None:
        payout_ratio = round(payout_ratio, 1)

    return {
        'name': company_name,
        'code': code,
        'dividend_yield': dividend_yield,
        # トレンド系 (up/flat/down/unknown)
        'sales_trend': sales_trend,
        'eps_trend': eps_trend,
        # 数値系
        'operating_margin': operating_margin,
        'equity_ratio': equity_ratio,
        # CF
        'operating_cf': operating_cf,   # up_positive/positive/has_negative/unknown
        'cash_trend': cash_trend,
        # 配当
        'dividend_trend': dividend_trend,   # stable_growing/stable/has_cut/unknown
        'payout_ratio': payout_ratio,
        # デバッグ用
        '_debug': {
            'sales_recent':  [(y, v) for y, v in sales[-5:]  if v is not None],
            'eps_recent':    [(y, v) for y, v in eps[-5:]    if v is not None],
            'margin_recent': [(y, v) for y, v in margin[-3:] if v is not None],
            'equity_recent': [(y, v) for y, v in equity[-3:] if v is not None],
            'ocf_recent':    [(y, v) for y, v in ocf[-5:]    if v is not None],
            'cash_recent':   [(y, v) for y, v in cash[-5:]   if v is not None],
            'div_recent':    [(y, v) for y, v in div[-5:]    if v is not None],
            'payout_recent': [(y, v) for y, v in payout[-3:] if v is not None],
        }
    }


# ==================================================
#  高配当候補銘柄リスト（キュレーション済み約130銘柄）
# ==================================================
# 外部サイトへのスクレイピングは不安定なため、
# 日本市場で高配当として知られる代表的な銘柄を内蔵リストとして保持。
_CANDIDATE_STOCKS = [
    # 商社
    ('8058','三菱商事'),('8031','三井物産'),('8053','住友商事'),
    ('8001','伊藤忠商事'),('8002','丸紅'),('8015','豊田通商'),
    ('2768','双日'),('8088','岩谷産業'),('8081','カナデン'),
    # 通信
    ('9432','NTT'),('9433','KDDI'),('9434','ソフトバンク'),
    # 銀行・金融
    ('8316','三井住友FG'),('8306','三菱UFJ FG'),('8411','みずほFG'),
    ('7182','ゆうちょ銀行'),('8354','ふくおかFG'),('8473','SBI HD'),
    ('8253','クレディセゾン'),('8309','三井住友トラストHD'),
    ('7186','コンコルディアFG'),('8327','山口FG'),
    ('7181','かんぽ生命'),('8308','りそなHD'),
    # 保険
    ('8725','MS&ADインシュアランスG'),('8766','東京海上HD'),
    ('8750','第一生命HD'),('8630','SOMPOホールディングス'),
    # 証券
    ('8601','大和証券G本社'),('8604','野村HD'),('8628','松井証券'),
    ('8698','マネックスG'),('7164','全国保証'),
    # エネルギー・資源
    ('1605','INPEX'),('5019','出光興産'),('5020','ENEOSホールディングス'),
    # 鉄鋼・金属
    ('5401','日本製鉄'),('5411','JFEホールディングス'),
    ('5713','住友金属鉱山'),('3436','SUMCO'),
    # 化学
    ('4063','信越化学工業'),('4005','住友化学'),('4188','三菱ケミカルG'),
    ('4183','三井化学'),('4021','日産化学'),('3407','旭化成'),
    ('4612','日本ペイントHD'),
    # 紙・パルプ
    ('3861','王子HD'),('3863','日本製紙'),('3105','日清紡HD'),
    # 海運（利回り高め）
    ('9101','日本郵船'),('9104','商船三井'),('9107','川崎汽船'),
    # 陸運・物流
    ('9064','ヤマトHD'),('9062','日本通運'),('9006','京急電鉄'),
    # 電力・ガス
    ('9501','東京電力HD'),('9502','中部電力'),('9503','関西電力'),
    ('9531','東京ガス'),('9532','大阪ガス'),
    # 食品・飲料・たばこ
    ('2914','JT'),('2502','アサヒGHD'),('2503','キリンHD'),
    ('2282','日本ハム'),('2269','明治HD'),('2801','キッコーマン'),
    ('2587','サントリーBF'),('1332','ニッスイ'),('1301','極洋'),
    # 医薬品
    ('4502','武田薬品工業'),('4519','中外製薬'),('4568','第一三共'),
    ('4523','エーザイ'),('4507','塩野義製薬'),('4543','テルモ'),
    # 自動車・輸送機器
    ('7203','トヨタ自動車'),('7267','本田技研工業'),('7270','SUBARU'),
    ('7269','スズキ'),('7272','ヤマハ発動機'),('6902','DENSO'),
    ('7202','いすゞ自動車'),('7201','日産自動車'),('7012','川崎重工業'),
    # 電機・機械
    ('6501','日立製作所'),('6503','三菱電機'),('6752','パナソニックHD'),
    ('6301','コマツ'),('6326','クボタ'),('7011','三菱重工業'),
    ('7751','キヤノン'),('6724','セイコーエプソン'),('6702','富士通'),
    ('6723','ルネサスエレクトロニクス'),('6857','アドバンテスト'),
    ('6981','村田製作所'),('4901','富士フイルムHD'),
    ('6503','三菱電機'),('4307','野村総合研究所'),
    # 不動産・建設
    ('8802','三菱地所'),('8804','東急不動産HD'),('8830','住友不動産'),
    ('8801','三井不動産'),('1928','積水ハウス'),('1925','大和ハウス工業'),
    ('3003','ヒューリック'),('1802','大林組'),('1803','清水建設'),
    ('1801','大成建設'),('1812','鹿島建設'),
    # 小売・サービス
    ('8267','イオン'),('3382','セブン&アイHD'),('2651','ローソン'),
    ('3099','三越伊勢丹HD'),('9843','ニトリHD'),
    # 素材・その他製造
    ('4452','花王'),('4911','資生堂'),('5802','住友電気工業'),
    ('9101','日本郵船'),('6703','OKI'),
    ('8096','兼松エレクトロニクス'),
    # 航空・交通
    ('9202','ANAホールディングス'),('9201','日本航空'),
    # インターネット・IT
    ('6098','リクルートHD'),('9984','ソフトバンクG'),
    # その他高配当として話題の銘柄
    ('8252','丸井G'),('5943','ノーリツ'),('7014','名村造船所'),
    ('8075','神鋼商事'),('5901','東洋製罐GHD'),
    ('9783','ベネッセHD'),('4816','東映アニメーション'),
]

def get_dividend_ranking(max_stocks=120):
    """高配当候補銘柄リストを返す（内蔵キュレーション済みリスト）"""
    seen = set()
    result = []
    for code, name in _CANDIDATE_STOCKS:
        if code not in seen:
            seen.add(code)
            result.append({'code': code, 'name': name})
    return result[:max_stocks]


def fetch_yahoo_ranking(max_stocks=300):
    """
    ヤフーファイナンスの配当利回りランキングから銘柄コードを取得する。
    最大 max_stocks 件取得して返す（4桁の証券コードのみ抽出）。
    失敗時は _CANDIDATE_STOCKS にフォールバック。
    """
    base_url = 'https://finance.yahoo.co.jp/stocks/ranking/dividendYield?market=all&page={}'
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'ja,en-US;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://finance.yahoo.co.jp/',
    }

    seen = set()
    result = []
    # 1ページあたり約47銘柄。必要ページ数を動的に計算（バッファ+3ページ）
    import math
    max_pages = min(math.ceil(max_stocks / 47) + 3, 100)  # 最大100ページ
    consecutive_empty = 0  # 連続空ページカウント

    for page in range(1, max_pages + 1):
        if len(result) >= max_stocks:
            break
        try:
            url = base_url.format(page)
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')

            # 証券コードは <li class="*supplement*"> に4桁数字として入る
            codes_found = 0
            for li in soup.find_all('li', class_=lambda c: c and 'supplement' in c):
                text = li.get_text(strip=True)
                if re.match(r'^\d{4}$', text) and text not in seen:
                    seen.add(text)
                    result.append({'code': text, 'name': ''})
                    codes_found += 1
                    if len(result) >= max_stocks:
                        break

            print(f"  Yahoo Finance p{page}/{max_pages}: {codes_found}件取得（累計{len(result)}件）")

            if codes_found == 0:
                consecutive_empty += 1
                print(f"  p{page} でデータなし（連続{consecutive_empty}回）")
                if consecutive_empty >= 2:
                    # 2ページ連続でデータなし → ランキング終端とみなす
                    print("  ランキング終端に達しました → 終了")
                    break
            else:
                consecutive_empty = 0  # データがあればリセット

        except Exception as e:
            print(f"  Yahoo Finance p{page} 取得失敗: {e}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break

    if not result:
        # フォールバック：内蔵リストを使用
        print("  Yahoo Finance 取得失敗 → 内蔵リストにフォールバック")
        return get_dividend_ranking(max_stocks)

    return result


# ==================================================
#  Flask ルーティング
# ==================================================
@app.route('/')
def index():
    resp = send_from_directory(BASE_DIR, 'index.html')
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp


@app.route('/api/dividend_ranking')
def dividend_ranking():
    try:
        stocks = get_dividend_ranking(max_stocks=130)
        return jsonify({'success': True, 'stocks': stocks, 'count': len(stocks)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/yahoo_ranking')
def yahoo_ranking():
    try:
        from flask import request as flask_request
        count = flask_request.args.get('count', 300, type=int)
        count = max(50, min(count, 2000))   # 50〜2000の範囲に制限
        stocks = fetch_yahoo_ranking(max_stocks=count)
        return jsonify({'success': True, 'stocks': stocks, 'count': len(stocks)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/stock/<code>')
def get_stock(code):
    try:
        data = scrape_irbank(code)
        return jsonify({'success': True, 'data': data})

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else '?'
        return jsonify({
            'success': False,
            'error': f'銘柄が見つかりません（HTTP {status}）。証券コードを確認してください。'
        }), 404

    except requests.exceptions.ConnectionError:
        return jsonify({
            'success': False,
            'error': 'IR BANK への接続に失敗しました。インターネット接続を確認してください。'
        }), 503

    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'error': 'IR BANK への接続がタイムアウトしました。しばらくしてから再試行してください。'
        }), 504

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================================================
#  起動
# ==================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    url = f'http://localhost:{port}'

    print()
    print('=' * 52)
    print('  📈 リベ大流 高配当株選別ツール')
    print('=' * 52)
    print(f'  🌐 URL  : {url}')
    print('  🛑 終了 : Ctrl+C')
    print('=' * 52)
    print()

    is_local = (port == 8080 and not os.environ.get('PORT'))
    if is_local:
        def open_browser():
            import time
            time.sleep(1.8)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
