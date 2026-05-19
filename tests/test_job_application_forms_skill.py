import unittest

import skills


class JobApplicationFormsSkillTests(unittest.TestCase):
    def test_browser_requests_route_to_job_application_skill(self):
        selected = skills.choose_skill(
            "Fill this Workday job application form from my resume PDF, professional experience only.",
            tool="browser",
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, "job_application_forms")

    def test_skill_bundle_contains_non_submission_guardrail(self):
        bundle = skills.load_skill_bundle("job_application_forms")

        self.assertIn("Never click Save and Continue", bundle)
        self.assertIn("Manual fallback format:", bundle)
        self.assertIn("dispatch normal input and change events", bundle)


if __name__ == "__main__":
    unittest.main()
