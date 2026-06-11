from flask import Flask, render_template, jsonify, request
import yfinance as yf

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/stock')
def get_stock_data():
    ticker = request.args.get('ticker', 'AAPL').upper()
    period_type = request.args.get('type', 'D')
    
    if period_type == 'W':
        interval, period = '1wk', '3y'
    elif period_type == 'M':
        interval, period = '1mo', '10y'
    else:
        interval, period = '1d', '1y'
        
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period=period, interval=interval).dropna()
        
        if df.empty:
            return jsonify({"error": "查無數據"}), 404
        
        chart_data = []
        for index, row in df.iterrows():
            chart_data.append({
                "time": index.strftime('%Y-%m-%d'),
                "open": round(float(row['Open']), 2),
                "high": round(float(row['High']), 2),
                "low": round(float(row['Low']), 2),
                "close": round(float(row['Close']), 2),
                "volume": int(row['Volume']) # 確保將成交量傳給前端
            })
        return jsonify(chart_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)