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
import time
from collections import defaultdict

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
CORS(app, origins=['https://high-dividend-stock-tool.onrender.com', 'http://localhost:8080', 'http://127.0.0.1:8080'])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------
# シンプルなレート制限（メモリ内）
# 1IPあたり60秒間に最大30リクエストまで
# --------------------------------------------------
_rate_limit_store: dict = defaultdict(list)
_RATE_LIMIT_MAX = 30
_RATE_LIMIT_WINDOW = 60  # seconds

def _check_rate_limit(ip: str) -> bool:
    """True=通過可, False=制限超過"""
    now = time.time()
    window_start = now - _RATE_LIMIT_WINDOW
    timestamps = [t for t in _rate_limit_store[ip] if t > window_start]
    if len(timestamps) >= _RATE_LIMIT_MAX:
        _rate_limit_store[ip] = timestamps
        return False
    timestamps.append(now)
    _rate_limit_store[ip] = timestamps
    return True

def _validate_code(code: str) -> bool:
    """証券コードが4桁の数字かチェック"""
    return bool(re.match(r'^\d{4}$', code.strip()))

_ALLOWED_ORIGINS = [
    'high-dividend-stock-tool.onrender.com',
    'localhost',
    '127.0.0.1',
]

def _check_referer(req) -> bool:
    """
    Refererヘッダーが存在する場合、許可オリジンからのリクエストか確認。
    Refererなし（直接アクセスや一部ブラウザ設定）は通過させる。
    """
    referer = req.headers.get('Referer', '')
    origin = req.headers.get('Origin', '')
    # Refererもoriginもない場合はlocalhostからのアクセスのみ許可
    check_str = referer or origin
    if not check_str:
        # 開発環境（localhost）からの直接アクセスは許可
        remote = req.remote_addr or ''
        return remote in ('127.0.0.1', '::1')
    return any(o in check_str for o in _ALLOWED_ORIGINS)

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
    title_tag = soup.find('title')
    if title_tag:
        m = re.match(r'^(.+?)（\d+）', title_tag.get_text(strip=True))
        if m:
            company_name = m.group(1).strip()

    # ---- テーブル ----
    tables = soup.find_all('table')
    if len(tables) < 4:
        raise ValueError(
            f"テーブルが {len(tables)} 個しか見つかりません。"
            "証券コードを確認してください。"
        )

    t0, t1, t2, t3 = tables[0], tables[1], tables[2], tables[3]

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
                    if 0 < v < 20:
                        dividend_yield = round(v, 2)
                        break

    sales = extract_column(t0, '収益') or extract_column(t0, '売上')
    sales_trend = calc_trend(sales)
    eps = extract_column(t0, 'EPS')
    eps_trend = calc_trend(eps)
    margin = extract_column(t0, '営利率')
    operating_margin = last_valid(margin)
    if operating_margin is not None:
        operating_margin = round(operating_margin, 1)
    equity = extract_column(t1, '自己資本比率') or extract_column(t1, '株主資本比率')
    equity_ratio = last_valid(equity)
    if equity_ratio is not None:
        equity_ratio = round(equity_ratio, 1)
    ocf = extract_column(t2, '営業CF')
    operating_cf = cf_status(ocf)
    cash = extract_column(t2, '現金等')
    cash_trend = calc_trend(cash)
    div = extract_column(t3, '一株配当')
    dividend_trend = dividend_status(div)
    payout = extract_column(t3, '配当性向')
    payout_ratio = last_valid(payout)
    if payout_ratio is not None:
        payout_ratio = round(payout_ratio, 1)

    return {
        'name': company_name,
        'code': code,
        'dividend_yield': dividend_yield,
        'sales_trend': sales_trend,
        'eps_trend': eps_trend,
        'operating_margin': operating_margin,
        'equity_ratio': equity_ratio,
        'operating_cf': operating_cf,
        'cash_trend': cash_trend,
        'dividend_trend': dividend_trend,
        'payout_ratio': payout_ratio,
    }


