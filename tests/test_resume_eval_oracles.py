"""Hand-computed examples check that reference oracles match their contracts."""

import importlib
import json
from pathlib import Path


def test_reference_rules_against_hand_computed_examples(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    workloads = importlib.import_module("resume_formal_workloads")
    examples = {
        "numeric": (
            12,
            [
                12,
                [0, 12],
                1,
                78,
                163,
                False,
                3,
                3,
                2,
                False,
                [1, 2, 3, 4, 6, 12],
                144,
                144,
                {"dollars": 0, "cents": 12, "negative": False},
                12,
                12,
                24,
                4,
                True,
                "12th",
                "000C",
                53.6,
                "medium",
                "35",
            ],
        ),
        "string": (
            "fooBar_One",
            [
                "fooBar_One",
                "foobar-one",
                "foo_bar_one",
                5,
                "fooBar_One",
                False,
                "F",
                "",
                {"foobar_one": 1},
                "foBar_One",
                "sbbOne_Bar",
                [
                    ["f", 1],
                    ["o", 2],
                    ["B", 1],
                    ["a", 1],
                    ["r", 1],
                    ["_", 1],
                    ["O", 1],
                    ["n", 1],
                    ["e", 1],
                ],
                "fooBar_One",
                "******_One",
                "",
                ["fooBar_One"],
                True,
                "Foobar_one",
                986,
                "",
                "foobarOne",
                "fooBar_One",
                "ar",
                1,
            ],
        ),
        "list": (
            [3, 1, 2],
            [
                [3, 1, 2],
                [1, 2, 3],
                [3, 4, 6],
                [-2, 1],
                2,
                1,
                2,
                [1, 2, 3],
                [2, 3, 1],
                6,
                2,
                2,
                [[3, 1, 2]],
                [3, 4, 5],
                3,
                4,
                {"3": 1, "1": 1, "2": 1},
                [2, 0, 1],
                [],
                6,
            ],
        ),
        "mapping": (
            {"a": 1, "b": 2},
            [
                3,
                {"a": 1, "b": 2},
                {"1": ["a"], "2": ["b"]},
                [["a", 1], ["b", 2]],
                "b",
                {"a": 1, "b": 2},
                {"a": 33.33, "b": 66.67},
                [],
                ["b", "a"],
                ["c"],
                {"a": 1, "b": 2, "c": 0},
                {"a": 1, "b": 3},
                {"negative": 0, "zero": 0, "positive": 2},
                {"a": 1, "b": 1},
                {"a": 1, "b": 2},
                {"a": 1, "b": 2},
            ],
        ),
        "booking": (
            {"a": [10, 40], "b": [20, 50]},
            [
                True,
                30,
                0,
                40,
                [20, 40],
                False,
                125,
                True,
                0,
                45,
                False,
                "00:10",
                [[10, 40], [20, 50]],
                2,
                [[0, 10], [40, 1440]],
                [0, 10],
            ],
        ),
    }
    namespace = {name: getattr(workloads, name) for name in ["math", "re", "statistics", "Counter"]}
    specs = workloads.coding_specs()
    for family, (value, expected) in examples.items():
        subset = [s for s in specs if s["family"] == family]
        assert len(subset) == len(expected)
        for spec, answer in zip(subset, expected):
            function = eval("lambda x: " + spec["reference"], namespace)
            assert json.dumps(function(value), sort_keys=True) == json.dumps(
                answer, sort_keys=True
            ), spec["id"]
