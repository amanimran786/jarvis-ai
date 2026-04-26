import unittest

import skill_export


class SkillExportPreviewTests(unittest.TestCase):
    def test_preview_is_non_mutating_and_targets_agents_skills(self):
        payload = skill_export.preview_skill_exports()

        self.assertEqual(payload["mode"], "preview_only")
        self.assertFalse(payload["would_write"])
        self.assertEqual(payload["export_root"], ".agents/skills")
        self.assertGreater(payload["skill_count"], 0)
        self.assertTrue(payload["skills"])
        self.assertTrue(
            all(item["target_path"].startswith(".agents/skills/") for item in payload["skills"])
        )

    def test_rendered_export_content_marks_canonical_source(self):
        first = skill_export.skills.all_skills()[0]
        rendered = skill_export.render_export_content(first)

        self.assertIn("jarvis_export: true", rendered)
        self.assertIn('"jarvis_skill_id"', rendered)
        self.assertIn(first.id, rendered)
        self.assertIn("Do not edit this copy directly", rendered)


if __name__ == "__main__":
    unittest.main()
