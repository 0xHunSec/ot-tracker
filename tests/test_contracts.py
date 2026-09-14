import unittest

from ot_tracker.contracts import (
    cross_source_contract_mismatches,
    cross_source_contract_observations,
)


class CrossSourceContractTest(unittest.TestCase):
    def test_detects_third_party_contract_mismatch_case_insensitively(self) -> None:
        trials = [
            {
                "trial_name": "ExampleTrial",
                "allow_third_party_origins": False,
            }
        ]
        declarations = [
            {
                "runtime_feature_name": "ExampleRuntime",
                "trial_name": "exampletrial",
                "fields": {"origin_trial_allows_third_party": True},
            }
        ]

        observations = cross_source_contract_observations(trials, declarations)
        self.assertEqual(1, len(observations))
        self.assertFalse(observations[0]["matches"])
        self.assertEqual(["ExampleRuntime"], observations[0]["runtime_features"])
        self.assertEqual(
            [
                {
                    "trial_name": "ExampleTrial",
                    "field": "allow_third_party_origins",
                    "console": False,
                    "chromium": True,
                }
            ],
            cross_source_contract_mismatches(trials, declarations),
        )

    def test_skips_contract_when_runtime_declaration_is_unavailable(self) -> None:
        self.assertEqual(
            [],
            cross_source_contract_observations(
                [
                    {
                        "trial_name": "ExampleTrial",
                        "allow_third_party_origins": False,
                    }
                ],
                [],
            ),
        )


if __name__ == "__main__":
    unittest.main()
