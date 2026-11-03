import unittest
from datetime import date, timedelta
from unittest.mock import Mock

from reelready.sources.douban import DoubanClient


class DoubanAvailabilityTests(unittest.TestCase):
    def detail(self, vendor, **extra):
        client = DoubanClient.__new__(DoubanClient)
        client._get = Mock(return_value=dict(id='36882191', title='八仙！', vendors=[vendor], **extra))
        return client.detail('36882191')

    def vendor(self, **changes):
        return dict(title='咪咕视频', url='https://www.miguvideo.com/p/detail/123', **changes)

    def test_real_playback_link_is_accepted_including_paid_content(self):
        self.assertEqual(self.detail(self.vendor(is_paid=True, accessible=True)).vendors, ['咪咕视频'])

    def test_baxian_ad_with_future_date_and_empty_link_is_not_playable(self):
        future = date.today() + timedelta(days=3)
        info = self.detail(dict(title='咪咕视频', url='', uri='', is_ad=True,
                                accessible=True, pre_release_desc=f'{future:%Y年%m月%d日} 上架'),
                           pre_playable_date=f'{future} 00:00:00')
        self.assertEqual(info.vendors, [])
        self.assertEqual(info.pre_playable_date, f'{future} 00:00:00')

    def test_each_weak_signal_is_rejected_independently(self):
        future = date.today() + timedelta(days=3)
        for changes in (dict(is_ad=True), dict(accessible=False),
                        dict(pre_release_desc=f'{future:%Y年%m月%d日} 上架'),
                        dict(pre_release_desc='即将上线'), dict(pre_release_desc='2026年13月40日 上架')):
            with self.subTest(changes=changes):
                self.assertEqual(self.detail(self.vendor(**changes)).vendors, [])

    def test_search_empty_and_app_launcher_links_are_rejected(self):
        for link in ('', 'miguvideo://skynet.douban.com',
                     'https://www.miguvideo.com/p/search?keywords=八仙！',
                     'https://search.youku.com/search_video/q_八仙',
                     'https://example.com/#/search?q=八仙',
                     'https://example.com/%73earch?q=八仙',
                     'https://www.miguvideo.com/', 'not-a-url'):
            with self.subTest(link=link):
                self.assertEqual(self.detail(dict(title='平台', url=link)).vendors, [])

    def test_future_global_release_does_not_count_vendor_as_available(self):
        future = (date.today() + timedelta(days=3)).isoformat()
        self.assertEqual(self.detail(self.vendor(), pre_playable_date=future).vendors, [])

    def test_malformed_announced_date_is_not_confirmation(self):
        self.assertEqual(self.detail(self.vendor(), pre_playable_date='待定').vendors, [])

    def test_past_release_still_needs_real_link(self):
        past = date.today() - timedelta(days=3)
        self.assertEqual(self.detail(self.vendor(pre_release_desc=f'{past} 上架')).vendors, ['咪咕视频'])
        self.assertEqual(self.detail(dict(title='咪咕视频'), pre_playable_date=str(past)).vendors, [])


if __name__ == '__main__':
    unittest.main()
