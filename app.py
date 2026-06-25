from flask import Flask, render_template, request, jsonify
import yfinance as yf
import pandas as pd
import math
import re
import requests

# ── 台股中文名稱字典 ─────────────────────────────────────────────────────────
# 啟動時一次性從 TWSE（上市）與 TPEX（上櫃）官方 API 批次載入所有公司名稱
tw_names_cache = {}

def _load_tw_names():
    """
    從 TWSE 與 TPEX 官方 API 下載完整上市/上櫃公司名稱對照表，
    存入全域 tw_names_cache。失敗時靜默略過，不影響其他功能。
    """
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.twse.com.tw/'
    }

    # 1. 台灣證券交易所（TWSE）上市公司列表
    try:
        url_twse = 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL'
        r = requests.get(url_twse, headers=headers, timeout=10)
        if r.status_code == 200:
            for item in r.json():
                code = item.get('Code', '').strip()
                name = item.get('Name', '').strip()
                if code and name:
                    tw_names_cache[code] = name
    except Exception:
        pass

    # 2. 台灣證券交易所（TWSE）ETF / 其他完整清單
    try:
        url_twse2 = 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L'
        r = requests.get(url_twse2, headers=headers, timeout=10)
        if r.status_code == 200:
            for item in r.json():
                code = item.get('公司代號', '').strip()
                name = item.get('公司簡稱', '').strip() or item.get('公司名稱', '').strip()
                if code and name:
                    tw_names_cache[code] = name
    except Exception:
        pass

    # 3. 櫃買中心（TPEX）上櫃公司列表
    try:
        url_tpex = 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes'
        r = requests.get(url_tpex, headers=headers, timeout=10)
        if r.status_code == 200:
            for item in r.json():
                code = item.get('SecuritiesCompanyCode', '').strip()
                name = item.get('CompanyAbbreviation', '').strip()
                if code and name:
                    tw_names_cache[code] = name
    except Exception:
        pass

    # 4. 若以上均空（離線環境），退回手工常用對照表
    if not tw_names_cache:
        tw_names_cache.update({
            '2330': '台積電', '2317': '鴻海', '2454': '聯發科',
            '0050': '元大台灣50', '0056': '元大高股息', '2303': '聯電',
            '2881': '富邦金', '2882': '國泰金', '2603': '長榮',
            '2609': '陽明', '2615': '萬海', '2382': '廣達',
            '3231': '緯創', '2357': '華碩', '3008': '大立光',
            '2412': '中華電', '1301': '台塑', '1303': '南亞',
            '2886': '兆豐金', '2891': '中信金',
        })

# 啟動時立即載入
_load_tw_names()

def get_tw_chinese_name(code):
    """查詢台股中文名稱；快取命中直接回傳，否則回傳 None。"""
    code = str(code).upper().split('.')[0]
    return tw_names_cache.get(code)

# Parse stock futures ticker
def parse_stock_futures(ticker):
    ticker = ticker.upper().split('.')[0]
    m = re.match(r'^(\d+)(F(\d*))$', ticker)
    if m:
        underlying = m.group(1)
        suffix_num = m.group(3)
        suffix = "遠月" if (suffix_num and suffix_num != "1") else "近月"
        return True, underlying, suffix
    return False, None, None

app = Flask(__name__)

# 精確計算台股跳動檔位（Tick Size）後的漲跌停價
def calc_tw_limit(prev_close):
    # 預估原始漲跌停價 (上下 10%)
    raw_up = prev_close * 1.10
    raw_down = prev_close * 0.90
    
    # 依台股現行法規精確修正跳動檔位
    up_limit = round_tw_tick(raw_up, is_up=True)
    down_limit = round_tw_tick(raw_down, is_up=False)
    return up_limit, down_limit

