#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
�芥�憭扳� 擃�敶�詨��� - ���胯�喋��萸�
IR BANK ���芸���踴��芸�����嫘�Ｙ��箝��整�

雿踴���:
  1. pip install flask flask-cors requests beautifulsoup4
  2. python server.py
  3. ��艾�� http://localhost:5000 ����
"""

import re
import sys
import os
import webbrowser
import threading

# --------------------------------------------------
# 靘����晞�詻�芸��扎�嫘��潦
# --------------------------------------------------
def install_if_missing(packages):
    import importlib
    for pkg, import_name in packages:
        try:
            importlib.import_module(import_name)
        except ImportError:
            print(f"� {pkg} ��喋��思葉...")
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
#  �扎��潦��  ��� �� float
# ==================================================
def parse_value(text):
    if not text:
        return None
    text = re.sub(r'[,\s��*]', '', str(text)).strip()
    if text in ['-', '', '鈭�', '��', '嚗�', '--']:
        return None

    m = re.search(r'^(-?[\d.]+)��$', text)
    if m:
        return float(m.group(1)) * 1e12

    m = re.search(r'^(-?[\d.]+)��$', text)
    if m:
        return float(m.group(1)) * 1e8

    m = re.search(r'^(-?[\d.]+)��$', text)
    if m:
        return float(m.group(1))

    m = re.search(r'^(-?\\d.]+)%$', text)
    if m:
        return float(m.group(1))

    m = re.search(r'^(-?[\d.]+)$', text)
    if m:
        return float(m.group(1))

    return None


# ==================================================
#  ��喋�閮�
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
    �嗆平CF撠: 韏文��� + ��喋�
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
    ������: 皜��� + ��喋�
    returns: 'stable_growing' / 'stable' / 'has_cut' / 'unknown'
    """
    valid = [(y, v) for y, v in pairs if v is not None and v > 0]
    if len(valid) < 2:
        return 'unknown'
    recent10 = valid[-10:]
    for i in range(1, len(recent10)):
        if recent10[i][1] < recent10[i - 1][1] * 0.95:   # 5%頞皜�
            return 'has_cut'
    trend = calc_trend(pairs)
    return 'stable_growing' if trend == 'up' else 'stable'


