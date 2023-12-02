"""\
Implementation of bin packing / binning algorithm
to automatically select most optimal set of tests
to run on each node.
"""
import glob
import json
import os
from pathlib import Path
from typing import List, Tuple


def load_bins(path: str | Path | None = None) -> List[List[str]]:
    """\
    Between tests we record durations.csv files which record how long each test took to run.
    We then transform those into a series of bins which evenly distribute the tests.
    This function loads that bins.json file into memory.
    """
    if isinstance(path, str):
        path = Path(path)

    if path is None:
        root_dir = os.environ.get("TEST_DIR")
        if root_dir is None:
            root_dir = Path.cwd()
        else:
            root_dir = Path(root_dir)

        path = root_dir / "bins.json"

    with path.open() as f:
        return json.load(f)


def discover_all_tests() -> List[str]:
    """\
    bins.json may not have all of the tests that we want to run.
    For example: what if someone adds a new test file?
    Here we collect the remaining set of tests.
    """
    python_files = [
        test
        for test in glob.glob("tests/**/*.py", recursive=True)
        if not any(
            sentinel in test
            for sentinel in [
                ".pyc",
                "__pycache__",
                "__init__.py",
                "conftest.py",
                "tests/incremental",
            ]
        )
    ]

    return [
        filename
        for filename in python_files
        if not any(
            sentinel in filename
            for sentinel in [
                ".pyc",
                "__pycache__",
                "__init__.py",
                "conftest.py",
                "tests/incremental",
            ]
        )
    ]


def find_new_tests(bins: List[List[str]], all_tests: List[str]) -> List[str]:
    """\
    Wow! The last docstring was a spoiler!
    Here we're doing the thing where we find new tests.
    """
    bined_tests = set()
    for bin in bins:
        bined_tests.update(bin)

    return [
        test
        for test in all_tests
        if test not in bined_tests
    ]


def bin_new_tests(bins: List[List[str]], new_tests: List[str]) -> List[List[str]]:
    """\
    And then finally we add the new tests we've discovered to the bins.
    These are the final bins we use to run the tests.
    """
    bins = [[*bin] for bin in bins]
    for new_test in new_tests:
        current_min_index = -1
        current_min_count = -1

        for i, bin in enumerate(bins):
            count = len(bin)
            if current_min_count == -1 or count < current_min_count:
                current_min_index = i
                current_min_count = count

        bins[current_min_index].append(new_test)
    return bins


def get_bins_and_new_tests() -> Tuple[List[List[str]], List[str]]:
    bins = load_bins()
    all_tests = discover_all_tests()
    new_tests = find_new_tests(bins, all_tests)
    return (
        bin_new_tests(bins, new_tests),
        new_tests,
    )
