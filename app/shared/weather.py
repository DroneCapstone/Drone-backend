"""
app/shared/weather.py
─────────────────────
Fetch thời tiết thực tế từ Open-Meteo API theo tọa độ (lat, lng).

Open-Meteo:
- Hoàn toàn miễn phí, không cần API key, không giới hạn calls
- Dữ liệu từ các mô hình khí tượng quốc gia (NOAA, ECMWF, DWD)
- Docs: https://open-meteo.com/en/docs
"""

import time
import httpx
from typing import Optional
from dataclasses import dataclass


@dataclass
class WeatherData:
    temperature: Optional[float] = None        # °C
    apparent_temperature: Optional[float] = None  # °C cảm giác
    dew_point: Optional[float] = None          # °C điểm sương
    humidity: Optional[float] = None           # %
    wind_speed: Optional[float] = None         # m/s
    wind_gust: Optional[float] = None          # m/s gió giật
    wind_direction: Optional[float] = None     # độ (0-360)
    precipitation: Optional[float] = None      # mm
    pressure: Optional[float] = None           # hPa
    cloud_cover: Optional[float] = None        # %
    weather_code: Optional[int] = None         # WMO weather code
    condition_label: str = "Trời quang"        # Nhãn mô tả tiếng Việt


# In-memory cache 5 phút theo tọa độ (lat, lng) làm tròn 2 chữ số thập phân (~1.1 km)
_WEATHER_CACHE: dict[tuple[float, float], tuple[float, WeatherData]] = {}
_WEATHER_CACHE_TTL_SEC = 300.0


# WMO weather code → mô tả ngắn gọn
WMO_DESCRIPTIONS = {
    0:  "Trời quang",
    1:  "Ít mây",
    2:  "Nhiều mây",
    3:  "Âm u",
    45: "Sương mù",
    48: "Sương mù đóng băng",
    51: "Mưa phùn nhẹ",
    53: "Mưa phùn",
    55: "Mưa phùn dày",
    61: "Mưa nhẹ",
    63: "Mưa vừa",
    65: "Mưa to",
    71: "Tuyết nhẹ",
    73: "Tuyết vừa",
    75: "Tuyết dày",
    80: "Mưa rào nhẹ",
    81: "Mưa rào",
    82: "Mưa rào mạnh",
    95: "Giông",
    96: "Giông kèm mưa đá",
    99: "Giông kèm mưa đá lớn",
}


def weather_description(code: Optional[int]) -> str:
    """Chuyển WMO weather code thành mô tả tiếng Việt."""
    if code is None:
        return "Không rõ"
    return WMO_DESCRIPTIONS.get(code, f"Mã {code}")


async def fetch_weather(lat: float, lng: float, use_cache: bool = True) -> Optional[WeatherData]:
    """
    Gọi Open-Meteo API lấy thời tiết hiện tại tại tọa độ (lat, lng).
    Hỗ trợ in-memory cache 5 phút theo tọa độ làm tròn 2 chữ số thập phân để tăng tốc lập tuyến.
    Trả về WeatherData hoặc None nếu lỗi (không làm hỏng luồng chính).
    """
    cache_key = (round(float(lat or 0.0), 2), round(float(lng or 0.0), 2))
    now_mono = time.monotonic()

    if use_cache and cache_key in _WEATHER_CACHE:
        cached_at, cached_data = _WEATHER_CACHE[cache_key]
        if (now_mono - cached_at) <= _WEATHER_CACHE_TTL_SEC:
            return cached_data

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        f"&current=temperature_2m,apparent_temperature,dew_point_2m,"
        f"relative_humidity_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m,"
        f"precipitation,surface_pressure,cloud_cover,weather_code"
        f"&wind_speed_unit=ms"
        f"&timezone=Asia%2FHo_Chi_Minh"
    )

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None

            data = resp.json().get("current", {})
            w_code = data.get("weather_code")
            cond_label = weather_description(int(w_code)) if w_code is not None else "Trời quang"
            w_obj = WeatherData(
                temperature=data.get("temperature_2m"),
                apparent_temperature=data.get("apparent_temperature"),
                dew_point=data.get("dew_point_2m"),
                humidity=data.get("relative_humidity_2m"),
                wind_speed=data.get("wind_speed_10m"),
                wind_gust=data.get("wind_gusts_10m"),
                wind_direction=data.get("wind_direction_10m"),
                precipitation=data.get("precipitation"),
                pressure=data.get("surface_pressure"),
                cloud_cover=data.get("cloud_cover"),
                weather_code=int(w_code) if w_code is not None else None,
                condition_label=cond_label,
            )
            _WEATHER_CACHE[cache_key] = (now_mono, w_obj)
            return w_obj

    except Exception:
        return None