def round_tw_tick(price, is_up):
    # 判斷台股六大價格級距的跳動規格
    if price < 10: tick = 0.01
    elif price < 50: tick = 0.05
    elif price < 100: tick = 0.1
    elif price < 500: tick = 0.5
    elif price < 1000: tick = 1.0
    else: tick = 5.0
    
    # 台灣證券交易所規定：漲停不能超過 +10% (向下取最接近檔位)，跌停不能低於 -10% (向上取最接近檔位)
    if is_up:
        return math.floor(price / tick + 0.0001) * tick
    else:
        return math.ceil(price / tick - 0.0001) * tick

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stock')
def get_stock_data():
    ticker = request.args.get('ticker', 'AAPL').upper()
    period_type = request.args.get('type', 'D')
    
    # Check if stock future
    is_future, underlying, suffix = parse_stock_futures(ticker)
    
    if is_future:
        fetch_ticker = f"{underlying}.TW"
    elif ticker == 'TXF=F':
        fetch_ticker = '^TWII'
    else:
        fetch_ticker = ticker
        
    pure_code = fetch_ticker.split('.')[0]
    is_tw_code = pure_code.isdigit()
    
    period_map = {
        '15m': '60d', '30m': '60d', '60m': '730d', '4h': '730d',
        'D': 'max', 'W': 'max', 'M': 'max'
    }
    interval_map = {
        '15m': '15m', '30m': '30m', '60m': '60m', '4h': '90m',
        'D': '1d', 'W': '1wk', 'M': '1mo'
    }
    
    is_tw_4h_mode = (is_tw_code and period_type == '4h')
    
    if is_tw_4h_mode:
        p = '730d'
        i = '60m'
    else:
        p = period_map.get(period_type, '1y')
        i = interval_map.get(period_type, '1d')
    
    stock = yf.Ticker(fetch_ticker)
    df = stock.history(period=p, interval=i)
    
    if (df is None or df.empty) and is_tw_code and ".TW" in fetch_ticker:
        fetch_ticker = f"{pure_code}.TWO"
        stock = yf.Ticker(fetch_ticker)
        df = stock.history(period=p, interval=i)
        
    if df is None or df.empty:
        return jsonify({'error': '查無數據'}), 404
        
    if is_tw_4h_mode:
        try:
            if df.index.tz is not None:
                df = df.to_period(freq='Min').to_timestamp()
            resampled = df.resample('240Min', origin='start_day', offset='9H').agg({
                'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
            })
            df = resampled.dropna(subset=['Close'])
        except Exception as ex:
            df = stock.history(period='1y', interval='1d')
            period_type = 'D'

    try:
        if ticker == 'TXF=F':
            comp_name = '臺指期'
        elif is_future:
            underlying_name = get_tw_chinese_name(underlying) or stock.info.get('longName') or stock.info.get('shortName') or underlying
            comp_name = f"{underlying_name.split(' (')[0]}期貨{suffix}"
        else:
            comp_name = None
            if is_tw_code:
                comp_name = get_tw_chinese_name(pure_code)
            if not comp_name:
                comp_name = stock.info.get('longName') or stock.info.get('shortName') or ticker
    except:
        if ticker == 'TXF=F':
            comp_name = '臺指期'
        elif is_future:
            comp_name = f"{underlying}期貨{suffix}"
        else:
            comp_name = ticker
        
    candles = []
    for idx, row in df.iterrows():
        if math.isnan(row['Close']): continue
        t_str = idx.strftime('%Y-%m-%d %H:%M') if ('m' in period_type or period_type == '4h' or is_tw_4h_mode) else idx.strftime('%Y-%m-%d')
        
        # Adjust K-line prices for simulated futures
        c = float(row['Close'])
        o = float(row['Open'])
        h = float(row['High'])
        l = float(row['Low'])
        if is_future:
            multiplier = 1.001 if suffix == "近月" else 0.998
            c *= multiplier
            o *= multiplier
            h *= multiplier
            l *= multiplier
            
        candles.append({
            'time': t_str,
            'open': o, 'high': h,
            'low': l, 'close': c,
            'volume': int(row['Volume']) if not math.isnan(row['Volume']) else 0
        })
    
    return jsonify({
        'companyName': comp_name, 'tickerLabel': ticker, 'candles': candles
    })