# ==================================================
#  高配当候補銘柄リスト（キュレーション済み約130銘柄）
# ==================================================
_CANDIDATE_STOCKS = [
    ('8058','三菱商事'),('8031','三井物産'),('8053','住友商事'),
    ('8001','伊藤忠商事'),('8002','丸紅'),('8015','豊田通商'),
    ('2768','双日'),('8088','岩谷産業'),('8081','カナデン'),
    ('9432','NTT'),('9433','KDDI'),('9434','ソフトバンク'),
    ('8316','三井住友FG'),('8306','三菱UFJ FG'),('8411','みずほFG'),
    ('7182','ゆうちょ銀行'),('8354','ふくおかFG'),('8473','SBI HD'),
    ('8253','クレディセゾン'),('8309','三井住友トラストHD'),
    ('7186','コンコルディアFG'),('8327','山口FG'),
    ('7181','かんぽ生命'),('8308','りそなHD'),
    ('8725','MS&ADインシュアランスG'),('8766','東京海上HD'),
    ('8750','第一生命HD'),('8630','SOMPOホールディングス'),
    ('8601','大和証券G本社'),('8604','野村HD'),('8628','松井証券'),
    ('8698','マネックスG'),('7164','全国保証'),
    ('1605','INPEX'),('5019','出光興産'),('5020','ENEOSホールディングス'),
    ('5401','日本製鉄'),('5411','JFEホールディングス'),
    ('5713','住友金属鉱山'),('3436','SUMCO'),
    ('4063','信越化学工業'),('4005','住友化学'),('4188','三菱ケミカルG'),
    ('4183','三井化学'),('4021','日産化学'),('3407','旭化成'),
    ('4612','日本ペイントHD'),
    ('3861','王子HD'),('3863','日本製紙'),('3105','日清紡HD'),
    ('9101','日本郵船'),('9104','商船三井'),('9107','川崎汽船'),
    ('9064','ヤマトHD'),('9062','日本通運'),('9006','京急電鉄'),
    ('9501','東京電力HD'),('9502','中部電力'),('9503','関西電力'),
    ('9531','東京ガス'),('9532','大阪ガス'),
    ('2914','JT'),('2502','アサヒGHD'),('2503','キリンHD'),
    ('2282','日本ハム'),('2269','明治HD'),('2801','キッコーマン'),
    ('2587','サントリーBF'),('1332','ニッスイ'),('1301','極洋'),
    ('4502','武田薬品工業'),('4519','中外製薬'),('4568','第一三共'),
    ('4523','エーザイ'),('4507','塩野義製薬'),('4543','テルモ'),
    ('7203','トヨタ自動車'),('7267','本田技研工業'),('7270','SUBARU'),
    ('7269','スズキ'),('7272','ヤマハ発動機'),('6902','DENSO'),
    ('7202','いすゞ自動車'),('7201','日産自動車'),('7012','川崎重工業'),
    ('6501','日立製作所'),('6503','三菱電機'),('6752','パナソニックHD'),
    ('6301','コマツ'),('6326','クボタ'),('7011','三菱重工業'),
    ('7751','キヤノン'),('6724','セイコーエプソン'),('6702','富士通'),
    ('6723','ルネサスエレクトロニクス'),('6857','アドバンテスト'),
    ('6981','村田製作所'),('4901','富士フイルムHD'),
    ('4307','野村総合研究所'),
    ('8802','三菱地所'),('8804','東急不動産HD'),('8830','住友不動産'),
    ('8801','三井不動産'),('1928','積水ハウス'),('1925','大和ハウス工業'),
    ('3003','ヒューリック'),('1802','大林組'),('1803','清水建設'),
    ('1801','大成建設'),('1812','鹿島建設'),
    ('8267','イオン'),('3382','セブン&アイHD'),('2651','ローソン'),
    ('3099','三越伊勢丹HD'),('9843','ニトリHD'),
    ('4452','花王'),('4911','資生堂'),('5802','住友電気工業'),
    ('6703','OKI'),('8096','兼松エレクトロニクス'),
    ('9202','ANAホールディングス'),('9201','日本航空'),
    ('6098','リクルートHD'),('9984','ソフトバンクG'),
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
    失敗時・タイムアウト時は取得済み件数 or 内蔵リストにフォールバック。
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
    import math
    max_pages = min(math.ceil(max_stocks / 47) + 3, 100)
    consecutive_empty = 0
    deadline = time.time() + max(30, max_pages * 3)  # ページ数に比例（1000件≒75秒）

    for page in range(1, max_pages + 1):
        if len(result) >= max_stocks:
            break
        if time.time() > deadline:
            print(f"  時間制限に達しました（{len(result)}件取得済み）→ 途中結果を返します")
            break
        try:
            url = base_url.format(page)
            resp = requests.get(url, headers=headers, timeout=5)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')

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
                if consecutive_empty >= 3:
                    print("  ランキング終端に達しました → 終了")
                    break
            else:
                consecutive_empty = 0

        except Exception as e:
            print(f"  Yahoo Finance p{page} 取得失敗: {e}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break

    if not result:
        print("  Yahoo Finance 取得失敗 → 内蔵リストにフォールバック")
        return get_dividend_ranking(max_stocks)

    return result


# ==================================================
#  Flask ルーティング
# ==================================================
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/api/name/<code>')
def get_name(code):
    """証券コードから銘柄名だけを高速取得（タイトルタグのみ参照）"""
    from flask import request as flask_request
    if not _check_referer(flask_request):
        return jsonify({'success': False, 'error': 'アクセスが拒否されました。'}), 403
    if not _check_rate_limit(flask_request.remote_addr):
        return jsonify({'success': False, 'error': 'リクエストが多すぎます。しばらくしてから再試行してください。'}), 429
    try:
        code = re.sub(r'\s', '', str(code))
        if not _validate_code(code):
            return jsonify({'success': False, 'error': '証券コードは4桁の数字を入力してください。'}), 400
        url = f'https://irbank.net/{code}/results'
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'ja,en;q=0.9',
            'Referer': 'https://irbank.net/',
        }
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        name = ''
        title_tag = soup.find('title')
        if title_tag:
            m = re.match(r'^(.+?)（\d+）', title_tag.get_text(strip=True))
            if m:
                name = m.group(1).strip()
        return jsonify({'success': True, 'code': code, 'name': name})
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else '?'
        return jsonify({'success': False, 'error': f'銘柄が見つかりません（HTTP {status}）。証券コードを確認してください。'}), 404
    except Exception:
        return jsonify({'success': False, 'error': '銘柄名の取得に失敗しました。しばらくしてから再試行してください。'}), 500


@app.route('/api/profile/<code>')
def get_profile(code):
    """Yahoo Finance から企業情報（特色・連結事業）を取得"""
    from flask import request as flask_request
    if not _check_referer(flask_request):
        return jsonify({'success': False, 'error': 'アクセスが拒否されました。'}), 403
    if not _check_rate_limit(flask_request.remote_addr):
        return jsonify({'success': False, 'error': 'リクエストが多すぎます。しばらくしてから再試行してください。'}), 429
    try:
        code = re.sub(r'\s', '', str(code))
        if not _validate_code(code):
            return jsonify({'success': False, 'error': '証券コードは4桁の数字を入力してください。'}), 400
        url = f'https://finance.yahoo.co.jp/quote/{code}.T/profile'
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'ja,en;q=0.9',
            'Referer': 'https://finance.yahoo.co.jp/',
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'html.parser')

        tokushoku = ''
        jigyou = ''
        jigyou_label = '連結事業'

        # 方法1: dt/dd 定義リスト形式
        for dt in soup.find_all('dt'):
            label = dt.get_text(strip=True)
            dd = dt.find_next_sibling('dd')
            if not dd:
                continue
            content = dd.get_text(strip=True)
            if '特色' in label and not tokushoku:
                tokushoku = content
            elif '連結事業' in label and not jigyou:
                jigyou = content
                jigyou_label = '連結事業'
            elif '単独事業' in label and not jigyou:
                jigyou = content
                jigyou_label = '単独事業'

        # 方法2: 見出しタグの後続兄弟要素
        if not tokushoku or not jigyou:
            for heading in soup.find_all(['h2', 'h3', 'h4', 'th', 'strong']):
                txt = heading.get_text(strip=True)
                sib = heading.find_next_sibling()
                if not sib:
                    continue
                content = sib.get_text(strip=True)
                if txt in ('特色', '【特色】') and not tokushoku:
                    tokushoku = content
                elif ('連結事業' in txt or '【連結事業】' in txt) and not jigyou:
                    jigyou = content
                    jigyou_label = '連結事業'
                elif ('単独事業' in txt or '【単独事業】' in txt) and not jigyou:
                    jigyou = content
                    jigyou_label = '単独事業'

        # 方法3: ページ全文の行解析
        if not tokushoku or not jigyou:
            lines = [l.strip() for l in soup.get_text('\n').split('\n') if l.strip()]
            for i, line in enumerate(lines):
                if line in ('特色', '【特色】') and i + 1 < len(lines) and not tokushoku:
                    tokushoku = lines[i + 1]
                elif (('連結事業' in line or '単独事業' in line) and len(line) <= 10
                        and i + 1 < len(lines) and not jigyou):
                    jigyou = lines[i + 1]
                    jigyou_label = '連結事業' if '連結' in line else '単独事業'

        return jsonify({
            'success': True,
            'code': code,
            'tokushoku': tokushoku[:500],
            'jigyou': jigyou[:300],
            'jigyou_label': jigyou_label,
        })
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else '?'
        return jsonify({'success': False, 'error': f'企業情報が見つかりません（HTTP {status}）。'}), 404
    except Exception:
        return jsonify({'success': False, 'error': '企業情報の取得に失敗しました。'}), 500


