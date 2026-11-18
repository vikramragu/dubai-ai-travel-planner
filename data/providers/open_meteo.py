"""Live weather from Open-Meteo (research.md §2.8): a daily forecast, tagged ``api``.

Open-Meteo forecasts about 16 days ahead. Trip days inside that window get the forecast; days
beyond it get the monthly climate normal from the local provider (tagged ``estimated``), so a
trip can mix both. A trip entirely outside the window (e.g. no dates given, so the planner
assumes next December) is not this provider's job: it raises :class:`NotApplicable` and the
chain uses the local climate normals without logging a fallback.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol

from core.config import Settings
from data.providers.base import NotApplicable, ProviderError
from data.providers.http import HttpClient
from schemas import DayWeather, Source

DUBAI = (25.2048, 55.2708)
FORECAST_DAYS = 16
DAILY = "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max"


class _Climate(Protocol):
    def forecast(self, start: date, days: int) -> list[DayWeather]: ...


class OpenMeteoProvider:
    name = "open-meteo"

    def __init__(
        self,
        http: HttpClient,
        settings: Settings,
        *,
        climate: _Climate,
        today: date | None = None,
    ) -> None:
        self.http = http
        self.settings = settings
        self.climate = climate
        self._today = today

    @property
    def today(self) -> date:
        return self._today or date.today()

    def forecast(self, start: date, days: int) -> list[DayWeather]:
        wanted = [start + timedelta(days=i) for i in range(days)]
        last = self.today + timedelta(days=FORECAST_DAYS - 1)
        inside = [d for d in wanted if self.today <= d <= last]
        if not inside:
            raise NotApplicable("dates outside the forecast range")
        body = self.http.get_json(
            "open-meteo",
            self.settings.open_meteo_url,
            params={
                "latitude": str(DUBAI[0]),
                "longitude": str(DUBAI[1]),
                "daily": DAILY,
                "timezone": "Asia/Dubai",
                "start_date": inside[0].isoformat(),
                "end_date": inside[-1].isoformat(),
            },
            ttl_h=self.settings.cache_ttl_weather_h,
        )
        try:
            daily = body["daily"]
            by_date = {date.fromisoformat(t): i for i, t in enumerate(daily["time"])}
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"open-meteo: unexpected response ({exc})") from exc
        out = []
        for d in wanted:
            i = by_date.get(d)
            tmax = daily["temperature_2m_max"][i] if i is not None else None
            if i is None or tmax is None:
                out.append(self.climate.forecast(d, 1)[0])  # beyond the forecast: climate
                continue
            prob = daily.get("precipitation_probability_max", [None] * len(daily["time"]))[i]
            out.append(
                DayWeather(
                    date=d,
                    temp_max_c=float(tmax),
                    temp_min_c=float(daily["temperature_2m_min"][i]),
                    precipitation_mm=max(0.0, float(daily["precipitation_sum"][i] or 0.0)),
                    rain_probability_pct=None if prob is None else float(prob),
                    source=Source.API,
                    reference="Open-Meteo forecast (live, CC BY 4.0)",
                )
            )
        return out
