from pathlib import Path
import unittest

from camera_based_rgbww_optimizer.paths import PROJECT_ROOT, PROJECT_TMP_DIR


class PathsTests(unittest.TestCase):
    def test_project_root_points_to_repository_root(self):
        expected_root = Path(__file__).resolve().parents[1]
        self.assertEqual(PROJECT_ROOT, expected_root)
        self.assertTrue((PROJECT_ROOT / "pyproject.toml").exists())
        self.assertEqual(PROJECT_TMP_DIR, PROJECT_ROOT / "tmp")


if __name__ == "__main__":
    unittest.main()
