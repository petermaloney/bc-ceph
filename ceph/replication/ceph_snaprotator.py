#!/usr/bin/env python3
#
# rotates snapshots to keep only a certain number of daily,weekly,monthly ones.
# the algorithm keeps the oldest snapshot in each period
#
# TODO: make it save a day's worth of hourly snapshots, and an hour's worth of 20min snapshots

import datetime
import subprocess
import json
import argparse
import fcntl
import os
import collections
import traceback

from dateutil.relativedelta import relativedelta


def log_debug(message):
    if args.debug:
        print("DEBUG: %s" % message)


def log_verbose(message):
    if args.verbose:
        print("VERBOSE: %s" % message)


def log_info(message):
    print("INFO: %s" % message)


def log_error(message):
    print("ERROR: %s" % message)


def read_file(fileobj):
    ret = ""
    for line in fileobj:
        if type(line) != str:
            line = line.decode("utf-8")
        ret += line

    return ret


# Returns a list of snapshot names
#
# TODO: for now we just support absolute path so we know it's a local dir rather than rbd path with ceph client... support more later maybe.
def get_snaps(image_path):
    if image_path[0:1] == "/":
        # file/directory storage
        
        ret = []
        for snap in sorted(os.listdir(image_path)):
            if ".tmp" in snap:
                continue
            ret += [snap]
        
        return ret
    else:
        args = ["rbd", "snap", "ls", image_path, "--format", "json"]

        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.wait()
        if( p.returncode == 0 ):
            o = json.loads( read_file(p.stdout) )

            names=[]
            for obj in o:
                names += [obj["name"]]

            return sorted(names)

        raise Exception("Failed to list snaps of \"%s\":\n%s" % (image_path, read_file(p.stderr)))

# just list all files, and then iterate until the snap_name is found, and return next one
def get_next_snap(image_path, snap_name):
    found = False
    for n in sorted(os.listdir(image_path)):
        #log_debug("        looking at \"%s\" and \"%s\"" % (n,snap_name))
        if ".tmp" in n:
            continue
        if found:
            return n
        if n == snap_name:
            found = True
    
# for rbd, this actually destroys snaps
# (obsolete: for files, this uses the rbd merge-diff command)
def destroy_snap(image_path, snap_name):
    if image_path[0:1] == "/":
        # THIS SECTION OBSOLETE - replaced by merge_snaps(...)
        # file/directory storage
        snap_file = os.path.join(image_path, snap_name)
        next_snap = get_next_snap(image_path, snap_name)
        
        if not next_snap:
            # we can't merge the last file, but we don't delete it either just in case there is a bug
            return
        next_file = os.path.join(image_path, next_snap)

        log_debug("in destroy_snap()")
        log_debug("    %s" % snap_file)
        log_debug("    %s" % next_file)
        
        args = ["rbd", "merge-diff", snap_file, next_file, next_file+".tmp"]

        log_debug("args = %s"%args)
        log_info("merging %s and %s" % (snap_name, next_snap))
        
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.wait()
        if( p.returncode == 0 ):
            os.rename(next_file+".tmp", next_file)
            os.remove(snap_file)
            return

        raise Exception("Failed to merge snap \"%s\" and \"%s\":\n%s" % (snap_file, next_file, read_file(p.stderr)))
        
    else:
        snap_path = image_path + "@" + snap_name
        args = ["rbd", "snap", "rm", snap_path]

        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.wait()
        if( p.returncode == 0 ):
            return

        raise Exception("Failed to destroy snap \"%s\":\n%s" % (snap_path, read_file(p.stderr)))

def make_merge_snaps_tmp(image_path):
    name = os.path.basename(image_path)
    path = os.path.dirname(image_path)
    return os.path.join(path, "."+name+".merge_snaps.tmp")
    
