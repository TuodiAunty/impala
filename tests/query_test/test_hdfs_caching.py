# Copyright (c) 2012 Cloudera, Inc. All rights reserved.
# Validates limit on scan nodes

import pytest
import re
import time
from subprocess import check_call

from tests.common.impala_cluster import ImpalaCluster
from tests.common.impala_test_suite import ImpalaTestSuite
from tests.common.skip import SkipIfS3, SkipIfIsilon, SkipIfLocal
from tests.common.test_dimensions import create_single_exec_option_dimension
from tests.util.filesystem_utils import get_fs_path
from tests.util.shell_util import exec_process

# End to end test that hdfs caching is working.
@SkipIfS3.caching # S3: missing coverage: verify SET CACHED gives error
@SkipIfIsilon.caching
@SkipIfLocal.caching
class TestHdfsCaching(ImpalaTestSuite):
  @classmethod
  def get_workload(self):
    return 'tpch'

  @classmethod
  def add_test_dimensions(cls):
    super(TestHdfsCaching, cls).add_test_dimensions()
    cls.TestMatrix.add_constraint(lambda v:\
        v.get_value('exec_option')['batch_size'] == 0)
    cls.TestMatrix.add_constraint(lambda v:\
        v.get_value('table_format').file_format == "text")

  # The tpch nation table is cached as part of data loading. We'll issue a query
  # against this table and verify the metric is updated correctly.
  @pytest.mark.execute_serially
  def test_table_is_cached(self, vector):
    cached_read_metric = "impala-server.io-mgr.cached-bytes-read"
    query_string = "select count(*) from tpch.nation"
    expected_bytes_delta = 2199
    impala_cluster = ImpalaCluster()

    # Collect the cached read metric on all the impalads before running the query
    cached_bytes_before = list()
    for impalad in impala_cluster.impalads:
      cached_bytes_before.append(impalad.service.get_metric_value(cached_read_metric))

    # Execute the query.
    result = self.execute_query(query_string)
    assert(len(result.data) == 1)
    assert(result.data[0] == '25')

    # Read the metrics again.
    cached_bytes_after = list()
    for impalad in impala_cluster.impalads:
      cached_bytes_after.append(impalad.service.get_metric_value(cached_read_metric))

    # Verify that the cached bytes increased by the expected number on exactly one of
    # the impalads.
    num_metrics_increased = 0
    assert(len(cached_bytes_before) == len(cached_bytes_after))
    for i in range(0, len(cached_bytes_before)):
      assert(cached_bytes_before[i] == cached_bytes_after[i] or\
             cached_bytes_before[i] + expected_bytes_delta == cached_bytes_after[i])
      if cached_bytes_after[i] > cached_bytes_before[i]:
        num_metrics_increased = num_metrics_increased + 1

    if num_metrics_increased != 1:
      # Test failed, print the metrics
      for i in range(0, len(cached_bytes_before)):
        print "%d %d" % (cached_bytes_before[i], cached_bytes_after[i])
      assert(False)

  def test_cache_cancellation(self, vector):
    """ This query runs on some mix of cached and not cached tables. The query has
        a limit so it exercises the cancellation paths. Regression test for
        IMPALA-1019. """
    num_iters = 100
    query_string = """
      with t1 as (select int_col x, bigint_col y from functional.alltypes limit 2),
           t2 as (select int_col x, bigint_col y from functional.alltypestiny limit 2),
           t3 as (select int_col x, bigint_col y from functional.alltypessmall limit 2)
      select * from t1, t2, t3 where t1.x = t2.x and t2.x = t3.x """

    # Run this query for some iterations since it is timing dependent.
    for x in xrange(1, num_iters):
      result = self.execute_query(query_string)
      assert(len(result.data) == 2)

