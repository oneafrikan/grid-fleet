"""hub.py reader (task 4.1): skip rules, mtime staleness, aggregation, read-only."""

import json
import os
import tempfile
import unittest

from grid_fleet import hub
from helpers import synthetic_snapshot as syn
from helpers.synthetic_snapshot import NOW, NULL, make_snapshot, write_inbox_file


class HubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name

    def put(self, host, snap=None, age_s=180, **kw):
        """Write inbox/<host>.json whose mtime is `age_s` seconds before NOW."""
        snap = snap if snap is not None else make_snapshot(host, **kw)
        return write_inbox_file(self.state, host + ".json", snap, NOW - age_s)

    def read(self, expected=()):
        return hub.read_hub(self.state, NOW, expected)

    def host(self, state, name):
        return [h for h in state.hosts if h.name == name][0]


class TestSkipRules(HubCase):
    def test_missing_inbox_is_empty_not_an_error(self):
        s = self.read()
        self.assertEqual((s.hosts, s.skipped, s.fresh), ((), 0, 0))

    def test_valid_file_is_read(self):
        self.put("alpha")
        s = self.read()
        self.assertEqual([h.name for h in s.hosts], ["alpha"])
        self.assertEqual(s.skipped, 0)

    def test_dotfiles_ignored_and_not_counted(self):
        # An in-flight upload is normal state, not "bad input".
        write_inbox_file(self.state, ".alpha.json.tmp", make_snapshot("alpha"), NOW)
        write_inbox_file(self.state, ".hidden.json", make_snapshot("hidden"), NOW)
        s = self.read()
        self.assertEqual((s.hosts, s.skipped), ((), 0))

    def test_non_json_names_ignored_and_not_counted(self):
        write_inbox_file(self.state, "notes.txt", "hello", NOW)
        s = self.read()
        self.assertEqual((s.hosts, s.skipped), ((), 0))

    def test_invalid_json_skipped_and_counted(self):
        self.put("alpha")
        write_inbox_file(self.state, "beta.json", "{not json", NOW)
        s = self.read()
        self.assertEqual([h.name for h in s.hosts], ["alpha"])
        self.assertEqual(s.skipped, 1)

    def test_non_object_json_skipped(self):
        write_inbox_file(self.state, "beta.json", "[1, 2, 3]", NOW)
        self.assertEqual(self.read().skipped, 1)

    def test_invalid_utf8_skipped(self):
        write_inbox_file(self.state, "beta.json", b"\xff\xfe\x00garbage", NOW)
        self.assertEqual(self.read().skipped, 1)

    def test_deeply_nested_json_skipped_not_crash(self):
        write_inbox_file(self.state, "beta.json", "[" * 100000, NOW)
        self.assertEqual(self.read().skipped, 1)

    def test_unknown_schema_skipped(self):
        write_inbox_file(self.state, "beta.json", make_snapshot("beta", schema=2), NOW)
        s = self.read()
        self.assertEqual((s.hosts, s.skipped), ((), 1))

    def test_missing_and_boolean_schema_skipped(self):
        snap = make_snapshot("beta")
        del snap["schema"]
        write_inbox_file(self.state, "beta.json", snap, NOW)
        write_inbox_file(self.state, "gamma.json", make_snapshot("gamma", schema=True), NOW)
        self.assertEqual(self.read().skipped, 2)

    def test_size_cap_boundary(self):
        # Pad with trailing whitespace so the JSON stays valid at exactly the cap.
        body = json.dumps(make_snapshot("alpha")).encode()
        at_cap = body + b" " * (hub.MAX_FILE_BYTES - len(body))
        over = at_cap + b" "
        self.assertEqual(len(at_cap), 256 * 1024)
        write_inbox_file(self.state, "alpha.json", at_cap, NOW)
        write_inbox_file(self.state, "beta.json", over, NOW)
        s = self.read()
        self.assertEqual([h.name for h in s.hosts], ["alpha"])
        self.assertEqual(s.skipped, 1)

    def test_directory_named_like_a_snapshot_skipped(self):
        os.makedirs(os.path.join(self.state, "inbox", "weird.json"))
        s = self.read()
        self.assertEqual((s.hosts, s.skipped), ((), 1))

    def test_hostile_file_name_sanitised_for_display(self):
        write_inbox_file(self.state, "al\x1b[31mpha.json", make_snapshot("alpha"), NOW)
        names = [h.name for h in self.read().hosts]
        self.assertEqual(len(names), 1)
        self.assertNotIn("\x1b", names[0])
        self.assertEqual(names[0], "al__31mpha")  # ESC and [ both become _


