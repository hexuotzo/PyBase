import zk.client as zk
import region.client as region
from pb.Client_pb2 import GetRequest, MutateRequest, ScanRequest
from helpers.helpers import families_to_columns, values_to_column_values
import region.region_info as region_info
import sys
import logging
import logging.config
from intervaltree import Interval, IntervalTree
from collections import defaultdict
from itertools import chain
from threading import Lock

# Using a tiered logger such that all submodules propagate through to this
# logger. Changing the logging level here should affect all other modules.
logger = logging.getLogger('pybase')
logging.config.dictConfig({
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
        }
    },
    'handlers': {
        'default': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'standard'
        }
    },
    'loggers': {
        '': {
            'handlers': ['default'],
            'level': 'INFO',
            'propagate': True
        }
    }
})


# Table + Family used when requesting meta information from the
# MetaRegionServer
metaTableName = "hbase:meta,,1"
metaInfoFamily = {"info": []}


# This class represents the main Client that will be created by the user.
# All HBase interaction goes through this client.
class MainClient:
    # So far we only need to maintain two class variables:
    #   - zkquorum represents the location of ZooKeeper
    #   - meta_client is a client that maintains a persistent connection
    # to the meta regionserver used to discover regions when we get a cache
    # miss on our region cache.

    def __init__(self, zkquorum, client):
        self.zkquorum = zkquorum
        # We need a persistent connection to the meta_client in order to
        # perform any meta lookups in the case that we get a cache miss.
        self.meta_client = client
        # Cache 1
        # IntervalTree data structure that allows me to create ranges
        # representing known row keys that fall within a specific region. Any
        # 'region look up' is then O(logn)
        self.region_inf_cache = IntervalTree()
        # Cache 2
        # Simple dictionary that takes a known region and maps it to our Client
        # instance that serves that region (clients serve entire RegionServers,
        # each of which will serve several regions)
        self.region_client_cache = {}
        # Cache 2.5
        # Opposite of cache 2. Takes a client's host:port as key and maps
        # it to a tuple (instance of client, a list of regions it serves).
        # This allows us to 1) When we discover a new region check if we've
        # already created a Client for that RegionServer and if so, use that
        # one (otherwise create a new). 2) Keep track of how many known regions
        # a given Client serves. If we're purging stale region information and
        # discover a Client no longer tracks any legitimate regions we can
        # close the client and clear up resources.
        self.reverse_region_client_cache = defaultdict(lambda: (None, []))
        # Mutex used for all caching operations.
        self._cache_lock = Lock()

    def _find_hosting_region_client(self, table, key, return_stop=False):
        # We can probably refactor 'return_stop' out as it's only used for Scan
        # RPCs (we need to know a region's stopping key to know where to start
        # scanning next). Leaving the code smell for now.
        meta_key = self._construct_meta_key(table, key)
        region_inf = self._get_from_meta_key_to_region_inf_cache(meta_key)
        if region_inf is None:
            # We couldn't find the region in our cache.
            logger.info('Region cache miss! Table: %s, Key: %s', table, key)
            return self._discover_region(meta_key, return_stop)
        region_name = region_inf.region_name
        region_client = self._get_from_region_name_to_region_client_cache(
            region_name)
        if region_client is None:
            logger.warn('Client cache miss! region_name: %s', region_name)
            # This should very rarely happen. When a region is discovered it's paired with
            # a RegionClient instance. If we have knowledge of the region but
            # not the RegionClient hosting it then that's pretty bad.
            #
            # TODO: We need to handle this properly but for now just
            # re-discover the whole region.
            return self._discover_region(meta_key, return_stop)
        if return_stop:
            return region_client, region_name, region_inf.stop_key
        return region_client, region_name

    # Constructs the string used to query the MetaClient
    def _construct_meta_key(self, table, key):
        return table + "," + key + ",:"

    # This function takes a meta_key and queries the MetaClient for the
    # RegionServer hosting that region.
    def _discover_region(self, meta_key, return_stop):
        rq = GetRequest()
        rq.get.row = meta_key
        rq.get.column.extend(families_to_columns(metaInfoFamily))
        rq.get.closest_row_before = True
        rq.region.type = 1
        rq.region.value = metaTableName
        rsp = self.meta_client._send_rpc(rq, "Get")
        client, region_inf = self._create_new_region(rsp)
        if return_stop:
            return client, region_inf.region_name, region_inf.stop_key
        return client, region_inf.region_name

    # This function takes the result of a MetaQuery, parses it, creates a new
    # RegionClient if necessary then insert into both caches.
    def _create_new_region(self, rsp):
        # Response comes down in different cells, each cell giving different
        # information. Iterate over them and pull what we need.
        for cell in rsp.result.cell:
            if cell.qualifier == "regioninfo":
                region_inf = region_info.region_info_from_cell(cell)
            elif cell.qualifier == "server":
                value = cell.value.split(':')
                host = value[0]
                port = int(value[1])
            else:
                continue
        # Check if we already have a client serving this RegionServer
        client = self.reverse_region_client_cache[host + ":" + str(port)][0]
        if client is None:
            client = region.NewClient(host, port)
        # Add to the caches!
        self._add_to_region_name_to_region_client_cache(
            region_inf.region_name, client)
        self._add_to_meta_key_to_region_inf_cache(region_inf)
        return client, region_inf

    def _add_to_meta_key_to_region_inf_cache(self, region_inf):
        stop_key = region_inf.stop_key
        if stop_key == '':
            # This is hacky but our interval tree requires hard interval stops.
            # So what's the largest char out there? chr(255) -> '\xff'. If
            # you're using '\xff' as a prefix for your rows then this'll cause
            # a cache miss on every request.
            stop_key = '\xff'
        start_key = region_inf.table + ',' + region_inf.start_key
        stop_key = region_inf.table + ',' + stop_key
        # We remove any intervals that overlap with our new range (they're
        # stale data and will cause a HBase region not served error as there
        # was a split). Before we remove them we want to grab them so we can
        # remove stale data from the other cache.
        self._cache_lock.acquire()
        old_regions = self.region_inf_cache[start_key:stop_key]
        self._purge_old_region_clients(old_regions)
        self.region_inf_cache.remove_overlap(start_key, stop_key)
        self.region_inf_cache[start_key:stop_key] = region_inf
        self._cache_lock.release()

    def _get_from_meta_key_to_region_inf_cache(self, meta_key):
        self._cache_lock.acquire()
        # We don't care about the last two characters ',:' in the meta_key.
        meta_key = meta_key[:-2]
        regions = self.region_inf_cache[meta_key]
        self._cache_lock.release()
        if len(regions) == 0:
            return None
        return regions.pop().data

    def _add_to_region_name_to_region_client_cache(self, region_name, region_client):
        self.region_client_cache[region_name] = region_client
        key = region_client.host = ":" + str(region_client.port)
        # Annoying but need to do the below and tuples are immutable -
        #       "host:port": (None, [])
        #        -->
        #       "host:port": (client_instance, [region1])
        self.reverse_region_client_cache[key][1].append(region_name)
        self.reverse_region_client_cache[key] = (
            region_client, self.reverse_region_client_cache[key][1])

    def _get_from_region_name_to_region_client_cache(self, region_name):
        self._cache_lock.acquire()
        to_return = None
        if region_name in self.region_client_cache:
            to_return = self.region_client_cache[region_name]
        self._cache_lock.release()
        return to_return

    def _purge_old_region_clients(self, old):
        for interval in old:
            region_name = interval.data.region_name
            client = self.region_client_cache.pop(region_name, None)
            if client is not None:
                key = client.host + ":" + client.port
                try:
                    # Be aware that .remove is a O(n) operation where n =
                    # number of regions in a RegionServer. Not ideal but this
                    # function should be pretty rare and only really called
                    # during splits.
                    self.reverse_region_client_cache[
                        key][1].remove(region_name)
                except KeyError, ValueError:
                    pass
                if len(self.reverse_region_client_cache[key][1]) == 0:
                    client.close()
                    self.reverse_region_client_cache.pop(key, None)

    def get(self, table, key, families={}, filters=None):
        # Step 1. Figure out where to send it.
        region_client, region_name = self._find_hosting_region_client(
            table, key)

        # Step 2. Build the appropriate pb message.
        rq = GetRequest()
        rq.get.row = key
        rq.get.column.extend(families_to_columns(families))
        rq.region.type = 1
        rq.region.value = region_name

        # Step 3. Send the message and twiddle our thumbs
        response = region_client._send_rpc(rq, "Get")

        # Step 4. Profit
        return response.result.cell

    def scan(self, table, start_key=None, stop_key=None, families={}, filters=None):
        # Using chain.from_iterable because it's the fastest way to flatten a
        # list of lists. We have a list of lists because...
        #       [
        #               [ Cell, Cell, Cell ],     # results from row0
        #               [ Cell ]                  # results from row1
        #       ]
        # We may or may not want to return a flattened output. Still undecided.
        return list(chain.from_iterable(
            self._scan_helper(
                table, start_key, stop_key, families, filters, None)
        ))

    def _scan_helper(self, table, start_key, stop_key, families, filters, scanner_id):
        cells_to_return = []
        # Create the initial scanner for this region at the specified
        # start_key.
        region_client, rq, region_stop_key = self._scan_build_object(
            table, start_key, stop_key, families, filters, None, False)
        response = region_client._send_rpc(rq, "Scan")
        cells_to_return.extend([result.cell for result in response.results])
        # Response returns a boolean 'more_results_in_region' which indicates
        # exactly what you'd think. If it's true then we need to send another
        # query to the same region with a 'scanner_id' that we got back from
        # the response. We don't need to include all the other data as that's
        # persisted across scanner ids.
        while response.more_results_in_region:
            # Keep scanning using the scanner_id and append cells until no more
            # results in region
            region_client, rq, region_stop_key = self._scan_build_object(
                table, start_key, None, None, None, response.scanner_id, False)
            response = region_client._send_rpc(rq, "Scan")
            cells_to_return.extend(
                [result.cell for result in response.results])

        # This region's done. Close the region's scanner.
        region_client, rq, region_stop_key = self._scan_build_object(
            table, start_key, None, None, None, response.scanner_id, True)
        response = region_client._send_rpc(rq, "Scan")

        # Should we move on to the next region?
        if region_stop_key == '' or (stop_key is not None and region_stop_key > stop_key):
            return cells_to_return

        # We should move on!
        # Recursively keep scanning the next region.
        # WARNING - Maximum recursion depth is 998. If we're scanning more than
        # 998 regions then we'll get a stack overflow.
        # TODO: Don't use recursion.
        return cells_to_return.extend(
            self._scan_helper(
                table, region_stop_key, stop_key, families, filters, None)
        )

    def _scan_build_object(self, table, start_key, stop_key, families, filters, scanner_id, close):
        region_client, region_name, region_stop_key = self._find_hosting_region_client(
            table, start_key or '', return_stop=True)
        rq = ScanRequest()
        rq.region.type = 1
        rq.region.value = region_name
        # TODO: configurable. Stole 128 from asynchbase
        rq.number_of_rows = 128
        if close:
            rq.close_scanner = close
        if scanner_id is not None:
            rq.scanner_id = scanner_id
            # Don't need to include the other data if we have a scanner_id set.
            return region_client, rq, region_stop_key
        rq.scan.column.extend(families_to_columns(families))
        rq.scan.start_row = start_key or ''
        if stop_key is not None:
            rq.scan.stop_row = stop_key
        if filters is not None:
            rq.scan.filter = filters
        return region_client, rq, region_stop_key

    # All mutate requests (PUT/DELETE/APP/INC) require a values field that looks like:
    #
    #   {
    #      "cf1": {
    #           "mycol": "hodor",
    #           "mycol2": "alsohodor"
    #      },
    #      "cf2": {
    #           "mycolumn7": 24
    #      }
    #   }
    #
    def put(self, table, key, values):
        # Step 1
        region_client, region_name = self._find_hosting_region_client(
            table, key)

        # Step 2
        rq = MutateRequest()
        rq.region.type = 1
        rq.region.value = region_name
        rq.mutation.row = key
        rq.mutation.mutate_type = 2
        rq.mutation.column_value.extend(values_to_column_values(values))

        # Step 3
        response = region_client._send_rpc(rq, "Mutate")

        # Step 4
        # Do we need to return anything?

    def delete(self, table, key, values):
        # Step 1
        region_client, region_name = self._find_hosting_region_client(
            table, key)

        # Step 2
        rq = MutateRequest()
        rq.region.type = 1
        rq.region.value = region_name
        rq.mutation.row = key
        rq.mutation.mutate_type = 3
        rq.mutation.column_value.extend(
            values_to_column_values(values, delete=True))

        # Step 3
        response = region_client._send_rpc(rq, "Mutate")

        # Step 4
        # Do we need to return anything?

    def app(self, table, key, values):
        # Step 1
        region_client, region_name = self._find_hosting_region_client(
            table, key)

        # Step 2
        rq = MutateRequest()
        rq.region.type = 1
        rq.region.value = region_name
        rq.mutation.row = key
        rq.mutation.mutate_type = 0
        rq.mutation.column_value.extend(values_to_column_values(values))

        # Step 3
        response = region_client._send_rpc(rq, "Mutate")

        # Step 4
        # Do we need to return anything?

    def inc(self, table, key, values):
        # Step 1
        region_client, region_name = self._find_hosting_region_client(
            table, key)

        # Step 2
        rq = MutateRequest()
        rq.region.type = 1
        rq.region.value = region_name
        rq.mutation.row = key
        rq.mutation.mutate_type = 1
        rq.mutation.column_value.extend(values_to_column_values(values))

        # Step 3
        response = region_client._send_rpc(rq, "Mutate")

        # Step 4
        # Do we need to return anything?


# Entrypoint into the whole system. Given a string representing the
# location of ZooKeeper this function will ask ZK for the location of the
# meta table and create the region client responsible for future meta
# lookups (metaclient). Returns an instance of MainClient
def NewClient(zkquorum):
    ip, port = zk.LocateMeta(zkquorum)
    meta_client = region.NewClient(ip, port)
    return MainClient(zkquorum, meta_client)

