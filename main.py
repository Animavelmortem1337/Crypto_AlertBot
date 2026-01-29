import asyncio
import os
import io
import json
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
import matplotlib.pyplot as plt
import mplfinance as mpf
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, timedelta
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile
from dotenv import load_dotenv

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
API_KEY, API_SECRET = os.getenv("BYBIT_API_KEY"), os.getenv("BYBIT_SECRET")
TG_TOKEN, TG_CHANNEL_ID = os.getenv("TG_TOKEN"), "-1003738958585"
GOOGLE_JSON = os.getenv("GOOGLE_SHEETS_JSON")

SYMBOL = 'BTC/USDT'

class AdvancedBot:
    def __init__(self):
        self.exchange = ccxt.bybit({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True})
        self.bot = Bot(token=TG_TOKEN)
        self.processed_signals = set()
        self.active_trades = []
        self.daily_stats = {'total': 0, 'wins': 0, 'losses': 0, 'profit': 0.0}
        self._connect_google()

    def _connect_google(self):
        if GOOGLE_JSON:
            try:
                creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_JSON), 
                        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
                self.sheet = gspread.authorize(creds).open("BTC_Signals_Log").sheet1
            except: self.sheet = None

    async def fetch_data(self, tf, limit=100):
        try:
            ohlcv = await self.exchange.fetch_ohlcv(SYMBOL, tf, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except: return None

    async def create_chart(self, df, title, tf, entry=None, tp=None, sl=None, p_level=None):
        """Отрисовка реального графика со свечами и уровнями"""
        # Настройка стиля
        mc = mpf.make_marketcolors(up='#00ff00', down='#ff0000', inherit=True)
        s = mpf.make_mpf_style(base_mpf_style='charles', marketcolors=mc, gridcolor='#222222', facecolor='black')
        
        # Добавляем индикаторы
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=200, append=True)
        
        add_plots = [
            mpf.make_addplot(df['EMA_20'], color='#00d4ff', width=1),
            mpf.make_addplot(df['EMA_200'], color='#ffaa00', width=1.5)
        ]

        # Линии уровней
        h_lines = []
        h_colors = []
        if entry: 
            h_lines.extend([entry, tp, sl])
            h_colors.extend(['blue', 'green', 'red'])
        if p_level:
            h_lines.append(p_level)
            h_colors.append('#ffffff55') # Белая полупрозрачная линия Pivot

        buf = io.BytesIO()
        mpf.plot(df.tail(50), type='candle', style=s, addplot=add_plots,
                 hlines=dict(hlines=h_lines, colors=h_colors, linestyle='-.', linewidths=1),
                 title=f"\n{title}", ylabel='Price (USDT)', 
                 savefig=dict(fname=buf, format='png', bbox_inches='tight'),
                 figsize=(12, 7))
        buf.seek(0)
        return buf

    async def analyze(self):
        df_5m = await self.fetch_data('5m')
        df_1h = await self.fetch_data('1h')
        df_d = await self.fetch_data('1d', limit=2)
        if any(x is None for x in [df_5m, df_1h, df_d]): return

        # Логика Pivot
        last_d = df_d.iloc[-1]
        p_val = (last_d['high'] + last_d['low'] + last_d['close']) / 3
        
        df_5m.ta.ema(length=20, append=True)
        c, prev = df_5m.iloc[-1], df_5m.iloc[-2]
        side = 'LONG' if c['close'] > c['EMA_20'] else 'SHORT'
        
        score = 0
        reasons = []

        # 1. EMA 20 Cross (35%)
        if (side == 'LONG' and prev['close'] <= prev['EMA_20']) or (side == 'SHORT' and prev['close'] >= prev['EMA_20']):
            score += 35; reasons.append(f"📈 Пересечение EMA20 (+35%)")
        else: return # Нет пересечения — нет входа

        # 2. Pivot (25%)
        if (side == 'LONG' and c['close'] > p_val) or (side == 'SHORT' and c['close'] < p_val):
            score += 25; reasons.append(f"📍 Выше/Ниже Pivot (+25%)")

        # 3. Trend 1h (20%)
        df_1h.ta.ema(length=200, append=True)
        if (side == 'LONG' and df_1h.iloc[-1]['close'] > df_1h.iloc[-1]['EMA_200']) or \
           (side == 'SHORT' and df_1h.iloc[-1]['close'] < df_1h.iloc[-1]['EMA_200']):
            score += 20; reasons.append(f"🌊 Тренд 1ч EMA200 (+20%)")

        # 4. RSI & Volume (20%)
        df_5m.ta.rsi(length=14, append=True)
        if 35 < c['RSI_14'] < 65:
            score += 10; reasons.append(f"⚡️ RSI в норме (+10%)")
        if c['volume'] > df_5m['volume'].rolling(20).mean().iloc[-1]:
            score += 10; reasons.append(f"📊 Объем подтвержден (+10%)")

        if score >= 55:
            sig_id = f"{side}_{df_5m.index[-1].strftime('%H%M')}"
            if sig_id not in self.processed_signals:
                entry = round(c['close'], 1)
                tp = round(entry * 1.01 if side == 'LONG' else entry * 0.99, 1)
                sl = round(entry * 0.985 if side == 'LONG' else entry * 1.015, 1)
                
                # Создаем графики
                img_5m = await self.create_chart(df_5m, f"BTC 5m | {side}", '5m', entry, tp, sl, p_val)
                img_1h = await self.create_chart(df_1h, f"BTC 1h | Global View", '1h', p_level=p_val)

                msg = (f"🔥 <b>{side} SIGNAL | BTC/USDT</b>\n"
                       f"---------------------------\n"
                       f"📊 <b>Анализ ({score}%):</b>\n" + "\n".join(reasons) + "\n"
                       f"---------------------------\n"
                       f"🎯 Вход: {entry}\n💰 Тейк: {tp}\n🛡 Стоп: {sl}\n"
                       f"📈 Винрейт дня: {self.get_wr()}%")

                await self.bot.send_photo(TG_CHANNEL_ID, BufferedInputFile(img_5m.read(), "5m.png"), caption=msg, parse_mode=ParseMode.HTML)
                await self.bot.send_photo(TG_CHANNEL_ID, BufferedInputFile(img_1h.read(), "1h.png"), caption="<i>Глобальный контекст 1ч + Pivot</i>", parse_mode=ParseMode.HTML)
                
                self.processed_signals.add(sig_id)
                self.active_trades.append({'side': side, 'tp': tp, 'sl': sl, 'entry': entry, 'score': score})

    def get_wr(self):
        total = self.daily_stats['wins'] + self.daily_stats['losses']
        return round((self.daily_stats['wins'] / total * 100), 1) if total > 0 else 0

    async def run(self):
        await self.bot.send_message(TG_CHANNEL_ID, "🚀 Бот-снайпер запущен!\nРеальные графики Bybit активны.")
        while True:
            print(f"🔄 [{datetime.now().strftime('%H:%M')}] Check BTC...", flush=True)
            try: await self.analyze()
            except Exception as e: print(f"Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(AdvancedBot().run())
