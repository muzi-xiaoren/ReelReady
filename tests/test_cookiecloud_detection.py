import unittest

from reelready.cookiecloud import cookie_header_for, detect_nexusphp_hosts, synced_cookie_hosts
from reelready.sites.catalog import lookup_site


def snapshot(entries):
    return {"cookie_data": {"cookies": [
        {"domain": domain, "name": name, "value": "synthetic"}
        for domain, names in entries.items() for name in names
    ]}}


class CookieCloudDetectionTests(unittest.TestCase):
    def test_distinctive_login_cookie_variants(self):
        data = snapshot({
            ".classic.example": ["c_secure_uid", "c_secure_pass"],
            "partial.example": ["c_secure_pass"],
            "token.example": ["nexusphp_uid", "nexusphp_token"],
            "unknown.example": ["nexusphp_uid"],
            "ordinary.example": ["SITE_TOTAL_ID", "session", "cf_clearance"],
        })
        self.assertEqual(detect_nexusphp_hosts(data), [
            "classic.example", "partial.example", "token.example",
        ])

    def test_deduplication_preserves_unique_subdomains(self):
        data = snapshot({
            ".tracker.example": ["c_secure_pass"],
            "www.tracker.example": ["c_secure_pass"],
            "www.only.example": ["c_secure_pass"],
            "pt.tracker.example": ["c_secure_pass"],
        })
        self.assertEqual(detect_nexusphp_hosts(data), [
            "pt.tracker.example", "tracker.example", "www.only.example",
        ])

    def test_manual_choices_include_unclassified_hosts(self):
        data = snapshot({"unknown.example": ["SITE_TOTAL_ID"], "www.only.example": ["session"]})
        self.assertEqual(synced_cookie_hosts(data), ["unknown.example", "www.only.example"])
        self.assertEqual(detect_nexusphp_hosts(data), [])
        self.assertEqual(synced_cookie_hosts({}), [])

    def test_catalog_requires_site_specific_login_cookies(self):
        data = snapshot({
            "springsunday.net": ["SPRINGID"],
            "13city.org": ["SITE_TOTAL_ID"],
            "ordinary.example": ["SITE_TOTAL_ID"],
            "springsunday.net.evil.example": ["session"],
        })
        self.assertEqual(detect_nexusphp_hosts(data), ["springsunday.net"])

    def test_expired_and_empty_login_cookies_are_excluded(self):
        data = {"cookie_data": {"cookies": [
            {"domain": "13city.org", "name": "c_secure_pass", "value": "", "expirationDate": 9999999999},
            {"domain": "hdfans.org", "name": "c_secure_pass", "value": "old", "expirationDate": 1},
            {"domain": "tjupt.org", "name": "access_token", "value": "active"},
        ]}}
        self.assertEqual(detect_nexusphp_hosts(data), ["tjupt.org"])

    def test_cookie_header_respects_host_only_and_expiration(self):
        data = {"cookie_data": {"cookies": [
            {"domain": "tracker.example", "name": "auth", "value": "private", "hostOnly": True},
            {"domain": ".tracker.example", "name": "shared", "value": "yes", "hostOnly": False},
            {"domain": ".tracker.example", "name": "expired", "value": "old", "expirationDate": 1},
        ]}}
        self.assertEqual(cookie_header_for(data, "https://www.tracker.example"), "shared=yes")
        self.assertEqual(cookie_header_for(data, "https://tracker.example"), "auth=private; shared=yes")

    def test_catalog_alias_and_architecture(self):
        self.assertEqual(lookup_site("www.13city.org")["id"], lookup_site("13city.org")["id"])
        self.assertIsNone(lookup_site("unrelated.13city.org"))
        self.assertIsNone(lookup_site("13city.org.evil.example"))
        catalog_site = lookup_site("greatposterwall.com")
        self.assertIsNotNone(catalog_site)
        self.assertNotEqual(catalog_site["schema"], "NexusPHP")
        self.assertEqual(detect_nexusphp_hosts(snapshot({"greatposterwall.com": ["c_secure_pass"]})), [])

    def test_www_login_host_is_preserved(self):
        data = snapshot({
            "pttime.org": ["cf_clearance"],
            "www.pttime.org": ["c_secure_uid", "c_secure_pass"],
        })
        self.assertEqual(detect_nexusphp_hosts(data), ["www.pttime.org"])
        self.assertEqual(synced_cookie_hosts(data), ["pttime.org", "www.pttime.org"])


if __name__ == "__main__":
    unittest.main()
