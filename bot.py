import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
import yfinance as yf
from telegram import Bot
import asyncio
import logging
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD
from ta.volatility import AverageTrueRange
import time

# ========== تنظیمات اولیه ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    logger.warning("⚠️ TELEGRAM_TOKEN یا CHAT_ID تنظیم نشده است!")

# ========== تنظیمات مدیریت ریسک - سناریوی حرفه‌ای ==========
class RiskConfigProfessional:
    MAX_POSITION_SIZE = 0.12
    STOP_LOSS = 0.025
    TAKE_PROFIT = 0.05
    MIN_CONFIDENCE = 68
    MAX_DAILY_LOSS = 0.04
    NAME = "حرفه‌ای"

# ========== کلاس مدیریت ریسک و معاملات ==========
class TradingSimulator:
    def __init__(self, initial_capital=10000, config=RiskConfigProfessional):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.config = config
        self.trades = []
        self.positions = {}
        self.daily_pnl = 0
        self.total_pnl = 0
        self.win_count = 0
        self.loss_count = 0
        self.peak_capital = initial_capital
        self.max_drawdown = 0
        self.last_trade_day = None
        self.is_running = True
        
    def calculate_position_size(self, price, atr):
        stop_distance = max(self.config.STOP_LOSS * price, atr * 1.5)
        max_risk = self.current_capital * self.config.STOP_LOSS
        position_size = max_risk / stop_distance
        max_position = (self.current_capital * self.config.MAX_POSITION_SIZE) / price
        position_size = min(position_size, max_position)
        if position_size < 0.001:
            return 0
        return position_size
    
    def get_trading_signal(self, df, current_idx):
        if current_idx < 20:
            return None, 0, {}
        
        current_price = df['Close'].iloc[current_idx]
        current_volume = df['Volume'].iloc[current_idx]
        
        rsi = RSIIndicator(df['Close'], window=14).rsi().iloc[current_idx]
        macd = MACD(df['Close']).macd_diff().iloc[current_idx]
        atr = AverageTrueRange(df['High'], df['Low'], df['Close']).average_true_range().iloc[current_idx]
        atr_percent = (atr / current_price) * 100
        
        avg_volume = df['Volume'].iloc[current_idx-20:current_idx].mean()
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
        
        score = 0
        details = {}
        
        if rsi < 30:
            score += 1
            details['rsi'] = 'oversold'
        elif rsi > 70:
            score -= 1
            details['rsi'] = 'overbought'
        else:
            details['rsi'] = 'neutral'
        
        if macd > 0:
            score += 0.5
            details['macd'] = 'bullish'
        else:
            score -= 0.5
            details['macd'] = 'bearish'
        
        sma_20 = df['Close'].rolling(20).mean().iloc[current_idx]
        sma_50 = df['Close'].rolling(50).mean().iloc[current_idx] if current_idx > 50 else sma_20
        
        if current_price > sma_20 and current_price > sma_50:
            score += 0.5
            details['trend'] = 'bullish'
        elif current_price < sma_20 and current_price < sma_50:
            score -= 0.5
            details['trend'] = 'bearish'
        else:
            details['trend'] = 'neutral'
        
        if volume_ratio > 1.5 and score > 0:
            score += 0.5
            details['volume'] = 'high'
        elif volume_ratio < 0.5 and score < 0:
            score -= 0.5
            details['volume'] = 'low'
        
        if 0.5 < atr_percent < 3:
            score += 0.5
            details['atr'] = 'normal'
        elif atr_percent > 5:
            score -= 1
            details['atr'] = 'high_volatility'
        
        if score >= 2:
            signal = 'BUY'
            confidence = min(70 + score * 5, 95)
        elif score <= -2:
            signal = 'SELL'
            confidence = min(70 + abs(score) * 5, 95)
        else:
            signal = 'HOLD'
            confidence = 50
        
        details['score'] = score
        return signal, confidence, details
    
    def execute_trade(self, symbol, signal, price, atr, details, timestamp):
        if signal == 'HOLD':
            return None
        
        if symbol in self.positions and self.positions[symbol]['type'] == signal:
            return None
        
        position_size = self.calculate_position_size(price, atr)
        if position_size == 0:
            return None
        
        if signal == 'BUY':
            stop_loss = price * (1 - self.config.STOP_LOSS)
            take_profit = price * (1 + self.config.TAKE_PROFIT)
        else:
            stop_loss = price * (1 + self.config.STOP_LOSS)
            take_profit = price * (1 - self.config.TAKE_PROFIT)
        
        position = {
            'type': signal,
            'entry_price': price,
            'position_size': position_size,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'entry_time': timestamp,
            'atr': atr,
            'details': details
        }
        
        self.positions[symbol] = position
        logger.info(f"📈 {timestamp.strftime('%Y-%m-%d %H:%M')} - {signal} {symbol} @ {price:.2f} | حجم: {position_size:.4f}")
        return position
    
    def close_position(self, symbol, current_price, timestamp, exit_reason):
        if symbol not in self.positions:
            return None
        
        position = self.positions[symbol]
        
        if position['type'] == 'BUY':
            pnl_percent = (current_price - position['entry_price']) / position['entry_price']
        else:
            pnl_percent = (position['entry_price'] - current_price) / position['entry_price']
        
        pnl_amount = pnl_percent * position['position_size'] * position['entry_price']
        
        trade = {
            'symbol': symbol,
            'type': position['type'],
            'entry_price': position['entry_price'],
            'exit_price': current_price,
            'position_size': position['position_size'],
            'pnl_percent': pnl_percent * 100,
            'pnl_amount': pnl_amount,
            'entry_time': position['entry_time'],
            'exit_time': timestamp,
            'duration_hours': (timestamp - position['entry_time']).total_seconds() / 3600,
            'exit_reason': exit_reason
        }
        self.trades.append(trade)
        
        self.current_capital += pnl_amount
        self.total_pnl += pnl_amount
        self.daily_pnl += pnl_amount
        
        if pnl_amount > 0:
            self.win_count += 1
        else:
            self.loss_count += 1
        
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital
        else:
            drawdown = (self.peak_capital - self.current_capital) / self.peak_capital * 100
            self.max_drawdown = max(self.max_drawdown, drawdown)
        
        del self.positions[symbol]
        
        logger.info(f"📉 {timestamp.strftime('%Y-%m-%d %H:%M')} - {exit_reason} {symbol} @ {current_price:.2f} | سود/زیان: {pnl_amount:.2f} ({pnl_percent*100:+.2f}%)")
        return trade
    
    def update_positions(self, df, current_idx):
        current_price = df['Close'].iloc[current_idx]
        timestamp = df.index[current_idx]
        exits = []
        
        for symbol in list(self.positions.keys()):
            position = self.positions[symbol]
            
            if position['type'] == 'BUY':
                if current_price <= position['stop_loss']:
                    exit_reason = 'STOP_LOSS'
                    exit_price = position['stop_loss']
                elif current_price >= position['take_profit']:
                    exit_reason = 'TAKE_PROFIT'
                    exit_price = position['take_profit']
                else:
                    continue
            else:
                if current_price >= position['stop_loss']:
                    exit_reason = 'STOP_LOSS'
                    exit_price = position['stop_loss']
                elif current_price <= position['take_profit']:
                    exit_reason = 'TAKE_PROFIT'
                    exit_price = position['take_profit']
                else:
                    continue
            
            trade = self.close_position(symbol, exit_price, timestamp, exit_reason)
            if trade:
                exits.append(trade)
        
        return exits
    
    def check_entry(self, symbol, df, current_idx):
        if current_idx < 20:
            return None
        
        signal, confidence, details = self.get_trading_signal(df, current_idx)
        
        if confidence < self.config.MIN_CONFIDENCE:
            return None
        
        if self.daily_pnl < -self.config.MAX_DAILY_LOSS * self.current_capital:
            return None
        
        if signal in ['BUY', 'SELL']:
            atr = AverageTrueRange(df['High'], df['Low'], df['Close']).average_true_range().iloc[current_idx]
            current_price = df['Close'].iloc[current_idx]
            timestamp = df.index[current_idx]
            return self.execute_trade(symbol, signal, current_price, atr, details, timestamp)
        
        return None
    
    def process_market_data(self, df, symbol='GOLD'):
        if df.empty or len(df) < 50:
            logger.warning(f"⚠️ داده کافی برای {symbol} وجود ندارد")
            return
        
        last_idx = len(df) - 1
        
        current_day = df.index[last_idx].day
        if self.last_trade_day != current_day:
            self.daily_pnl = 0
            self.last_trade_day = current_day
        
        exits = self.update_positions(df, last_idx)
        entry = self.check_entry(symbol, df, last_idx)
        
        return exits, entry
    
    def get_performance_metrics(self):
        if len(self.trades) == 0:
            return {
                'config_name': self.config.NAME,
                'total_return': 0,
                'win_rate': 0,
                'profit_factor': 0,
                'sharpe_ratio': 0,
                'max_drawdown': 0,
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'final_capital': self.current_capital
            }
        
        total_return = (self.current_capital - self.initial_capital) / self.initial_capital * 100
        win_rate = self.win_count / len(self.trades) * 100 if len(self.trades) > 0 else 0
        
        pnl_values = [t['pnl_amount'] for t in self.trades]
        avg_win = np.mean([p for p in pnl_values if p > 0]) if any(p > 0 for p in pnl_values) else 0
        avg_loss = np.mean([abs(p) for p in pnl_values if p < 0]) if any(p < 0 for p in pnl_values) else 0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else 0
        
        returns = [t['pnl_percent'] for t in self.trades]
        if len(returns) > 1:
            annual_return = np.mean(returns) * 252
            annual_std = np.std(returns) * np.sqrt(252)
            sharpe_ratio = annual_return / annual_std if annual_std > 0 else 0
        else:
            sharpe_ratio = 0
        
        return {
            'config_name': self.config.NAME,
            'total_return': total_return,
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': self.max_drawdown,
            'total_trades': len(self.trades),
            'winning_trades': self.win_count,
            'losing_trades': self.loss_count,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'final_capital': self.current_capital,
            'trades': self.trades[-5:]
        }

