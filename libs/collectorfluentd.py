#!/usr/bin/env python
# -*- coding:utf-8 -*-

#*********************************************************#
# @@ScriptName: collectorfluentd.py
# @@Author: Fang.Li<surivlee@gmail.com>
# @@Create Date: 2013-12-05 14:21:57
# @@Modify Date: 2014-03-13 17:53:02
# @@Function:
#*********************************************************#


import os
import time
import signal
import glob
import random
import string
import pickle
import msgpack_pure
import daemonize
from shelljob import proc
from common import log
from dataparser import Dataparser


class CollectorFluentd(object):
    """The main collector-fluentd class.

    There are 3 steps in a full running circle:

        1. getPluginsOutput, this function executes all plugins and gets
        there outputs lines as a list object. each item indicates a metric.

        2. write2Cache(outputs), write all metrics to local FS first.
        The content in cache files have already been msgpack encoded.

        3. sendAllCache, no param required.
        sendAllCache will search all cached files in cache folder, and send them
        to the remote fluentd server in order.
    """

    def __init__(self, conf):
        self.conf = conf
        self.data_parser = Dataparser(conf)
        log("Collector-fluentd daemon started, PID: %i" % daemonize.getPid())

    def _executePlugins(self, files):

        if not files: return []

        procs = []
        outputs = []

        # Running plugins parallel
        g = proc.Group()
        for f in files:
            procs.append(g.run(f))
            time.sleep(0.05)

        # Get lines
        t0 = time.time()
        while time.time() - t0 <= self.conf.plugin_timeout:
            if g.is_pending():
                _lines = g.readlines(max_lines=1)
                if _lines and _lines[0][1].strip():
                    outputs.append(_lines[0][1].strip())
            else:
                break

        # Clean up
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except:
                pass
        g.get_exit_codes()
        g.clear_finished()

        return outputs

    def getPluginsOutput(self):
        log("Collecting metrics from plugins...", -1)
        log("Current plugin path: %s" % self.conf.plugin_path, -1)

        plugin_list = glob.glob(self.conf.plugin_path)
        plugin_list = [
            x for x in plugin_list if os.access(x, os.X_OK) and os.path.isfile(x)
        ]

        log("Found %i valid plugins: %s" % (
            len(plugin_list), str([os.path.basename(f) for f in plugin_list])), -1)
        
        log("Executing plugins...", -1)
        outputs = self._executePlugins(plugin_list)

        for output in outputs:
            log("Get valid plugin output: %s" % output.strip(), -1)

        return outputs

    def _getValidMetric(self, metric, prefix, addition={}):
        m = metric.split()

        if len(m) < 3:
            return False

        if not m[1].isdigit():
            return False

        try:
            v = float(m[2])
            if v == int(v):
                v = int(v)
        except:
            return False

        cf_datatype = "gauge"

        tags = {}
        for t in m[3:]:
            _t = t.split("=")
            if len(_t) != 2:
                return False
            if not _t[0].strip():
                return False
            if not _t[1].strip():
                return False
            if _t[0] == "cf_datatype":
                cf_datatype = _t[1].lower()
            else:
                tags[_t[0].strip()] = _t[1].strip()

        tags["_value"] = v

        # Like RRD database, the CF datatype could be one of GAUGE, COUNTER and DERIVE

        if addition:
            for k in addition.keys():
                tags[k] = addition[k]

        pack = [prefix + m[0], int(m[1]), tags]
        if cf_datatype == "gauge":
            pack = self.data_parser.gauge(pack)
        elif cf_datatype == "counter":
            pack = self.data_parser.counter(pack)
        elif cf_datatype == "derive":
            pack = self.data_parser.derive(pack)
        else:
            return False

        if pack == None:
            log("Metric %s does not have a valid history data, waiting for next circle..." % m[0], 1)
            return None
        else:
            log("Write validated metric %s to local FS..." % m[0], -1)
            return msgpack_pure.packs(pack)

    def write2Cache(self, outputs):
        fname = "".join((
            self.conf.cache_path, "/",
            self.conf.cache_file_prefix,
            str(int(time.time())),
            "_",
            ''.join(random.sample(string.ascii_letters + string.digits, 8)),
            ".dat",
        ))
        log("Writting current metrics to local FS...", -1)
        log("Open cache file %s" % fname, -1)

        valid_outputs = []
        for m in outputs:
            metric = self._getValidMetric(m, self.conf.metric_prefix, self.conf.tags)
            if metric:
                valid_outputs.append(metric)
            elif metric == False:
                log("Invalid metric string: %s (IGNORED)" % m, 1)
        if valid_outputs:
            fcache = open(fname, "wb")
            pickle.dump(valid_outputs, fcache)
            fcache.close()
        else:
            log("No new metrics generated, ignore.", 0)

    def logError(self, metric):
        err_msg = [" ".join((
            metric,
            str(int(time.time())),
            "1"
        ))]
        self.write2Cache(err_msg)


    def _getCachedMsg(self):

        fname = "".join((
            self.conf.cache_path, "/",
            self.conf.cache_file_prefix,
            "*.dat",
        ))
        cache_list = glob.glob(fname)
        cache_list.sort()
        if cache_list:
            return cache_list[0], pickle.load(open(cache_list[0], "rb"))
        else:
            return None, None

    def sendAllCache(self, sock):
        log("Sending all cached message to remote server...", -1)
        while True:
            fname, msg = self._getCachedMsg()
            if fname:

                log("Sending cache file %s to server..." % os.path.basename(fname), -1)

                for _msg in msg:
                    if not sock.send(_msg):
                        self.logError("collector.error.send")
                        log("Could not connect to the fluentd server, metrics will be sent next time.", 2)
                        return False

                log("Successful sent metrics to server in cache file %s" % os.path.basename(fname), -1)
                os.remove(fname)
            else:
                return True
