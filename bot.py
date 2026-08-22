"""
Gold & Silver Trading Bot - Professional Risk Management Strategy
All comments are in English for clarity.
"""

import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
import yfinance as yf
from telegram import Bot
from telegram.error import TelegramError
import asyncio
import logging
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD
from ta.volatility import AverageTrueRange
import time
import sys

# =====================================================
# 1. LOGGING CONFIGURATION - Enhanced for debugging
# =====================================================

logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG for more details
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =====================================================
# 2. ENVIRONMENT VARIABLES - Check them immediately
# =====================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

logger.info(f"🔍 TELEGRAM_TOKEN exists: {'Yes' if TOKEN else 'NO!'}")
logger.info(f"🔍 CHAT_ID exists: {'Yes' if CHAT_ID else 'NO!'}")
logger.info(f"🔍 TOKEN length: {len(TOKEN) if TOKEN else 0}")

if not TOKEN or not CHAT_ID:
    logger.error("❌ CRITICAL: TELEGRAM_TOKEN or CHAT_ID is not set!")
    logger.error("❌ Please check GitHub Secrets configuration.")
    sys.exit(1)

# =====================================================
# 3. RISK MANAGEMENT CONFIGURATION
# =====================================================

class RiskConfigProfessional:
    """Professional risk management configuration."""
    MAX_POSITION_SIZE = 0.12
    STOP_LOSS = 0.025
    TAKE_PROFIT = 0.05
    MIN_CONFIDENCE = 68
    MAX_DAILY_LOSS = 0.04
    NAME = "Professional"

# =====================================================
# 4. TRADING SIMULATOR CLASS (simplified for debugging)
# =====================================================

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
        logger.info(f"📈 {timestamp.strftime('%Y-%m-%d %H:%M')} - {signal} {symbol} @ {price:.2f} | Size: {position_size:.4f}")
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
        
        logger.info(f"📉 {timestamp.strftime('%Y-%m-%d %H:%M')} - {exit_reason} {symbol} @ {current_price:.2f} | P&L: {pnl_amount:.2f} ({pnl_percent*100:+.2f}%)")
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
            logger.warning(f"⚠️ Insufficient data for {symbol}")
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

# =====================================================
# 5. DATA FETCHING FUNCTIONS
# =====================================================

def get_iran_gold_18k():
    """Fetch Iran 18-karat gold price from tgju.org."""
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
        logger.error(f"Error fetching Iran gold price: {e}")
        return None

def get_market_data(ticker, days=60):
    """Fetch market data from Yahoo Finance."""
    try:
        ticker_obj = yf.Ticker(ticker)
        hist = ticker_obj.history(period=f"{days}d")
        if hist.empty:
            return None
        return hist
    except Exception as e:
        logger.error(f"Error fetching data for {ticker}: {e}")
        return None

# =====================================================
# 6. REPORT GENERATION FUNCTIONS
# =====================================================