@app.route('/api/intraday')
def get_intraday_data():
    raw_ticker = request.args.get('ticker', 'AAPL').upper()
    
    # Check if stock future
    is_future, underlying, suffix = parse_stock_futures(raw_ticker)
    
    if is_future:
        fetch_ticker = f"{underlying}.TW"
    elif raw_ticker == 'TXF=F':
        fetch_ticker = '^TWII'
    else:
        fetch_ticker = raw_ticker
        
    pure_code = fetch_ticker.split('.')[0]
    is_tw_code = pure_code.isdigit()
    
    final_ticker = f"{pure_code}.TW" if (is_tw_code and ".TW" not in fetch_ticker and ".TWO" not in fetch_ticker) else fetch_ticker
    
    stock = yf.Ticker(final_ticker)
    df = stock.history(period='1d', interval='1m')
    
    if (df is None or df.empty) and is_tw_code:
        final_ticker = f"{pure_code}.TWO"
        stock = yf.Ticker(final_ticker)
        df = stock.history(period='1d', interval='1m')
        
    if df is None or df.empty:
        return jsonify({'error': '查無今日即時分時數據'}), 404
        
    try:
        info = stock.info
        prev_close = info.get('previousClose')
    except Exception:
        info = {}
        prev_close = None
        
    if not prev_close:
        prev_close = df['Open'].iloc[0] if not df.empty else 0.0

    # 🎯 核心防禦：如果是台股，直接抹除 yfinance 似是而非的 marketHigh 欄位，強制啟動精確模擬
    if is_tw_code:
        up_limit, down_limit = calc_tw_limit(prev_close)
    else:
        up_limit = info.get('upper_limit') or info.get('regularMarketDayHigh')
        down_limit = info.get('lower_limit') or info.get('regularMarketDayLow')
        if not info.get('upper_limit'):
            up_limit, down_limit = None, None

    intraday_data = []
    total_volume = 0
    total_turnover = 0

    for index, row in df.iterrows():
        c = row['Close']
        v = row['Volume']
        if math.isnan(c): continue
        
        v = 0 if math.isnan(v) else int(v)
        total_volume += v
        total_turnover += c * v
        avg_price = (total_turnover / total_volume) if total_volume > 0 else c

        # Adjust price for simulated stock futures
        adjusted_price = c
        adjusted_avg = avg_price
        if is_future:
            multiplier = 1.001 if suffix == "近月" else 0.998
            adjusted_price = c * multiplier
            adjusted_avg = avg_price * multiplier

        intraday_data.append({
            'time': index.strftime('%H:%M'),
            'price': float(adjusted_price), 'volume': int(v), 'avg_price': float(adjusted_avg)
        })
    
    # 在迴圈之後，intraday_data 已經填滿了 09:00 - 13:24 的資料
    # 我們檢查是否需要補上收盤價 (13:30)
    if is_tw_code and intraday_data:
        # 如果最後一筆時間還不到 13:30，我們統一補上一筆收盤價
        if intraday_data[-1]['time'] < "13:30":
            # 優先使用收盤價 (regularMarketPrice)，若無則沿用最後一筆成交價
            closing_price = stock.info.get('regularMarketPrice') or df['Close'].iloc[-1]
            if is_future:
                multiplier = 1.001 if suffix == "近月" else 0.998
                closing_price = closing_price * multiplier
            
            intraday_data.append({
                'time': '13:30',
                'price': float(closing_price),
                'volume': 0, # 試搓資料不計入成交量，避免圖表變形
                'avg_price': float(closing_price)
            })

    is_tw_mkt = is_tw_code or final_ticker.endswith('.TW') or final_ticker.endswith('.TWO') or final_ticker in ['^TWII', '^TWOII', 'TXF=F']

    # Adjust up/down limits for futures if needed
    if is_future and up_limit and down_limit:
        multiplier = 1.001 if suffix == "近月" else 0.998
        up_limit = up_limit * multiplier
        down_limit = down_limit * multiplier

    return jsonify({
        'marketType': 'TW' if is_tw_mkt else 'US',
        'prevClose': float(prev_close * (1.001 if suffix == "近月" else 0.998)) if (is_future and prev_close) else float(prev_close),
        'upLimit': float(up_limit) if up_limit else None,
        'downLimit': float(down_limit) if down_limit else None,
        'totalVolume': int(total_volume),
        'points': intraday_data
    })

