import json
import tkinter as tk
from tkinter import ttk, messagebox
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

COUNTRIES_API = "https://restcountries.com/v3.1/all?fields=name,capital"
GEOCODING_API = (
    "https://geocoding-api.open-meteo.com/v1/search"
    "?name={query}&count=1&language=en&format=json"
)
WEATHER_API = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code"
)

WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def fetch_json(url: str) -> dict | list:
    with urlopen(url, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def load_countries() -> dict[str, str]:
    data = fetch_json(COUNTRIES_API)
    countries: dict[str, str] = {}

    for item in data:
        name = item.get("name", {}).get("common")
        capitals = item.get("capital") or []
        capital = capitals[0] if capitals else None
        if name and capital:
            countries[name] = capital

    if not countries:
        raise RuntimeError("No countries were loaded from the API.")

    return dict(sorted(countries.items(), key=lambda x: x[0]))


def get_coordinates(place_name: str) -> tuple[float, float]:
    url = GEOCODING_API.format(query=quote(place_name))
    data = fetch_json(url)
    results = data.get("results") or []
    if not results:
        raise RuntimeError(f"No coordinates found for '{place_name}'.")

    lat = results[0].get("latitude")
    lon = results[0].get("longitude")
    if lat is None or lon is None:
        raise RuntimeError(f"Missing coordinates for '{place_name}'.")

    return float(lat), float(lon)


def get_current_weather(lat: float, lon: float) -> tuple[float, str]:
    url = WEATHER_API.format(lat=lat, lon=lon)
    data = fetch_json(url)
    current = data.get("current")
    if not current:
        raise RuntimeError("Weather data is unavailable right now.")

    temp = current.get("temperature_2m")
    code = current.get("weather_code")
    if temp is None or code is None:
        raise RuntimeError("Incomplete weather response received.")

    summary = WEATHER_CODES.get(code, f"Unknown weather code ({code})")
    return float(temp), summary


class WeatherCountryApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Country Weather Checker")
        self.root.geometry("560x300")

        self.country_to_capital = load_countries()
        self.country_names = list(self.country_to_capital.keys())

        self.selected_country = tk.StringVar(value=self.country_names[0])

        self.build_ui()

    def build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Select a country to view current temperature",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        self.country_combo = ttk.Combobox(
            frame,
            textvariable=self.selected_country,
            values=self.country_names,
            state="readonly",
            height=20,
        )
        self.country_combo.pack(fill="x")

        action_row = ttk.Frame(frame)
        action_row.pack(fill="x", pady=12)

        ttk.Button(
            action_row,
            text="Check Weather",
            command=self.check_weather,
        ).pack(side="left")

        self.result_label = ttk.Label(
            frame,
            text="",
            justify="left",
            font=("Segoe UI", 11),
        )
        self.result_label.pack(anchor="w", pady=(10, 0))

        self.status_label = ttk.Label(frame, text="Ready", foreground="#555")
        self.status_label.pack(anchor="w", pady=(10, 0))

        self.check_weather()

    def check_weather(self) -> None:
        country = self.selected_country.get()
        capital = self.country_to_capital.get(country)

        if not capital:
            messagebox.showerror("Error", "No capital found for selected country.")
            return

        self.status_label.config(text=f"Fetching weather for {capital}, {country}...")
        self.root.update_idletasks()

        try:
            lat, lon = get_coordinates(capital)
            temp_c, summary = get_current_weather(lat, lon)
        except (RuntimeError, HTTPError, URLError, TimeoutError, ValueError) as exc:
            self.result_label.config(text="")
            self.status_label.config(text="Request failed")
            messagebox.showerror("Weather Error", str(exc))
            return

        self.result_label.config(
            text=(
                f"Country: {country}\n"
                f"City used: {capital}\n"
                f"Current temperature: {temp_c:.1f} °C\n"
                f"Conditions: {summary}"
            )
        )
        self.status_label.config(text="Last update successful")


if __name__ == "__main__":
    try:
        app_root = tk.Tk()
        WeatherCountryApp(app_root)
        app_root.mainloop()
    except Exception as err:
        messagebox.showerror("Startup Error", f"Could not start app:\n{err}")
