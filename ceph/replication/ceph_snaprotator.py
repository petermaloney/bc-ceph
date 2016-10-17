#!/usr/bin/env python3
#
# rotates snapshots to keep only a certain number of daily,weekly,monthly ones.
# the algorithm keeps the oldest snapshot in each period

import datetime
import subprocess
import json
import argparse
import fcntl
import os
import collections


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
        return sorted(os.listdir(image_path))
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
        print("        looking at \"%s\" and \"%s\"" % (n,snap_name))
        if ".tmp" in n:
            continue
        if found:
            return n
        if n == snap_name:
            found = True
    
# for rbd, this actually destroys snaps
# for files, this uses the rbd merge-diff command
def destroy_snap(image_path, snap_name):
    if image_path[0:1] == "/":
        # file/directory storage
        snap_file = os.path.join(image_path, snap_name)
        next_snap = get_next_snap(image_path, snap_name)
        next_file = os.path.join(image_path, next_snap)

        log_debug("in destroy_snap()")
        log_debug("    %s" % snap_file)
        log_debug("    %s" % next_file)
        
        args = ["rbd", "merge-diff", snap_file, next_file, next_file+".tmp"]

        log_debug("args = %s"%args)
        
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

    for snap in get_snaps(image_path):
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
        if keep[snap]:
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
        if keep[snap]:
            keep[snap] += ",m"
        else:
            keep[snap] = "m"

        monthly -= 1

    log_debug("count = %s" % len(keep))

    log_info("Done planning... keeping:")

    od = collections.OrderedDict(sorted(keep.items()))

    for snap in od:
        log_info("%s - %s" % (snap, keep[snap]))

    count_kept = 0
    count_deleted = 0
    for snap in get_snaps(image_path):
        log_debug("snap = %s" % snap)

        if snap in keep:
            log_debug("Keeping %s - %s" % (snap, keep[snap]))
            count_kept += 1
        else:
            log_verbose("deleting snap \"%s\"" % snap)
            if not args.dry_run:
                destroy_snap(image_path, snap)
            count_deleted += 1
    log_info("kept %s and deleted %s snapshots" %(count_kept, count_deleted))

def run(spec):
    for image_path in args.image_paths:
        # TODO: support a path that is just a pool name without image names
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
                    help='comma separated daily, weekly, monthly counts to keep.')
    parser.add_argument('image_paths', metavar='image_paths', type=str, nargs='+',
                    help='rbd image paths(s) to clean up, eg. rbd/vm-101-disk1')

    args = parser.parse_args()
    spec = Spec(args.spec)

    got_lock = False
    lockFile = "/var/run/ceph_snaprotator.lock"
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