# ========== توابع دریافت داده ==========
def get_iran_gold_18k():
    try:
        url = "https://www.tgju.org/profile/geram18"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        price_element = soup.find("span", {"data-col": "info.last_trade.PDrrVal"})
        if price_element:
            text = price_element.get_text(strip=True).replace(",", "").replace("ریال", "").strip()
            if text and text.isdigit():
                return int(text)
        
        for td in soup.find_all("td"):
            text = td.get_text(strip=True).replace(",", "").replace("ریال", "").strip()
            if text and text.isdigit() and len(text) >= 7:
                return int(text)
        
        return None
    except Exception as e:
        logger.error(f"خطا در دریافت طلای ایران: {e}")
        return None

def get_market_data(ticker, days=60):
    try:
        ticker_obj = yf.Ticker(ticker)
        hist = ticker_obj.history(period=f"{days}d")
        if hist.empty:
            return None
        return hist
    except Exception as e:
        logger.error(f"خطا در دریافت داده‌های {ticker}: {e}")
        return None

# ========== توابع گزارش و ارسال ==========
def generate_report(simulator_gold, simulator_silver, iran_gold_price):
    now = datetime.now(pytz.timezone("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S")
    
    metrics_gold = simulator_gold.get_performance_metrics()
    metrics_silver = simulator_silver.get_performance_metrics()
    
    total_capital = metrics_gold['final_capital'] + metrics_silver['final_capital'] - 20000
    total_return = ((metrics_gold['final_capital'] + metrics_silver['final_capital']) / 20000 - 1) * 100
    
    report = f"""
🧠 **ربات معامله‌گر هوشمند طلا و نقره**
⏰ **زمان:** {now}
💰 **سرمایه اولیه:** $۲۰,۰۰۰ (۱۰,۰۰۰ طلا + ۱۰,۰۰۰ نقره)
📊 **استراتژی:** سناریوی حرفه‌ای (حد ضرر ۲.۵٪ - حد سود ۵٪)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🇮🇷 **طلای ۱۸ عیار ایران:** {iran_gold_price:,} ریال

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 **طلای جهانی (GC=F):**
💰 سرمایه فعلی: ${metrics_gold['final_capital']:,.2f}
📈 بازده کل: {metrics_gold['total_return']:+.2f}%
📊 نرخ موفقیت: {metrics_gold['win_rate']:.1f}%
🎯 نسبت سود/ضرر: {metrics_gold['profit_factor']:.2f}
📉 حداکثر ریزش: {metrics_gold['max_drawdown']:.2f}%
📊 نسبت شارپ: {metrics_gold['sharpe_ratio']:.2f}
🔄 تعداد معاملات: {metrics_gold['total_trades']}
✅ معاملات موفق: {metrics_gold['winning_trades']}
❌ معاملات ناموفق: {metrics_gold['losing_trades']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 **نقره جهانی (XAGUSD=X):**
💰 سرمایه فعلی: ${metrics_silver['final_capital']:,.2f}
📈 بازده کل: {metrics_silver['total_return']:+.2f}%
📊 نرخ موفقیت: {metrics_silver['win_rate']:.1f}%
🎯 نسبت سود/ضرر: {metrics_silver['profit_factor']:.2f}
📉 حداکثر ریزش: {metrics_silver['max_drawdown']:.2f}%
📊 نسبت شارپ: {metrics_silver['sharpe_ratio']:.2f}
🔄 تعداد معاملات: {metrics_silver['total_trades']}
✅ معاملات موفق: {metrics_silver['winning_trades']}
❌ معاملات ناموفق: {metrics_silver['losing_trades']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 **جمع کل سرمایه:**
💰 سرمایه کل: ${total_capital + 20000:,.2f}
📈 بازده کل: {total_return:+.2f}%

📝 **۵ معامله اخیر طلا:**
"""
    for trade in metrics_gold['trades'][-5:]:
        report += f"• {trade['exit_time'].strftime('%Y-%m-%d')} | {trade['type']} | {trade['pnl_percent']:+.2f}% | {trade['exit_reason']}\n"
    
    report += f"""
📝 **۵ معامله اخیر نقره:**
"""
    for trade in metrics_silver['trades'][-5:]:
        report += f"• {trade['exit_time'].strftime('%Y-%m-%d')} | {trade['type']} | {trade['pnl_percent']:+.2f}% | {trade['exit_reason']}\n"
    
    report += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🛡️ **قوانین مدیریت ریسک (سناریوی حرفه‌ای):**
• حداکثر حجم هر معامله: ۱۲٪ سرمایه
• حد ضرر: ۲.۵٪
• حد سود: ۵٪ (نسبت ۱:۲)
• حداقل اطمینان: ۶۸٪
• حداکثر ضرر روزانه: ۴٪

⚠️ **توجه:** این تحلیل بر اساس داده‌های تاریخی و شبیه‌سازی است و توصیه مالی محسوب نمی‌شود.
"""
    return report.strip()

async def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        logger.error("❌ توکن یا چت آیدی تنظیم نشده است!")
        return False
    
    try:
        bot = Bot(token=TOKEN)
        if len(text) > 4096:
            for i in range(0, len(text), 4096):
                await bot.send_message(chat_id=CHAT_ID, text=text[i:i+4096], parse_mode='Markdown')
        else:
            await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='Markdown')
        logger.info("✅ پیام با موفقیت به تلگرام ارسال شد")
        return True
    except Exception as e:
        logger.error(f"❌ خطا در ارسال به تلگرام: {e}")
        return False

# ========== تابع اصلی ==========
def main():
    logger.info("🚀 شروع ربات معامله‌گر با سناریوی حرفه‌ای...")
    
    iran_gold = get_iran_gold_18k()
    if not iran_gold:
        logger.warning("⚠️ قیمت طلای ایران دریافت نشد")
        iran_gold = 0
    
    gold_data = get_market_data("GC=F", days=60)
    silver_data = get_market_data("XAGUSD=X", days=60)
    
    if gold_data is None or silver_data is None:
        logger.error("❌ داده‌های بازار دریافت نشد")
        return
    
    simulator_gold = TradingSimulator(initial_capital=10000, config=RiskConfigProfessional)
    simulator_silver = TradingSimulator(initial_capital=10000, config=RiskConfigProfessional)
    
    logger.info("🔄 پردازش داده‌های طلا...")
    simulator_gold.process_market_data(gold_data, 'GOLD')
    
    logger.info("🔄 پردازش داده‌های نقره...")
    simulator_silver.process_market_data(silver_data, 'SILVER')
    
    logger.info("📝 تولید گزارش...")
    report = generate_report(simulator_gold, simulator_silver, iran_gold)
    print(report)
    
    if TOKEN and CHAT_ID:
        asyncio.run(send_telegram(report))
    else:
        logger.warning("⚠️ توکن یا چت آیدی تنظیم نشده، گزارش فقط در کنسول نمایش داده شد")

if __name__ == "__main__":
    main()