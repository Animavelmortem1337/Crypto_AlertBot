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

# Настройка для работы без монитора (для Railway/Docker)
matplotlib.use('Agg') 
import mplfinance as mpf
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from aiogram import Bot, types
from aiogram.enums import ParseMode
from dotenv import load_dotenv

# --- 1. КОНФИГУРАЦИЯ ---
load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
GOOGLE_JSON = os.getenv("GOOGLE_SHEETS_JSON") 

# Твой ID Админа
TG_CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "8371135844")

SYMBOLS = ['BTC/USDT']

# Таймфреймы (Приоритет сверху вниз: 1ч -> 15м -> 5м)
TIMEFRAME_PAIRS = [
    {'work': '1h', 'filter': '4h'},     
    {'work': '15m', 'filter': '4h'},    
    {'work': '5m', 'filter': '1h'},     
]

# Настройки стратегии
MAX_SL_PCT = 0.018    # Макс стоп 1.8%
TARGET_MOVE = 0.012   # Цель: 1.2% чистого движения BTC
MIN_RR = 1.5          # Мин риск/прибыль
ATR_MULT = 2.0        # Множитель ATR для стопа
VOL_FACTOR = 1.3      # На сколько объем должен быть выше среднего

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

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
        """Подключение к Google Sheets для истории"""
        if GOOGLE_JSON:
            try:
                scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
                creds_dict = json.loads(GOOGLE_JSON)
                creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
                client = gspread.authorize(creds)
                self.sheet = client.open("BTC_Signals_Log").sheet1 
                logging.info("✅ Google Sheet connected!")
            except Exception as e:
                logging.error(f"❌ Google Sheet error: {e}")

    async def send_startup_message(self):
        """Уведомление админа о запуске"""
        try:
            await self.bot.send_message(
                chat_id=TG_CHANNEL_ID,
                text=f"🤖 <b>Бот-Аналитик Запущен!</b>\nTarget: >1.0% Move\nAdmin ID: {TG_CHANNEL_ID}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Startup msg error: {e}")

    async def fetch_data(self, symbol, timeframe, limit=100):
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except Exception as e:
            logging.error(f"Fetch error {timeframe}: {e}")
            return None

    def calculate_indicators(self, df):
        """Расчет всей математики"""
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.adx(length=14, append=True)
        df.ta.rsi(length=14, append=True)
        df.ta.atr(length=14, append=True)
        df.ta.bbands(length=20, std=2.0, append=True)
        
        # Ширина Боллинджера для Squeeze
        df['BB_WIDTH'] = (df['BBU_20_2.0'] - df['BBL_20_2.0']) / df['BBM_20_2.0']
        df['VOL_SMA_20'] = df['volume'].rolling(20).mean()
        
        # Уровни для графика
        df['DC_HIGH'] = df['high'].rolling(20).max()
        df['DC_LOW'] = df['low'].rolling(20).min()
        return df

    def check_global_trend(self, df_filter):
        if df_filter is None: return 'FLAT'
        curr = df_filter.iloc[-1]
        if curr['close'] > curr['EMA_50'] and curr['EMA_20'] > curr['EMA_50']:
            return 'UP'
        elif curr['close'] < curr['EMA_50'] and curr['EMA_20'] < curr['EMA_50']:
            return 'DOWN'
        return 'FLAT'

    def is_strong_candle(self, open_p, close_p, high_p, low_p):
        body = abs(close_p - open_p)
        full = high_p - low_p
        return (full > 0) and ((body / full) > 0.4)

    def calculate_score(self, trend, rsi, volume, vol_avg, bb_width, prev_bb_width, side):
        """Скоринг сигнала (винрейт)"""
        score = 0
        reasons = []

        if trend != 'FLAT':
            score += 30
            reasons.append("Trend ✅")

        if volume > vol_avg * VOL_FACTOR:
            score += 20
            reasons.append("Volume 🔥")

        # Фильтр RSI: Ищем потенциал для хода в 1%
        if side == 'LONG':
            if 45 <= rsi <= 65: score += 15; reasons.append("RSI Opt")
            elif rsi > 70: score -= 30; reasons.append("Overbought ⚠️")
        else:
            if 35 <= rsi <= 55: score += 15; reasons.append("RSI Opt")
            elif rsi < 30: score -= 30; reasons.append("Oversold ⚠️")

        # Если волатильность начинает расти
        if bb_width > prev_bb_width:
            score += 15
            reasons.append("Volatility Expand 💥")

        return max(0, score), ", ".join(reasons)

    def generate_chart(self, df, symbol, signal, timeframe):
        """Отрисовка проф. графика"""
        plot_df = df.tail(60)
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        
        apds = [
            mpf.make_addplot(plot_df['EMA_20'], color='cyan', width=0.8),
            mpf.make_addplot(plot_df['BBU_20_2.0'], color='gray', width=0.5, alpha=0.3),
            mpf.make_addplot(plot_df['BBL_20_2.0'], color='gray', width=0.5, alpha=0.3),
        ]
        
        lines = dict(hlines=[signal['entry'], signal['sl'], signal['tp']],
                     colors=['blue', 'red', 'green'], linewidths=[1, 1.5, 1.5], linestyle='-.')
        
        buf = io.BytesIO()
        mpf.plot(plot_df, type='candle', style=style, addplot=apds, hlines=lines, 
                 volume=True, title=f"\n{symbol} {timeframe} | Conf: {signal['score']}%",
                 savefig=dict(fname=buf, dpi=180, bbox_inches='tight'))
        buf.seek(0)
        return buf

    async def analyze_pair(self, symbol, tf_pair):
        work_tf, filter_tf = tf_pair['work'], tf_pair['filter']
        df_w = await self.fetch_data(symbol, work_tf)
        df_f = await self.fetch_data(symbol, filter_tf)
        if df_w is None or df_f is None: return False

        df_w = self.calculate_indicators(df_w)
        df_f = self.calculate_indicators(df_f)
        trend = self.check_global_trend(df_f)
        if trend == 'FLAT': return False

        curr, prev = df_w.iloc[-1], df_w.iloc[-2]
        side = None

        # Фильтры силы
        adx_ok = curr['ADX_14'] > 20
        vol_ok = curr['volume'] > curr['VOL_SMA_20'] * VOL_FACTOR
        candle_ok = self.is_strong_candle(curr['open'], curr['close'], curr['high'], curr['low'])
        
        # Логика пробоя EMA с фильтрами
        if trend == 'UP' and curr['close'] > curr['EMA_20'] and prev['close'] <= prev['EMA_20']:
            if adx_ok and vol_ok and curr['RSI_14'] < 70: side = 'LONG'
        elif trend == 'DOWN' and curr['close'] < curr['EMA_20'] and prev['close'] >= prev['EMA_20']:
            if adx_ok and vol_ok and curr['RSI_14'] > 30: side = 'SHORT'

        if side:
            entry = curr['close']
            atr = curr['ATRr_14']
            
            # Расчет целей (Тейк на 1.2%)
            if side == 'LONG':
                sl = entry - (atr * ATR_MULT)
                tp = entry * (1 + TARGET_MOVE)
            else:
                sl = entry + (atr * ATR_MULT)
                tp = entry * (1 - TARGET_MOVE)

            risk_pct = abs(entry - sl) / entry
            rr = round(abs(tp - entry) / abs(entry - sl), 2)
            score, reasons = self.calculate_score(trend, curr['RSI_14'], curr['volume'], 
                                               curr['VOL_SMA_20'], curr['BB_WIDTH'], prev['BB_WIDTH'], side)

            # Проверка качества сделки
            if rr >= MIN_RR and score >= 60 and risk_pct <= MAX_SL_PCT:
                sig_id = f"{symbol}_{side}_{work_tf}_{df_w.index[-1]}"
                if sig_id not in self.processed_signals:
                    # Запрос фандинга
                    funding_data = await self.exchange.fetch_funding_rate(symbol)
                    funding = funding_data['fundingRate']
                    
                    chart = self.generate_chart(df_w, symbol, 
                                                {'entry':entry,'sl':sl,'tp':tp,'score':score,'side':side}, 
                                                work_tf)
                    
                    msg = (f"🚀 <b>{side} Signal | BTC</b>\n"
                           f"⏱ <b>TF: {work_tf}</b>\n"
                           f"⚡ <b>Confidence: {score}%</b>\n"
                           f"🎯 <b>Target: >1.0% Move</b>\n"
                           f"<i>Factors: {reasons}</i>\n"
                           f"---------------------------\n"
                           f"🎯 <b>Entry:</b> {entry}\n"
                           f"🛡 <b>SL:</b> {sl:.2f} ({risk_pct*100:.2f}%)\n"
                           f"💰 <b>TP:</b> {tp:.2f} (+{TARGET_MOVE*100}%)\n"
                           f"⚖️ <b>R:R:</b> {rr}\n"
                           f"---------------------------\n"
                           f"📊 <b>Funding:</b> {funding*100:.4f}%\n"
                           f"📉 <b>ADX:</b> {curr['ADX_14']:.1f}\n")
                    
                    try:
                        input_file = types.BufferedInputFile(chart.read(), filename="chart.png")
                        await self.bot.send_photo(chat_id=TG_CHANNEL_ID, photo=input_file, caption=msg, parse_mode=ParseMode.HTML)
                        self.processed_signals.add(sig_id)
                        return True
                    except Exception as e:
                        logging.error(f"Telegram error: {e}")
        return False

    async def run(self):
        logging.info("Bot starting...")
        await self.send_startup_message()
        while True:
            t = datetime.now().strftime("%H:%M:%S")
            logging.info(f"🔄 [{t}] Checking BTC for 1% moves (1H -> 15M -> 5M)...")
            
            for tf in TIMEFRAME_PAIRS:
                sent = await self.analyze_pair('BTC/USDT', tf)
                if sent:
                    logging.info("🛑 Higher TF signal sent. Skipping lower TFs.")
                    break # Защита от спама на разных ТФ
                await asyncio.sleep(1)
            
            await asyncio.sleep(60) # Проверка раз в минуту

    async def close(self):
        await self.exchange.close()
        await self.bot.session.close()

async def main():
    bot = TradingBot()
    try:
        await bot.run()
    except Exception as e:
        logging.error(f"CRITICAL ERROR: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
