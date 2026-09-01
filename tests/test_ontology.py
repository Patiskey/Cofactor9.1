import unittest


from cofactor_bench.ontology import analyze_target_ancestry


class TargetOntologyTests(unittest.TestCase):
    def test_finds_direct_and_transitive_target_ancestors_with_distance(self):
        graph = {
            "nodes": [
                {"id": "http://purl.obolibrary.org/obo/CHEBI_1", "lbl": "leaf"},
                {"id": "http://purl.obolibrary.org/obo/CHEBI_2", "lbl": "middle"},
                {"id": "http://purl.obolibrary.org/obo/CHEBI_3", "lbl": "root"},
            ],
            "edges": [
                {
                    "sub": "http://purl.obolibrary.org/obo/CHEBI_1",
                    "pred": "is_a",
                    "obj": "http://purl.obolibrary.org/obo/CHEBI_2",
                },
                {
                    "sub": "http://purl.obolibrary.org/obo/CHEBI_2",
                    "pred": "is_a",
                    "obj": "http://purl.obolibrary.org/obo/CHEBI_3",
                },
            ],
        }

        result = analyze_target_ancestry(graph, {"CHEBI:1", "CHEBI:3"})

        self.assertEqual(
            result["pairs"],
            [
                {
                    "specific": "CHEBI:1",
                    "ancestor": "CHEBI:3",
                    "distance": 2,
                }
            ],
        )
        self.assertEqual(result["ancestor_targets"], ["CHEBI:3"])
        self.assertEqual(result["missing_target_nodes"], [])

    def test_reports_target_missing_from_ontology(self):
        result = analyze_target_ancestry({"nodes": [], "edges": []}, {"CHEBI:9"})

        self.assertEqual(result["missing_target_nodes"], ["CHEBI:9"])


if __name__ == "__main__":
    unittest.main()
