import os
import unittest

import router
from router import route_stream
from tests.jarvis_golden_cases import GOLDEN_CASES


RUN_GOLDENS = os.getenv("JARVIS_RUN_GOLDEN_CASES") == "1"


@unittest.skipUnless(RUN_GOLDENS, "Set JARVIS_RUN_GOLDEN_CASES=1 to run live golden cases.")
class JarvisGoldenCases(unittest.TestCase):
    def _reset_router_state(self):
        router._clear_pending_recipient()
        router._clear_pending_message_draft()
        router._clear_pending_email_draft()
        router._awaiting_msg_recipient = False
        router._last_msg_recipient = ""
        router._last_message_send_result = None
        router._fuzzy_contact_suggestions.clear()

    def test_golden_cases(self):
        failures = []

        for case in GOLDEN_CASES:
            self._reset_router_state()
            stream, label = route_stream(case["prompt"])
            text = "".join(stream)

            if label != case["expected_label"]:
                failures.append(
                    f"{case['id']}: expected label {case['expected_label']} but got {label}"
                )

            for needle in case.get("must_include_all", []):
                if needle not in text:
                    failures.append(f"{case['id']}: missing required substring {needle!r}")

            if case.get("must_include_any"):
                if not any(needle in text for needle in case["must_include_any"]):
                    failures.append(
                        f"{case['id']}: missing every allowed substring from {case['must_include_any']!r}"
                    )

            for needle in case.get("must_exclude_all", []):
                if needle in text:
                    failures.append(f"{case['id']}: found forbidden substring {needle!r}")

        if failures:
            self.fail("\n".join(failures))


if __name__ == "__main__":
    unittest.main(verbosity=2)
