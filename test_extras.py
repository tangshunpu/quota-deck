from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from remote_tokens import RemoteTokens
from weather import parse, parse_uapi, pm25_level, text_kind


class RemoteCacheTests(unittest.TestCase):
    def test_todays_reading_survives_restart_as_offline(self):
        today = datetime.now().strftime('%Y-%m-%d')
        with tempfile.TemporaryDirectory() as home:
            first = RemoteTokens(home)
            first.hosts = {'Mac':{'name':'Mac', 'status':'ok', 'note':'', 'updatedAt':1, 'day':today,
                                  'providers':{'codex':{'total':5}}},
                           'Old':{'name':'Old', 'status':'ok', 'note':'', 'updatedAt':1, 'day':'2000-01-01',
                                  'providers':{'codex':{'total':9}}}}
            first.save_cache()
            hosts = {h['name']:h for h in RemoteTokens(home).snapshot()}
            self.assertEqual(hosts['Mac']['status'], 'offline')
            self.assertEqual(hosts['Mac']['providers']['codex']['total'], 5)
            self.assertNotIn('Old', hosts)

    def test_corrupt_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home)/'.cache/quota-deck/remote-tokens.json'
            path.parent.mkdir(parents=True)
            path.write_text('{oops')
            self.assertEqual(RemoteTokens(home).snapshot(), [])


class WeatherTests(unittest.TestCase):
    def test_parse_and_air_levels(self):
        data = parse({'current':{'temperature_2m':21.1, 'weather_code':3, 'is_day':1},
                      'daily':{'temperature_2m_max':[22], 'temperature_2m_min':[13]}},
                     {'current':{'pm2_5':23.5}})
        self.assertEqual((data['label'], data['kind'], data['level'], data['high']), ('阴', 'cloud', '优', 22))
        self.assertEqual([pm25_level(v) for v in (35, 36, 80, 300, None)], ['优', '良', '轻度污染', '严重污染', None])
        self.assertIsNone(parse({}, {})['pm25'])

    def test_uapi_parse(self):
        data = parse_uapi({'weather':'小雨', 'temperature':18, 'temp_max':20, 'temp_min':15,
                           'air_pollutants':{'pm25':40}, 'wind_direction':'东风', 'wind_power':'3级',
                           'forecast':[{'sunrise':'00:00', 'sunset':'23:59'}, {'weather_day':'晴', 'temp_max':22, 'temp_min':14}]})
        self.assertEqual((data['kind'], data['level'], data['wind'], data['tomorrow']['label']), ('rain', '良', '东风 3级', '晴'))
        self.assertEqual([text_kind(t) for t in ('晴', '多云', '阴', '雷阵雨', '雾', '小雪')],
                         ['clear', 'partly', 'cloud', 'storm', 'fog', 'snow'])


if __name__ == '__main__':
    unittest.main()
