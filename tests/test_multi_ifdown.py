import pgautofailover_utils as pgautofailover
from nose.tools import raises, eq_
import time

import os.path

cluster = None
monitor = None
node1 = None
node2 = None
node3 = None

def setup_module():
    global cluster
    cluster = pgautofailover.Cluster()

def teardown_module():
    cluster.destroy()

def test_000_create_monitor():
    global monitor
    monitor = cluster.create_monitor("/tmp/multi_ifdown/monitor")
    monitor.run()
    monitor.wait_until_pg_is_running()

def test_001_init_primary():
    global node1
    node1 = cluster.create_datanode("/tmp/multi_ifdown/node1")
    node1.create()
    node1.run()
    assert node1.wait_until_state(target_state="single")

def test_002_add_standby():
    global node2

    node2 = cluster.create_datanode("/tmp/multi_ifdown/node2")
    node2.create()
    node2.run()

    assert node2.wait_until_state(target_state="secondary")

    assert node1.has_needed_replication_slots()
    assert node2.has_needed_replication_slots()

    # make sure we reached primary on node1 before next tests
    assert node1.wait_until_state(target_state="primary")

def test_003_add_standby():
    global node3

    node3 = cluster.create_datanode("/tmp/multi_ifdown/node3")
    node3.create()
    node3.run()

    assert node3.wait_until_state(target_state="secondary")
    assert node2.wait_until_state(target_state="secondary")
    assert node1.wait_until_state(target_state="primary")

    assert node1.has_needed_replication_slots()
    assert node2.has_needed_replication_slots()
    assert node3.has_needed_replication_slots()

    # the formation number_sync_standbys is expected to be set to 1 now
    assert node1.get_number_sync_standbys() == 1

    # make sure we reached primary on node1 before next tests
    assert node1.wait_until_state(target_state="primary")

def test_004_write_into_primary():
    node1.run_sql_query("CREATE TABLE t1(a int)")
    node1.run_sql_query("INSERT INTO t1 VALUES (1), (2), (3), (4)")
    node1.run_sql_query("CHECKPOINT")

    results = node1.run_sql_query("SELECT * FROM t1")
    assert results == [(1,), (2,), (3,), (4,)]

def test_005_set_candidate_priorities():
    print()
    assert node1.wait_until_state(target_state="primary")

    # set priorities in a way that we know the candidate: node3
    node1.set_candidate_priority(90) # current primary
    node2.set_candidate_priority(0)  # not a candidate anymore
    node3.set_candidate_priority(90)

    # when we set candidate priority we go to apply_settings then primary
    print()
    assert node1.wait_until_state(target_state="primary")
    assert node2.wait_until_state(target_state="secondary")
    assert node3.wait_until_state(target_state="secondary")

    # node1 should still be "sync"
    assert node1.get_number_sync_standbys() == 1
    assert node2.get_replication_quorum()

    # also let's see synchronous_standby_names here
    print("Monitor: %s" % node1.get_synchronous_standby_names())
    print("Node 1:  %s" %
          node1.run_sql_query("show synchronous_standby_names")[0][0])

def test_006_ifdown_node3():
    node3.ifdown()

def test_007_insert_rows():
    node1.run_sql_query(
        "INSERT INTO t1 SELECT x+10 FROM generate_series(1, 10000) as gs(x)")
    node1.run_sql_query("CHECKPOINT")

    lsn1 = node1.run_sql_query("select pg_current_wal_lsn()")[0][0]
    print("%s " % lsn1, end="", flush=True)

    # node2 is sync and should get the WAL
    lsn2 = node2.run_sql_query("select pg_last_wal_receive_lsn()")[0][0]
    print("%s " % lsn2, end="", flush=True)

    while lsn2 != lsn1:
        time.sleep(1)
        lsn2 = node2.run_sql_query("select pg_last_wal_receive_lsn()")[0][0]
        print("%s " % lsn2, end="", flush=True)

    eq_(lsn1, lsn2)