def weather_to_override_dict(weather: Optional[WeatherData]) -> Optional[dict]:
    """Chuyển WeatherData thành dict tham số để truyền trực tiếp vào AI predict_flight_energy."""
    if not weather:
        return None
    return {
        "temperature_c": weather.temperature,
        "humidity_pct": weather.humidity,
        "avg_wind_speed": weather.wind_speed,
        "wind_gust_ms": weather.wind_gust,
        "wind_dir_deg": weather.wind_direction,
        "pressure_hpa": weather.pressure,
        "cloud_cover_pct": weather.cloud_cover,
        "precipitation_mm": weather.precipitation,
        "weather_code": weather.weather_code,
        "condition_label": weather.condition_label,
    }


def evaluate_weather_safety(weather: Optional[WeatherData], altitude_m: float = 45.0) -> dict:
    """
    Đánh giá mức độ an toàn thời tiết trên tuyến bay (kể cả hiệu ứng gió mạnh hơn ở tầng cao 60m).
    Ngưỡng xấu: gió hiệu dụng >= 8.0 m/s, gió giật >= 11.0 m/s, mưa >= 2.0 mm, hoặc mã giông/mưa to.
    """
    if not weather:
        return {
            "temperature_c": None,
            "wind_speed_ms": None,
            "wind_gust_ms": None,
            "precipitation_mm": None,
            "humidity_pct": None,
            "condition_label": "Chưa có dữ liệu thời tiết",
            "is_bad_weather": False,
            "warning_message": None,
            "weather_summary": "Thời tiết bình thường (mặc định)",
        }

    # Hiệu ứng đứt gió (Wind Shear): gió tầng 60m thường mạnh hơn mặt đất ~15%
    wind_mult = 1.15 if altitude_m >= 60.0 else 1.0
    base_wind = float(weather.wind_speed or 0.0)
    base_gust = float(weather.wind_gust or base_wind)
    eff_wind = round(base_wind * wind_mult, 1)
    eff_gust = round(base_gust * wind_mult, 1)
    precip = float(weather.precipitation or 0.0)
    temp = weather.temperature
    hum = weather.humidity
    w_code = weather.weather_code
    cond = weather.condition_label or (weather_description(w_code) if w_code is not None else "Trời quang")

    warnings = []
    if eff_wind >= 8.0:
        alt_note = f" tại tầng {int(altitude_m)}m" if altitude_m >= 60.0 else ""
        warnings.append(f"Gió mạnh {eff_wind:.1f} m/s{alt_note} (>= ngưỡng an toàn 8.0 m/s)")
    if eff_gust >= 11.0:
        warnings.append(f"Gió giật {eff_gust:.1f} m/s")
    if precip >= 2.0:
        warnings.append(f"Mưa lớn {precip:.1f} mm")
    elif w_code in (65, 82, 95, 96, 99):
        warnings.append(f"Thời tiết cực đoan ({cond})")

    is_bad = len(warnings) > 0
    icon = "⛈️" if is_bad else ("🌧️" if precip > 0.2 else "🌤️")
    temp_str = f"{temp:.0f}°C" if temp is not None else "N/A"
    parts = [f"{icon} {temp_str}", f"Gió {eff_wind:.1f} m/s"]
    if precip > 0:
        parts.append(f"Mưa {precip:.1f} mm")
    parts.append(cond)
    summary = " • ".join(parts)

    return {
        "temperature_c": round(float(temp), 1) if temp is not None else None,
        "wind_speed_ms": eff_wind,
        "wind_gust_ms": eff_gust,
        "precipitation_mm": round(precip, 2),
        "humidity_pct": round(float(hum), 1) if hum is not None else None,
        "condition_label": cond,
        "is_bad_weather": is_bad,
        "warning_message": "; ".join(warnings) if warnings else None,
        "weather_summary": summary,
    }
