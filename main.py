import asyncio
import os
import sys
import logging
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

# --- 1. ЗАГРУЗКА НАСТРОЕК ---
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
GOOGLE_JSON = os.getenv("GOOGLE_SHEETS_JSON") 

# Настройки канала и параметров
TG_CHANNEL_ID = "-1003738958585"
SYMBOLS = ['BTC/USDT']
TIMEFRAME_PAIRS = [
    {'work': '1h', 'filter': '4h'}, 
    {'work': '15m', 'filter': '4h'}, 
    {'work': '5m', 'filter': '1h'}
]

# Параметры стратегии
MIN_TARGET_PCT = 0.008   # Минимум 0.8%
MAX_SL_PCT = 0.018       # Макс стоп 1.8%
ATR_MULT_SL = 1.8        # Плотность стопа
ATR_MULT_TP = 3.5        # Потенциал тейка (динамический)
VOL_FACTOR = 1.25        # Фильтр объема

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])

class TradingBot:
    def __init__(self):
        self.exchange = ccxt.bybit({
            'apiKey': API_KEY, 
            'secret': API_SECRET, 
            'enableRateLimit': True, 
            'options': {'defaultType': 'swap'}
        })
        self.bot = Bot(token=TG_TOKEN)
        self.processed_signals = set()
        self.sheet = None
        self._connect_google()

    def _connect_google(self):
        if GOOGLE_JSON:
            try:
                creds = ServiceAccountCredentials.from_json_keyfile_dict(
                    json.loads(GOOGLE_JSON), 
                    ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
                )
                self.sheet = gspread.authorize(creds).open("BTC_Signals_Log").sheet1
                logging.info("✅ Google Sheet connected!")
            except Exception as e: logging.error(f"❌ GS Error: {e}")

    async def fetch_data(self, symbol, timeframe, limit=100):
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except: return None

    def calculate_indicators(self, df):
        # Трендовые и осцилляторы
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.ema(length=200, append=True)
        df.ta.adx(length=14, append=True)
        df.ta.rsi(length=14, append=True)
        df.ta.atr(length=14, append=True)
        
        # Волатильность (Боллинджер)
        bb = df.ta.bbands(length=20, std=2.0)
        df = pd.concat([df, bb], axis=1)
        
        # Фикс колонок и расчет объема
        df['BBU_FIX'] = df.filter(like='BBU').iloc[:, 0]
        df['BBL_FIX'] = df.filter(like='BBL').iloc[:, 0]
        df['VOL_SMA_20'] = df['volume'].rolling(20).mean()
        return df

    def check_global_trend(self, df_f):
        if df_f is None: return 'FLAT'
        c = df_f.iloc[-1]
        if c['close'] > c['EMA_50'] and c['EMA_20'] > c['EMA_50']: return 'UP'
        if c['close'] < c['EMA_50'] and c['EMA_20'] < c['EMA_50']: return 'DOWN'
        return 'FLAT'

    def generate_chart(self, df, sig, tf):
        pdf = df.tail(60)
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        apds = [
            mpf.make_addplot(pdf['EMA_20'], color='cyan', width=0.8),
            mpf.make_addplot(pdf['EMA_200'], color='white', width=1.0, alpha=0.5)
        ]
        lines = dict(hlines=[sig['entry'], sig['sl'], sig['tp']], colors=['blue', 'red', 'green'], linewidths=[1, 1.5, 1.5], linestyle='-.')
        buf = io.BytesIO()
        mpf.plot(pdf, type='candle', style=style, addplot=apds, hlines=lines, volume=True, 
                 title=f"\nBTC {tf} | Conf: {sig['score']}% | Target: {sig['target_pct']}%", 
                 savefig=dict(fname=buf, dpi=180, bbox_inches='tight'))
        buf.seek(0)
        return buf

    async def analyze_pair(self, symbol, tf_p):
        dw = await self.fetch_data(symbol, tf_p['work'])
        df = await self.fetch_data(symbol, tf_p['filter'])
        if dw is None or df is None: return False
        
        dw, df = self.calculate_indicators(dw), self.calculate_indicators(df)
        trend = self.check_global_trend(df)
        c, p = dw.iloc[-1], dw.iloc[-2]
        
        side = None
        if trend == 'UP' and c['close'] > c['EMA_20'] and p['close'] <= p['EMA_20']: side = 'LONG'
        if trend == 'DOWN' and c['close'] < c['EMA_20'] and p['close'] >= p['EMA_20']: side = 'SHORT'

        # Основной фильтр входа
        if side and c['ADX_14'] > 18 and c['volume'] > c['VOL_SMA_20'] * 1.1:
            entry, atr = c['close'], c['ATRr_14']
            sl = entry - (atr * ATR_MULT_SL) if side == 'LONG' else entry + (atr * ATR_MULT_SL)
            
            # Расчет динамического тейка
            tp_dist = max(atr * ATR_MULT_TP, entry * MIN_TARGET_PCT)
            tp = entry + tp_dist if side == 'LONG' else entry - tp_dist
            
            target_pct = round((abs(tp - entry) / entry) * 100, 2)
            risk_pct = abs(entry - sl) / entry
            rr = round(abs(tp - entry) / abs(entry - sl), 2)

            if rr >= 1.2 and risk_pct <= MAX_SL_PCT:
                sig_id = f"{symbol}_{side}_{tf_p['work']}_{dw.index[-1]}"
                if sig_id not in self.processed_signals:
                    # ПОДСЧЕТ ВЕСОВЫХ ПРИЧИН
                    score, reasons = 0, []
                    if trend != 'FLAT': 
                        score += 30; reasons.append(f"Тренд {trend} (+30%)")
                    vol_r = c['volume'] / c['VOL_SMA_20']
                    if vol_r > 1.3: 
                        score += 25; reasons.append("Высокий объем 🔥 (+25%)")
                    if 40 <= c['RSI_14'] <= 65: 
                        score += 20; reasons.append("RSI Оптимален (+20%)")
                    if c['ADX_14'] > 22: 
                        score += 25; reasons.append("Сильный импульс 💪 (+25%)")

                    chart = self.generate_chart(dw, {'entry':entry,'sl':sl,'tp':tp,'score':score,'target_pct':target_pct}, tf_p['work'])
                    reasons_text = "\n".join([f"• {r}" for r in reasons])
                    
                    msg = (f"🚀 <b>{side} Signal | BTC</b>\n⏱ <b>Таймфрейм: {tf_p['work']}</b>\n⚡ <b>Уверенность: {score}%</b>\n🎯 <b>Цель: +{target_pct}%</b>\n"
                           f"---------------------------\n📝 <b>Анализ факторов:</b>\n{reasons_text}\n"
                           f"---------------------------\n🎯 Вход: {entry}\n🛡 Стоп: {sl:.2f} ({risk_pct*100:.2f}%)\n💰 Тейк: {tp:.2f}\n⚖️ R:R: {rr}\n"
                           f"---------------------------\n📈 ADX: {c['ADX_14']:.1f} | RSI: {c['RSI_14']:.1f}")
                    
                    # Запись в Google Sheet
                    try:
                        if self.sheet:
                            self.sheet.append_row([str(datetime.now()), symbol, side, tf_p['work'], entry, sl, tp, score])
                    except: pass

                    await self.bot.send_photo(chat_id=TG_CHANNEL_ID, photo=types.BufferedInputFile(chart.read(), filename="chart.png"), caption=msg, parse_mode=ParseMode.HTML)
                    self.processed_signals.add(sig_id); return True
        return False

    async def run(self):
        await self.bot.send_message(chat_id=TG_CHANNEL_ID, text="Бот запущен")
        while True:
            try:
                for tf in TIMEFRAME_PAIRS:
                    if await self.analyze_pair('BTC/USDT', tf): break
                    await asyncio.sleep(1)
            except Exception as e: logging.error(f"Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(TradingBot().run())