# for directory storage, merges a group of snap files together
def merge_snaps(image_path, group, outfile=None, remove_merged=True):
    print("merging group %s into %s" % (group[0:-1], group[-1]))

    p = None
    
    first_snap_path = os.path.join(image_path, group[0])
    second_snap_path = os.path.join(image_path, group[1])
    last_out = None
    if len(group) == 2:
        last_out = make_merge_snaps_tmp(second_snap_path)
        firstout = os.path.join(image_path, last_out)
    else:
        firstout = "-"
    args = ["rbd", "merge-diff", first_snap_path, second_snap_path, firstout]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    if len(group) > 3:
        for snap_name in group[2:-1]:
            snap_file = os.path.join(image_path, snap_name)
            
            args = ["rbd", "merge-diff", "-", snap_file, "-"]
            p = subprocess.Popen(args, stdin=p.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if len(group) > 2:
        last_snap_file = os.path.join(image_path, group[-1])
        last_out = make_merge_snaps_tmp(os.path.join(image_path, last_snap_file))
        args = ["rbd", "merge-diff", "-", last_snap_file, last_out]
        p = subprocess.Popen(args, stdin=p.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
    p.wait()
    if( p.returncode == 0 ):
        if outfile == None:
            outfile = group[-1]
        out_path = os.path.join(image_path, outfile)
        os.rename(last_out, out_path)
        if remove_merged:
            for snap_name in group[0:-1]:
                snap_file = os.path.join(image_path, snap_name)
                os.remove(snap_file)
        return
    raise Exception("Failed to merge snaps:\n%s" % (read_file(p.stderr)))

# for ceph storage, removes snaps listed in snaps
# for directory storage, merges snaps together so that the ones listed in snaps are all gone; the original files are removed (including the one not listed that the listed ones are merged into), but a new file is made that is the merged version
def destroy_snaps(image_path, snaps):
    if image_path[0:1] == "/":
        # group together snaps, piping them all together in one operation
        
        log_debug("in destroy_snaps, image_path = %s, snaps = %s" % (image_path, snaps))
       
        # the list of snaps to merge together; the last one in the list is not destroyed; other snaps merge into the last one
        group = []
        for snap_name in snaps:
            # for all the snap names, we look for next snap...
            snap_file = os.path.join(image_path, snap_name)
            next_snap = get_next_snap(image_path, snap_name)
            
            log_debug("snap_name = %s, next = %s, found = %s" % (snap_name, next_snap, next_snap in snaps))
            
            group += [snap_name]
            if next_snap in snaps:
                # if the next snap is in snaps, then we join it together with that one
                pass
            else:
                # if the next snap is not in snaps, we keep it separate
                if next_snap:
                    group += [next_snap]
                try:
                    merge_snaps(image_path, group)
                except:
                    log_error("failed to merge for image_path = %s, group = %s" % (image_path, group))
                    traceback.print_exc()
                group = []
            
            
    else:
        for snap in snaps:
            log_verbose("deleting snap \"%s\"" % snap)
            destroy_snap(image_path, snap)
    

class Spec:
    def __init__(self, spec):
        spec = args.spec.split(",")
        self.daily = int(spec[0])
        self.weekly = int(spec[1])
        self.monthly = int(spec[2])


def rotate(image_path, spec):
    # datetime objects of previous snaps
    prevdaily = None
    prevweekly = None
    prevmonthly = None

    maybekeepd=[]
    maybekeepw=[]
    maybekeepm=[]

    daily = spec.daily
    weekly = spec.weekly
    monthly = spec.monthly

    # Two passes... to ensure we don't delete old snapshots just because they're not old enough to be the oldest monthly one
    # First pass, flag them as the monthly,weekly,daily intervals (oldest of that period)
    log_debug("First pass... find candidates")

    daydelta = datetime.timedelta(days=1)

    latest_snap = None
    count_total = 0
    for snap in get_snaps(image_path):
        count_total += 1
        snapdate_str = snap[snap.find("-")+1:]

        snapdate = datetime.datetime.strptime(snapdate_str, "%Y-%m-%dT%H:%M:%S")

        # with hour+minute+second trimmed, so it's rounded down
        snapdate = snapdate.replace(hour=0, minute=0, second=0)

        log_debug("snap = %s, snapdate = %s" % (snap, snapdate))

        if not prevdaily or (snapdate - datetime.timedelta(days=1)) >= prevdaily:
            # if this snap is at least a day before the previous "daily" snap
            log_debug("    daily")
            prevdaily = snapdate
            maybekeepd += [snap]

        if not prevweekly or (snapdate - datetime.timedelta(days=7)) >= prevweekly:
            # if this one is at least a week earlier
            log_debug("    weekly")
            prevweekly = snapdate
            maybekeepw += [snap]

        if not prevmonthly or (snapdate - relativedelta(months=1)) >= prevmonthly:
            # if this one is at least a month earlier
            log_debug("    monthly")
            prevmonthly = snapdate
            maybekeepm += [snap]

        latest_snap = snap
    keep = {}

    log_debug("Second pass... keep only a few candidates")

    # Second pass, keep the newest few of each interval, based on limit settings
    for idx in range(len(maybekeepd)-1, -1, -1):
        snap = maybekeepd[idx]
        log_debug("snap was %s" % snap)

        if daily == 0:
            break

        # if we didn't find enough daily snapshots yet
        log_debug("keeping %s as daily" % snap)
        keep[snap] = "d"
        daily -= 1

    for idx in range(len(maybekeepw)-1, -1, -1):
        snap = maybekeepw[idx]
        log_debug("snap was %s" % snap)

        if weekly == 0:
            break

        # if we didn't find enough weekly snapshots yet
        log_debug("keeping %s as weekly" % snap)
        if snap in keep:
            keep[snap] += ",w"
        else:
            keep[snap] = "w"

        weekly -= 1

    for idx in range(len(maybekeepm)-1, -1, -1):
        snap = maybekeepm[idx]
        log_debug("snap was %s" % snap)

        if monthly == 0:
            break

        # if we didn't find enough monthly snapshots yet
        log_debug("keeping %s as monthly" % snap)
        if snap in keep:
            keep[snap] += ",m"
        else:
            keep[snap] = "m"

        monthly -= 1

    # in addition to the time based logic, we also always keep the last snap
    if not latest_snap in keep:
        keep[latest_snap] = "*"

    log_debug("count = %s" % len(keep))

    log_info("Done planning... keeping:")

    od = collections.OrderedDict(sorted(keep.items()))

    for snap in od:
        log_info("%s - %s" % (snap, keep[snap]))

    count_keeping = 0
    count_deleting = 0
    snaps_to_destroy = []
    for snap in get_snaps(image_path):
        log_debug("snap = %s" % snap)

        if snap in keep:
            log_debug("Keeping %s - %s" % (snap, keep[snap]))
            count_keeping += 1
        else:
            log_verbose("queueing deletion of snap \"%s\"" % snap)
            if not args.dry_run:
                snaps_to_destroy += [snap]
            count_deleting += 1
    log_info("keeping %s and deleting %s snapshots out of %s" %(count_keeping, count_deleting, count_total))
    destroy_snaps(image_path, snaps_to_destroy)
    
def get_images(pool):
    if pool[0:1] == "/":
        return sorted(os.listdir(pool))
    else:
        args = ["rbd", "ls", pool]
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.wait()
        if( p.returncode == 0 ):
            ret = []
            for line in p.stdout:
                line = line.decode("utf-8").splitlines()[0]
                ret += [line]
            return ret
        
        raise Exception("Failed to get list of rbd images in pool %s:\n%s" % (pool, read_file(p.stderr)))


def run(spec):
    for image_path in args.image_paths:
        if image_path.endswith("/"):
            for image in get_images(image_path[0:-1]):
                if image.endswith(".old"):
                    continue
                log_info("rotating image %s" % image_path + image)
                rotate(image_path + image, spec)
                
        else:
            # an image name
            rotate(image_path, spec)


if __name__ == "__main__":
    global args, spec

    parser = argparse.ArgumentParser(description="Clean up old Ceph RBD snapshots, keeping only a certain number of daily,weekly,monthly ones.")

    parser.add_argument('--debug', dest='debug', action='store_const',
                    const=True, default=False,
                    help='enable debug level output')
    parser.add_argument('--verbose', '-v', dest='verbose', action='store_const',
                    const=True, default=False,
                    help='enable verbose level output, such as snapshots being deleted (useful with --dry-run)')
    parser.add_argument('--dry-run', '-n', dest='dry_run', action='store_const',
                    const=True, default=False,
                    help='no action, only print what would be done')
    parser.add_argument('-s', dest='spec', action='store_const',
                    const=True, default="7,4,6",
                    help='comma separated daily, weekly, monthly counts to keep (default 7,4,6).')
    parser.add_argument('image_paths', metavar='image_paths', type=str, nargs='+',
                    help='rbd image paths(s) to clean up, eg. rbd/vm-101-disk1, or pool name(s) with trailing slash, eg. rbd/')

    args = parser.parse_args()
    spec = Spec(args.spec)

    got_lock = False
    lockFile = "/var/run/ceph_repl.lock"
    try:
        with open(lockFile, "wb") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got_lock = True
            except: # python3.4.x has BlockingIOError here, but python 3.2.x has IOError here... so just don't use those class names
                print("Could not obtain lock; another process already running? quitting")
                exit(1)
            run(spec)
    finally:
        if got_lock:
            os.remove(lockFile)