class TestStaleness(HubCase):
    def test_fresh_at_exactly_twice_interval(self):
        self.put("alpha", age_s=1200, interval_s=600)
        self.assertEqual(self.host(self.read(), "alpha").status, hub.FRESH)

    def test_stale_one_second_past_twice_interval(self):
        self.put("alpha", age_s=1201, interval_s=600)
        h = self.host(self.read(), "alpha")
        self.assertEqual((h.status, h.age_s), (hub.STALE, 1201))

    def test_41_minute_old_file_with_interval_600_is_stale(self):
        self.put("beta", age_s=41 * 60, interval_s=600)
        h = self.host(self.read(), "beta")
        self.assertEqual((h.status, h.age_s), (hub.STALE, 2460))

    def test_interval_comes_from_the_snapshot(self):
        # 25 min old: stale for a 300 s host, fresh for a 900 s host.
        self.put("fast", age_s=1500, interval_s=300)
        self.put("slow", age_s=1500, interval_s=900)
        s = self.read()
        self.assertEqual(self.host(s, "fast").status, hub.STALE)
        self.assertEqual(self.host(s, "slow").status, hub.FRESH)

    def test_bad_interval_falls_back_to_default(self):
        snap = make_snapshot("alpha")
        snap["interval_s"] = "soon"
        self.put("alpha", snap, age_s=1200)
        h = self.host(self.read(), "alpha")
        self.assertEqual((h.status, h.interval_s), (hub.FRESH, 600))

    def test_clock_skew_does_not_matter(self):
        # Spoke clock an hour behind: collected_at is ancient, but the file just landed.
        self.put("alpha", age_s=30, collected_at=syn.COLLECTED_AT - 3600)
        # Spoke clock an hour ahead on a file that stopped arriving 41 minutes ago.
        self.put("beta", age_s=41 * 60, collected_at=NOW + 3600)
        s = self.read()
        self.assertEqual(self.host(s, "alpha").status, hub.FRESH)
        self.assertEqual(self.host(s, "beta").status, hub.STALE)

    def test_future_mtime_is_fresh_with_zero_age(self):
        self.put("alpha", age_s=-500)
        h = self.host(self.read(), "alpha")
        self.assertEqual((h.status, h.age_s), (hub.FRESH, 0))


class TestExpectedHosts(HubCase):
    def test_expected_host_without_file_is_never_reported(self):
        self.put("alpha")
        s = self.read(expected=["alpha", "beta"])
        beta = self.host(s, "beta")
        self.assertEqual(beta.status, hub.NEVER)
        self.assertIsNone(beta.age_s)
        self.assertEqual((s.fresh, s.host_count), (1, 2))

    def test_expected_host_with_file_not_duplicated(self):
        self.put("alpha")
        self.assertEqual(self.read(expected=["alpha"]).host_count, 1)

    def test_expected_host_whose_file_is_corrupt_is_never_reported(self):
        write_inbox_file(self.state, "beta.json", "{oops", NOW)
        s = self.read(expected=["beta"])
        self.assertEqual(self.host(s, "beta").status, hub.NEVER)
        self.assertEqual(s.skipped, 1)

    def test_unexpected_hosts_still_shown(self):
        self.put("alpha")
        self.put("gamma")
        self.assertEqual(self.read(expected=["alpha"]).host_count, 2)

    def test_hosts_sorted_by_name(self):
        for n in ("gamma", "alpha", "beta"):
            self.put(n)
        self.assertEqual([h.name for h in self.read().hosts], ["alpha", "beta", "gamma"])