# A separate class has been created for "test_hdfs_caching_fallback_path" to make it
# run as a part of exhaustive tests which require the workload to be 'functional-query'.
# TODO: Move this to TestHdfsCaching once we make exhaustive tests run for other workloads
@SkipIfS3.caching
@SkipIfIsilon.caching
@SkipIfLocal.caching
class TestHdfsCachingFallbackPath(ImpalaTestSuite):
  @classmethod
  def get_workload(self):
    return 'functional-query'

  @SkipIfS3.hdfs_encryption
  @SkipIfIsilon.hdfs_encryption
  @SkipIfLocal.hdfs_encryption
  def test_hdfs_caching_fallback_path(self, vector, unique_database, testid_checksum):
    """ This tests the code path of the query execution where the hdfs cache read fails
    and the execution falls back to the normal read path. To reproduce this situation we
    rely on IMPALA-3679, where zcrs are not supported with encryption zones. This makes
    sure ReadFromCache() fails and falls back to ReadRange() to read the scan range."""

    if self.exploration_strategy() != 'exhaustive' or\
        vector.get_value('table_format').file_format != 'text':
      pytest.skip()

    # Create a new encryption zone and copy the tpch.nation table data into it.
    encrypted_table_dir = get_fs_path("/test-warehouse/" + testid_checksum)
    create_query_sql = "CREATE EXTERNAL TABLE %s.cached_nation like tpch.nation "\
        "LOCATION '%s'" % (unique_database, encrypted_table_dir)
    check_call(["hdfs", "dfs", "-mkdir", encrypted_table_dir], shell=False)
    check_call(["hdfs", "crypto", "-createZone", "-keyName", "testKey1", "-path",\
        encrypted_table_dir], shell=False)
    check_call(["hdfs", "dfs", "-cp", get_fs_path("/test-warehouse/tpch.nation/*.tbl"),\
        encrypted_table_dir], shell=False)
    # Reduce the scan range size to force the query to have multiple scan ranges.
    exec_options = vector.get_value('exec_option')
    exec_options['max_scan_range_length'] = 1024
    try:
      self.execute_query_expect_success(self.client, create_query_sql)
      # Cache the table data
      self.execute_query_expect_success(self.client, "ALTER TABLE %s.cached_nation set "
         "cached in 'testPool'" % unique_database)
      # Wait till the whole path is cached. We set a deadline of 20 seconds for the path
      # to be cached to make sure this doesn't loop forever in case of caching errors.
      caching_deadline = time.time() + 20
      while not is_path_fully_cached(encrypted_table_dir):
        if time.time() > caching_deadline:
          pytest.fail("Timed out caching path: " + encrypted_table_dir)
        time.sleep(2)
      self.execute_query_expect_success(self.client, "invalidate metadata "
          "%s.cached_nation" % unique_database);
      result = self.execute_query_expect_success(self.client, "select count(*) from "
          "%s.cached_nation" % unique_database, exec_options)
      assert(len(result.data) == 1)
      assert(result.data[0] == '25')
    except Exception as e:
      pytest.fail("Failure in test_hdfs_caching_fallback_path: " + str(e))
    finally:
      check_call(["hdfs", "dfs", "-rm", "-r", "-f", "-skipTrash", encrypted_table_dir],\
          shell=False)


