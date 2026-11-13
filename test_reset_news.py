import copy
import unittest

from reset_news import ANNOUNCEMENTS, MAX_AGE, ResetNews, parse_forecast, timestamp

NOW = timestamp('2026-10-23T08:00:00Z')
ANNOUNCEMENT = {'events':[], 'stats':{'last_reset_at':'2026-10-22T17:00:00Z'}, 'scheduled':{
    'announced_at':'2026-10-23T04:47:34Z', 'scheduled_for':'2026-10-23T17:00:00Z',
    'reset_type':'regular', 'tweet_url':'https://x.com/thsottiaux/status/123'}}
FORECAST = {'updated_at':'2026-10-23T08:00:00Z', 'probabilities':{'rounded_24h':32, 'rounded_48h':53},
            'last_reset_at':'2026-10-22T17:10:01Z', 'mode':'model', 'official_signal':None}


class ResetNewsTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.announcement = copy.deepcopy(ANNOUNCEMENT)
        self.forecast = copy.deepcopy(FORECAST)
        self.fail = set()
        def fetch(url):
            if url in self.fail:
                raise OSError('offline')
            return self.announcement if url == ANNOUNCEMENTS else self.forecast
        self.store = ResetNews(fetch=fetch, clock=lambda:self.now)

    def read(self):
        self.store.refresh()
        return self.store.snapshot()

    def test_announcement_precedes_prediction_and_uses_utc_instant(self):
        data = self.read()
        self.assertEqual(data['mode'], 'scheduled')
        self.assertEqual(data['scheduled']['deadline'], timestamp('2026-10-24T01:00:00+08:00'))
        self.assertIsNone(data['forecast'])
        self.assertFalse(data['stale'])

    def test_no_announcement_uses_forecast_without_invented_deadline(self):
        self.announcement['scheduled'] = None
        data = self.read()
        self.assertEqual(data['mode'], 'forecast')
        self.assertEqual(data['forecast']['probability24h'], 32)
        self.assertEqual(data['forecast']['probability48h'], 53)
        self.assertIsNone(data['scheduled'])

    def test_expired_announcement_is_not_a_completed_reset(self):
        self.announcement['scheduled']['scheduled_for'] = '2026-10-23T07:00:00Z'
        self.assertEqual(self.read()['mode'], 'forecast')

    def test_completed_reset_supersedes_pending_announcement(self):
        self.forecast['last_reset_at'] = '2026-10-23T07:00:00Z'
        self.assertEqual(self.read()['mode'], 'forecast')

    def test_banked_grant_is_not_global_reset(self):
        self.announcement['scheduled']['reset_type'] = 'banked'
        self.assertEqual(self.read()['mode'], 'forecast')

    def test_forecast_can_supply_dated_announcement(self):
        self.announcement['scheduled'] = None
        self.forecast.update(mode='announced', official_signal={
            'at':'2026-10-23T04:47:34Z', 'signal_type':'dated_commitment',
            'url':'https://x.com/thsottiaux/status/123',
            'window':{'target_at':'2026-10-23T17:00:00Z'}})
        self.assertEqual(self.read()['mode'], 'scheduled')
        self.forecast['official_signal']['signal_type'] = 'hint'
        self.assertEqual(self.read()['mode'], 'forecast')

    def test_failure_preserves_timestamp_and_expires_cached_messages(self):
        initial = self.read()
        self.fail.add(ANNOUNCEMENTS)
        self.now += 60
        stale = self.read()
        self.assertTrue(stale['stale'])
        self.assertEqual(stale['updatedAt'], initial['updatedAt'])
        self.now += MAX_AGE
        self.assertEqual(self.read()['mode'], 'unavailable')

    def test_successful_no_announcement_clears_previous_schedule(self):
        self.read()
        self.announcement['scheduled'] = None
        self.assertEqual(self.read()['mode'], 'forecast')

    def test_old_forecast_is_unavailable_even_if_fetch_succeeds(self):
        self.announcement['scheduled'] = None
        self.forecast['updated_at'] = '2026-10-22T08:00:00Z'
        self.assertEqual(self.read()['mode'], 'unavailable')

    def test_invalid_probabilities_and_naive_dates_are_rejected(self):
        for value in (-1, 101, float('nan'), True, '32', None):
            with self.subTest(value=value):
                data = copy.deepcopy(FORECAST)
                data['probabilities']['rounded_24h'] = value
                with self.assertRaises(ValueError):
                    parse_forecast(data)
        self.assertIsNone(timestamp('2026-10-23T08:00:00'))

    def test_no_data_and_unsafe_source_link(self):
        self.assertEqual(self.store.snapshot()['mode'], 'unavailable')
        self.announcement['scheduled']['tweet_url'] = 'javascript:alert(1)'
        self.assertIsNone(self.read()['scheduled']['url'])

    def test_only_fresh_announcements_emit_signals(self):
        self.assertEqual(self.read()['signals'], [])
        self.announcement['scheduled']['announced_at'] = '2026-10-23T07:59:00Z'
        self.assertEqual(self.read()['signals'][0]['id'], 'announcement:123')
        self.fail.add(ANNOUNCEMENTS)
        self.assertEqual(self.read()['signals'], [])

    def test_recent_completed_reset_emits_signal_but_banked_and_future_do_not(self):
        self.announcement['scheduled'] = None
        self.announcement['events'] = [
            {'reset_type':'regular','tweet_url':'https://x.com/thsottiaux/status/456',
             'announced_at':'2026-10-23T07:59:00Z'},
            {'reset_type':'banked','tweet_url':'https://x.com/thsottiaux/status/789',
             'announced_at':'2026-10-23T07:59:00Z'},
            {'reset_type':'regular','tweet_url':'https://x.com/thsottiaux/status/999',
             'announced_at':'2026-10-23T09:00:00Z'}]
        self.assertEqual(self.read()['signals'], [
            {'id':'reset:456','kind':'reset','at':timestamp('2026-10-23T07:59:00Z')}])


if __name__ == '__main__':
    unittest.main()
