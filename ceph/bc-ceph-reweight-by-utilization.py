#!/usr/bin/env python3
#
# Variance is calculated based on the size of pgs, not the used space in the filesystem. The values will be different than seen with ceph osd reweight-by-utilization or ceph osd df. But it means we can predict how full the OSDs will be when rebalance is done. That way you can reweight during rebalance until we know the balance will be right when rebalance is done. And it seems more stable... not having to reweight again too soon.
#
# To run it by carefully testing first:
#    # back up the old weights
#    ./bc-ceph-reweight-by-utilization.py -b "$(date +%Y-%m-%dT%H:%M).reweight"
#
#    # make it so ceph won't move data, only let you change weight and then it will peer
#    ceph osd set norecover
#
#    # Luminous and newer - if you have device classes, you must specify a class:
#    # for example:
#    #     ./bc-ceph-reweight-by-utilization.py -al --device-class "hdd"
#    # and repeat that for each class you want to reweight
#
#    # run the script until it's balanced, and then ctrl+c. This will run peering, which has (I believe very small) potential performance impact.
#    # if you have PGs that are too large, or OSDs that are too small, or some other condition, it might never finish with the default goals.
#    # To use a non-default goal, either end early, or set -o higher (default 1.03), for example -o 1.06.
#    # There is no support for multiple classes or special CRUSH rules, so if you have such a setup, add --include-osds to include only one class at a time.
#    ./bc-ceph-reweight-by-utilization.py -al
#
#    # check result by looking at the future result (var_new)
#    ./bc-ceph-reweight-by-utilization.py -R
#
#    # if it looks good... above output looks balanced (var_new is between 0.94 and 1.06 for example), and ceph -s shows not too crazy amount of data to move, then you could accept it by letting ceph recover
#    ceph osd unset norecover
#
#    # if it looks bad, or you don't want to apply it for some reason, restore old weights
#    ./bc-ceph-reweight-by-utilization.py -B 2017-10-23T10:15.reweight
#
# Licensed GNU GPLv2; if you did not recieve a copy of the license, get one at http://www.gnu.org/licenses/gpl-2.0.html

import sys
import subprocess
import re
import argparse
import time
import logging
import json
import socket

#====================
# global variables
#====================

osds = {}
pools = {}
avg_old = 0
avg_new = 0
health = ""
json_nan_regex = None
hostname = socket.gethostname()

#====================
# logging
#====================

# Log levels:
# TRACE   = 5
# DEBUG   = 10
# VERBOSE = 15
# INFO    = 20
# WARN    = 30 (aka WARNING)
# ERROR   = 40
# FATAL   = 50 (aka CRITICAL)

logging.VERBOSE = 15
def log_verbose(self, message, *args, **kws):
    if self.isEnabledFor(logging.VERBOSE):
        self.log(logging.VERBOSE, message, *args, **kws)

logging.addLevelName(logging.VERBOSE, "VERBOSE")
logging.Logger.verbose = log_verbose

logging.TRACE = 5
def log_trace(self, message, *args, **kws):
    if self.isEnabledFor(logging.TRACE):
        self.log(logging.TRACE, message, *args, **kws)

logging.addLevelName(logging.TRACE, "TRACE")
logging.Logger.trace = log_trace

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
        raise Exception("ceph health command failed; err = %s" % str(err))