@app.route('/api/chart/<code>')
def get_chart(code):
    """Yahoo Finance チャート画像をプロキシして返す"""
    from flask import request as flask_request, Response
    if not _check_referer(flask_request):
        return jsonify({'success': False, 'error': 'アクセスが拒否されました。'}), 403
    if not _check_rate_limit(flask_request.remote_addr):
        return jsonify({'success': False, 'error': 'リクエストが多すぎます。'}), 429
    try:
        code = re.sub(r'\s', '', str(code))
        if not _validate_code(code):
            return jsonify({'success': False, 'error': '証券コードは4桁の数字を入力してください。'}), 400
        url = 'https://chart.yahoo.co.jp/?code=' + code + '.T&ct=z&t=6m&q=c&l=on&z=m&a=v'
        headers = {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/124.0.0.0 Safari/537.36'),
            'Referer': 'https://finance.yahoo.co.jp/quote/' + code + '.T/chart',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', 'image/gif')
        return Response(resp.content, content_type=content_type,
                        headers={'Cache-Control': 'public, max-age=3600'})
    except Exception:
        return jsonify({'success': False, 'error': 'チャート画像の取得に失敗しました。'}), 502


@app.route('/api/dividend_ranking')
def dividend_ranking():
    from flask import request as flask_request
    if not _check_referer(flask_request):
        return jsonify({'success': False, 'error': 'アクセスが拒否されました。'}), 403
    try:
        stocks = get_dividend_ranking(max_stocks=130)
        return jsonify({'success': True, 'stocks': stocks, 'count': len(stocks)})
    except Exception:
        return jsonify({'success': False, 'error': '銘柄リストの取得に失敗しました。'}), 500


@app.route('/api/yahoo_ranking')
def yahoo_ranking():
    from flask import request as flask_request
    if not _check_referer(flask_request):
        return jsonify({'success': False, 'error': 'アクセスが拒否されました。'}), 403
    try:
        count = flask_request.args.get('count', 300, type=int)
        count = max(50, min(count, 1000))
        stocks = fetch_yahoo_ranking(max_stocks=count)
        return jsonify({'success': True, 'stocks': stocks, 'count': len(stocks)})
    except Exception:
        return jsonify({'success': False, 'error': 'ランキングの取得に失敗しました。'}), 500


@app.route('/api/stock/<code>')
def get_stock(code):
    from flask import request as flask_request
    if not _check_referer(flask_request):
        return jsonify({'success': False, 'error': 'アクセスが拒否されました。'}), 403
    if not _check_rate_limit(flask_request.remote_addr):
        return jsonify({'success': False, 'error': 'リクエストが多すぎます。しばらくしてから再試行してください。'}), 429
    code = re.sub(r'\s', '', str(code))
    if not _validate_code(code):
        return jsonify({'success': False, 'error': '証券コードは4桁の数字を入力してください。'}), 400
    try:
        data = scrape_irbank(code)
        return jsonify({'success': True, 'data': data})
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else '?'
        return jsonify({'success': False, 'error': f'銘柄が見つかりません（HTTP {status}）。証券コードを確認してください。'}), 404
    except requests.exceptions.ConnectionError:
        return jsonify({'success': False, 'error': 'IR BANK への接続に失敗しました。インターネット接続を確認してください。'}), 503
    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'error': 'IR BANK への接続がタイムアウトしました。しばらくしてから再試行してください。'}), 504
    except Exception:
        return jsonify({'success': False, 'error': 'データの取得に失敗しました。しばらくしてから再試行してください。'}), 500


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
