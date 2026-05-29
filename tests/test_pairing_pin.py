import time
import unittest
from fastapi.testclient import TestClient
from fastapi import HTTPException
import api


class TestPairingPIN(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app)

    def setUp(self):
        # Reset the registries before each test
        api._active_pins.clear()
        api._pin_failures.clear()
        api._pin_lockout_until.clear()

    def test_create_pairing_pin_programmatic(self):
        pin = api.create_pairing_pin()
        self.assertEqual(len(pin), 6)
        self.assertTrue(pin.isdigit())
        self.assertIn(pin, api._active_pins)
        self.assertEqual(api._active_pins[pin]["token"], api._API_TOKEN)
        self.assertGreater(api._active_pins[pin]["expires_at"], time.time())

    def test_authenticate_correct_pin(self):
        pin = api.create_pairing_pin()
        
        # Call GET /bridge/pair?pin=...
        response = self.client.get(f"/bridge/pair?pin={pin}")
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["token"], api._API_TOKEN)
        
        # PIN should be one-time use and popped from registry
        self.assertNotIn(pin, api._active_pins)

    def test_authenticate_incorrect_pin_decrements_attempts(self):
        # Set up a correct PIN
        pin = api.create_pairing_pin()
        wrong_pin = "000000" if pin != "000000" else "111111"
        
        # Call with incorrect PIN
        response = self.client.get(f"/bridge/pair?pin={wrong_pin}")
        self.assertEqual(response.status_code, 400)
        
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_pin")
        self.assertEqual(data["remaining_attempts"], 4)

    def test_brute_force_lockout(self):
        # 5 consecutive failures should lock out the IP
        wrong_pin = "111111"
        
        for i in range(4):
            resp = self.client.get(f"/bridge/pair?pin={wrong_pin}")
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["remaining_attempts"], 4 - i)

        # 5th attempt triggers lockout
        resp = self.client.get(f"/bridge/pair?pin={wrong_pin}")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"], "rate_limit_lockout")
        self.assertGreater(resp.json()["lockout_seconds"], 0)

        # Subsequent attempts are instantly blocked
        resp2 = self.client.get(f"/bridge/pair?pin=123456")
        self.assertEqual(resp2.status_code, 429)
        self.assertEqual(resp2.json()["error"], "rate_limit_lockout")

    def test_pin_sanitization(self):
        pin = api.create_pairing_pin()
        # PIN with hyphens or whitespace
        dirty_pin = f" {pin[:3]} - {pin[3:]} "
        
        response = self.client.get(f"/bridge/pair?pin={dirty_pin}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["token"], api._API_TOKEN)


if __name__ == "__main__":
    unittest.main()
