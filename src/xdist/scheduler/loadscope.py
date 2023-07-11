from collections import OrderedDict

from _pytest.runner import CollectReport
from xdist.remote import Producer
from xdist.report import report_collection_diff
from xdist.workermanage import parse_spec_config


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

    def __init__(self, config, log=None):
        self.numnodes = len(parse_spec_config(config))
        self.collection = None

        self.assigned_work = OrderedDict()
        self.registered_collections = OrderedDict()

        if log is None:
            self.log = Producer("loadscopesched")
        else:
            self.log = log.loadscopesched

        self.config = config

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
        self.log("TESTS FINISHED CHECK")
        if not self.collection_is_completed:
            return False

        # if self.workqueue:
        #    return False

        for node in self.assigned_work:
            if not all([x for x in self.assigned_work[node].values()]):
                return False

        return True

    @property
    def has_pending(self):
        """Return True if there are pending test items.

        This indicates that collection has finished and nodes are still
        processing test items, so this can be thought of as
        "the scheduler is active".
        """
        for assigned_unit in self.assigned_work.values():
            if self._pending_of(assigned_unit) > 0:
                return True

        return False

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
        workload = self.assigned_work.pop(node)
        if not self._pending_of(workload):
            return None

        # The node crashed, identify test that crashed
        for work_unit in workload.values():
            for nodeid, completed in work_unit.items():
                if not completed:
                    crashitem = nodeid
                    break
            else:
                continue
            break
        else:
            raise RuntimeError(
                "Unable to identify crashitem on a workload with pending items"
            )

        for node in self.assigned_work:
            self._reschedule(node)

        return crashitem

    def add_node_collection(self, node, collection):
        """Add the collected test items from a node.

        The collection is stored in the ``.registered_collections`` dictionary.

        Called by the hook:

        - ``DSession.worker_collectionfinish``.
        """

        # Check that add_node() was called on the node before
        assert node in self.assigned_work

        # A new node has been added later, perhaps an original one died.
        if self.collection_is_completed:
            # Assert that .schedule() should have been called by now
            assert self.collection
            self.registered_collections[node] = list(collection)

        self.registered_collections[node] = list(collection)

    def mark_test_complete(self, node, item_index, duration=0):
        """Mark test item as completed by node.

        Called by the hook:

        - ``DSession.worker_testreport``.
        """
        nodeid = self.registered_collections[node][item_index]

        self.assigned_work[node][nodeid] = True
        self._reschedule(node)

    def mark_test_pending(self, item):
        raise NotImplementedError()

    def _assign_work_unit(self, node):
        """Assign a work unit to a node."""
        self.log(self.assigned_work)
        self.log(self.registered_collections)

        nodeids_indexes = [i for i in range(len(self.registered_collections[node]))]

        for idx in nodeids_indexes:
            nodeid = self.registered_collections[node][idx]

            assigned_to_node = self.assigned_work.setdefault(
                node, default=OrderedDict()
            )
            assigned_to_node[nodeid] = False

        node.send_runtest_some(nodeids_indexes)

    def _split_scope(self, nodeid):
        """Determine the scope (grouping) of a nodeid.

        There are usually 3 cases for a nodeid::

            example/loadsuite/test/test_beta.py::test_beta0
            example/loadsuite/test/test_delta.py::Delta1::test_delta0
            example/loadsuite/epsilon/__init__.py::epsilon.epsilon

        #. Function in a test module.
        #. Method of a class in a test module.
        #. Doctest in a function in a package.

        This function will group tests with the scope determined by splitting
        the first ``::`` from the right. That is, classes will be grouped in a
        single work unit, and functions from a test module will be grouped by
        their module. In the above example, scopes will be::

            example/loadsuite/test/test_beta.py
            example/loadsuite/test/test_delta.py::Delta1
            example/loadsuite/epsilon/__init__.py
        """
        return nodeid

    def _pending_of(self, workload):
        """Return the number of pending tests in a workload."""
        pending = sum(list(scope.values()).count(False) for scope in workload.values())
        return pending

    def _reschedule(self, node):
        """Maybe schedule new items on the node.

        If there are any globally pending work units left then this will check
        if the given node should be given any more tests.
        """

    def schedule(self):
        """Initiate distribution of the test collection.

        Initiate scheduling of the items across the nodes.  If this gets called
        again later it behaves the same as calling ``._reschedule()`` on all
        nodes so that newly added nodes will start to be used.

        If ``.collection_is_completed`` is True, this is called by the hook:

        - ``DSession.worker_collectionfinish``.
        """
        assert self.collection_is_completed

        # Initial distribution already happened, reschedule on all nodes
        if self.collection is not None:
            for node in self.nodes:
                self._reschedule(node)
            return

        # Check that all nodes collected the same tests
        # if not self._check_nodes_have_same_collection():
        #    self.log("**Different tests collected, aborting run**")
        #    return

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

    def _check_nodes_have_same_collection(self):
        """Return True if all nodes have collected the same items.

        If collections differ, this method returns False while logging
        the collection differences and posting collection errors to
        pytest_collectreport hook.
        """
        node_collection_items = list(self.registered_collections.items())
        first_node, col = node_collection_items[0]
        same_collection = True

        for node, collection in node_collection_items[1:]:
            msg = report_collection_diff(
                col, collection, first_node.gateway.id, node.gateway.id
            )
            if not msg:
                continue

            same_collection = False
            # self.log(msg)

            if self.config is None:
                continue

            rep = CollectReport(node.gateway.id, "failed", longrepr=msg, result=[])
            self.config.hook.pytest_collectreport(report=rep)

        return same_collection
