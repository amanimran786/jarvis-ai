import unittest

import skill_audit


class SkillAuditTests(unittest.TestCase):
    def test_audit_returns_structured_issues(self):
        payload = skill_audit.audit_skills()

        self.assertIn("ok", payload)
        self.assertGreater(payload["skill_count"], 0)
        self.assertIn("by_severity", payload)
        self.assertIn("issues", payload)
        self.assertIsInstance(payload["issues"], list)

    def test_missing_negative_triggers_are_reported(self):
        payload = skill_audit.audit_skills()
        codes = {issue["code"] for issue in payload["issues"]}

        self.assertIn("missing_negative_triggers", codes)

    def test_format_audit_is_human_readable(self):
        text = skill_audit.format_audit(limit=3)

        self.assertIn("Jarvis Skill Audit", text)
        self.assertIn("Severity", text)
        self.assertIn("Findings", text)


if __name__ == "__main__":
    unittest.main()
