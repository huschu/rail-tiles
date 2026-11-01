import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cache_key as K


class Inputs(unittest.TestCase):
    def test_the_graph_build_depends_on_its_imports_and_their_data(self):
        names = {os.path.basename(p) for p in K.inputs([os.path.join(K.HERE, "graph.py")])}
        self.assertTrue({"graph.py", "classify.py", "colour.py", "geometry.py", "routes.py", "stations.py",
                         "protection.py", "train_protection.json"} <= names)
        self.assertNotIn("graph_join.py", names)


if __name__ == "__main__":
    unittest.main()