class TestAggregation(HubCase):
    def test_fresh_host_metrics(self):
        self.put("alpha")
        h = self.host(self.read(), "alpha")
        self.assertEqual((h.busy, h.waiting), (2, 1))
        self.assertEqual((h.out_24h, h.out_7d), (412000, 3100000))
        self.assertEqual(h.cache_24h, 91000 + 1200000 + 55000000)
        self.assertEqual(h.cache_7d, 640000 + 9000000 + 420000000)

    def test_totals_exclude_stale_and_never(self):
        self.put("alpha", sessions={"busy": 2, "waiting": 1})
        self.put("beta", age_s=41 * 60, sessions={"busy": 9, "waiting": 9})
        s = self.read(expected=["alpha", "beta", "gamma"])
        self.assertEqual((s.busy.value, s.waiting.value), (2, 1))
        self.assertEqual(s.out_24h.value, 412000)
        self.assertEqual((s.fresh, s.host_count), (1, 3))

    def test_totals_sum_across_fresh_hosts(self):
        self.put("alpha", sessions={"busy": 2, "waiting": 1})
        self.put("beta", sessions={"busy": 1, "waiting": 0})
        s = self.read()
        self.assertEqual((s.busy.value, s.waiting.value), (3, 1))
        self.assertEqual(s.out_24h, hub.Total(824000, True))

    def test_stale_host_carries_no_metrics(self):
        self.put("beta", age_s=41 * 60)
        h = self.host(self.read(), "beta")
        self.assertEqual((h.busy, h.waiting, h.out_24h, h.out_7d), (None, None, None, None))

    def test_null_sessions_is_none_not_zero(self):
        self.put("alpha", sessions=NULL)
        h = self.host(self.read(), "alpha")
        self.assertEqual((h.busy, h.waiting), (None, None))
        self.assertEqual(h.out_24h, 412000)  # other sections unaffected

    def test_null_tokens_is_none_not_zero(self):
        self.put("alpha", tokens=NULL)
        h = self.host(self.read(), "alpha")
        self.assertEqual((h.out_24h, h.out_7d, h.cache_24h, h.cache_7d), (None,) * 4)
        self.assertEqual((h.busy, h.waiting), (2, 1))

    def test_partial_total_when_a_fresh_host_has_null_section(self):
        self.put("alpha", sessions={"busy": 2, "waiting": 1})
        self.put("beta", sessions=NULL)
        s = self.read()
        self.assertEqual(s.busy, hub.Total(2, False))  # lower bound, flagged

    def test_total_unknown_when_no_fresh_host_reports(self):
        self.put("alpha", sessions=NULL)
        s = self.read()
        self.assertIsNone(s.busy.value)
        # and with no fresh hosts at all
        self.assertIsNone(hub.build_state(hub.InboxScan(), NOW).busy.value)

    def test_absurdly_large_counts_are_unknown_not_a_crash(self):
        huge = 10 ** 400
        self.put("alpha", tokens={"last_24h": syn.tokens_window(huge, huge, huge, huge),
                                  "last_7d": syn.tokens_window(huge, huge, huge, huge), "by_day": []})
        self.put("beta")
        s = self.read()
        h = self.host(s, "alpha")
        self.assertEqual((h.out_24h, h.out_7d, h.cache_24h, h.cache_7d), (None,) * 4)
        self.assertEqual(self.host(s, "beta").out_24h, 412000)

    def test_wrong_typed_fields_are_not_trusted(self):
        snap = make_snapshot("alpha", sessions={"busy": "2", "waiting": True})
        self.put("alpha", snap)
        h = self.host(self.read(), "alpha")
        self.assertEqual((h.busy, h.waiting), (None, None))

    def test_missing_cache_field_makes_cache_unknown(self):
        snap = make_snapshot("alpha")
        del snap["tokens"]["last_24h"]["cache_read"]
        self.put("alpha", snap)
        h = self.host(self.read(), "alpha")
        self.assertIsNone(h.cache_24h)
        self.assertEqual(h.out_24h, 412000)


class TestSignatureAndReadOnly(HubCase):
    def test_signature_changes_when_a_file_lands_or_is_replaced(self):
        s0 = hub.inbox_signature(self.state)
        self.put("alpha")
        s1 = hub.inbox_signature(self.state)
        self.assertNotEqual(s0, s1)
        self.put("alpha", age_s=10)  # same name, new mtime
        self.assertNotEqual(s1, hub.inbox_signature(self.state))

    def test_signature_ignores_dotfiles(self):
        self.put("alpha")
        before = hub.inbox_signature(self.state)
        write_inbox_file(self.state, ".beta.json.tmp", "partial", NOW)
        self.assertEqual(before, hub.inbox_signature(self.state))

    def test_reader_changes_nothing_on_disk(self):
        self.put("alpha")
        self.put("beta", age_s=41 * 60)
        write_inbox_file(self.state, "bad.json", "{oops", NOW)
        write_inbox_file(self.state, ".x.json.tmp", "partial", NOW)
        before = syn.snapshot_tree(self.state)
        hub.read_hub(self.state, NOW, ["alpha", "gamma"])
        hub.inbox_signature(self.state)
        self.assertEqual(before, syn.snapshot_tree(self.state))

    def test_reader_does_not_create_missing_inbox(self):
        hub.read_hub(self.state, NOW)
        self.assertEqual(os.listdir(self.state), [])


if __name__ == "__main__":
    unittest.main()
