from flask import Flask, render_template, jsonify, request
import yfinance as yf
import math  # 🎯 1. 必須引入這個庫，才能判斷 NaN

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stock')
def get_stock_data():
    raw_ticker = request.args.get('ticker', 'AAPL').upper()
    period_type = request.args.get('type', 'D')
    
    # 1. K 線週期參數轉換
    if period_type == 'D': period, interval = '1y', '1d'
    elif period_type == 'W': period, interval = '2y', '1wk'
    elif period_type == 'M': period, interval = '5y', '1mo'
    elif period_type == '15m': period, interval = '60d', '15m'
    elif period_type == '30m': period, interval = '60d', '30m'
    elif period_type == '60m': period, interval = '60d', '60m'
    elif period_type == '4h': period, interval = '60d', '1h'
    else: period, interval = '1y', '1d'

    pure_code = raw_ticker.split('.')[0]
    is_tw_code = pure_code.isdigit()

    df = None
    final_ticker = raw_ticker

    # 🎯 2. 動態真數據交叉打撈機制 (全面拔除台灣官方 API 與死字典)
    if is_tw_code:
        # 如果使用者輸入純數字，預設先嘗試上市格式 (.TW)
        if ".TW" not in final_ticker and ".TWO" not in final_ticker:
            final_ticker = f"{pure_code}.TW"
        try:
            print(f"📡 嘗試撈取台股渠道 A: {final_ticker}")
            stock = yf.Ticker(final_ticker)
            df = stock.history(period=period, interval=interval)
        except Exception:
            df = None

        # 🔥 如果渠道 A 失敗或沒資料（代表它其實是上櫃股），立刻無縫切換成上櫃格式 (.TWO) 撈取真數據！
        if df is None or df.empty:
            final_ticker = f"{pure_code}.TWO"
            try:
                print(f"📡 渠道 A 查無資料，自動切換台股渠道 B: {final_ticker}")
                stock = yf.Ticker(final_ticker)
                df = stock.history(period=period, interval=interval)
            except Exception:
                df = None
    else:
        # 美股處置
        stock = yf.Ticker(final_ticker)
        try:
            df = stock.history(period=period, interval=interval)
        except Exception:
            df = None

    # 🛑 堅守真實數據底線：如果 Yahoo 資料庫完全沒這檔股票，直接回報 404
    if df is None or df.empty:
        return jsonify({'error': f'Yahoo Finance 全球資料庫中查無 {pure_code} 的真實 K 線報價'}), 404

    # 🎯 3. 數據存在！開始全自動解析「市場標籤」與「公司名稱」
    try:
        # 向 Yahoo 索取這檔股票在國際市場登記的正式名稱
        raw_name = stock.info.get('longName') or stock.info.get('shortName') or f"Stock {pure_code}"
    except Exception:
        raw_name = f"Stock {pure_code}"

    # 清理名稱尾贅字 (例如 INC, CORP, LTD)，讓畫面更乾淨
    for sfx in [' CO.', ' CORP.', ' INC.', ' LTD.', ' CO,', ' LIMITED']:
        if sfx in raw_name.upper():
            idx = raw_name.upper().find(sfx)
            raw_name = raw_name[:idx]
    raw_name = raw_name.strip().title() # 轉成漂亮的首字大寫英文

    # 🎯 4. 依據最終撈到歷史資料的「後綴」，百分之百精準貼上市場分類標籤！
    if is_tw_code:
        if ".TWO" in final_ticker:
            company_name = f"[櫃] {raw_name}" # 只要是 .TWO 撈成功的，100% 絕對是上櫃/興櫃股
        else:
            company_name = f"[市] {raw_name}" # .TW 撈成功的，100% 絕對是上市股
    else:
        company_name = f"[美] {raw_name}"

    # 4小時 K 線重採樣
    if period_type == '4h':
        df = df.resample('4h').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
        }).dropna()

    # 🎯 5. 封裝技術分析 K 線陣列 (加入關鍵防禦，攔截並清洗 NaN)
    chart_data = []
    last_valid_close = None  # 保底用的前一筆有效收盤價

    for index, row in df.iterrows():
        # 讀取原始數值
        o, h, l, c, v = row['Open'], row['High'], row['Low'], row['Close'], row['Volume']

        # 🛑 核心防禦：如果 Close 欄位是 NaN，強制用上一根 K 線的收盤價遞補；若都沒有則預設為 0
        if math.isnan(c):
            c = last_valid_close if last_valid_close is not None else 0.0
        else:
            last_valid_close = c

        # 其他欄位如果也是 NaN，一律用安全遞補後的 c 覆蓋，避免出現不合法 JSON
        o = c if math.isnan(o) else o
        h = c if math.isnan(h) else h
        l = c if math.isnan(l) else l
        v = 0 if math.isnan(v) else int(v)

        time_str = index.strftime('%Y-%m-%d %H:%M') if period_type in ['15m', '30m', '60m', '4h'] else index.strftime('%Y-%m-%d')
        
        chart_data.append({
            'time': time_str,
            'open': float(o),
            'high': float(h),
            'low': float(l),
            'close': float(c),
            'volume': int(v)
        })

    return jsonify({
        'companyName': company_name,
        'tickerLabel': pure_code,
        'candles': chart_data
    })

if __name__ == '__main__':
    app.run(debug=True)