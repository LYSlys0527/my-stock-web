from flask import Flask, render_template, request, jsonify
import yfinance as yf
import pandas as pd
import math

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
    
    pure_code = ticker.split('.')[0]
    is_tw_code = pure_code.isdigit()
    
    period_map = {
        '15m': '1d', '30m': '5d', '60m': '1mo', '4h': '3mo',
        'D': '1y', 'W': '5y', 'M': 'max'
    }
    interval_map = {
        '15m': '15m', '30m': '30m', '60m': '60m', '4h': '90m',
        'D': '1d', 'W': '1wk', 'M': '1mo'
    }
    
    is_tw_4h_mode = (is_tw_code and period_type == '4h')
    
    if is_tw_4h_mode:
        p = '3mo'
        i = '60m'
    else:
        p = period_map.get(period_type, '1y')
        i = interval_map.get(period_type, '1d')
    
    stock = yf.Ticker(ticker)
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
        comp_name = stock.info.get('longName') or stock.info.get('shortName') or ticker
    except:
        comp_name = ticker
        
    candles = []
    for idx, row in df.iterrows():
        if math.isnan(row['Close']): continue
        t_str = idx.strftime('%Y-%m-%d %H:%M') if ('m' in period_type or period_type == '4h' or is_tw_4h_mode) else idx.strftime('%Y-%m-%d')
        candles.append({
            'time': t_str,
            'open': float(row['Open']), 'high': float(row['High']),
            'low': float(row['Low']), 'close': float(row['Close']),
            'volume': int(row['Volume']) if not math.isnan(row['Volume']) else 0
        })
    
    return jsonify({
        'companyName': comp_name, 'tickerLabel': ticker, 'candles': candles
    })

@app.route('/api/intraday')
def get_intraday_data():
    raw_ticker = request.args.get('ticker', 'AAPL').upper()
    pure_code = raw_ticker.split('.')[0]
    is_tw_code = pure_code.isdigit()
    
    final_ticker = f"{pure_code}.TW" if (is_tw_code and ".TW" not in raw_ticker and ".TWO" not in raw_ticker) else raw_ticker
    
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

        intraday_data.append({
            'time': index.strftime('%H:%M'),
            'price': float(c), 'volume': int(v), 'avg_price': float(avg_price)
        })
    
    # 在迴圈之後，intraday_data 已經填滿了 09:00 - 13:24 的資料
    # 我們檢查是否需要補上收盤價 (13:30)
    if is_tw_code:
        # 從 yfinance 取得今日最新價作為收盤參考
        last_price = df['Close'].iloc[-1]
        last_time = intraday_data[-1]['time']
        
        # 如果最後一筆時間還沒到 13:30，我們強行補一個點
        if last_time < "13:30":
            # 這裡簡單假設最後一筆即為收盤價，或是從 info['currentPrice'] 取得
            closing_price = stock.info.get('regularMarketPrice') or last_price
            
            intraday_data.append({
                'time': '13:30',
                'price': float(closing_price),
                'volume': 0, # 如果盤後沒有額外成交資訊，量設為 0 或忽略
                'avg_price': float(closing_price)
            })

    # 🛑 合併後的補點邏輯：只執行一次
    if is_tw_code and intraday_data:
        # 如果最後一筆時間還不到 13:30，我們統一補上一筆收盤價
        if intraday_data[-1]['time'] < "13:30":
            # 優先使用收盤價 (regularMarketPrice)，若無則沿用最後一筆成交價
            closing_price = stock.info.get('regularMarketPrice') or intraday_data[-1]['price']
            
            intraday_data.append({
                'time': '13:30',
                'price': float(closing_price),
                'volume': 0, # 試搓資料不計入成交量，避免圖表變形
                'avg_price': float(closing_price)
            })

    return jsonify({
        'marketType': 'TW' if is_tw_code else 'US',
        'prevClose': float(prev_close),
        'upLimit': float(up_limit) if up_limit else None,
        'downLimit': float(down_limit) if down_limit else None,
        'totalVolume': int(total_volume),
        'points': intraday_data
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)