# ==================================================
#  �������潦���
# ==================================================
def extract_column(table, col_keyword):
    """
    table: BeautifulSoup table element
    col_keyword: ����潦�怒���准�胯��
    returns: [(year_str, float_or_None), ...]  鈭葫銵�文�
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
        # 鈭葫銵瘜券�銵��文�
        if '鈭�' in year or '��' in year or not re.search(r'\d{4}', year):
            continue
        val = parse_value(cells[col_idx].get_text(strip=True))
        rows.append((year, val))
    return rows


def last_valid(pairs):
    """�渲��格��孵扎�餈�"""
    for _, v in reversed(pairs):
        if v is not None:
            return v
    return None


# ==================================================
#  IR BANK �嫘�研��唳雿�
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

    # ---- ���� ----
    company_name = ''
    h1 = soup.find('h1')
    if h1:
        raw = h1.get_text(strip=True)
        m = re.match(r'^\d+\s+(.+)$', raw)
        company_name = m.group(1) if m else raw

    # ---- �� ----
    tables = soup.find_all('table')
    if len(tables) < 4:
        raise ValueError(
            f"���� {len(tables)} �����扎������"
            "閮澆�喋��蝣箄��������"
        )

    t0, t1, t2, t3 = tables[0], tables[1], tables[2], tables[3]

    # ---- ���拙����萸��芥�� dt/dd 瑽���敺�----
    # IR BANK�桃����潦�怒 <dt>�� 鈭�</dt><dd>3.1%</dd> �具��耦�批����頛���
    dividend_yield = None
    for dt in soup.find_all('dt'):
        dt_text = dt.get_text(strip=True)
        if ('��' in dt_text
                and '���批�' not in dt_text
                and '����' not in dt_text
                and '����' not in dt_text):
            dd = dt.find_next_sibling('dd')
            if dd:
                m = re.search(r'([\d.]+)%', dd.get_text(strip=True))
                if m:
                    v = float(m.group(1))
                    if 0 < v < 20:   # �啣虜�日憭�
                        dividend_yield = round(v, 2)
                        break

    # �� 憯脖�擃���嚗�
    sales = extract_column(t0, '��') or extract_column(t0, '憯脖�')
    sales_trend = calc_trend(sales)

    # �� EPS
    eps = extract_column(t0, 'EPS')
    eps_trend = calc_trend(eps)

    # �� �嗆平�拍���
    margin = extract_column(t0, '�嗅��')
    operating_margin = last_valid(margin)
    if operating_margin is not None:
        operating_margin = round(operating_margin, 1)

    # �� �芸楛鞈瘥�嚗R BANK�扼�撌梯��祆���嚗�
    equity = extract_column(t1, '�芸暘X��祆���') or extract_column(t1, '�芯蜓鞈瘥�')
    equity_ratio = last_valid(equity)
    if equity_ratio is not None:
        equity_ratio = round(equity_ratio, 1)

    # �� �嗆平CF
    ocf = extract_column(t2, '�嗆平CF')
    operating_cf = cf_status(ocf)

    # �� �暸�蝑�
    cash = extract_column(t2, '�暸�蝑�')
    cash_trend = calc_trend(cash)

    # �� 1�芷�敶�
    div = extract_column(t3, '銝�芷�敶�')
    dividend_trend = dividend_status(div)

    # �� ���批�
    payout = extract_column(t3, '���批�')
    payout_ratio = last_valid(payout)
    if payout_ratio is not None:
        payout_ratio = round(payout_ratio, 1)

    return {
        'name': company_name,
        'code': code,
        'dividend_yield': dividend_yield,
        # ��喋�蝟� (up/flat/down/unknown)
        'sales_trend': sales_trend,
        'eps_trend': eps_trend,
        # �啣斤頂
        'operating_margin': operating_margin,
        'equity_ratio': equity_ratio,
        # CF
        'operating_cf': operating_cf,   # up_positive/positive/has_negative/unknown
        'cash_trend': cash_trend,
        # ��
        'dividend_trend': dividend_trend,   # stable_growing/stable/has_cut/unknown
        'payout_ratio': payout_ratio,
        # �����
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
#  擃�敶����芥���准�研�瑯�單��輻�130��嚗�
# ==================================================
# 憭�萸��柴�胯�扎��喋�臭�摰��芥���
# �交撣�折����具��衣���誨銵函��芷�����芥��靽���
_CANDIDATE_STOCKS = [
    # �冗
    ('8058','銝��'),('8031','銝��拍'),('8053','雿���'),
    ('8001','隡敹�鈭�'),('8002','銝貊�'),('8015','鞊��'),
    ('4768','�'),('8088','撗抵健��平'),('8081','�怒��'),
    # �縑
    ('9432','NTT'),('9433','KDDI'),('9434','�賬����喋'),
    # �銵��
    ('8316','銝�雿�FG'),('8306','銝UFJ FG'),('8411','�踴��肇G'),
    ('7182','���～��銵�'),('8354','�賬���FG'),('8473','SBI HD'),
    ('8253','�胯��颯��'),('8309','銝�雿���嫘�HD'),
    ('7186','�喋�喋��￠G'),('8327','撅勗FG'),
    ('7181','���賜���'),('8308','���杳D'),
    # 靽
    ('8725','MS&AD�扎�瑯�Ｕ�喋G'),('8766','�曹漪瘚瑚�HD'),
    ('8750','蝚砌��HD'),('8630','SOMPO��怒����啜'),
    # 閮澆
    ('8601','憭批�閮澆G�祉冗'),('8604','��HD'),('8628','�曆�閮澆'),
    ('8698','����逼'),('7164','�典靽釆'),
    # �具��怒�潦鞈�
    ('1605','INPEX'),('5019','�箏��'),('5020','ENEOS��怒����啜'),
    # ��駁�撅�
    ('5401','�交鋆賡�'),('5411','JFE��怒����啜'),
    ('5713','雿����勗控'),('3436','SUMCO'),
    # �郎
    ('4063','靽∟��郎撌交平'),('4005','雿��郎'),('4188','銝�晞��怒G'),
    ('4183','銝��郎'),('4021','�亦�郎'),('3407','�剖���'),
    ('4612','�交��喋�HD'),
    # 蝝���
    ('3861','��HD'),('3863','�交鋆賜�'),('3105','�交�蝝？D'),
    # 瘚琿�嚗��擃�嚗�
    ('9101','�交�菔'),('9104','�銝�'),('9107','撌�瘙質'),
    # �賊��餌瘚�
    ('9064','�扎��D'),('9062','�交��'),('9006','鈭祆仿��'),
    # �餃��潦��
    ('9501','�曹漪�餃�HD'),('9502','銝剝�餃�'),('9503','�Ｚ正�餃�'),
    ('9531','�曹漪�研'),('9532','憭折�研'),
    # 憌��駁ㄡ����
    ('2914','JT'),('2502','�Ｕ�HD'),('2503','�准�蚵D'),
    ('2282','�交��'),('2269','�祥HD'),('2801','�准��喋�'),
    ('2587','�萸��劉F'),('1332','���嫘'),('1301','璆菜�'),
    # �餉��
    ('4502','甇衣�砍�撌交平'),('4519','銝剖�鋆質'),('4568','蝚砌�銝'),
    ('4523','�具�嗚'),('4507','憛拚�蝢抵ˊ��'),('4543','���'),
    # �芸�頠頛賊���
    ('7203','��輯��'),('7267','�祉��極璆�'),('7270','SUBARU'),
    ('7269','�嫘��'),('7272','�扎����'),('6902','DENSO'),
    ('7202','����'),('7201','�亦�芸�頠�'),('7012','撌��極璆�'),
    # �餅��餅�璇�
    ('6501','�亦�鋆賭��'),('6503','銝�餅�'),('6752','���賬��HD'),
    ('6301','�喋���'),('6326','�胯���'),('7011','銝�極璆�'),
    ('7751','�准�'),('6724','�颯�喋�具��賬'),('6702','撖ㄚ��'),
    ('6723','�怒��萸�具�胯��准��胯'),('6857','�Ｕ�����'),
    ('6981','�鋆賭��'),('4901','撖ㄚ��怒�HD'),
    ('6503','銝�餅�'),('4307','��蝺��弦�'),
    # 銝���撱箄身
    ('8802','銝�唳�'),('8804','�望乩��HD'),('8830','雿�銝���'),
    ('8801','銝�銝���'),('1928','蝛偌���'),('1925','憭批���孵極璆�'),
    ('3003','��潦�'),('1802','憭扳�蝯�'),('1803','皜偌撱箄身'),
    ('1801','憭扳�撱箄身'),('1812','暽踹雀撱箄身'),
    # 撠ㄡ�颯�潦���
    ('8267','�扎��'),('3382','�颯���&�ＵHD'),('2651','�准�賬'),
    ('3099','銝�隡銝違D'),('9843','���杳D'),
    # 蝝��颯��桐�鋆賡�
    ('4452','�梁�'),('4911','鞈���'),('5802','雿��餅�撌交平'),
    ('9101','�交�菔'),('6703','OKI'),
    ('8096','�潭�具�胯��准��胯'),
    # �芰征�颱漱��
    ('5202','ANA��怒����啜'),('5201','�交�芰征'),
    # �扎�踴���IT
    ('6098','�芥�怒�D'),('9984','�賬����喋G'),
    # �隞����具��西店憿��
    ('8252','銝訾�G'),('5943','��芥�'),('7014','����'),
    ('8075','蟡��'),('5901','�望�鋆賜�GHD'),
    ('9783','���HD'),('4816','�望��Ｕ��～�瑯��'),
]

def get_dividend_ranking(max_stocks=120):
    """擃�敶����芥��餈�嚗��萸�乓�潦�扼皜�芥��"""
    seen = set()
    result = []
    for code, name in _CANDIDATE_STOCKS:
        if code not in seen:
            seen.add(code)
            result.append({'code': code, 'name': name})
    return result[:max_stocks]


def fetch_yahoo_ranking(max_stocks=300):
    """
    �扎��潦��～��嫘���拙���喋�喋�����喋��������
    �憭� max_stocks 隞嗅�敺��西���4獢閮澆�喋��踵�綽���
    憭望�� _CANDIDATE_STOCKS �怒��押�怒����
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
    # 1��詻���蝝�47����閬��潦�啜����怨�蝞����+3��賂�
    import math
    max_pages = min(math.ceil(max_stocks / 47) + 3, 100)  # �憭�100���
    consecutive_empty = 0  # ���蝛箝��潦�怒�喋�

    for page in range(1, max_pages + 1):
        if len(result) >= max_stocks:
            break
        try:
            url = base_url.format(page)
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')

            # 閮澆�喋� <li class="*supplement*"> ��4獢摮��乓�
            codes_found = 0
            for li in soup.find_all('li', class_=lambda c: c and 'supplement' in c):
                text = li.get_text(strip=True)
                if re.match(r'^\d{4}$', text) and text not in seen:
                    seen.add(text)
                    result.append({'code': text, 'name': ''})
                    codes_found += 1
                    if len(result) >= max_stocks:
                        break

            print(f"  Yahoo Finance p{page}/{max_pages}: {codes_found}隞嗅�敺�蝝航�{len(result)}隞塚�")

            if codes_found == 0:
                consecutive_empty += 1
                print(f"  p{page} �扼��潦�芥�嚗��{consecutive_empty}��")
                if consecutive_empty >= 2:
                    # 2��賊���扼��潦�芥� �� �押�准�啁�蝡胯�踴��
                    print("  �押�准�啁�蝡胯���整��� �� 蝯�")
                    break
            else:
                consecutive_empty = 0  # ��踴����啜�颯���

        except Exception as e:
            print(f"  Yahoo Finance p{page} ��憭望�: {e}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break

    if not result:
        # ��潦���荔���芥��雿輻
        print("  Yahoo Finance ��憭望� �� ��芥���潦����")
        return get_dividend_ranking(max_stocks)

    return result


# ==================================================
#  Flask �怒��喋
# ==================================================
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, '擃�敶�詨���.html')


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
        count = max(50, min(count, 2000))   # 50��2000�桃��脯�園�
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
            'error': f'�����扎����嚗TTP {status}嚗釆�詻�潦��Ⅱ隤��艾�����'
        }), 404

    except requests.exceptions.ConnectionError:
        return jsonify({
            'success': False,
            'error': 'IR BANK �詻�亦��怠仃���整���喋�潦����亦��Ⅱ隤��艾�����'
        }), 503

    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'error': 'IR BANK �詻�亦���扎��Ｕ���整����啜����艾���閰西��������'
        }), 504

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================================================
#  韏瑕�
# ==================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    url = f'http://localhost:{port}'

    print()
    print('=' * 52)
    print('  �� �芥�憭扳� 擃�敶�詨���')
    print('=' * 52)
    print(f'  �� URL  : {url}')
    print('  �� 蝯� : Ctrl+C')
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