def test_008_failover():
    print()
    print("Injecting failure of node1")
    node1.fail()

    # have node2 re-join the network and hopefully reconnect etc
    print("Reconnecting node3 (ifconfig up)")
    node3.ifup()

    # now we should be able to continue with the failover, and fetch missing
    # WAL bits from node2
    node3.wait_until_pg_is_running()
    assert node2.has_needed_replication_slots()
    assert node3.has_needed_replication_slots()

    assert node3.wait_until_state(target_state="wait_primary", timeout=120)
    assert node2.wait_until_state(target_state="secondary")
    assert node3.wait_until_state(target_state="primary")

def test_009_read_from_new_primary():
    results = node3.run_sql_query("SELECT count(*) FROM t1")
    assert results == [(10004,)]

def test_010_start_node1_again():
    node1.run()
    assert node1.wait_until_state(target_state="secondary")

    assert node2.wait_until_state(target_state="secondary")
    assert node3.wait_until_state(target_state="primary")

    assert node1.has_needed_replication_slots()
    assert node2.has_needed_replication_slots()
    assert node3.has_needed_replication_slots()

# test_011_XXX, test_012_XXX, test_013_XXX, test_014_XXX and test_015_XXX
# are meant to test the scenario when the most advanced secondary
# becomes inaccessible at the same time when the primary is inaccessible
def test_011_prepare_candidate_priorities():
    # we are aiming to promote node2
    assert node2.set_candidate_priority(100)  # the next primary

    # other nodes are already candidates for primary, but with less
    # priority
    assert node1.get_candidate_priority() == 90
    assert node3.get_candidate_priority() == 90

def test_012_prepare_replication_quorums():
    # for the purpose of this test, we need one node
    # async, to allow that we should decrement the sync stanbys
    node3.set_number_sync_standbys(0)

    # to emulate one node is behind, it is easier to make it async
    # we want node2 to be behind others
    assert node2.set_replication_quorum("false")

    # others should be sync
    assert node1.get_replication_quorum()
    assert node3.get_replication_quorum()

def test_013_secondary_gets_behind_primary():
    # make sure that node2 gets behind of the primary
    node2.ifdown()

    # primary ingests some data
    node3.run_sql_query("INSERT INTO t1 VALUES (5), (6)")
    node3.run_sql_query("CHECKPOINT")

    # ensure that the healthy secondary gets the change
    results = node1.run_sql_query("SELECT count(*) FROM t1")
    assert results == [(10006,)]

    lsn1 = node1.run_sql_query("select pg_last_wal_receive_lsn()")[0][0]
    print("%s " % lsn1, end="", flush=True)

    # ensure the monitor received this lsn
    time.sleep(2)
    q = "select reportedlsn from pgautofailover.node where nodeid = 1"
    lsn1m = monitor.run_sql_query(q)[0][0]
    print("%s " % lsn1m, end="", flush=True)

    eq_(lsn1, lsn1m)

def test_014_secondary_reports_lsn():
    # make the primary and mostAdvanced secondary inaccessible
    # and the candidate for failover as accessible
    # which means that node2 will not be able to fetch wal
    # and blocked until the other secondary is up
    node3.ifdown()    # primary
    node1.ifdown()    # most advanced standby
    node2.ifup()      # failover candidate

    print()
    print("Calling pgautofailover.failover() on the monitor")
    monitor.failover()

    # node2 reports its LSN while others are inaccessible
    assert node2.wait_until_state(target_state="report_lsn")

def test_015_finalize_failover_after_most_advanced_secondary_gets_back():
    # when they are accessible again, both should become
    # secondaries
    node1.ifup()    # old most advanced secondary, now secondary
    node3.ifup()    # old primary, now secondary

    # and, node2 should finally become the primary without losing any data
    assert node2.wait_until_state(target_state="wait_primary")

    print("%s" % monitor.pg_autoctl.err)

    results = node2.run_sql_query("SELECT count(*) FROM t1")
    eq_(results, [(10006,)])

    assert node1.wait_until_state(target_state="secondary")
    assert node3.wait_until_state(target_state="secondary")
    assert node2.wait_until_state(target_state="primary")
