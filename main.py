import asyncio
import os
import sys
import io
import json
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
import matplotlib
matplotlib.use('Agg') 
import mplfinance as mpf
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from aiogram import Bot, types
from aiogram.enums import ParseMode
from dotenv import load_dotenv

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
GOOGLE_JSON = os.getenv("GOOGLE_SHEETS_JSON") 

TG_CHANNEL_ID = "-1003738958585"
SYMBOLS = ['BTC/USDT']
TIMEFRAME_PAIRS = [{'work': '1h', 'filter': '4h'}, {'work': '15m', 'filter': '4h'}, {'work': '5m', 'filter': '1h'}]

# Параметры стратегии
MIN_TARGET_PCT = 0.008   
MAX_SL_PCT = 0.018       
ATR_MULT_SL, ATR_MULT_TP = 1.8, 3.5

class TradingBot:
    def __init__(self):
        self.exchange = ccxt.bybit({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
        self.bot = Bot(token=TG_TOKEN)
        self.processed_signals = set()
        self.active_trades = [] 
        self.sheet = None
        self._connect_google()

    def _connect_google(self):
        if GOOGLE_JSON:
            try:
                creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_JSON), ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
                self.sheet = gspread.authorize(creds).open("BTC_Signals_Log").sheet1
                print("✅ Google Sheet подключена!", flush=True)
            except Exception as e: print(f"❌ Ошибка Google Sheet: {e}", flush=True)

    async def fetch_data(self, symbol, timeframe, limit=150):
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except: return None

    def calculate_indicators(self, df):
        # Важно: EMA 200 требует много данных, поэтому limit=150 в fetch_data
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.ema(length=200, append=True)
        df.ta.adx(length=14, append=True)
        df.ta.rsi(length=14, append=True)
        df.ta.atr(length=14, append=True)
        return df

    async def track_results(self, current_price):
        """Проверка активных сделок"""
        if not self.active_trades: return
        
        for trade in self.active_trades[:]:
            side = trade['side']
            tp, sl = trade['tp'], trade['sl']
            
            is_tp = (side == 'LONG' and current_price >= tp) or (side == 'SHORT' and current_price <= tp)
            is_sl = (side == 'LONG' and current_price <= sl) or (side == 'SHORT' and current_price >= sl)
            
            if is_tp or is_sl:
                result_emoji = "✅ Take Profit!" if is_tp else "❌ Stop Loss"
                pnl = f"+{trade['target_pct']}%" if is_tp else f"-{trade['risk_pct']}%"
                
                msg = (f"🏁 <b>Сделка завершена</b>\n"
                       f"ID: {trade['id']}\n"
                       f"Результат: {result_emoji}\n"
                       f"Итог: <b>{pnl}</b>")
                
                await self.bot.send_message(chat_id=TG_CHANNEL_ID, text=msg, parse_mode=ParseMode.HTML)
                self.active_trades.remove(trade)
                print(f"📉 Закрыто {trade['id']}: {pnl}", flush=True)

    async def analyze_pair(self, symbol, tf_p):
        dw = await self.fetch_data(symbol, tf_p['work'])
        df = await self.fetch_data(symbol, tf_p['filter'])
        if dw is None or df is None: return False
        
        dw = self.calculate_indicators(dw)
        df = self.calculate_indicators(df)
        
        # Проверка наличия колонок перед использованием
        if 'EMA_200' not in dw.columns: return False

        c, p = dw.iloc[-1], dw.iloc[-2]
        
        # Логика входа
        side = None
        if c['close'] > c['EMA_200'] and c['close'] > c['EMA_20'] and p['close'] <= p['EMA_20']:
            side = 'LONG'
        elif c['close'] < c['EMA_200'] and c['close'] < c['EMA_20'] and p['close'] >= p['EMA_20']:
            side = 'SHORT'

        if side and c['ADX_14'] > 18:
            entry, atr = c['close'], c['ATRr_14']
            sl = entry - (atr * ATR_MULT_SL) if side == 'LONG' else entry + (atr * ATR_MULT_SL)
            tp_dist = max(atr * ATR_MULT_TP, entry * MIN_TARGET_PCT)
            tp = entry + tp_dist if side == 'LONG' else entry - tp_dist
            
            target_pct = round((abs(tp - entry) / entry) * 100, 2)
            risk_pct = round((abs(entry - sl) / entry) * 100, 2)
            
            sig_id = f"ID_{dw.index[-1].strftime('%H%M')}"
            
            if sig_id not in self.processed_signals:
                msg = (f"🚀 <b>{side} Signal | BTC</b>\n🎯 Цель: +{target_pct}%\n"
                       f"---------------------------\n🎯 Вход: {entry}\n🛡 Стоп: {sl:.2f}\n💰 Тейк: {tp:.2f}\n"
                       f"---------------------------\nID: {sig_id}")
                
                await self.bot.send_message(chat_id=TG_CHANNEL_ID, text=msg, parse_mode=ParseMode.HTML)
                
                self.active_trades.append({
                    'id': sig_id, 'side': side, 'tp': tp, 'sl': sl, 
                    'target_pct': target_pct, 'risk_pct': risk_pct
                })
                self.processed_signals.add(sig_id)
                return True
        return False

    async def run(self):
        await self.bot.send_message(chat_id=TG_CHANNEL_ID, text="Бот запущен")
        print("🚀 Сканер активен...", flush=True)

        while True:
            try:
                ticker = await self.exchange.fetch_ticker('BTC/USDT')
                cur_price = ticker['last']
                t = datetime.now().strftime('%H:%M:%S')
                
                print(f"🔄 [{t}] BTC: {cur_price} | Активных сделок: {len(self.active_trades)}", flush=True)
                
                await self.track_results(cur_price)
                
                for tf in TIMEFRAME_PAIRS:
                    if await self.analyze_pair('BTC/USDT', tf): break
                    await asyncio.sleep(1)
            except Exception as e:
                print(f"⚠️ Ошибка в цикле: {e}", flush=True)
            
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(TradingBot().run())