def ceph_mon_dump():
    p = subprocess.Popen(["ceph", "mon", "dump", "--format=json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        jsontxt = out.decode("UTF-8")
        try:
            return json.loads(jsontxt)
        except ValueError as e:
            raise JsonValueError(e)
    else:
        raise Exception("ceph osd mon dump command failed; err = %s" % str(err))


def ceph_osd_pool_ls_detail():
    p = subprocess.Popen(["ceph", "osd", "pool", "ls", "detail", "--format=json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        jsontxt = out.decode("UTF-8")
        try:
            return json.loads(jsontxt)
        except ValueError as e:
            raise JsonValueError(e)
    else:
        raise Exception("ceph osd pool ls detail command failed; err = %s" % str(err))


# in json output, pool ls doesn't show the id, only name... so we look it up using this one
def ceph_osd_pool_stats():
    p = subprocess.Popen(["ceph", "osd", "pool", "stats", "--format=json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        jsontxt = out.decode("UTF-8")
        try:
            return json.loads(jsontxt)
        except ValueError as e:
            raise JsonValueError(e)
    else:
        raise Exception("ceph osd pool stats command failed; err = %s" % str(err))


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


def ceph_osd_reweight(osd_id, weight):
    p = subprocess.Popen(["ceph", "osd", "reweight", str(osd_id), str(weight)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        return
    else:
        raise Exception("ceph osd df command failed; err = %s" % str(err))


# weighted average, based on bytes and weight
def refresh_average():
    global osds
    global avg_old
    global avg_new
    
    total_old = 0
    total_new = 0
    count = 0
    
    for osd in osds.values():
        total_old += osd.bytes_old / osd.weight
        total_new += osd.bytes_new / osd.weight
        count += 1
    
    avg_old = total_old/count
    avg_new = total_new/count

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("avg_old = %s" % avg_old)
        logger.debug("avg_new = %s" % avg_new)


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


class Pool:
    def __init__(self, pool_id):
        self.pool_id = pool_id
        self.pool_name = None
        
        self.type = None # 1 = replicated, 3 = erasure
        self.size = None # this seems to be k+m for EC
        self.min_size = None # this seems to be k for EC

    def is_replicated(self):
        return self.type == 1
        
    def is_erasure(self):
        return self.type == 3

    # this is the number of bytes used up on an osd when the pg is there
    # for replication it's just the same as the normal number of bytes
    def get_osd_bytes(self, num_bytes):
        if self.is_erasure():
            # TODO: test: 
            # for EC, don't just add num_bytes, but num_bytes/k, and k shows up as min_size in the pool list
            return num_bytes / self.min_size
        elif self.is_replicated():
            return num_bytes
        else:
            raise Exception("Unsupported pool type: %s" % self.type)

    def __str__(self):
        
        return "pool %s %s %s %s" % (self.pool_id, type(self.pool_id), self.pool_name, self.type)

def refresh_pools():
    global pools
    pools_by_name = {}
    
    for row in ceph_osd_pool_stats():
        pool_id = row["pool_id"]
        p = Pool(pool_id)
        
        p.pool_name = row["pool_name"]
        
        pools[pool_id] = p
        pools_by_name[p.pool_name] = p
        
    for row in ceph_osd_pool_ls_detail():
        pool_name = row["pool_name"]
        
        p = pools_by_name[pool_name]
        
        p.type = int(row["type"])
        p.size = int(row["size"])
        p.min_size = int(row["min_size"])
        
    
def refresh_weight():
    global osds

    # for the safety check, to make sure that you specify --device-class if you have more than one class; this only populates if you did not use --device-class
    classes_seen = []

    for row in ceph_osd_df()["nodes"]:
        osd_id = row["id"]
        
        # limit the result to the list specified on command line
        if args.include_osds and osd_id not in args.include_osds:
            if osd_id in osds.keys():
                del osds[osd_id]
            continue
        
        # limit the result to the class specified on command line
        if args.device_class:
            if "device_class" in row.keys():
                device_class = row["device_class"]
                if device_class != args.device_class:
                    if osd_id in osds.keys():
                        del osds[osd_id]
                    continue
            else:
                raise Exception("You have used the --device-class argument, but your ceph version doesn't seem to support device classes (ceph osd df does not have a \"device_class\" field).")
        else:
            if "device_class" in row.keys():
                device_class = row["device_class"]
                if not device_class in classes_seen:
                    classes_seen += [device_class]

        if osd_id in osds:
            osd = osds[osd_id]
        else:
            osd = Osd(osd_id)
            osds[osd_id] = osd
        
        osd.weight = row["crush_weight"]
        if osd.weight == 0:
            # if weight is zero, it won't ever peer and get pgs, so we can ignore it
            del osds[osd_id]
            continue
        
        osd.reweight = row["reweight"]
        
        utilization = row["utilization"]
        if utilization == "-nan":
            # if utilization is -nan, it isn't really added to crush properly, so it can't reweight, so ignore it
            del osds[osd_id]
            continue
            
        osd.use_percent = row["utilization"]
        
        osd.size = row["kb"]*1024
        if osd.size == 0:
            # if size is zero, it won't ever get pgs, so we can ignore it
            del osds[osd_id]
            continue
        
        osd.df_var = row["var"]

    if len(osds) == 0:
        raise Exception("No osds were selected. Check your --include-osds and --device-class arguments, and ceph df output to see if any are valid.")
    if len(classes_seen) > 1:
        raise Exception("You have multiple device classes, but you did not specify one.")


def refresh_bytes():
    global osds
    
    for osd in osds.values():
        osd.bytes_old = 0
        osd.bytes_new = 0
        osd.pgs_old = 0
        osd.pgs_new = 0
        
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("DEBUG: pools:")
        for p in pools:
            logger.debug("DEBUG: %s" % pools[p])
            
    for row in ceph_pg_dump():
        pgid = row["pgid"]
        pool_id = int(pgid.split(".")[0])
        pool = pools[pool_id]
        
        num_bytes = row["stat_sum"]["num_bytes"]
        up = row["up"]
        acting = row["acting"]
        
        if logger.isEnabledFor(logging.TRACE):
            logger.trace("num_bytes = %s, up = %s, acting = %s" % (num_bytes,up,acting))
        
        osds_old = acting
        osds_new = up

        if logger.isEnabledFor(logging.TRACE):
            logger.trace("osds_old = %s, osds_new = %s" % (osds_old, osds_new))
        
        for osd_id in osds_old:
            osd_id = int(osd_id)
            if osd_id not in osds:
                continue
            osd = osds[osd_id]
            if not osd.bytes_old:
                osd.bytes_old = 0

            osd.bytes_old += pool.get_osd_bytes(num_bytes)

            osd.pgs_old += 1

        for osd_id in osds_new:
            osd_id = int(osd_id)
            if osd_id not in osds:
                continue
            osd = osds[osd_id]
            if not osd.bytes_new:
                osd.bytes_new = 0

            if pool.is_erasure():
                # TODO: test: 
                osd.bytes_new += num_bytes / pool.min_size
            elif pool.is_replicated():
                osd.bytes_new += num_bytes
            else:
                raise Exception("Unsupported pool type: %s" % pool.type)

            osd.pgs_new += 1


class WaitForHealthException(Exception):
    pass

def refresh_var():
    global osds
    global avg_old
    global avg_new
    
    for osd in osds.values():
        osd.var_old = osd.bytes_old / osd.weight / avg_old
        osd.var_new = osd.bytes_new / osd.weight / avg_new
        
        if args.fudge and osd.df_fudge is None:
            if "remapped" in health or "misplaced" in health or "degraded" in health or "peering" in health:
                raise WaitForHealthException()
            
            # adding the fudge factor to try to match `ceph osd df` but also allow predicting post recovery size
            myuse = osd.bytes_old/osd.size*100
            if myuse != 0:
                osd.df_fudge = osd.use_percent / myuse
            else:
                osd.df_fudge = 1
            
            osd.var_old *= osd.df_fudge
            osd.var_new *= osd.df_fudge


def refresh_all():
    health = ceph_health()
    refresh_pools()
    refresh_weight()
    refresh_bytes()
    refresh_average()
    refresh_var()


def print_report():
    global osds, args
    
    osds_sorted = sorted(osds.values(), key=lambda osd: getattr(osd, args.sort_by))
    
    # top 10 and low 10 osds
    osds_filtered = []
    if args.report_short and len(osds_sorted) > 10:
        osds_filtered += osds_sorted[0:10]
        osds_filtered += osds_sorted[-10:]
    else:
        osds_filtered = osds_sorted
    
    if args.verbose:
        # all osds and columns
        print("%-6s %-7s %-8s %-7s %-14s %-7s %-7s %-14s %-7s %-8s" % (
            "osd_id", "weight", "reweight", "pgs_old", "bytes_old", "var_old", "pgs_new", "bytes_new", "var_new", "improvement"))
        for osd in osds_filtered:
            bytes_old = osd.bytes_old / args.block_size
            bytes_new = osd.bytes_new / args.block_size
            
            # improvement is how much closer to 1 the var_new is compared to var_old... so negative means we are moving data unnecessarily, and positive means we move it because it balances better
            if osd.var_old < 1:
                improvement = (1 - osd.var_old) - (osd.var_new - 1)
            else:
                improvement = (osd.var_old - 1) - (1 - osd.var_new)
                
            print("%6d %7.5f %8.5f %7d %14d %7.5f %7d %14d %7.5f %8.5f" % 
                (osd.osd_id, osd.weight, osd.reweight, osd.pgs_old, bytes_old, osd.var_old, osd.pgs_new, bytes_new, osd.var_new, improvement))
    else:
        print("%-6s %-7s %-8s %-14s %-7s %-14s %-7s" % (
            "osd_id", "weight", "reweight", "bytes_old", "var_old", "bytes_new", "var_new"))

        for osd in osds_filtered:
            bytes_old = osd.bytes_old / args.block_size
            bytes_new = osd.bytes_new / args.block_size
            print("%6d %7.5f %8.5f %14d %7.5f %14d %7.5f" % 
                (osd.osd_id, osd.weight, osd.reweight, bytes_old, osd.var_old, bytes_new, osd.var_new))
        


def get_increment(var):
    if var < 0.85 or var > 1.15:
        return args.step
    
    # relatively how far between 0.85 or 1.15 and 1 are we
    p = abs(1 - var) / 0.15
    
    # sharply lower step relative to p
    return p**2 * args.step


def adjust():
    lowest = None
    highest = None
    reweight_1_count = 0
    
    for osd in osds.values():
        if osd.reweight == 1:
            reweight_1_count += 1
        if lowest is None or osd.var_new < lowest.var_new:
            lowest = osd
        if highest is None or osd.var_new > highest.var_new:
            highest = osd
    
    spread = highest.var_new
    max_spread = args.oload
    
    txt = "lowest osd_id = %s, var = %.5f" % (lowest.osd_id, lowest.var_new)
    txt += ", highest osd_id = %s, var = %.5f" % (highest.osd_id, highest.var_new)
    txt += ", oload = %.5f" % (args.oload)
    logger.info(txt)

    adjustment_made = False
    
    # difference from 1 so we can choose only the worst of the 2, which possibly prevents very low var osds from flapping to/from high to low because of another worse osd needing reweight
    lowest_d = 1 - lowest.var_new
    highest_d = highest.var_new - 1
    
    # We want to keep at least one at reweight=1, so when the reweight=1 osd is the highest (or we have none at 1), we raise the lowest instead. 
    # That way, eventually another osd gets raised to reweight=1, and the other that was 1 before can be lowered.
    if highest.reweight == 1 and reweight_1_count == 1 or reweight_1_count == 0:
        choose_lowest = True
    else:
        # We reweight the lowest when it is further from the middle.
        choose_lowest = lowest_d >= highest_d and lowest.reweight < 1
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("choose_lowest = %s, spread = %s, max_spread = %s" % (choose_lowest, spread, max_spread))
    
    if choose_lowest and spread > max_spread:
        increment = get_increment(lowest.var_new)
        new = round(round(lowest.reweight,4) + increment, 5)
        if new > 1:
            new = 1
        logger.info("Doing reweight: osd_id = %s, reweight = %s -> %s" % (lowest.osd_id, lowest.reweight, new))
        if not args.dry_run:
            ceph_osd_reweight(lowest.osd_id, new)
        adjustment_made = True
    else:
        logger.verbose("Skipping reweight: osd_id = %s, reweight = %s" % (lowest.osd_id, lowest.reweight))
        
    if not choose_lowest and spread > max_spread:
        increment = get_increment(highest.var_new)
        new = round(round(highest.reweight,4) - increment, 5)
        logger.info("Doing reweight: osd_id = %s, reweight = %s -> %s" % (highest.osd_id, highest.reweight, new))
        if not args.dry_run:
            ceph_osd_reweight(highest.osd_id, new)
        adjustment_made = True
    else:
        logger.verbose("Skipping reweight: osd_id = %s, reweight = %s" % (highest.osd_id, highest.reweight))
    
    return adjustment_made


def write_backup_file(f):
    for osd in osds.values():
        f.write("%s %s\n" % (osd.osd_id, osd.reweight))


def restore_backup_file(f):
    while True:
        line = f.readline()
        if not line:
            break
        osd_id, reweight = line.split()
        osd_id = int(osd_id)
        reweight = float(reweight)

        if osd_id not in osds:
            logger.info("osd not found: osd_id = %s" % osd_id)
            continue
        if osds[osd_id].reweight == reweight:
            if logger.isEnabledFor(logging.VERBOSE):
                logger.verbose("osd weight is the same: osd_id = %s" % osd_id)
            continue
        logger.info("Doing reweight: osd_id = %s, reweight = %s -> %s" % (osd_id, osds[osd_id].reweight, reweight))

        if not args.dry_run:
            ceph_osd_reweight(osd_id, reweight)


def write_backup():
    global args

    if args.backup == "-":
        write_backup_file(sys.stdout)
    else:
        with open(args.backup, "w") as f:
            write_backup_file(f)

def restore_backup():
    global args

    if args.restore == "-":
        restore_backup_file(sys.stdin)
    else:
        with open(args.restore, "r") as f:
            restore_backup_file(f)


myrank = None
# return True if the machine where this is run is the first mon listed in quorum
def is_leader():
    global myrank
    
    dump = ceph_mon_dump()
    mons = dump["mons"]
    quorum = dump["quorum"]
    leader = quorum[0]
    
    if myrank == None:
        for mon in mons:
            name = mon["name"]
            rank = mon["rank"]
            if name == hostname:
                myrank = rank
                break
    
    if myrank == leader:
        return True
    
    return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Reweight OSDs so they have closer to equal space used.')
    parser.add_argument('-d', '--debug', action='store_const', const=True,
                    help='enable debug level logging')
    parser.add_argument('-v', '--verbose', action='store_const', const=True, default=False,
                    help='verbose mode')
    parser.add_argument('-q', '--quiet', action='store_const', const=True, default=False,
                    help='quiet mode')
    parser.add_argument('-F', '--fudge', action='store_const', const=True, default=False,
                    help='Compare to ceph osd df to calculate a fudge factor to use when calculating var. This is useful for looking at the report and comparing to ceph osd df, but probably not a good idea to use along with -a.')

    parser.add_argument('-r', '--report', action='store_const', const=True, default=False,
                    help='print report table')
    parser.add_argument('--sort-by', action='store', default="var_new",
                    help='specify sort column for report table (default var_new)')
    parser.add_argument('-R', '--report-short', action='store_const', const=True, default=False,
                    help='print short report table with max 10 low and high osds')
    parser.add_argument('--block-size', action='store', default=1, type=str,
                    help='scale sizes by SIZE (default 1) before printing them, eg. --block-size=1MB or --block-size=1000000 would print it in megabytes')
    parser.add_argument('--include-osds', action='store', default=None, type=str,
                    help='optional comma separated list of osds to work with, default is equivalent to all non-0 weight osds (report, calculations, adjustment, backup, restore)')
    parser.add_argument('--device-class', action='store', default=None, type=str,
                    help='optional device class to work with')
    
    parser.add_argument('-a', '--adjust', action='store_const', const=True, default=False,
                    help='adjust the reweight (default is report only)')
    parser.add_argument('-n', '--dry-run', action='store_const', const=True, default=False,
                    help='if combined with --adjust, go through all the adjustment code but don\'t actually adjust')
    
    parser.add_argument('-b', '--backup', action='store', default=None,
                    help='write reweights to a file (or - for stdout) before other actions')
    parser.add_argument('-B', '--restore', action='store', default=None,
                    help='restore reweights from a file (or - for stdin), after backup, and before other actions')
    
    parser.add_argument('-o', '--oload', default=1.03, action='store', type=float,
                    help='minimum var before reweight (default 1.03)')
    parser.add_argument('-s', '--step', default=0.03, action='store', type=float,
                    help='max step size for each reweight iteration. the value is scaled down when 0.85<var<1.15 (default 0.03)')

    parser.add_argument('-l', '--loop', action='store_const', const=True, default=False,
                    help='Repeat the reweight process forever.')
    parser.add_argument('--sleep', action='store', default=60, type=float,
                    help='Seconds to sleep between loops (default 60)')
    parser.add_argument('--sleep-short', action='store', default=1, type=float,
                    help='Seconds to sleep between loops that do adjustments (default 1)')
    parser.add_argument('-c', '--cluster', action='store_const', const=True, default=False,
                    help='Look at the mons in the ceph cluster, and if this machine is the leading mon, the loop runs as normal, and if not, it just sleeps. This is used to run the script at the same time on many machines so it\'s HA.')
    
    args = parser.parse_args()

    if args.oload <= 1:
        logger.error("oload must be greater than 1")
        exit(1)

    if not args.report and not args.report_short and not args.adjust and not args.backup and not args.restore:
        logger.error("Either report, adjust, backup or restore must be set")
        exit(1)
    
    if args.report_short:
        args.report = True
        
    if args.debug:
        logger.setLevel(logging.DEBUG)
    elif args.verbose:
        logger.setLevel(logging.VERBOSE)
    elif args.quiet:
        logger.setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.INFO)

    try:
        args.block_size = int(args.block_size)
    except:
        r = re.compile("^([0-9]+)([^0-9]*)$")
        m = r.match(args.block_size)
        magnitude = m.group(1)
        unit = m.group(2)
        
        if unit == "kB":
            args.block_size = int(args.block_size.replace("kB","", 1)) * 1000
        elif unit == "MB":
            args.block_size = int(args.block_size.replace("MB","", 1)) * 1000000
        elif unit == "GB":
            args.block_size = int(args.block_size.replace("GB","", 1)) * 1000000000
        elif unit == "KiB":
            args.block_size = int(args.block_size.replace("KiB","", 1)) * 1024
        elif unit == "MiB":
            args.block_size = int(args.block_size.replace("MiB","", 1)) * 1048576
        elif unit == "GiB":
            args.block_size = int(args.block_size.replace("GiB","", 1)) * 1073741824
        elif unit == "B":
            args.block_size = int(args.block_size.replace("B","", 1))
    int(args.block_size)
    
    if args.include_osds == "":
        raise Exception("invalid arguments: --include-osds \"%s\"" % args.include_osds)
    if args.include_osds:
        tmp = map(lambda x: int(x), args.include_osds.replace("[ ]?,[ ]?", "").split(","))
        args.include_osds = []
        args.include_osds += tmp

    did_backup = False
    
    while True:
        if args.cluster:
            if not is_leader():
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("This node is not the leader... sleeping.")
                time.sleep(args.sleep)
                continue
            elif logger.isEnabledFor(logging.DEBUG):
                logger.debug("This node is the leader... running loop.")
            
        try:
            refresh_all()
        except WaitForHealthException:
            logger.info("fudge is enabled; need to wait for no pgs/objects are remapped, misplaced or degraded")
            time.sleep(args.sleep)
            continue
        except JsonValueError:
            # I'll just assume this is the ceph command's fault, and ignore it. It seems to happen when osds are going out or in.
            logger.warning("got ValueError from ceph... sleeping 5s and will retry")
            time.sleep(5)
            continue
        
        if not did_backup:
            if args.backup:
                write_backup()

            if args.restore:
                restore_backup()

            did_backup = True

        if args.report:
            print_report()

        do_short_sleep = False
        if args.adjust:
            # our "new" bytes and variance numbers will only be right after peering is done, so don't run until then
            if "peering" in health:
                logger.info("refusing to reweight during peering. Try again later.")
                while "peering" in ceph_health():
                    time.sleep(1)
                continue
            else:
                do_short_sleep = adjust()

        if not args.loop:
            break
        
        if do_short_sleep:
            time.sleep(args.sleep_short)
        else:
            time.sleep(args.sleep)
            
        if args.report:
            print()