@app.route('/api/revenue')
def get_revenue_data():
    raw_ticker = request.args.get('ticker', 'AAPL').upper()
    pure_code = raw_ticker.split('.')[0]
    is_tw_code = pure_code.isdigit()
    
    final_ticker = f"{pure_code}.TW" if (is_tw_code and ".TW" not in raw_ticker and ".TWO" not in raw_ticker) else raw_ticker
    
    try:
        stock = yf.Ticker(final_ticker)
        df = stock.quarterly_financials
        
        # 嘗試取得櫃買市場數據 (若無上市數據)
        if (df is None or df.empty) and is_tw_code and ".TW" in final_ticker:
            final_ticker = f"{pure_code}.TWO"
            stock = yf.Ticker(final_ticker)
            df = stock.quarterly_financials
            
        # 若仍無季營收，則改拿年營收
        if df is None or df.empty:
            df = stock.financials
            
        if df is None or df.empty:
            return jsonify({'error': '無法取得此股票的財務營收數據'}), 404
            
        # 尋找營收列 (Total Revenue 或 Revenue)
        revenue_row = None
        for idx in df.index:
            if 'revenue' in str(idx).lower():
                revenue_row = df.loc[idx]
                break
                
        # 尋找淨利列 (Net Income)
        net_income_row = None
        for idx in df.index:
            if 'net income' in str(idx).lower():
                net_income_row = df.loc[idx]
                break
                
        data = []
        if revenue_row is not None:
            for col in df.columns:
                date_str = col.strftime('%Y-%m-%d') if hasattr(col, 'strftime') else str(col)
                rev_val = revenue_row[col]
                net_val = net_income_row[col] if net_income_row is not None else 0.0
                
                # 防禦 nan 數據
                if isinstance(rev_val, float) and math.isnan(rev_val): rev_val = 0.0
                if isinstance(net_val, float) and math.isnan(net_val): net_val = 0.0
                
                data.append({
                    'date': date_str,
                    'revenue': float(rev_val) if rev_val is not None else 0.0,
                    'netIncome': float(net_val) if net_val is not None else 0.0
                })
                
        # 轉為時間正序
        data.reverse()
        
        comp_name = None
        if is_tw_code:
            comp_name = get_tw_chinese_name(pure_code)
        if not comp_name:
            comp_name = stock.info.get('longName') or stock.info.get('shortName') or final_ticker
            
        return jsonify({
            'companyName': comp_name,
            'tickerLabel': final_ticker,
            'financials': data
        })
    except Exception as e:
        return jsonify({'error': f'伺服器錯誤: {str(e)}'}), 500

