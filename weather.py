"""Hangzhou weather and PM2.5: uapis.cn (domestic stations), Open-Meteo as fallback. No keys."""
import copy
import json
import threading
import time
import urllib.parse
import urllib.request

CITY, LAT, LON = '杭州', 30.27, 120.15
UAPI = 'https://uapis.cn/api/v1/misc/weather?city=%s&extended=true&forecast=true' % urllib.parse.quote(CITY)
FORECAST = ('https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&timezone=Asia%%2FShanghai&forecast_days=1'
            '&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,is_day,wind_speed_10m'
            '&daily=temperature_2m_max,temperature_2m_min' % (LAT, LON))
AIR = ('https://air-quality-api.open-meteo.com/v1/air-quality?latitude=%s&longitude=%s'
       '&timezone=Asia%%2FShanghai&current=pm2_5,pm10' % (LAT, LON))

# WMO weather codes -> (Chinese label, icon kind used by the UI)
CODES = {0:('晴','clear'), 1:('晴间多云','partly'), 2:('多云','partly'), 3:('阴','cloud'),
         45:('雾','fog'), 48:('雾凇','fog'), 51:('小毛毛雨','drizzle'), 53:('毛毛雨','drizzle'),
         55:('毛毛雨','drizzle'), 56:('冻毛毛雨','drizzle'), 57:('冻毛毛雨','drizzle'),
         61:('小雨','rain'), 63:('中雨','rain'), 65:('大雨','rain'), 66:('冻雨','rain'), 67:('冻雨','rain'),
         71:('小雪','snow'), 73:('中雪','snow'), 75:('大雪','snow'), 77:('雪粒','snow'),
         80:('阵雨','rain'), 81:('阵雨','rain'), 82:('强阵雨','rain'), 85:('阵雪','snow'), 86:('阵雪','snow'),
         95:('雷阵雨','storm'), 96:('雷阵雨伴冰雹','storm'), 99:('雷阵雨伴冰雹','storm')}


def pm25_level(value):
    # China HJ 633-2012 24-hour PM2.5 breakpoints.
    if value is None:
        return None
    for limit, label in ((35,'优'), (75,'良'), (115,'轻度污染'), (150,'中度污染'), (250,'重度污染')):
        if value <= limit:
            return label
    return '严重污染'


def number(value):
    return value if type(value) in (int, float) else None


def get(url):
    request = urllib.request.Request(url, headers={'Accept':'application/json', 'User-Agent':'QuotaDeck/1.0'})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def text_kind(text):
    # uapis.cn reports Chinese conditions; map them to the UI's icon kinds.
    for words, kind in (('雷',), 'storm'), (('雪','冰'), 'snow'), (('雨',), 'rain'), (('雾','霾','沙','尘'), 'fog'), \
                       (('阴',), 'cloud'), (('多云','少云','晴间'), 'partly'), (('晴',), 'clear'):
        if any(word in text for word in words):
            return kind
    return 'cloud'


def minutes(clock):
    try:
        hours, mins = clock.split(':')
        return int(hours)*60+int(mins)
    except (AttributeError, ValueError):
        return None


def parse_uapi(data):
    days = data.get('forecast') if isinstance(data.get('forecast'), list) else []
    today = days[0] if days and isinstance(days[0], dict) else {}
    tomorrow = days[1] if len(days) > 1 and isinstance(days[1], dict) else None
    rise, down = minutes(today.get('sunrise')), minutes(today.get('sunset'))
    now = time.localtime()
    day = True if rise is None or down is None else rise <= now.tm_hour*60+now.tm_min < down
    label = data.get('weather') if isinstance(data.get('weather'), str) else '未知'
    pm25 = number((data.get('air_pollutants') or {}).get('pm25'))
    wind = ' '.join(v for v in (data.get('wind_direction'), data.get('wind_power')) if isinstance(v, str))
    return {'city':CITY, 'temperature':number(data.get('temperature')), 'feelsLike':number(data.get('feels_like')),
            'humidity':number(data.get('humidity')), 'wind':wind or None,
            'high':number(data.get('temp_max')) if number(data.get('temp_max')) is not None else number(today.get('temp_max')),
            'low':number(data.get('temp_min')) if number(data.get('temp_min')) is not None else number(today.get('temp_min')),
            'label':label, 'kind':text_kind(label), 'day':day,
            'pm25':pm25, 'pm10':number((data.get('air_pollutants') or {}).get('pm10')), 'level':pm25_level(pm25),
            'aqi':number(data.get('aqi')), 'aqiCategory':data.get('aqi_category') if isinstance(data.get('aqi_category'), str) else None,
            'tomorrow':{'label':tomorrow.get('weather_day'), 'high':number(tomorrow.get('temp_max')),
                        'low':number(tomorrow.get('temp_min')), 'pop':number(tomorrow.get('pop'))} if tomorrow else None,
            'source':'uapis.cn'}


def parse(forecast, air):
    current = forecast.get('current') or {}
    daily = forecast.get('daily') or {}
    code = number(current.get('weather_code'))
    label, kind = CODES.get(int(code) if code is not None else -1, ('未知', 'cloud'))
    first = lambda key: number((daily.get(key) or [None])[0])
    pm25 = number((air.get('current') or {}).get('pm2_5'))
    return {'city':'杭州', 'temperature':number(current.get('temperature_2m')),
            'feelsLike':number(current.get('apparent_temperature')),
            'humidity':number(current.get('relative_humidity_2m')), 'wind':number(current.get('wind_speed_10m')),
            'high':first('temperature_2m_max'), 'low':first('temperature_2m_min'),
            'label':label, 'kind':kind, 'day':current.get('is_day') != 0,
            'pm25':pm25, 'pm10':number((air.get('current') or {}).get('pm10')), 'level':pm25_level(pm25),
            'source':'Open-Meteo'}


class WeatherStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None

    def update(self):
        try:
            data = get(UAPI)
            if number(data.get('temperature')) is None:
                raise ValueError('no temperature')
            result = {**parse_uapi(data), 'updatedAt':time.time()}
            with self.lock:
                self.data = result
            return True
        except (OSError, ValueError, AttributeError):
            pass
        try:
            forecast = get(FORECAST)
            try:
                air = get(AIR)
            except (OSError, ValueError):
                air = {}
            result = {**parse(forecast, air), 'updatedAt':time.time()}
        except (OSError, ValueError):
            return False
        with self.lock:
            self.data = result
        return True

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def run(self, stop):
        # Every 15 minutes; retry after a minute while the network/proxy is failing.
        while not stop.is_set():
            stop.wait(900 if self.update() else 60)