def generate_report(simulator_gold, simulator_silver, iran_gold_price):
    """Generate a comprehensive performance report for Telegram."""
    now = datetime.now(pytz.timezone("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S")
    
    metrics_gold = simulator_gold.get_performance_metrics()
    metrics_silver = simulator_silver.get_performance_metrics()
    
    total_capital = metrics_gold['final_capital'] + metrics_silver['final_capital'] - 20000
    total_return = ((metrics_gold['final_capital'] + metrics_silver['final_capital']) / 20000 - 1) * 100
    
    report = f"""
🧠 **Gold & Silver Intelligent Trading Bot**
⏰ **Time:** {now}
💰 **Initial Capital:** $20,000 ($10,000 Gold + $10,000 Silver)
📊 **Strategy:** Professional Scenario (2.5% Stop Loss - 5% Take Profit)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🇮🇷 **Iran 18-Karat Gold:** {iran_gold_price:,} Rials

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 **Gold (GC=F):**
💰 Current Capital: ${metrics_gold['final_capital']:,.2f}
📈 Total Return: {metrics_gold['total_return']:+.2f}%
📊 Win Rate: {metrics_gold['win_rate']:.1f}%
🎯 Profit Factor: {metrics_gold['profit_factor']:.2f}
📉 Max Drawdown: {metrics_gold['max_drawdown']:.2f}%
📊 Sharpe Ratio: {metrics_gold['sharpe_ratio']:.2f}
🔄 Total Trades: {metrics_gold['total_trades']}
✅ Winning Trades: {metrics_gold['winning_trades']}
❌ Losing Trades: {metrics_gold['losing_trades']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 **Silver (XAGUSD=X):**
💰 Current Capital: ${metrics_silver['final_capital']:,.2f}
📈 Total Return: {metrics_silver['total_return']:+.2f}%
📊 Win Rate: {metrics_silver['win_rate']:.1f}%
🎯 Profit Factor: {metrics_silver['profit_factor']:.2f}
📉 Max Drawdown: {metrics_silver['max_drawdown']:.2f}%
📊 Sharpe Ratio: {metrics_silver['sharpe_ratio']:.2f}
🔄 Total Trades: {metrics_silver['total_trades']}
✅ Winning Trades: {metrics_silver['winning_trades']}
❌ Losing Trades: {metrics_silver['losing_trades']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 **Combined Portfolio:**
💰 Total Capital: ${total_capital + 20000:,.2f}
📈 Total Return: {total_return:+.2f}%

📝 **Last 5 Gold Trades:**
"""
    for trade in metrics_gold['trades'][-5:]:
        report += f"• {trade['exit_time'].strftime('%Y-%m-%d')} | {trade['type']} | {trade['pnl_percent']:+.2f}% | {trade['exit_reason']}\n"
    
    report += f"""
📝 **Last 5 Silver Trades:**
"""
    for trade in metrics_silver['trades'][-5:]:
        report += f"• {trade['exit_time'].strftime('%Y-%m-%d')} | {trade['type']} | {trade['pnl_percent']:+.2f}% | {trade['exit_reason']}\n"
    
    report += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🛡️ **Risk Management Rules (Professional Scenario):**
• Max Position Size: 12% of capital
• Stop Loss: 2.5%
• Take Profit: 5% (1:2 risk-reward)
• Min Confidence: 68%
• Max Daily Loss: 4%

⚠️ **Disclaimer:** This analysis is based on historical data and simulation.
This is not financial advice. Past performance does not guarantee future results.
"""
    return report.strip()

# =====================================================
# 7. TELEGRAM SENDING FUNCTION - Enhanced with error handling
# =====================================================

async def send_telegram(text):
    """
    Send a message to Telegram using the bot.
    Returns True if successful, False otherwise.
    """
    logger.info("📤 Attempting to send message to Telegram...")
    
    if not TOKEN or not CHAT_ID:
        logger.error("❌ TELEGRAM_TOKEN or CHAT_ID is not set!")
        return False
    
    try:
        # Create bot instance
        bot = Bot(token=TOKEN)
        logger.info(f"🤖 Bot created with token: {TOKEN[:10]}...")
        
        # Try to get bot info to verify token is valid
        try:
            me = await bot.get_me()
            logger.info(f"✅ Bot authenticated: @{me.username}")
        except TelegramError as e:
            logger.error(f"❌ Bot authentication failed: {e}")
            return False
        
        # Send the message
        logger.info(f"📤 Sending message to chat_id: {CHAT_ID}")
        
        # Split message if it's too long
        if len(text) > 4096:
            logger.info("📝 Message too long, splitting into parts...")
            for i in range(0, len(text), 4096):
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=text[i:i+4096],
                    parse_mode='Markdown'
                )
        else:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode='Markdown'
            )
        
        logger.info("✅ Message successfully sent to Telegram!")
        return True
        
    except TelegramError as e:
        logger.error(f"❌ Telegram error: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error sending to Telegram: {e}")
        return False

# =====================================================
# 8. TEST TELEGRAM CONNECTION FUNCTION
# =====================================================

async def test_telegram():
    """Test function to verify Telegram connection."""
    logger.info("🧪 Running Telegram connection test...")
    
    test_message = """
🧪 **Test Message from Gold/Silver Trading Bot**

✅ If you see this message, the Telegram connection is working!
✅ Your bot token and chat ID are correct.

⏰ Test time: """ + datetime.now(pytz.timezone("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S")

    return await send_telegram(test_message)

# =====================================================
# 9. MAIN FUNCTION
# =====================================================

def main():
    """
    Main entry point for the trading bot.
    """
    logger.info("🚀 Starting Trading Bot with Professional Strategy...")
    
    # =============================================
    # STEP 1: Test Telegram connection immediately
    # =============================================
    logger.info("📱 Testing Telegram connection first...")
    
    try:
        test_result = asyncio.run(test_telegram())
        if not test_result:
            logger.error("❌ Telegram connection test FAILED! Check your token and CHAT_ID.")
            logger.error("❌ Bot will continue but messages may not be sent.")
        else:
            logger.info("✅ Telegram connection test PASSED!")
    except Exception as e:
        logger.error(f"❌ Telegram test threw exception: {e}")
    
    # =============================================
    # STEP 2: Fetch data
    # =============================================
    logger.info("📊 Fetching market data...")
    
    # Iran gold price
    iran_gold = get_iran_gold_18k()
    if not iran_gold:
        logger.warning("⚠️ Iran gold price not available")
        iran_gold = 0
    else:
        logger.info(f"✅ Iran gold: {iran_gold:,} Rials")
    
    # Gold and silver data
    gold_data = get_market_data("GC=F", days=60)
    silver_data = get_market_data("XAGUSD=X", days=60)
    
    if gold_data is None:
        logger.error("❌ Gold data not available")
        gold_data = pd.DataFrame()
    
    if silver_data is None:
        logger.error("❌ Silver data not available")
        silver_data = pd.DataFrame()
    
    if gold_data.empty or silver_data.empty:
        logger.warning("⚠️ Some market data is missing, but continuing...")
    
    # =============================================
    # STEP 3: Run simulation
    # =============================================
    logger.info("🔄 Running simulations...")
    
    simulator_gold = TradingSimulator(initial_capital=10000, config=RiskConfigProfessional)
    simulator_silver = TradingSimulator(initial_capital=10000, config=RiskConfigProfessional)
    
    if not gold_data.empty:
        simulator_gold.process_market_data(gold_data, 'GOLD')
    else:
        logger.warning("⚠️ Skipping gold simulation - no data")
    
    if not silver_data.empty:
        simulator_silver.process_market_data(silver_data, 'SILVER')
    else:
        logger.warning("⚠️ Skipping silver simulation - no data")
    
    # =============================================
    # STEP 4: Generate and send report
    # =============================================
    logger.info("📝 Generating report...")
    report = generate_report(simulator_gold, simulator_silver, iran_gold)
    
    # Always print to console (visible in GitHub Actions logs)
    print("\n" + "="*80)
    print("📊 TRADING BOT REPORT")
    print("="*80)
    print(report)
    print("="*80 + "\n")
    
    # Send to Telegram
    if TOKEN and CHAT_ID:
        logger.info("📤 Sending report to Telegram...")
        try:
            result = asyncio.run(send_telegram(report))
            if result:
                logger.info("✅ Report sent successfully!")
            else:
                logger.error("❌ Failed to send report to Telegram")
        except Exception as e:
            logger.error(f"❌ Exception while sending to Telegram: {e}")
    else:
        logger.warning("⚠️ TELEGRAM_TOKEN or CHAT_ID not set, report printed to console only")
    
    logger.info("🏁 Bot execution complete!")

# =====================================================
# 10. SCRIPT ENTRY POINT
# =====================================================

if __name__ == "__main__":
    main()
