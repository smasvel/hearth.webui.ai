"""Build static daily closing-price charts from Nasdaq historical records."""
import datetime as dt
import math
import re

import requests

SVG_FENCE = re.compile(r"```svg\r?\n(<svg\b[\s\S]*?</svg>)\r?\n```")


def parse_request(text: str, previous_symbol: str | None = None) -> tuple[str, int] | None:
    if not re.search(r"\bcharts?\b", text, re.I):
        return None
    years = re.search(r"\b([1-5])\s*(?:years?|y)\b", text, re.I)
    daily = re.search(r"\b(?:daily|1\s*d)\b", text, re.I)
    symbols = re.findall(r"(?<![\w$])\$?([A-Z]{1,5})(?![\w])", text)
    symbols = [symbol for symbol in symbols if symbol not in {"D", "Y", "USD", "SHOW", "CHART", "DAILY", "HERE", "ME", "FOR"}]
    if years and daily and len(set(symbols)) == 1:
        return symbols[0], int(years[1])
    if years and daily and not symbols and previous_symbol:
        return previous_symbol, int(years[1])
    return None


def fetch_daily_chart(symbol: str, years: int) -> str:
    end = dt.date.today()
    try:
        start = end.replace(year=end.year - years)
    except ValueError:
        start = end.replace(year=end.year - years, day=28)
    try:
        response = requests.get(
            f"https://api.nasdaq.com/api/quote/{symbol}/historical",
            params={"assetclass": "stocks", "fromdate": start.isoformat(),
                    "todate": end.isoformat(), "limit": 2000},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=12,
        )
        response.raise_for_status()
        data = response.json()["data"]
        if data["symbol"] != symbol:
            raise ValueError("Unexpected symbol")
        points = {}
        for row in data["tradesTable"]["rows"]:
            day = dt.datetime.strptime(row["date"], "%m/%d/%Y").date()
            price = float(row["close"].replace("$", "").replace(",", ""))
            if start <= day <= end and math.isfinite(price) and price > 0:
                points[day] = price
    except (requests.RequestException, ValueError, KeyError, TypeError):
        points = {}
    if len(points) < 2:
        return f"Nasdaq did not return enough verified daily closing prices to draw the requested chart for {symbol}."
    ordered = sorted(points.items())
    left, top, width, height = 67, 27, 640, 250
    low, high = min(points.values()), max(points.values())
    spread = high - low or max(high * 0.02, 0.01)
    high = low + spread
    first, last = ordered[0][0], ordered[-1][0]
    days = (last - first).days
    coordinates = " ".join(
        f"{left + width * (day - first).days / days:.1f},{top + height * (high - price) / spread:.1f}"
        for day, price in ordered
    )
    grid = []
    for i in range(5):
        y = top + height * i / 4
        value = high - spread * i / 4
        grid.append(f'<line x1="{left}" y1="{y}" x2="{left + width}" y2="{y}" stroke="#dce4eb"/>')
        grid.append(f'<text x="{left - 8}" y="{y + 4}" text-anchor="end" font-size="12">${value:,.2f}</text>')
    return (
        f"**{symbol} · daily closing price · {years} years requested**\n\n"
        f"```svg\n<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 730 310\" "
        f'role="img" aria-label="{symbol} daily closing prices">'
        + "".join(grid)
        + f'<polyline points="{coordinates}" fill="none" stroke="#1665a8" stroke-width="2"/>'
        + f'<text x="{left}" y="299" font-size="12">{first}</text>'
        + f'<text x="{left + width}" y="299" font-size="12" text-anchor="end">{last}</text>'
        + f"</svg>\n```\n\nSource: Nasdaq historical quotes. {len(points):,} trading sessions, "
          "USD close. Historical data only. This is not investment advice."
    )
