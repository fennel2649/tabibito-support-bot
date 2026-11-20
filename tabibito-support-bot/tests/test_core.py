import tempfile
import unittest
from pathlib import Path

from tabibito.core import Activity, HOUR, Store, VoiceTracker, included, role_action


class PolicyTests(unittest.TestCase):
    def decision(self, activity=Activity(1, 5, 24 * HOUR), **flags):
        options = dict(graduate=False, support=False, eligible=True) | flags
        return role_action(activity, **options)

    def test_any_enrollment_condition(self):
        for activity in (Activity(0, 5, 24 * HOUR), Activity(1, 4, 24 * HOUR), Activity(1, 5, 24 * HOUR - 1)):
            with self.subTest(activity=activity):
                self.assertEqual(self.decision(activity), "add_support")
        self.assertEqual(self.decision(new=True), "add_support")
        self.assertIsNone(self.decision())
        self.assertIsNone(self.decision(Activity(), eligible=False))

    def test_graduation_and_priority(self):
        self.assertEqual(self.decision(Activity(0, 30, 0)), "graduate")
        self.assertEqual(self.decision(Activity(0, 0, 72 * HOUR)), "graduate")
        self.assertIsNone(self.decision(Activity(1, 29, 72 * HOUR - 1)))
        self.assertEqual(self.decision(requested=True, eligible=False), "graduate")
        self.assertIsNone(self.decision(Activity(), graduate=True, new=True))
        self.assertEqual(self.decision(graduate=True, support=True, requested=True), "remove_support")

    def test_channel_allow_and_deny(self):
        self.assertTrue(included({1, 2}, {2}, set()))
        self.assertFalse(included({1, 2}, {1}, {2}))
        self.assertFalse(included({1}, {2}, set()))
        self.assertTrue(included({3}, set(), {2}))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.sqlite3"
        self.store = Store(self.path, 1)
        self.addCleanup(lambda: self.store.db.close())
        self.voice = VoiceTracker(self.store)

    def reopen(self):
        self.store.db.close()
        self.store = Store(self.path, 1)
        self.voice = VoiceTracker(self.store)

    def test_posts_and_graduation_request_survive_restart(self):
        self.store.post(2, 100)
        self.store.request(2, 3)
        self.store.arrival(2)
        self.reopen()
        self.store.post(2, 100)
        self.store.post(2, 101)
        self.assertEqual(self.store.activity(2).posts, 2)
        self.assertEqual(self.store.requester(2), 3)
        self.assertTrue(self.store.arrived(2))
        self.store.complete(2)
        self.assertIsNone(self.store.requester(2))

    def test_vc_move_rejoin_exact_boundary_and_exclusion(self):
        self.voice.change(2, False, True, 10, 10)
        self.voice.change(2, True, True, 20, 20)
        self.voice.change(2, True, False, 30, 30)
        self.voice.change(2, False, False, 100, 100)
        self.voice.change(2, False, True, 330, 330)
        self.voice.change(2, True, False, 340, 340)
        self.voice.change(2, False, True, 641, 641)
        self.voice.tick(650)
        self.assertEqual(self.store.activity(2), Activity(0, 2, 39))

    def test_restarts_do_not_bill_unobserved_time(self):
        self.voice.change(2, False, True, 10, 10)
        self.voice.tick(20)
        self.reopen()
        self.voice.reset([2, 3], 10_000)
        self.voice.tick(10_005)
        self.assertEqual(self.store.activity(2), Activity(0, 1, 15))
        self.assertEqual(self.store.activity(3), Activity(0, 0, 5))
        self.voice.change(2, True, False, 10_010, 10_010)
        self.reopen()
        self.voice.change(2, False, True, 10_020, 10_020)
        self.assertEqual(self.store.activity(2), Activity(0, 1, 20))

    def test_correct_preserves_other_fields_and_records_actor(self):
        self.store.post(2, 100)
        self.store.correct(2, 3, visits=12, seconds=60)
        self.assertEqual(self.store.activity(2), Activity(1, 12, 60))
        _, actor, action, detail = self.store.history(2)[0]
        self.assertEqual((actor, action), (3, "記録修正"))
        self.assertIn("12", detail)


if __name__ == "__main__":
    unittest.main()
