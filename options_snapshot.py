"""
GME Options Snapshot & Alert
If yfinance gets blocked long-term, swap the fetch logic below for the Tradier API (or Polygon) 
without changing the CSV, Black-Scholes, or email logic.
"""
import os
import sys
import csv
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import yfinance as yf
import pandas as pd
import numpy as np
from scipy.stats import norm

# Configuration
SYMBOL = "GME"
RISK_FREE_RATE = 0.04
TARGET_EMAIL = "dsamar@gmail.com"
CSV_FILE = "data/gme_puts_history.csv"

def calculate_put_delta(S, K, T, r, sigma):
    # Standard Black-Scholes put delta formula. Returns 0 if expired or IV is 0.
    if T <= 0 or sigma <= 0 or pd.isna(sigma):
        return -1.0 if S <= K else 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return norm.cdf(d1) - 1.0

def send_email(subject, body):
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    
    if not user or not password:
        print("Email credentials missing. Outputting to console instead:\n")
        print(f"Subject: {subject}\n\n{body}")
        return

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = TARGET_EMAIL
    msg.set_content(body)

    # smtp.gmail.com uses port 587 for STARTTLS
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)

def main():
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")
    
    try:
        ticker = yf.Ticker(SYMBOL)
        
        # Fetching price history to calculate reliable % change (fast_info doesn't always have previous close)
        hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError("Yahoo Finance returned empty price history.")
            
        last_price = float(hist['Close'].iloc[-1])
        prev_close = float(hist['Close'].iloc[-2]) if len(hist) > 1 else last_price
        pct_change = ((last_price - prev_close) / prev_close) * 100
        
        # Bid/ask come from ticker.info — the flakiest call in yfinance (Yahoo
        # rate-limits it hard from cloud IPs). Never let it sink the whole run;
        # the options chain below is the actual payload and must survive a bad .info.
        bid = ask = float("nan")
        try:
            info = ticker.info
            bid = info.get('bid') or float("nan")
            ask = info.get('ask') or float("nan")
        except Exception as e:
            print(f"Warning: bid/ask fetch failed ({e}); continuing without it.")
        
        min_date = (now_utc + timedelta(days=5)).date()
        max_date = (now_utc + timedelta(days=35)).date()
        
        all_expirations = ticker.options
        valid_expirations = []
        for exp in all_expirations:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            if min_date <= exp_date <= max_date:
                valid_expirations.append(exp)
                
        strike_min = last_price * 0.82
        strike_max = last_price * 1.02
        
        email_body = f"GME Current Price: ${last_price:.2f} | Bid: ${bid:.2f} | Ask: ${ask:.2f} | Change: {pct_change:+.2f}%\n\n"
        csv_rows = []
        
        for exp in valid_expirations:
            chain = ticker.option_chain(exp)
            puts = chain.puts
            
            # Filter by dynamic strike range
            filtered_puts = puts[(puts['strike'] >= strike_min) & (puts['strike'] <= strike_max)].copy()
            if filtered_puts.empty:
                continue
                
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            # Calculate time to expiration in years for Black-Scholes
            T = (exp_date - now_utc.date()).days / 365.0
            
            email_body += f"=== Expiration: {exp} ===\n"
            email_body += f"{'Strike':<7} | {'Bid':<5} | {'Ask':<5} | {'Last':<5} | {'Vol':<5} | {'OI':<5} | {'IV':<7} | {'Delta':<7}\n"
            email_body += "-" * 67 + "\n"
            
            for _, row in filtered_puts.iterrows():
                strike = float(row['strike'])
                iv = float(row['impliedVolatility'])
                delta = calculate_put_delta(last_price, strike, T, RISK_FREE_RATE, iv)
                
                vol = int(row['volume']) if not pd.isna(row['volume']) else 0
                oi = int(row['openInterest']) if not pd.isna(row['openInterest']) else 0
                
                csv_rows.append({
                    "snapshot_datetime_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    "Expiry": exp,
                    "Strike": strike,
                    "Bid": row['bid'],
                    "Ask": row['ask'],
                    "Last": row['lastPrice'],
                    "Volume": vol,
                    "Open Interest": oi,
                    "IV": iv,
                    "Delta": delta
                })
                
                email_body += f"${strike:<6.2f} | {row['bid']:<5.2f} | {row['ask']:<5.2f} | {row['lastPrice']:<5.2f} | {vol:<5} | {oi:<5} | {iv:>6.1%} | {delta:>7.4f}\n"
            
            email_body += "\n"
        
        # Write to CSV
        os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
        file_exists = os.path.isfile(CSV_FILE)
        
        with open(CSV_FILE, mode='a', newline='') as f:
            fieldnames = ["snapshot_datetime_utc", "Expiry", "Strike", "Bid", "Ask", "Last", "Volume", "Open Interest", "IV", "Delta"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerows(csv_rows)
            
        send_email(f"GME CSP chain — {date_str}", email_body)
        print("Data pulled, saved, and emailed successfully.")

    except Exception as e:
        error_msg = f"GME options pull FAILED: {str(e)}"
        print(error_msg)
        send_email(f"ALERT: GME options pull FAILED — {date_str}", error_msg)
        sys.exit(1)

if __name__ == "__main__":
    main()
