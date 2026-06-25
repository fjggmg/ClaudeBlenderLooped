import asyncio
import unittest

from blendahbot.steering import Steering


class SteeringTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.s = Steering(self.loop, enabled=False)

    def tearDown(self):
        self.loop.close()

    def test_drain_returns_instructions_in_order(self):
        self.s._enqueue("make the roof red")
        self.s._enqueue("add a chimney")
        self.assertEqual(self.s.drain(), ["make the roof red", "add a chimney"])
        self.assertEqual(self.s.drain(), [])  # queue now empty

    def test_stop_token_sets_flag_and_is_filtered_out(self):
        self.s._enqueue("brighten the lighting")
        self.s._enqueue("/stop")
        out = self.s.drain()
        self.assertTrue(self.s.stop_requested)
        self.assertEqual(out, ["brighten the lighting"])

    def test_stop_aliases(self):
        for token in ("stop", "/quit", "/exit", "/done", "STOP"):
            s = Steering(self.loop, enabled=False)
            s._enqueue(token)
            self.assertTrue(s.stop_requested, token)

    def test_disabled_start_is_noop(self):
        # enabled=False must never spin up a reader thread.
        self.s.start()
        self.assertFalse(self.s._started)


if __name__ == "__main__":
    unittest.main()
