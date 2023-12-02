import re

import csv
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
import sys
from typing import List

from _pytest.runner import CollectReport
from _pytest.reports import TestReport
from xdist.remote import Producer
from xdist.report import report_collection_diff
from xdist.workermanage import parse_spec_config



@dataclass
class RetryInfo:
    retry_count: int
    original_test_report: TestReport


class LoadScopeScheduling:
    """Implement load scheduling across nodes, but grouping test by scope.

    This distributes the tests collected across all nodes so each test is run
    just once.  All nodes collect and submit the list of tests and when all
    collections are received it is verified they are identical collections.
    Then the collection gets divided up in work units, grouped by test scope,
    and those work units get submitted to nodes.  Whenever a node finishes an
    item, it calls ``.mark_test_complete()`` which will trigger the scheduler
    to assign more work units if the number of pending tests for the node falls
    below a low-watermark.

    When created, ``numnodes`` defines how many nodes are expected to submit a
    collection. This is used to know when all nodes have finished collection.

    Attributes:

    :numnodes: The expected number of nodes taking part.  The actual number of
       nodes will vary during the scheduler's lifetime as nodes are added by
       the DSession as they are brought up and removed either because of a dead
       node or normal shutdown.  This number is primarily used to know when the
       initial collection is completed.

    :collection: The final list of tests collected by all nodes once it is
       validated to be identical between all the nodes.  It is initialised to
       None until ``.schedule()`` is called.

    :workqueue: Ordered dictionary that maps all available scopes with their
       associated tests (nodeid). Nodeids are in turn associated with their
       completion status. One entry of the workqueue is called a work unit.
       In turn, a collection of work unit is called a workload.

       ::

            workqueue = {
                '<full>/<path>/<to>/test_module.py': {
                    '<full>/<path>/<to>/test_module.py::test_case1': False,
                    '<full>/<path>/<to>/test_module.py::test_case2': False,
                    (...)
                },
                (...)
            }

    :assigned_work: Ordered dictionary that maps worker nodes with their
       assigned work units.

       ::

            assigned_work = {
                '<worker node A>': {
                    '<full>/<path>/<to>/test_module.py': {
                        '<full>/<path>/<to>/test_module.py::test_case1': False,
                        '<full>/<path>/<to>/test_module.py::test_case2': False,
                        (...)
                    },
                    (...)
                },
                (...)
            }

    :registered_collections: Ordered dictionary that maps worker nodes with
       their collection of tests gathered during test discovery.

       ::

            registered_collections = {
                '<worker node A>': [
                    '<full>/<path>/<to>/test_module.py::test_case1',
                    '<full>/<path>/<to>/test_module.py::test_case2',
                ],
                (...)
            }

    :log: A py.log.Producer instance.

    :config: Config object, used for handling hooks.
    """

    RETRIES_MODULE_AND_TEST_REGEX = re.compile('([^:]+)::(.+)')

    def __init__(self, config, log=None, *, new_tests: List[str]):
        self.numnodes = len(parse_spec_config(config))
        self.collection = None

        self.assigned_work = OrderedDict()
        self.registered_collections = OrderedDict()
        self.durations = OrderedDict()

        self.retries: dict[str, RetryInfo] = {}
        self.retry_queue = OrderedDict()

        if log is None:
            self.log = Producer("loadscopesched")
        else:
            self.log = log.loadscopesched

        self.config = config
        self.new_tests = new_tests

    @property
    def nodes(self):
        """A list of all active nodes in the scheduler."""
        return list(self.assigned_work.keys())

    @property
    def collection_is_completed(self):
        """Boolean indication initial test collection is complete.

        This is a boolean indicating all initial participating nodes have
        finished collection.  The required number of initial nodes is defined
        by ``.numnodes``.
        """
        return len(self.registered_collections) >= self.numnodes

    @property
    def tests_finished(self):
        """Return True if all tests have been executed by the nodes."""

        # if self.workqueue:
        #    return False

        if len(self.assigned_work) == 0:
            # We haven't begun
            return False

        if all([len(i) == 0 for i in self.assigned_work.values()]):
            # We haven't begun
            return False

        for node in self.assigned_work:
            if not all([x for x in self.assigned_work[node].values()]):
                return False

        with open('durations.csv', 'w') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['nodeid', 'duration'])

            for nodeid, duration in self.durations.items():
                writer.writerow([nodeid, duration])

        with open('flakes.csv', 'w') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['filepath', 'test', 'num_retries'])
            for entry, retry_info in self.retries.items():
                    match = LoadScopeScheduling.RETRIES_MODULE_AND_TEST_REGEX.match(entry)
                    if match is None:
                        continue
                    try:
                        filepath = match.groups()[0]
                        test_name = match.groups()[1]
                        writer.writerow([filepath, test_name, retry_info.retry_count])
                    except IndexError:
                        print(f"FAILURE ON FLAKES REGEX {entry}")

        if self.retries:
            print("======== Flaky Tests Output ========")
            for nodeid, retry_info in self.retries.items():
                print(f"---- {nodeid} ----")
                print(retry_info.original_test_report.longreprtext)
                print()

        return True

    @property
    def has_pending(self):
        """Return True if there are pending test items.

        This indicates that collection has finished and nodes are still
        processing test items, so this can be thought of as
        "the scheduler is active".
        """
        return not self.tests_finished and self.collection_is_completed

    def add_node(self, node):
        """Add a new node to the scheduler.

        From now on the node will be assigned work units to be executed.

        Called by the ``DSession.worker_workerready`` hook when it successfully
        bootstraps a new node.
        """
        assert node not in self.assigned_work
        self.assigned_work[node] = OrderedDict()

    def remove_node(self, node):
        """Remove a node from the scheduler.

        This should be called either when the node crashed or at shutdown time.
        In the former case any pending items assigned to the node will be
        re-scheduled.

        Called by the hooks:

        - ``DSession.worker_workerfinished``.
        - ``DSession.worker_errordown``.

        Return the item being executed while the node crashed or None if the
        node has no more pending items.
        """
        self.log("remove_node", node)
        return None

    def add_node_collection(self, node, collection):
        """Add the collected test items from a node.

        The collection is stored in the ``.registered_collections`` dictionary.

        Called by the hook:

        - ``DSession.worker_collectionfinish``.
        """

        self.log("add_node_collection", node, len(collection))

        # Check that add_node() was called on the node before
        assert node in self.assigned_work

        # A new node has been added later, perhaps an original one died.
        if self.collection_is_completed:
            # Assert that .schedule() should have been called by now
            assert self.collection
            self.registered_collections[node] = list(collection)

        self.registered_collections[node] = list(collection)

        total_number = sum([len(i) for i in self.registered_collections.values()])

        return total_number

    def mark_test_complete(self, node, item_index, duration=0):
        """Mark test item as completed by node.

        Called by the hook:

        - ``DSession.worker_testreport``.
        """
        nodeid = self.registered_collections[node][item_index]

        self.durations[nodeid] = duration

        self.assigned_work[node][nodeid] = True
        self._reschedule(node)

    def mark_test_pending(self, item):
        raise NotImplementedError()

    def _assign_work_unit(self, node):
        """Assign a work unit to a node."""
        self.log("assign work unit")

        nodeids_indexes = [i for i in range(len(self.registered_collections[node]))]

        for idx in nodeids_indexes:
            nodeid = self.registered_collections[node][idx]

            assigned_to_node = self.assigned_work.setdefault(
                node, default=OrderedDict()
            )
            assigned_to_node[nodeid] = False

        self.log(f"Assigned work to {node}")
        self.log(f"Running {nodeids_indexes}")

        node.send_runtest_some(nodeids_indexes)

    def handle_failed_test(self, node, rep):
        if rep.nodeid in self.new_tests:
            print(f"Failing test {rep.nodeid} immediately because it is a new test.")
            return True

        if rep.nodeid not in self.retries:
            self.retries[rep.nodeid] = RetryInfo(
                retry_count=0,
                original_test_report=rep,
            )
        retry_info = self.retries[rep.nodeid]

        print (f"Handling a failed test, nodeid: {rep.nodeid}, retry count: {retry_info.retry_count}")

        if retry_info.retry_count >= 5:
            return True

        if retry_info.retry_count == 0:
            retry = self.retry_queue.setdefault(node, default=[])
            retry.append(rep.nodeid)

        retry_info.retry_count += 1

        return False

    def _pending_of(self, workload):
        """Return the number of pending tests in a workload."""
        pending = len([1 for scope in workload.values() if not scope])
        return pending

    def _reschedule(self, node):
        """Maybe schedule new items on the node.

        If there are any globally pending work units left then this will check
        if the given node should be given any more tests.
        """
        while self.retry_queue.get(node, []):
            nodeid = self.retry_queue[node].pop()
            if nodeid not in self.registered_collections[node]:
                continue

            nodeid_index = self.registered_collections[node].index(nodeid)
            node.send_runtest_some([
                nodeid_index,
                nodeid_index,
                nodeid_index,
                nodeid_index,
                nodeid_index
            ])
            print (f'Enqueing 5 retries for {nodeid}')

        if self._pending_of(self.assigned_work[node]) <= 1:
            self.log("Shutting down node due to no more work")
            node.shutdown()

    def schedule(self):
        """Initiate distribution of the test collection.

        Initiate scheduling of the items across the nodes.  If this gets called
        again later it behaves the same as calling ``._reschedule()`` on all
        nodes so that newly added nodes will start to be used.

        If ``.collection_is_completed`` is True, this is called by the hook:

        - ``DSession.worker_collectionfinish``.
        """
        assert self.collection_is_completed

        # Collections are identical, create the final list of items
        self.collection = list(next(iter(self.registered_collections.values())))

        if not self.collection:
            return

        # Avoid having more workers than work
        for node, values in self.registered_collections.items():
            if len(values) == 0:
                self.log(f"Shutting down unused node {node}")
                node.shutdown()

        # Assign initial workload
        for node in self.nodes:
            self._assign_work_unit(node)
