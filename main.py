import asyncio
import os
import logging
import io
import json
import pandas as pd
import pandas_ta as ta
import ccxt.async_support as ccxt
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
TG_CHANNEL_ID = os.getenv("TG_CHANNEL_ID")
GOOGLE_JSON = os.getenv("GOOGLE_SHEETS_JSON") 

# Актив: Только Биткоин
SYMBOLS = ['BTC/USDT']

# Пары таймфреймов (Рабочий -> Фильтр тренда)
TIMEFRAME_PAIRS = [
    {'work': '1m', 'filter': '5m'},    # Скальпинг
    {'work': '3m', 'filter': '15m'},   # Скальпинг
    {'work': '5m', 'filter': '15m'},   # Быстрый интрадей
    {'work': '15m', 'filter': '1h'},   # Интрадей классика
    {'work': '30m', 'filter': '1h'},   # Интрадей спокойный
    {'work': '1h', 'filter': '4h'}     # Свинг
]

# Риск-менеджмент
MAX_SL_PCT = 0.008   # Максимальный стоп-лосс: 0.8% от цены
MIN_RR = 1.8         # Минимальное соотношение Прибыль/Риск

# Настройка логов (чтобы было время)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class TradingBot:
    def __init__(self):
        # Подключение к Bybit (V5 API)
        self.exchange = ccxt.bybit({
            'apiKey': API_KEY,
            'secret': API_SECRET,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'} # Деривативы
        })
        self.bot = Bot(token=TG_TOKEN)
        # Хранилище отправленных сигналов
        self.processed_signals = set() 
        
        # Подключение к Google Sheets
        self.sheet = None
        if GOOGLE_JSON:
            try:
                scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
                creds_dict = json.loads(GOOGLE_JSON)
                creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
                client = gspread.authorize(creds)
                # Открываем таблицу. Имя должно совпадать с вашей таблицей!
                self.sheet = client.open("BTC_Signals_Log").sheet1 
                logging.info("✅ Google Sheet connected successfully!")
            except Exception as e:
                logging.error(f"❌ Google Sheet Connection Failed: {e}")
        else:
            logging.warning("⚠️ No Google JSON found. Logging to Sheets disabled.")

    async def fetch_data(self, symbol, timeframe, limit=100):
        """Получает свечи"""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except Exception as e:
            logging.error(f"Error fetching {symbol} {timeframe}: {e}")
            return None

    async def get_funding(self, symbol):
        """Получает текущий фандинг"""
        try:
            funding = await self.exchange.fetch_funding_rate(symbol)
            return funding['fundingRate']
        except:
            return 0.0

    def calculate_indicators(self, df):
        """Расчет индикаторов"""
        df.ta.ema(length=5, append=True)
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.rsi(length=14, append=True)
        df.ta.macd(append=True)
        df.ta.vwap(append=True)
        # Локальные экстремумы для SL
        df['rolling_high'] = df['high'].rolling(8).max()
        df['rolling_low'] = df['low'].rolling(8).min()
        return df

    def check_global_trend(self, df_filter):
        """Тренд на старшем ТФ"""
        if df_filter is None: return 'FLAT'
        curr = df_filter.iloc[-1]
        
        if curr['close'] > curr['EMA_50'] and curr['EMA_20'] > curr['EMA_50']:
            return 'UP'
        elif curr['close'] < curr['EMA_50'] and curr['EMA_20'] < curr['EMA_50']:
            return 'DOWN'
        return 'FLAT'

    def log_to_sheet(self, symbol, timeframe, signal, funding):
        """Запись сигнала в Гугл Таблицу"""
        if not self.sheet: return
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row = [
                now, symbol, signal['side'], timeframe,
                float(signal['entry']), float(signal['sl']), float(signal['tp']),
                float(signal['rr']), f"{signal['risk']*100:.2f}%", f"{funding*100:.4f}%"
            ]
            self.sheet.append_row(row)
            logging.info(f"📝 Logged to Sheets: {symbol} {timeframe}")
        except Exception as e:
            logging.error(f"Failed to log to sheet: {e}")

    def generate_chart(self, df, symbol, signal, timeframe):
        """Рисуем график"""
        plot_df = df.tail(60)
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        
        apds = [
            mpf.make_addplot(plot_df['EMA_20'], color='cyan', width=0.8),
            mpf.make_addplot(plot_df['EMA_50'], color='orange', width=1.0),
            mpf.make_addplot(plot_df['VWAP_D'], color='purple', width=0.8, linestyle='--'),
        ]

        lines = dict(
            hlines=[signal['entry'], signal['sl'], signal['tp']],
            colors=['blue', 'red', 'green'],
            linewidths=[1, 1.5, 1.5],
            linestyle='-.'
        )

        title = f"\n{symbol} [{timeframe}] | {signal['side']} | R:R {signal['rr']}"

        buf = io.BytesIO()
        mpf.plot(
            plot_df, type='candle', style=style, addplot=apds,
            hlines=lines, volume=True, title=title,
            savefig=dict(fname=buf, dpi=150, bbox_inches='tight')
        )
        buf.seek(0)
        return buf

    async def analyze_pair(self, symbol, tf_pair):
        work_tf = tf_pair['work']
        filter_tf = tf_pair['filter']

        # !!! ЛОГИРОВАНИЕ: Пишем в лог перед началом анализа
        logging.info(f"🔎 Scanning {symbol} [TF: {work_tf}]...")

        # 1. Загрузка данных
        df_work = await self.fetch_data(symbol, work_tf)
        df_filter = await self.fetch_data(symbol, filter_tf)
        
        if df_work is None or df_filter is None: return

        # 2. Индикаторы
        df_work = self.calculate_indicators(df_work)
        df_filter = self.calculate_indicators(df_filter)

        # 3. Фильтр тренда
        trend = self.check_global_trend(df_filter)
        if trend == 'FLAT': return

        curr = df_work.iloc[-1]
        prev = df_work.iloc[-2]
        signal = None
        
        # --- ЛОГИКА LONG ---
        if trend == 'UP':
            if curr['RSI_14'] < 70 and curr['close'] > curr['EMA_50']:
                cond_ema = (prev['close'] < prev['EMA_20']) and (curr['close'] > curr['EMA_20'])
                cond_vwap = (curr['low'] <= curr['VWAP_D']) and (curr['close'] > curr['VWAP_D'])
                
                if cond_ema or cond_vwap:
                    sl_price = df_work['rolling_low'].iloc[-1]
                    entry_price = curr['close']
                    if sl_price >= entry_price: sl_price = entry_price * 0.995
                    risk_pct = (entry_price - sl_price) / entry_price
                    
                    if 0.001 < risk_pct <= MAX_SL_PCT:
                        tp_price = entry_price + (entry_price - sl_price) * 2.0
                        rr = round((tp_price - entry_price) / (entry_price - sl_price), 2)
                        
                        if rr >= MIN_RR:
                            signal = {
                                'side': 'LONG 🟢', 'entry': entry_price, 'sl': sl_price, 
                                'tp': tp_price, 'risk': risk_pct, 'rr': rr
                            }

        # --- ЛОГИКА SHORT ---
        elif trend == 'DOWN':
            if curr['RSI_14'] > 30 and curr['close'] < curr['EMA_50']:
                cond_ema = (prev['close'] > prev['EMA_20']) and (curr['close'] < curr['EMA_20'])
                cond_vwap = (curr['high'] >= curr['VWAP_D']) and (curr['close'] < curr['VWAP_D'])
                
                if cond_ema or cond_vwap:
                    sl_price = df_work['rolling_high'].iloc[-1]
                    entry_price = curr['close']
                    if sl_price <= entry_price: sl_price = entry_price * 1.005
                    risk_pct = (sl_price - entry_price) / entry_price
                    
                    if 0.001 < risk_pct <= MAX_SL_PCT:
                        tp_price = entry_price - (sl_price - entry_price) * 2.0
                        rr = round((entry_price - tp_price) / (sl_price - entry_price), 2)
                        
                        if rr >= MIN_RR:
                            signal = {
                                'side': 'SHORT 🔴', 'entry': entry_price, 'sl': sl_price, 
                                'tp': tp_price, 'risk': risk_pct, 'rr': rr
                            }

        # --- ОТПРАВКА СИГНАЛА ---
        if signal:
            sig_id = f"{symbol}_{signal['side']}_{work_tf}_{df_work.index[-1]}"
            
            if sig_id not in self.processed_signals:
                funding = await self.get_funding(symbol)
                
                # Запись в Google Sheets
                self.log_to_sheet(symbol, work_tf, signal, funding)

                # Генерация графика
                chart_img = self.generate_chart(df_work, symbol, signal, work_tf)
                
                msg = (
                    f"🚀 <b>{signal['side']} | #{symbol.replace('/','')}</b>\n"
                    f"⏱ <b>TF: {work_tf}</b> (Trend: {trend} on {filter_tf})\n"
                    f"---------------------------\n"
                    f"🎯 <b>Entry:</b> {signal['entry']}\n"
                    f"🛡 <b>Stop Loss:</b> {signal['sl']:.2f} ({signal['risk']*100:.2f}%)\n"
                    f"💰 <b>Take Profit:</b> {signal['tp']:.2f}\n"
                    f"⚖️ <b>R:R:</b> {signal['rr']}\n"
                    f"---------------------------\n"
                    f"📊 <b>Funding:</b> {funding*100:.4f}%\n"
                )
                
                try:
                    input_file = types.BufferedInputFile(chart_img.read(), filename="chart.png")
                    await self.bot.send_photo(
                        chat_id=TG_CHANNEL_ID, 
                        photo=input_file, 
                        caption=msg, 
                        parse_mode=ParseMode.HTML
                    )
                    self.processed_signals.add(sig_id)
                    logging.info(f"✅ SIGNAL SENT: {sig_id}")
                except Exception as e:
                    logging.error(f"❌ Telegram Error: {e}")

    async def run(self):
        logging.info("Bot started checking BTC/USDT on all timeframes...")
        
        while True:
            # Цикл по всем парам таймфреймов
            for tf_pair in TIMEFRAME_PAIRS:
                for symbol in SYMBOLS:
                    await self.analyze_pair(symbol, tf_pair)
                    # Минимальная задержка между запросами
                    await asyncio.sleep(0.5) 
            
            # Логика ожидания: проверяем раз в 30 секунд
            logging.info("Cycle finished. Waiting...")
            await asyncio.sleep(30)

    async def close(self):
        await self.exchange.close()
        await self.bot.session.close()

async def main():
    bot = TradingBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.error(f"Critical Error: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