@SkipIfS3.caching
@SkipIfIsilon.caching
@SkipIfLocal.caching
class TestHdfsCachingDdl(ImpalaTestSuite):
  @classmethod
  def get_workload(self):
    return 'functional-query'

  @classmethod
  def add_test_dimensions(cls):
    super(TestHdfsCachingDdl, cls).add_test_dimensions()
    cls.TestMatrix.add_dimension(create_single_exec_option_dimension())

    cls.TestMatrix.add_constraint(lambda v:\
        v.get_value('table_format').file_format == 'text' and \
        v.get_value('table_format').compression_codec == 'none')

  def setup_method(self, method):
    self.cleanup_db("cachedb")
    self.client.execute("create database cachedb")

  def teardown_method(self, method):
    self.cleanup_db("cachedb")

  @pytest.mark.execute_serially
  def test_caching_ddl(self, vector):

    # Get the number of cache requests before starting the test
    num_entries_pre = get_num_cache_requests()
    self.run_test_case('QueryTest/hdfs-caching', vector)

    # After running this test case we should be left with 9 cache requests.
    # In this case, 1 for each table + 7 more for each cached partition + 1
    # for the table with partitions on both HDFS and local file system.
    assert num_entries_pre == get_num_cache_requests() - 9

    self.client.execute("drop table cachedb.cached_tbl_part")
    self.client.execute("drop table cachedb.cached_tbl_nopart")
    self.client.execute("drop table cachedb.cached_tbl_local")

    # Dropping the tables should cleanup cache entries leaving us with the same
    # total number of entries
    assert num_entries_pre == get_num_cache_requests()

  @pytest.mark.execute_serially
  def test_cache_reload_validation(self, vector):
    """This is a set of tests asserting that cache directives modified
       outside of Impala are picked up after reload, cf IMPALA-1645"""

    num_entries_pre = get_num_cache_requests()
    create_table = ("create table cachedb.cached_tbl_reload "
        "(id int) cached in 'testPool' with replication = 8")
    self.client.execute(create_table)

    # Access the table once to load the metadata
    self.client.execute("select count(*) from cachedb.cached_tbl_reload")

    create_table = ("create table cachedb.cached_tbl_reload_part (i int) "
        "partitioned by (j int) cached in 'testPool' with replication = 8")
    self.client.execute(create_table)

    # Add two partitions
    self.client.execute("alter table cachedb.cached_tbl_reload_part add partition (j=1)")
    self.client.execute("alter table cachedb.cached_tbl_reload_part add partition (j=2)")

    assert num_entries_pre + 4 == get_num_cache_requests(), \
      "Adding the tables should be reflected by the number of cache directives."

    # Modify the cache directive outside of Impala and reload the table to verify
    # that changes are visible
    drop_cache_directives_for_path("/test-warehouse/cachedb.db/cached_tbl_reload")
    drop_cache_directives_for_path("/test-warehouse/cachedb.db/cached_tbl_reload_part")
    drop_cache_directives_for_path(
        "/test-warehouse/cachedb.db/cached_tbl_reload_part/j=1")
    change_cache_directive_repl_for_path(
        "/test-warehouse/cachedb.db/cached_tbl_reload_part/j=2", 3)

    # Create a bogus cached table abusing an existing cache directive ID, IMPALA-1750
    dirid = get_cache_directive_for_path("/test-warehouse/cachedb.db/cached_tbl_reload_part/j=2")
    self.client.execute(("create table cachedb.no_replication_factor (id int) " \
                         "tblproperties(\"cache_directive_id\"=\"%s\")" % dirid))
    self.run_test_case('QueryTest/hdfs-caching-validation', vector)
    # Temp fix for IMPALA-2510. Due to IMPALA-2518, when the test database is dropped,
    # the cache directives are not removed for table 'cached_tbl_reload_part'.
    drop_cache_directives_for_path(
        "/test-warehouse/cachedb.db/cached_tbl_reload_part/j=2")

def drop_cache_directives_for_path(path):
  """Drop the cache directive for a given path"""
  rc, stdout, stderr = exec_process("hdfs cacheadmin -removeDirectives -path %s" % path)
  assert rc == 0, \
      "Error removing cache directive for path %s (%s, %s)" % (path, stdout, stderr)

def is_path_fully_cached(path):
  """Returns true if all the bytes of the path are cached, false otherwise"""
  rc, stdout, stderr = exec_process("hdfs cacheadmin -listDirectives -stats -path %s" % path)
  assert rc == 0
  caching_stats = stdout.strip("\n").split("\n")[-1].split()
  # Compare BYTES_NEEDED and BYTES_CACHED, the output format is as follows
  # "ID POOL REPL EXPIRY PATH BYTES_NEEDED BYTES_CACHED FILES_NEEDED FILES_CACHED"
  return len(caching_stats) > 0 and caching_stats[5] == caching_stats[6]


def get_cache_directive_for_path(path):
  rc, stdout, stderr = exec_process("hdfs cacheadmin -listDirectives -path %s" % path)
  assert rc == 0
  dirid = re.search('^\s+?(\d+)\s+?testPool\s+?.*?$', stdout, re.MULTILINE).group(1)
  return dirid

def change_cache_directive_repl_for_path(path, repl):
  """Drop the cache directive for a given path"""
  dirid = get_cache_directive_for_path(path)
  rc, stdout, stderr = exec_process(
    "hdfs cacheadmin -modifyDirective -id %s -replication %s" % (dirid, repl))
  assert rc == 0, \
      "Error modifying cache directive for path %s (%s, %s)" % (path, stdout, stderr)

def get_num_cache_requests():
  """Returns the number of outstanding cache requests"""
  rc, stdout, stderr = exec_process("hdfs cacheadmin -listDirectives -stats")
  assert rc == 0, 'Error executing hdfs cacheadmin: %s %s' % (stdout, stderr)
  return len(stdout.split('\n'))
