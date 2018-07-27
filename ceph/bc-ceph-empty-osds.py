#!/usr/bin/env python3
#
# tells you if an osd is empty (no pgs up or acting, and no weight)
# (most of the code here was copied from bc-ceph-reweight-by-utilization.py)
#
# Author: Peter Maloney
# Licensed GNU GPLv2; if you did not recieve a copy of the license, get one at http://www.gnu.org/licenses/gpl-2.0.html

import sys
import subprocess
import re
import argparse
import time
import logging
import json

#====================
# global variables
#====================

osds = {}
health = ""
json_nan_regex = None

#====================
# logging
#====================

logging.VERBOSE = 15
def log_verbose(self, message, *args, **kws):
    if self.isEnabledFor(logging.VERBOSE):
        self.log(logging.VERBOSE, message, *args, **kws)

logging.addLevelName(logging.VERBOSE, "VERBOSE")
logging.Logger.verbose = log_verbose

formatter = logging.Formatter(
    fmt='%(asctime)-15s.%(msecs)03d %(levelname)s: %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

handler = logging.StreamHandler()
handler.setFormatter(formatter)

logger = logging.getLogger("bc-ceph-reweight-by-utilization")

logger.addHandler(handler)

#====================

class JsonValueError(Exception):
    def __init__(self, cause):
        self.cause = cause
    
def ceph_health():
    p = subprocess.Popen(["ceph", "health"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        lines = out.decode("UTF-8")
        return lines
    else:
        raise Exception("ceph osd df command failed; err = %s" % str(err))

def ceph_osd_df():
    p = subprocess.Popen(["ceph", "osd", "df", "--format=json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        jsontxt = out.decode("UTF-8")
        try:
            return json.loads(jsontxt)
        except ValueError as e:
            # we expect this is because some osds are not fully added, so they have "-nan" in the output.
            # that's not valid json, so here's a quick fix without parsing properly (which is the json lib's job)
            try:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("DOING WORKAROUND. jsontxt = %s" % jsontxt)
                global json_nan_regex
                if not json_nan_regex:
                    json_nan_regex = re.compile("([^a-zA-Z0-9]+)(-nan)")
                jsontxt = json_nan_regex.sub("\\1\"-nan\"", jsontxt)
                return json.loads(jsontxt)
            except ValueError as e2:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("FAILED WORKAROUND. jsontxt = %s" % jsontxt)
                raise JsonValueError(e)
    else:
        raise Exception("ceph osd df command failed; err = %s" % str(err))


def ceph_pg_dump():
    #bc-ceph-pg-dump -a -s

    p = subprocess.Popen(["ceph", "pg", "dump", "--format=json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        try:
            return json.loads(out.decode("UTF-8"))["pg_stats"]
        except ValueError as e:
            raise JsonValueError(e)
    else:
        raise Exception("pg dump command failed; err = %s" % str(err))


class Osd:
    def __init__(self, osd_id):
        self.osd_id = osd_id
        
        # from ceph osd df
        self.weight = None
        self.reweight = None
        self.use_percent = None
        self.size = None
        self.df_var = None

        # from ceph pg dump
        self.bytes_old = None
        self.bytes_new = None
        self.pgs_old = None
        self.pgs_new = None

        self.var_old = None
        self.var_new = None
        # fudge factor to take the "new" numbers and adjust them to be closer to what ceph osd df gives you
        self.df_fudge = None

def refresh_weight():
    global osds
    
    for row in ceph_osd_df()["nodes"]:
        osd_id = row["id"]
        
        if osd_id in osds:
            osd = osds[osd_id]
        else:
            osd = Osd(osd_id)
            osds[osd_id] = osd
        
        osd.weight = row["crush_weight"]
        osd.reweight = row["reweight"]
        
        utilization = row["utilization"]
        #if utilization == "-nan":
            # TODO: handle this? (bc-ceph-reweight-by-utilization.py skips here, but we don't want to skip any here)
            
        osd.use_percent = row["utilization"]
        
        osd.size = row["kb"]*1024
        
        osd.df_var = row["var"]

def refresh_bytes():
    global osds
    
    for osd in osds.values():
        osd.bytes_old = 0
        osd.bytes_new = 0
        osd.pgs_old = 0
        osd.pgs_new = 0
        
    for row in ceph_pg_dump():
        size = row["stat_sum"]["num_bytes"]
        up = row["up"]
        acting = row["acting"]
        
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("DEBUG: size = %s, up = %s, acting = %s" % (size,up,acting))
        
        osds_old = acting
        osds_new = up

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("DEBUG: osds_old = %s, osds_new = %s" % (osds_old, osds_new))
        
        for osd_id in osds_old:
            osd_id = int(osd_id)
            if osd_id not in osds:
                continue
            osd = osds[osd_id]
            if not osd.bytes_old:
                osd.bytes_old = 0
            osd.bytes_old += size
            osd.pgs_old += 1

        for osd_id in osds_new:
            osd_id = int(osd_id)
            if osd_id not in osds:
                continue
            osd = osds[osd_id]
            if not osd.bytes_new:
                osd.bytes_new = 0
            osd.bytes_new += size
            osd.pgs_new += 1

def refresh_all():
    health = ceph_health()
    refresh_weight()
    refresh_bytes()


def print_report():
    global osds, args

    for osd in osds.values():
        osd.empty = osd.bytes_old == 0 and osd.bytes_new == 0 and osd.pgs_old == 0 and osd.pgs_new == 0 and (osd.weight == 0 or osd.reweight == 0)
    
    osds_sorted = sorted(osds.values(), key=lambda osd: getattr(osd, args.sort_by))

    print("%-6s %-7s %-8s %-7s %-14s %-7s %-14s %s" % (
        "osd_id", "weight", "reweight", "pgs_old", "bytes_old", "pgs_new", "bytes_new", "empty"))
    for osd in osds_sorted:
        
        if not ( args.all or osd.empty ):
            continue
            
        print("%6d %7.5f %8.5f %7d %14d %7d %14d %s" % 
            (osd.osd_id, osd.weight, osd.reweight, osd.pgs_old, osd.bytes_old, osd.pgs_new, osd.bytes_new, 
                osd.empty))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Reweight OSDs so they have closer to equal space used.')
    parser.add_argument('-d', '--debug', action='store_const', const=True,
                    help='enable debug level logging')
    parser.add_argument('-v', '--verbose', action='store_const', const=True, default=False,
                    help='verbose mode')
    parser.add_argument('-q', '--quiet', action='store_const', const=True, default=False,
                    help='quiet mode')

    parser.add_argument('--sort-by', action='store', default="osd_id",
                    help='specify sort column for report table (default osd_id)')
    parser.add_argument('-a', '--all', action='store_const', const=True, default=False,
                    help='list safe and unsafe to remove')
    
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
    elif args.verbose:
        logger.setLevel(logging.VERBOSE)
    elif args.quiet:
        logger.setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.INFO)

    try:
        refresh_all()
        print_report()
    except JsonValueError:
        # I'll just assume this is the ceph command's fault, and ignore it. It seems to happen when osds are going out or in.
        logger.error("got ValueError from ceph... giving up")
        
