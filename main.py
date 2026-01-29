import asyncio
import os
import json
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, timedelta
from aiogram import Bot
from aiogram.enums import ParseMode
from dotenv import load_dotenv

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHANNEL_ID = "-1003738958585"
GOOGLE_JSON = os.getenv("GOOGLE_SHEETS_JSON")

SYMBOL = 'BTC/USDT'
TIMEFRAMES = ['1h', '15m', '5m']

class AdvancedBot:
    def __init__(self):
        self.exchange = ccxt.bybit({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
        self.bot = Bot(token=TG_TOKEN)
        self.processed_signals = set()
        self.active_trades = [] # Список активных сделок
        self.daily_stats = {'total': 0, 'wins': 0, 'losses': 0, 'profit': 0.0}
        self.sheet = None
        self._connect_google()

    def _connect_google(self):
        if GOOGLE_JSON:
            try:
                creds_dict = json.loads(GOOGLE_JSON)
                creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
                self.sheet = gspread.authorize(creds).open("BTC_Signals_Log").sheet1
                print("✅ [SYSTEM] Google Таблица подключена!", flush=True)
            except Exception as e: print(f"❌ [ERROR] Sheets: {e}", flush=True)

    async def log_to_sheets(self, data):
        if self.sheet:
            try: self.sheet.append_row(data)
            except Exception as e: print(f"⚠️ [ERROR] Таблица: {e}", flush=True)

    async def fetch_data(self, symbol, timeframe, limit=200):
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df.set_index(pd.to_datetime(df['timestamp'], unit='ms'), inplace=True)
            return df
        except: return None

    async def track_results(self):
        """Проверка активных сделок на достижение целей"""
        if not self.active_trades: return
        
        ticker = await self.exchange.fetch_ticker(SYMBOL)
        cur_price = ticker['last']
        
        for trade in self.active_trades[:]:
            is_closed = False
            result_text = ""
            
            if trade['side'] == 'LONG':
                if cur_price >= trade['tp']:
                    is_closed, res = True, "✅ TAKE PROFIT"
                    self.daily_stats['wins'] += 1
                    self.daily_stats['profit'] += 1.0
                elif cur_price <= trade['sl']:
                    is_closed, res = True, "❌ STOP LOSS"
                    self.daily_stats['losses'] += 1
                    self.daily_stats['profit'] -= 1.5
            else: # SHORT
                if cur_price <= trade['tp']:
                    is_closed, res = True, "✅ TAKE PROFIT"
                    self.daily_stats['wins'] += 1
                    self.daily_stats['profit'] += 1.0
                elif cur_price >= trade['sl']:
                    is_closed, res = True, "❌ STOP LOSS"
                    self.daily_stats['losses'] += 1
                    self.daily_stats['profit'] -= 1.5

            if is_closed:
                msg = f"🔔 <b>Сделка закрыта: {SYMBOL} ({trade['tf']})</b>\nРезультат: {res}\nЦена закрытия: {cur_price}"
                await self.bot.send_message(TG_CHANNEL_ID, msg, parse_mode=ParseMode.HTML)
                
                # Обновляем таблицу
                tz_utc2 = timezone(timedelta(hours=2))
                log_data = [datetime.now(tz_utc2).strftime('%H:%M'), SYMBOL, trade['tf'], "CLOSE", cur_price, res]
                await self.log_to_sheets(log_data)
                
                self.active_trades.remove(trade)

    async def analyze(self):
        df_d = await self.fetch_data(SYMBOL, '1d', 2)
        if df_d is None: return
        last_d = df_d.iloc[-1]
        p = (last_d['high'] + last_d['low'] + last_d['close']) / 3
        
        for tf in TIMEFRAMES:
            df = await self.fetch_data(SYMBOL, tf)
            if df is None: continue
            df.ta.ema(length=20, append=True)
            df.ta.ema(length=200, append=True)
            df.ta.rsi(length=14, append=True)
            df['VOL_SMA'] = df['volume'].rolling(20).mean()
            
            c, prev = df.iloc[-1], df.iloc[-2]
            side = 'LONG' if c['close'] > c['EMA_20'] else 'SHORT'
            score = 0
            
            if (side == 'LONG' and prev['close'] <= prev['EMA_20']) or (side == 'SHORT' and prev['close'] >= prev['EMA_20']): score += 30
            if (side == 'LONG' and c['close'] > p) or (side == 'SHORT' and c['close'] < p): score += 25
            if (side == 'LONG' and c['close'] > c['EMA_200']) or (side == 'SHORT' and c['close'] < c['EMA_200']): score += 20
            if c['volume'] > df['volume'].rolling(20).mean() * 1.1: score += 15
            if (side == 'LONG' and c['RSI_14'] < 65) or (side == 'SHORT' and c['RSI_14'] > 35): score += 10

            if score >= 70:
                sig_id = f"{side}_{tf}_{df.index[-1].strftime('%H%M')}"
                if sig_id not in self.processed_signals:
                    entry = round(c['close'], 2)
                    tp = round(entry * 1.01 if side == 'LONG' else entry * 0.99, 2)
                    sl = round(entry * 0.985 if side == 'LONG' else entry * 1.015, 2)

                    msg = (f"🚀 <b>{side} SIGNAL: BTC ({tf})</b>\nУверенность: {score}%\n"
                           f"---------------------------\n🎯 Вход: {entry}\n💰 Тейк: {tp}\n🛡 Стоп: {sl}")
                    await self.bot.send_message(TG_CHANNEL_ID, msg, parse_mode=ParseMode.HTML)

                    tz_utc2 = timezone(timedelta(hours=2))
                    time_now = datetime.now(tz_utc2).strftime('%Y-%m-%d %H:%M')
                    await self.log_to_sheets([time_now, SYMBOL, tf, side, entry, tp, sl, f"{score}%"])
                    
                    # Добавляем в АКТИВНЫЕ сделки
                    self.active_trades.append({'side': side, 'tf': tf, 'tp': tp, 'sl': sl, 'entry': entry})
                    self.processed_signals.add(sig_id)
                    self.daily_stats['total'] += 1

    async def send_daily_report(self):
        """Отчет в 23:00 по Киеву"""
        tz_utc2 = timezone(timedelta(hours=2))
        now = datetime.now(tz_utc2)
        if now.hour == 23 and now.minute == 0:
            msg = (f"📊 <b>ИТОГИ ДНЯ (UTC+2)</b>\n"
                   f"---------------------------\n"
                   f"Всего сигналов: {self.daily_stats['total']}\n"
                   f"✅ Побед: {self.daily_stats['wins']}\n"
                   f"❌ Поражений: {self.daily_stats['losses']}\n"
                   f"💰 Итог: {self.daily_stats['profit']:.1f}%")
            await self.bot.send_message(TG_CHANNEL_ID, msg, parse_mode=ParseMode.HTML)
            # Сброс статистики на новый день
            self.daily_stats = {'total': 0, 'wins': 0, 'losses': 0, 'profit': 0.0}
            await asyncio.sleep(60)

    async def run(self):
        await self.bot.send_message(TG_CHANNEL_ID, "🤖 Бот-снайпер запущен!\nТрекинг сделок и отчет в 23:00 ВКЛ.")
        while True:
            tz_utc2 = timezone(timedelta(hours=2))
            print(f"🔄 [{datetime.now(tz_utc2).strftime('%H:%M:%S')}] Проверка рынка и сделок...", flush=True)
            try:
                await self.analyze()
                await self.track_results()
                await self.send_daily_report()
            except Exception as e: print(f"⚠️ Ошибка: {e}", flush=True)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(AdvancedBot().run())
