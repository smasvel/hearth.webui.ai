"""Tiny weather skill (OpenWeatherMap, free key at openweathermap.org)."""
import requests

API_KEY = "YOUR_OWM_API_KEY"
CITY = "YourCity,YourCountryCode"


def get_weather() -> str:
    r = requests.get("https://api.openweathermap.org/data/2.5/weather",
                     params={"q": CITY, "appid": API_KEY, "units": "metric"},
                     timeout=5).json()
    desc = r["weather"][0]["description"]
    temp = r["main"]["temp"]
    return f"It's {temp:.0f} degrees with {desc} in {CITY.split(',')[0]}."
