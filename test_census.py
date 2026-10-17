import unittest

from census import cohort_for, normalize_label, wilson


class CensusTests(unittest.TestCase):
    def test_cohort_boundaries(self):
        self.assertEqual(cohort_for("2012-12-31T23:59:59Z"), "2008-2012")
        self.assertEqual(cohort_for("2013-01-01T00:00:00Z"), "2013-2016")
        self.assertIsNone(cohort_for("2007-12-31T23:59:59Z"))

    def test_model_json_variants(self):
        self.assertEqual(normalize_label({"label": "human"}), "human")
        self.assertEqual(normalize_label({"human": "human"}), "human")
        self.assertEqual(normalize_label({"label": "anime (manga style)"}), "anime")
        self.assertEqual(normalize_label({"label": "not sure"}), "uncertain")

    def test_wilson_interval_contains_observed_fraction(self):
        low, high = wilson(3, 30)
        self.assertLessEqual(low, 0.1)
        self.assertGreaterEqual(high, 0.1)


if __name__ == "__main__":
    unittest.main()