@app.route('/api/futures')
def get_futures_data():
    # Use ^TWII as a data proxy for TAIEX futures (TXF=F)
    tickers_query_map = {
        '^TWII': '臺指期',
        'YM=F': '道瓊期',
        'ES=F': '標普期',
        'NQ=F': '那指期'
    }
    data = []
    try:
        tickers_str = " ".join(tickers_query_map.keys())
        tickers = yf.Tickers(tickers_str)
        for symbol, name in tickers_query_map.items():
            try:
                t = tickers.tickers[symbol]
                df = t.history(period='1d')
                if df is not None and not df.empty:
                    last_row = df.iloc[-1]
                    close_val = last_row['Close']
                    prev_close = t.info.get('previousClose') or last_row['Open']
                    change = close_val - prev_close
                    pct_change = (change / prev_close) * 100 if prev_close else 0.0
                    
                    out_symbol = 'TXF=F' if symbol == '^TWII' else symbol
                    data.append({
                        'symbol': out_symbol,
                        'name': name,
                        'price': float(close_val),
                        'change': float(change),
                        'pctChange': float(pct_change)
                    })
                else:
                    out_symbol = 'TXF=F' if symbol == '^TWII' else symbol
                    data.append({'symbol': out_symbol, 'name': name, 'price': 0.0, 'change': 0.0, 'pctChange': 0.0})
            except Exception:
                out_symbol = 'TXF=F' if symbol == '^TWII' else symbol
                data.append({'symbol': out_symbol, 'name': name, 'price': 0.0, 'change': 0.0, 'pctChange': 0.0})
        return jsonify({'futures': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/watchlist_prices')
def get_watchlist_prices():
    tickers_raw = request.args.get('tickers', '')
    if not tickers_raw:
        return jsonify({'stocks': []})
    
    ticker_list = [t.strip().upper() for t in tickers_raw.split(',') if t.strip()]
    data = []
    try:
        # Build raw tickers to fetch (resolving TXF=F and Stock Futures)
        fetch_tickers = []
        parsed_mapping = {} # maps raw_symbol to (fetch_symbol, is_future, suffix)
        
        for symbol in ticker_list:
            is_future, underlying, suffix = parse_stock_futures(symbol)
            if is_future:
                fetch_sym = f"{underlying}.TW"
                parsed_mapping[symbol] = (fetch_sym, True, suffix)
            elif symbol == 'TXF=F':
                fetch_sym = '^TWII'
                parsed_mapping[symbol] = (fetch_sym, False, "")
            else:
                fetch_sym = symbol
                parsed_mapping[symbol] = (fetch_sym, False, "")
            fetch_tickers.append(fetch_sym)
            
        tickers_str = " ".join(list(set(fetch_tickers)))
        tickers = yf.Tickers(tickers_str)
        for symbol in ticker_list:
            try:
                fetch_sym, is_future, suffix = parsed_mapping[symbol]
                t = tickers.tickers[fetch_sym]
                df = t.history(period='1d')
                if df is not None and not df.empty:
                    last_row = df.iloc[-1]
                    close_val = last_row['Close']
                    prev_close = t.info.get('previousClose') or last_row['Open']
                    
                    if is_future:
                        multiplier = 1.001 if suffix == "近月" else 0.998
                        close_val *= multiplier
                        prev_close *= multiplier
                    
                    change = close_val - prev_close
                    pct_change = (change / prev_close) * 100 if prev_close else 0.0
                    
                    if symbol == 'TXF=F':
                        comp_name = '臺指期'
                    elif is_future:
                        underlying_name = t.info.get('longName') or t.info.get('shortName') or symbol.split('F')[0]
                        comp_name = f"{underlying_name.split(' (')[0]}期貨{suffix}"
                    else:
                        comp_name = t.info.get('longName') or t.info.get('shortName') or symbol
                    
                    # Calculate Taiwan limit prices if applicable
                    is_tw = fetch_sym.endswith('.TW') or fetch_sym.endswith('.TWO')
                    up_limit = None
                    down_limit = None
                    if is_tw and prev_close:
                        up_limit, down_limit = calc_tw_limit(prev_close)
                        
                    data.append({
                        'symbol': symbol,
                        'name': comp_name,
                        'price': float(close_val),
                        'change': float(change),
                        'pctChange': float(pct_change),
                        'upLimit': float(up_limit) if up_limit else None,
                        'downLimit': float(down_limit) if down_limit else None
                    })
                else:
                    data.append({'symbol': symbol, 'name': symbol, 'price': 0.0, 'change': 0.0, 'pctChange': 0.0, 'upLimit': None, 'downLimit': None})
            except Exception:
                data.append({'symbol': symbol, 'name': symbol, 'price': 0.0, 'change': 0.0, 'pctChange': 0.0, 'upLimit': None, 'downLimit': None})
        return jsonify({'stocks': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)