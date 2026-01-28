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
from datetime import datetime, timezone # Добавили timezone
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
        self.daily_stats = {'total': 0, 'wins': 0, 'losses': 0, 'profit': 0.0}
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
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.ema(length=200, append=True)
        df.ta.adx(length=14, append=True)
        df.ta.rsi(length=14, append=True)
        df.ta.atr(length=14, append=True)
        return df

    async def send_daily_report(self):
        # Используем современный способ получения времени
        now_utc = datetime.now(timezone.utc)
        if self.daily_stats['total'] == 0:
            msg = f"📊 <b>Отчет за {now_utc.strftime('%d.%m')} (23:00 UTC+2)</b>\nСделок сегодня не было. 💤"
        else:
            winrate = (self.daily_stats['wins'] / self.daily_stats['total']) * 100
            msg = (f"📊 <b>Ежедневный отчет (23:00 UTC+2)</b>\n"
                   f"Дата: {now_utc.strftime('%d.%m.%Y')}\n\n"
                   f"✅ Профитных: {self.daily_stats['wins']}\n"
                   f"❌ Убыточных: {self.daily_stats['losses']}\n"
                   f"📈 Всего сделок: {self.daily_stats['total']}\n"
                   f"🎯 Винрейт: {winrate:.1f}%\n"
                   f"💰 Чистый профит: <b>{self.daily_stats['profit']:.2f}%</b>")
        
        await self.bot.send_message(chat_id=TG_CHANNEL_ID, text=msg, parse_mode=ParseMode.HTML)
        self.daily_stats = {'total': 0, 'wins': 0, 'losses': 0, 'profit': 0.0}
        self.processed_signals.clear()

    async def track_results(self, current_price):
        if not self.active_trades: return
        for trade in self.active_trades[:]:
            side = trade['side']
            tp, sl = trade['tp'], trade['sl']
            is_tp = (side == 'LONG' and current_price >= tp) or (side == 'SHORT' and current_price <= tp)
            is_sl = (side == 'LONG' and current_price <= sl) or (side == 'SHORT' and current_price >= sl)
            if is_tp or is_sl:
                self.daily_stats['total'] += 1
                if is_tp:
                    self.daily_stats['wins'] += 1
                    self.daily_stats['profit'] += trade['target_pct']
                    res_text = f"✅ Тейк достигнут! +{trade['target_pct']}%"
                else:
                    self.daily_stats['losses'] += 1
                    self.daily_stats['profit'] -= trade['risk_pct']
                    res_text = f"❌ Закрыто по стопу -{trade['risk_pct']}%"
                msg = (f"🏁 <b>Сделка {trade['id']} закрыта</b>\n{res_text}\nЦена: {current_price}")
                await self.bot.send_message(chat_id=TG_CHANNEL_ID, text=msg, parse_mode=ParseMode.HTML)
                self.active_trades.remove(trade)

    async def analyze_pair(self, symbol, tf_p):
        dw = await self.fetch_data(symbol, tf_p['work'])
        df = await self.fetch_data(symbol, tf_p['filter'])
        if dw is None or df is None: return False
        dw, df = self.calculate_indicators(dw), self.calculate_indicators(df)
        if 'EMA_200' not in dw.columns: return False
        c, p = dw.iloc[-1], dw.iloc[-2]
        side = None
        if c['close'] > c['EMA_200'] and c['close'] > c['EMA_20'] and p['close'] <= p['EMA_20']: side = 'LONG'
        elif c['close'] < c['EMA_200'] and c['close'] < c['EMA_20'] and p['close'] >= p['EMA_20']: side = 'SHORT'
        if side and c['ADX_14'] > 18:
            entry, atr = c['close'], c['ATRr_14']
            sl = entry - (atr * ATR_MULT_SL) if side == 'LONG' else entry + (atr * ATR_MULT_SL)
            tp_dist = max(atr * ATR_MULT_TP, entry * MIN_TARGET_PCT)
            tp = entry + tp_dist if side == 'LONG' else entry - tp_dist
            target_pct, risk_pct = round((abs(tp - entry) / entry) * 100, 2), round((abs(entry - sl) / entry) * 100, 2)
            sig_id = f"ID_{dw.index[-1].strftime('%H%M')}"
            if sig_id not in self.processed_signals:
                msg = (f"🚀 <b>{side} Signal | BTC</b>\n🎯 Цель: +{target_pct}%\n---------------------------\n🎯 Вход: {entry}\n🛡 Стоп: {sl:.2f}\n💰 Тейк: {tp:.2f}\n---------------------------\nID: {sig_id}")
                await self.bot.send_message(chat_id=TG_CHANNEL_ID, text=msg, parse_mode=ParseMode.HTML)
                self.active_trades.append({'id': sig_id, 'side': side, 'tp': tp, 'sl': sl, 'target_pct': target_pct, 'risk_pct': risk_pct})
                self.processed_signals.add(sig_id)
                return True
        return False

    async def run(self):
        await self.bot.send_message(chat_id=TG_CHANNEL_ID, text="Бот запущен. Ежедневный отчет в 23:00 (UTC+2).")
        print("🚀 Бот в сети...", flush=True)
        while True:
            try:
                # Обновленный способ получения времени (теперь без DeprecationWarning)
                now_utc = datetime.now(timezone.utc)
                if now_utc.hour == 21 and now_utc.minute == 0:
                    await self.send_daily_report()
                    await asyncio.sleep(61) 

                ticker = await self.exchange.fetch_ticker('BTC/USDT')
                cur_price = ticker['last']
                print(f"🔄 [{now_utc.strftime('%H:%M')} UTC] Цена: {cur_price} | Активных: {len(self.active_trades)}", flush=True)
                
                await self.track_results(cur_price)
                for tf in TIMEFRAME_PAIRS:
                    if await self.analyze_pair('BTC/USDT', tf): break
                    await asyncio.sleep(1)
            except Exception as e: print(f"⚠️ Ошибка: {e}", flush=True)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(TradingBot().run())
