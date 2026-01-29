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
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHANNEL_ID = "-1003738958585"
GOOGLE_JSON = os.getenv("GOOGLE_SHEETS_JSON")

SYMBOL = 'BTC/USDT'
SIGNAL_THRESHOLD = 55 # Твой порог агрессивности

class AdvancedBot:
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
                creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_JSON), 
                        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
                self.sheet = gspread.authorize(creds).open("BTC_Signals_Log").sheet1
                print("✅ [SYSTEM] Google Таблица подключена!", flush=True)
            except Exception as e: print(f"❌ [ERROR] Google Sheets: {e}", flush=True)

    async def log_to_sheets(self, data):
        if self.sheet:
            try: self.sheet.append_row(data)
            except Exception as e: print(f"⚠️ [ERROR] Ошибка записи в таблицу: {e}", flush=True)

    async def fetch_data(self, tf, limit=100):
        try:
            ohlcv = await self.exchange.fetch_ohlcv(SYMBOL, tf, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except: return None

    async def create_chart(self, df, title, entry=None, tp=None, sl=None, p_level=None):
        mc = mpf.make_marketcolors(up='#00ff00', down='#ff0000', inherit=True)
        s = mpf.make_mpf_style(base_mpf_style='charles', marketcolors=mc, gridcolor='#222222', facecolor='black')
        
        df = df.copy()
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=200, append=True)
        
        add_plots = [
            mpf.make_addplot(df['EMA_20'], color='#00d4ff', width=1),
            mpf.make_addplot(df['EMA_200'], color='#ffaa00', width=1.5)
        ]

        h_lines, h_colors = [], []
        if entry: 
            h_lines.extend([entry, tp, sl])
            h_colors.extend(['blue', 'green', 'red'])
        if p_level:
            h_lines.append(p_level)
            h_colors.append('#ffffff55')

        buf = io.BytesIO()
        mpf.plot(df.tail(50), type='candle', style=s, addplot=add_plots,
                 hlines=dict(hlines=h_lines, colors=h_colors, linestyle='-.', linewidths=1),
                 title=f"\n{title}", savefig=dict(fname=buf, format='png', bbox_inches='tight'),
                 figsize=(10, 6))
        buf.seek(0)
        return buf

    async def analyze(self):
        df_5m = await self.fetch_data('5m')
        df_1h = await self.fetch_data('1h')
        df_d = await self.fetch_data('1d', limit=15)
        if any(x is None for x in [df_5m, df_1h, df_d]): return

        # Индикаторы
        df_5m.ta.ema(length=20, append=True)
        df_5m.ta.atr(length=14, append=True)
        df_5m.ta.rsi(length=14, append=True)
        df_d.ta.atr(length=14, append=True)
        df_1h.ta.ema(length=200, append=True)

        c, prev = df_5m.iloc[-1], df_5m.iloc[-2]
        last_d = df_d.iloc[-1]
        p_val = (last_d['high'] + last_d['low'] + last_d['close']) / 3
        
        side = 'LONG' if c['close'] > c['EMA_20'] else 'SHORT'
        score, reasons = 0, []

        # 1. ТРИГГЕР: Пересечение EMA 20 (35%)
        if (side == 'LONG' and prev['close'] <= prev['EMA_20']) or (side == 'SHORT' and prev['close'] >= prev['EMA_20']):
            score += 35; reasons.append(f"📈 Пересечение EMA20 (+35%)")
        else: return # Нет пересечения - нет сигнала

        # 2. Pivot (25%)
        if (side == 'LONG' and c['close'] > p_val) or (side == 'SHORT' and c['close'] < p_val):
            score += 25; reasons.append(f"📍 Зона Pivot (+25%)")

        # 3. Trend 1h (20%)
        if (side == 'LONG' and df_1h.iloc[-1]['close'] > df_1h.iloc[-1]['EMA_200']) or \
           (side == 'SHORT' and df_1h.iloc[-1]['close'] < df_1h.iloc[-1]['EMA_200']):
            score += 20; reasons.append(f"🌊 Тренд 1ч EMA200 (+20%)")

        # 4. RSI & Volume (20%)
        if 35 < c['RSI_14'] < 65: score += 10; reasons.append(f"⚡️ RSI в норме (+10%)")
        if c['volume'] > df_5m['volume'].rolling(20).mean().iloc[-1]: score += 10; reasons.append(f"📊 Объемы (+10%)")

        # --- МЕТРИКИ РИСКА ---
        dist_to_ema = abs(c['close'] - c['EMA_20'])
        dist_ratio = round(dist_to_ema / c['ATRr_14'], 2)
        dist_text = f"✅ Дистанция: {dist_ratio} ATR" if dist_ratio <= 0.35 else f"⚠️ Дистанция: {dist_ratio} ATR (Высокая!)"

        day_open = df_d.iloc[-1]['open']
        move_abs = round(c['close'] - day_open, 1)
        energy_used = round(((df_d.iloc[-1]['high'] - df_d.iloc[-1]['low']) / df_d['ATRr_14'].iloc[-1]) * 100, 1)
        energy_text = f"✅ Энергия: {energy_used}% (Пройдено ${abs(move_abs)})" if energy_used < 85 else f"⚠️ Энергия: {energy_used}% (Израсходовано!)"

        if score >= SIGNAL_THRESHOLD:
            sig_id = f"{side}_{df_5m.index[-1].strftime('%H%M')}"
            if sig_id not in self.processed_signals:
                entry = round(c['close'], 1)
                tp = round(entry * 1.01 if side == 'LONG' else entry * 0.99, 1)
                sl = round(entry * 0.985 if side == 'LONG' else entry * 1.015, 1)

                img_5m = await self.create_chart(df_5m, f"BTC 5m | {side}", entry, tp, sl, p_val)
                img_1h = await self.create_chart(df_1h, f"BTC 1h | Global", p_level=p_val)

                msg = (f"🔥 <b>{side} SIGNAL ({score}%)</b>\n"
                       f"---------------------------\n"
                       f"📊 <b>Анализ:</b>\n" + "\n".join(reasons) + "\n"
                       f"---------------------------\n"
                       f"📏 <b>Метрики риска:</b>\n{dist_text}\n⛽️ {energy_text}\n"
                       f"---------------------------\n"
                       f"🎯 Вход: {entry}\n💰 Тейк: {tp}\n🛡 Стоп: {sl}\n"
                       f"📈 Винрейт: {self.get_wr()}%")

                await self.bot.send_photo(TG_CHANNEL_ID, BufferedInputFile(img_5m.read(), "5m.png"), caption=msg, parse_mode=ParseMode.HTML)
                await self.bot.send_photo(TG_CHANNEL_ID, BufferedInputFile(img_1h.read(), "1h.png"), caption="<i>Глобальный контекст и уровни</i>", parse_mode=ParseMode.HTML)
                
                tz_utc2 = timezone(timedelta(hours=2))
                await self.log_to_sheets([datetime.now(tz_utc2).strftime('%H:%M'), SYMBOL, "5m", side, entry, tp, sl, f"{score}%"])
                
                self.processed_signals.add(sig_id)
                self.active_trades.append({'side': side, 'tp': tp, 'sl': sl, 'entry': entry, 'tf': '5m'})
                self.daily_stats['total'] += 1

    def get_wr(self):
        total = self.daily_stats['wins'] + self.daily_stats['losses']
        return round((self.daily_stats['wins'] / total * 100), 1) if total > 0 else 0

    async def track_results(self):
        if not self.active_trades: return
        ticker = await self.exchange.fetch_ticker(SYMBOL)
        curr = ticker['last']
        for trade in self.active_trades[:]:
            closed = False
            if trade['side'] == 'LONG':
                if curr >= trade['tp']: closed, res, p = True, "✅ TAKE PROFIT", 1.1
                elif curr <= trade['sl']: closed, res, p = True, "❌ STOP LOSS", -0.5
            else:
                if curr <= trade['tp']: closed, res, p = True, "✅ TAKE PROFIT", 1.1
                elif curr >= trade['sl']: closed, res, p = True, "❌ STOP LOSS", -0.5
            
            if closed:
                await self.bot.send_message(TG_CHANNEL_ID, f"🔔 <b>Сделка закрыта</b>\n{res}\nЦена: {curr}")
                self.daily_stats['wins'] += 1 if "TAKE" in res else 0
                self.daily_stats['losses'] += 1 if "STOP" in res else 0
                self.daily_stats['profit'] += p
                self.active_trades.remove(trade)

    async def run(self):
        await self.bot.send_message(TG_CHANNEL_ID, "🤖 Бот запущен!\nПорог: 55% | Графики 5м+1ч | Метрики ATR")
        while True:
            tz_utc2 = timezone(timedelta(hours=2))
            print(f"🔄 [{datetime.now(tz_utc2).strftime('%H:%M:%S')}] Проверка...", flush=True)
            try:
                await self.analyze()
                await self.track_results()
                # Отчет в 23:00
                now = datetime.now(tz_utc2)
                if now.hour == 23 and now.minute == 0:
                    wr = self.get_wr()
                    report = f"📊 <b>ИТОГИ ДНЯ</b>\nСигналов: {self.daily_stats['total']}\nВинрейт: {wr}%\nПрофит: {self.daily_stats['profit']}%"
                    await self.bot.send_message(TG_CHANNEL_ID, report)
                    self.daily_stats = {'total': 0, 'wins': 0, 'losses': 0, 'profit': 0.0}
                    await asyncio.sleep(60)
            except Exception as e: print(f"Error: {e}", flush=True)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(AdvancedBot().run())
