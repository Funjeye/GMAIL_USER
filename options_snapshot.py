"""
GME Options Snapshot & Alert — Tradier edition.

Pulls GME's price and cash-secured-put chain from the Tradier API (a keyed feed
that does not bot-block cloud IPs the way Yahoo does), appends each snapshot to a
CSV so an IV/premium history builds over time, and emails a formatted summary.

Tradier returns delta and IV directly in the greeks block, so no Black-Scholes is
needed; a pure-Python fallback is kept only for the rare rows where greeks are null.

Required env vars:
  TRADIER_TOKEN       — Tradier access token (sandbox or production)
  GMAIL_USER          — sender Gmail address
  GMAIL_APP_PASSWORD  — 16-char Gmail app password
Optional:
  TRADIER_BASE_URL    — defaults to production; set to https://sandbox.tradier.com/v1
                        if you're using a free developer sandbox token (delayed data).
"""
import os
import sys
import csv
import math
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import requests

# Configuration
SYMBOL = "GME"
RISK_FREE_RATE = 0.04
TARGET_EMAIL = "dsamar@gmail.com"
CSV_FILE = "data/gme_puts_history.csv"
# `or` (not a default arg) so an empty-string env from an unset GitHub secret still falls back.
TRADIER_BASE_URL = os.environ.get("TRADIER_BASE_URL") or "https://api.tradier.com/v1"
TRADIER_TOKEN = os.environ.get("TRADIER_TOKEN")


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def calculate_put_delta(S, K, T, r, sigma):
    # Fallback only — used when Tradier doesn't return a delta for a contract.
    if T <= 0 or sigma is None or sigma <= 0:
        return -1.0 if S <= K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) - 1.0


def tradier_get(path, params):
    if not TRADIER_TOKEN:
        raise ValueError("TRADIER_TOKEN is not set.")
    resp = requests.get(
        TRADIER_BASE_URL + path,
        params=params,
        headers={"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def as_list(x):
    # Tradier returns a single dict when a query yields one item, a list otherwise.
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def num(x):
    return x if isinstance(x, (int, float)) else None


def f2(x):
    x = num(x)
    return f"{x:.2f}" if x is not None else "n/a"


def firm(x):
    # IV comes from Tradier as a decimal (0.85 = 85%).
    x = num(x)
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def send_email(subject, body):
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")

    if not user or not password:
        print("Email credentials missing. Outputting to console instead:\n")
        print(f"Subject: {subject}\n\n{body}")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = TARGET_EMAIL
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)


def main():
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")

    try:
        # 1) Quote — last, bid, ask, and today's % change (already a percent, not a decimal).
        quote_json = tradier_get("/markets/quotes", {"symbols": SYMBOL})
        quotes = as_list(quote_json.get("quotes", {}).get("quote"))
        if not quotes:
            raise ValueError("Tradier returned no quote for GME.")
        q = quotes[0]
        last_price = num(q.get("last")) or num(q.get("prevclose"))
        if last_price is None:
            raise ValueError("Tradier quote had no usable price.")
        bid = num(q.get("bid"))
        ask = num(q.get("ask"))
        pct_change = num(q.get("change_percentage"))

        # 2) Expirations within the 5–35 day window.
        exp_json = tradier_get(
            "/markets/options/expirations", {"symbol": SYMBOL, "includeAllRoots": "true"}
        )
        all_expirations = as_list(exp_json.get("expirations", {}).get("date"))
        min_date = (now_utc + timedelta(days=5)).date()
        max_date = (now_utc + timedelta(days=35)).date()
        valid_expirations = [
            e for e in all_expirations
            if min_date <= datetime.strptime(e, "%Y-%m-%d").date() <= max_date
        ]

        strike_min = last_price * 0.82
        strike_max = last_price * 1.02

        pct_str = f"{pct_change:+.2f}%" if pct_change is not None else "n/a"
        email_body = (
            f"GME Current Price: ${f2(last_price)} | Bid: ${f2(bid)} | "
            f"Ask: ${f2(ask)} | Change: {pct_str}\n\n"
        )
        csv_rows = []

        # 3) Put chain per expiration, greeks included.
        for exp in valid_expirations:
            chain = tradier_get(
                "/markets/options/chains",
                {"symbol": SYMBOL, "expiration": exp, "greeks": "true"},
            )
            options = as_list(chain.get("options", {}).get("option") if chain.get("options") else None)
            puts = [
                o for o in options
                if o.get("option_type") == "put"
                and num(o.get("strike")) is not None
                and strike_min <= o["strike"] <= strike_max
            ]
            if not puts:
                continue
            puts.sort(key=lambda o: o["strike"])

            T = (datetime.strptime(exp, "%Y-%m-%d").date() - now_utc.date()).days / 365.0

            email_body += f"=== Expiration: {exp} ===\n"
            email_body += (
                f"{'Strike':<7} | {'Bid':<5} | {'Ask':<5} | {'Last':<5} | "
                f"{'Vol':<5} | {'OI':<6} | {'IV':<7} | {'Delta':<7}\n"
            )
            email_body += "-" * 68 + "\n"

            for o in puts:
                strike = float(o["strike"])
                greeks = o.get("greeks") or {}
                iv = num(greeks.get("mid_iv"))
                delta = num(greeks.get("delta"))
                if delta is None:  # greeks missing — fall back to Black-Scholes
                    delta = calculate_put_delta(last_price, strike, T, RISK_FREE_RATE, iv)
                bid_o = num(o.get("bid"))
                ask_o = num(o.get("ask"))
                last_o = num(o.get("last"))
                vol = int(o.get("volume") or 0)
                oi = int(o.get("open_interest") or 0)

                csv_rows.append({
                    "snapshot_datetime_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    "Expiry": exp,
                    "Strike": strike,
                    "Bid": bid_o,
                    "Ask": ask_o,
                    "Last": last_o,
                    "Volume": vol,
                    "Open Interest": oi,
                    "IV": iv,
                    "Delta": delta,
                })

                delta_str = f"{delta:>7.4f}" if delta is not None else f"{'n/a':>7}"
                email_body += (
                    f"${strike:<6.2f} | {f2(bid_o):<5} | {f2(ask_o):<5} | {f2(last_o):<5} | "
                    f"{vol:<5} | {oi:<6} | {firm(iv):>6} | {delta_str}\n"
                )
            email_body += "\n"

        # 4) Append to CSV history.
        os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
        file_exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, mode="a", newline="") as f:
            fieldnames = ["snapshot_datetime_utc", "Expiry", "Strike", "Bid", "Ask",
                          "Last", "Volume", "Open Interest", "IV", "Delta"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerows(csv_rows)

        send_email(f"GME CSP chain — {date_str}", email_body)
        print(f"Data pulled ({len(csv_rows)} put rows across {len(valid_expirations)} "
              f"expirations), saved, and emailed successfully.")

    except Exception as e:
        error_msg = f"GME options pull FAILED: {str(e)}"
        print(error_msg)
        send_email(f"ALERT: GME options pull FAILED — {date_str}", error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
