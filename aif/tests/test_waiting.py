import unittest

from fakes import FakeClock

from aif.waiting import WaitTimeoutError, until


class UntilTest(unittest.TestCase):
    def test_returns_value_when_done(self) -> None:
        fake = FakeClock()
        answers = iter([(False, 1), (False, 2), (True, 3)])
        value = until(lambda: next(answers), timeout=60, what="x", describe=str, clock=fake.clock())
        self.assertEqual(value, 3)
        self.assertEqual(fake.sleeps, [5.0, 10.0])

    def test_backoff_is_capped(self) -> None:
        fake = FakeClock()
        with self.assertRaises(WaitTimeoutError):
            until(
                lambda: (False, 0),
                timeout=200,
                what="x",
                describe=str,
                interval=10,
                max_interval=30,
                clock=fake.clock(),
            )
        self.assertEqual(fake.sleeps[:4], [10, 20, 30, 30])
        self.assertAlmostEqual(sum(fake.sleeps), 200)

    def test_timeout_message_says_why(self) -> None:
        fake = FakeClock()
        with self.assertRaisesRegex(WaitTimeoutError, "waiting for apps: 2 not ready"):
            until(
                lambda: (False, 2),
                timeout=10,
                what="apps",
                describe=lambda n: f"{n} not ready",
                clock=fake.clock(),
            )
