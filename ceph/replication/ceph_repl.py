#!/usr/bin/env python3
#
# replicates Ceph RBD images between pools or clusters; intended to be run by cron.
#
# FIXME: make all the snapshots for the same vm at the same time, eg. vm-199-disk-1 and vm-199-disk-2
#    for all disks, "lock add"
#    for all again, snap and "lock rm"
#    continue backup

# TODO:
# a temp clean feature? just scan through image names on src, and for each image on dest, remove temp files
# a log feature, to a file instead of stdout, and no log for when can't get a lock

import datetime
import socket
import subprocess
import sys
import json
import argparse
import fcntl
import os
import glob
import traceback
import time


def log_error(message):
    print("ERROR: %s" % message)
    sys.stdout.flush()


def log_debug(message):
    if debug:
        print("DEBUG: %s" % message)
        sys.stdout.flush()


def log_info(message):
    print("INFO: %s" % message)
    sys.stdout.flush()
    

def ssh_test(remote_host):
    p = subprocess.Popen(["ssh", remote_host, "hostname -s"], 
        stdout=subprocess_devnull, stderr=subprocess_devnull)
    p.wait()
    
    if( p.returncode == 0 ):
        return True
    
    return False


def findhost(remote_cluster):
    # can be calculated by convention... ${dest_cluster}${n} increment n until dns does not resolve 3 in a row
    n = 1
    remote_host = None
    dns_fail_count = 0
    while True:
        remote_host = "%s%s" % (remote_cluster, n)
        if socket.gethostbyname(remote_host):
            if ssh_test(remote_host):
                break
        else:
            log_error("could not resolve \"%s\"" % remote_host)
            dns_fail_count += 1
        
        if dns_fail_count >= 3:
            log_error("giving up after too many failures")
            exit(1)

        n += 1
    
    return remote_host

def read_file(fileobj):
    ret = ""
    for line in fileobj:
        if type(line) != str:
            line = line.decode("utf-8")
        ret += line
        
    return ret

def set_direction(host, pargs):
    global cfg, args

    if args.nice:
        nice = ["ionice", "-c", "2", "-n", "7", "nice", "-n", "16"]
    else:
        nice = []
    
    # it is expected that when direction+host means "me" the host/xxx_host values are None
    if cfg.direction == "pull" and host == cfg.src_host:
        pargs = ["ssh", host] + nice + pargs
    elif cfg.direction == "pull" and host == cfg.dest_host:
        pargs = nice + pargs
    elif cfg.direction == "push" and host == cfg.src_host:
        pargs = nice + pargs
    elif cfg.direction == "push" and host == cfg.dest_host:
        pargs = ["ssh", host] + nice + pargs
    else: 
        raise Exception("unexpected direction = %s, host = %s" % (direction, host))
    
    log_debug("host = %s, pargs = %s" % (host, pargs))
    return pargs


def get_images(pool, host=None):
    args = set_direction(host, ["rbd", "ls", pool])

    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.wait()
    if( p.returncode == 0 ):
        ret = []
        for line in p.stdout:
            line = line.decode("utf-8").splitlines()[0]
            ret += [line]
        return ret
    
    raise Exception("Failed to get list of rbd images:\n%s" % read_file(p.stderr))


def snap_create(snap_path, host=None):
    args = set_direction(host, ["rbd", "snap", "create", snap_path])

    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.wait()
    if( p.returncode == 0 ):
        return
    
    raise Exception("Failed to create snapshot \"%s\":\n%s" % (snap_path, read_file(p.stderr)))


def snap_rm(snap_path, host=None):
    args = set_direction(host, ["rbd", "snap", "rm", snap_path])

    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.wait()
    if( p.returncode == 0 ):
        return
    
    raise Exception("Failed to rm snapshot \"%s\":\n%s" % (snap_path, read_file(p.stderr)))


# return size in MiB (just like argument to rbd create --size ...)
def get_size(image_path, host=None):
    args = set_direction(host, ["rbd", "info", image_path, "--format", "json"])
        
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.wait()
    if( p.returncode == 0 ):
        o = json.loads( read_file(p.stdout) )
        size = o["size"]
        sizeMB = size/1024/1024
        ret = int(sizeMB)
        if sizeMB != ret:
            #raise Exception("Rounding error... sizeMB \"%s\" -> \"%s\" handling not implemented" % (sizeMB, ret))
            ret+=1
        return ret
    
    raise Exception("Failed to get size of \"%s\":\n%s" % (image_path, read_file(p.stderr)))


def get_latest_snap(image_path, host=None):
    args = set_direction(host, ["rbd", "snap", "ls", image_path, "--format", "json"])
        
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.wait()
    if( p.returncode == 0 ):
        o = json.loads( read_file(p.stdout) )

        for obj in o:
            pass
        
        return obj["name"]
    
    raise Exception("Failed to get latest snap of \"%s\":\n%s" % (image_path, read_file(p.stderr)))


def rbd_create(image_path, size, host=None):
    args = set_direction(host, ["rbd", "create", image_path, "--size", str(size)])
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.wait()
    if( p.returncode == 0 ):
        return
    
    raise Exception("Failed to create destination image \"%s\" size \"%s\" MB:\n%s" % (image_path, size, read_file(p.stderr)))


def repl(snap_path, dest_image_path, prev_snap_name=None):
    global cfg
    
    log_info("Starting replication for snap src \"%s\" prev snap \"%s\" dest \"%s\"" 
        % (snap_path, prev_snap_name, dest_image_path))
    
    args = ["rbd", "export-diff"]
    if prev_snap_name:
        args += ["--from-snap", prev_snap_name]
    args += [snap_path, "-"]
    args = set_direction(cfg.src_host, args)
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    args = set_direction(cfg.dest_host, ["rbd", "import-diff", "-", dest_image_path])
    p2 = subprocess.Popen(args, stdin=p.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    p2.wait()
    if( p2.returncode == 0 ):
        log_info("replication successful \"%s\" -> \"%s\"" % (snap_path, dest_image_path))

        # clean up remote snap for better cluster performance... only keep one snap
        prev_snap_path = snap_path.split("@")[0] + "@" + prev_snap_name
        snap_rm(prev_snap_path, cfg.src_host)
        return
    raise Exception("failed to export/import diff the stream, src \"%s\" prev snap \"%s\" dest \"%s\":\n%s" % 
                    (snap_path, prev_snap_name, dest_image_path, read_file(p2.stderr)) )

def repl_to_directory(snap_path, dest_image_dir_path):
    global cfg, args
    
    newest = None
    prev_snap_name = None

    log_debug("repl_to_directory, snap_path = \"%s\" dest_image_dir_path = \"%s\"" 
        % (snap_path, dest_image_dir_path))
    
    try:
        newest = None
        for snap in sorted(glob.iglob(dest_image_dir_path+"/replication*"), key=os.path.getctime):
            if not snap.endswith(".tmp"):
                newest = snap
        if newest:
            prev_snap_name = newest.split("/")[-1]
    except:
        pass
    
    log_info("Starting replication for snap src \"%s\" prev snap \"%s\" dest \"%s\"" 
        % (snap_path, prev_snap_name, dest_image_dir_path))
    
    pargs = ["rbd", "export-diff"]
    if prev_snap_name:
        pargs += ["--from-snap", prev_snap_name]
    pargs += [snap_path, "-"]

    if args.compression == True:
        pargs += ["|", "lz4"]
    
    pargs = set_direction(cfg.src_host, pargs)
    
    p1 = subprocess.Popen(pargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1024*1024)
    p=p1

    if args.compression:
        p2 = subprocess.Popen(["lz4", "-d", "-c"], stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1024*1024)
        p=p2

    total=0

    snap_name = snap_path[ snap_path.index("@")+1: ]
    
    outfile = "%s/%s" % (dest_image_dir_path, snap_name)
    #prefix dot prevents above glob from matching
    outfiletmp = "%s/.%s.tmp" % (dest_image_dir_path, snap_name)
    
    with open(outfiletmp, "wb") as f:
        buf = bytearray(1024*1024)
        while True:
            r = p.stdout.readinto(buf)
            if not r:
                break
            
            f.write(buf[0:r])
            
            total += r

    log_info("read %s bytes" % total)
    p.wait()
    if( p.returncode == 0 ):
        os.rename(outfiletmp, outfile)

        # clean up remote snap for better cluster performance... only keep one snap
        if prev_snap_name:
            prev_snap_path = snap_path.split("@")[0] + "@" + prev_snap_name
            snap_rm(prev_snap_path, cfg.src_host)
    else:
        os.remove(outfiletmp)
        raise Exception("failed to export-diff the stream or save the file, src \"%s\" prev snap \"%s\" dest \"%s\":\n%s" % 
                        (snap_path, prev_snap_name, outfiletmp, read_file(p1.stderr) + read_file(p2.stderr)) )
        
    
def do_import(config_file):
    if config_file.endswith(".py"):
        config_file = config_file[:len(config_file)-3]
    if "/" in config_file:
        config_file = config_file.split("/")[-1]
    global cfg
    cfg = __import__(config_file, globals(), locals())
    
    try:
        cfg.dest_cluster
    except:
        cfg.dest_cluster = None
        
    try:
        cfg.dest_directory
    except:
        cfg.dest_directory = None
    
    try:
        cfg.image_excludes
    except:
        cfg.image_excludes = []

    try:
        cfg.image_includes
    except:
        cfg.image_includes = []

def create_snap_name():
    now = datetime.datetime.now(datetime.timezone.utc)
    nowstr = now.strftime("%Y-%m-%dT%H:%M:%S")
    snapname = "replication-%s" % nowstr
    return snapname
    
def run():
    global subprocess_devnull, cfg
    
    if hasattr(subprocess, "DEVNULL"):
        subprocess_devnull = subprocess.DEVNULL
    else:
        # python 3.2.3 (Ubuntu 12.04) doesn't have DEVNULL... so use PIPE
        subprocess_devnull = subprocess.PIPE

    if cfg.direction == "pull":
        cfg.src_host = findhost(cfg.src_cluster)
        cfg.dest_host = None
    else:
        cfg.src_host = None
        cfg.dest_host = findhost(dest_cluster)

    # destpool should be "backup-${cfg.src_cluster}-${cfg.src_pool}, eg. backup-ceph-rbd
    dest_pool = "backup-%s-%s" % (cfg.src_cluster, cfg.src_pool)

    log_debug("image_includes = %s" % cfg.image_includes)
    log_debug("image_excludes = %s" % cfg.image_excludes)
    found_resume = None
    for image in get_images(cfg.src_pool, cfg.src_host):
        if len(cfg.image_includes) != 0 and image not in cfg.image_includes:
            log_debug("skipping non-included %s" % image)
            continue
        if image in cfg.image_excludes:
            log_debug("skipping excluded %s" % image)
            continue
        if image.endswith(".old"):
            log_debug("skipping .old %s" % image)
            continue
        if not found_resume and args.resume:
            if image == args.resume or "%s/%s"%(cfg.src_pool,image) == args.resume:
                found_resume = True
            else:
                log_debug("resume is set, and skipping %s" % image)
                continue
        try: 
            snapname = create_snap_name()
            
            src_snap_path = "%s/%s@%s" % (cfg.src_pool, image, snapname)
            dest_snap_path = "%s/%s@%s" % (dest_pool, image, snapname)
            src_image_path = "%s/%s" % (cfg.src_pool, image)
            
            log_info("Making snapshot: %s" % src_snap_path)
            snap_create(src_snap_path, cfg.src_host)

            src_size = get_size(src_image_path, cfg.src_host)

            if cfg.dest_directory:
                dest_image_path = os.path.join(cfg.dest_directory, cfg.src_pool, image)
                
                if not os.path.exists(dest_image_path):
                    os.makedirs(dest_image_path)
               
                repl_to_directory(src_snap_path, dest_image_path)
            else:
                dest_image_path = "%s/%s" % (dest_pool,image)
                
                try:
                    dest_size = get_size(dest_image_path, cfg.dest_host)
                except:
                    dest_size = None

                log_debug("src size = %s, dest size = %s" % (src_size, dest_size))
                
                if not dest_size:
                    rbd_create(dest_image_path, src_size, host=cfg.dest_host)
                    repl(src_snap_path, dest_image_path)
                else:
                    # figure out prev_snap_name
                    prev_snap_name = get_latest_snap(dest_image_path, cfg.dest_host)
                    
                    repl(src_snap_path, dest_image_path, prev_snap_name=prev_snap_name)
        except Exception as e:
            traceback.print_exc()
        if args.sleep and args.sleep != 0:
            log_info("sleeping %ss" % args.sleep)
            time.sleep(args.sleep)

def boolarg(parser, name):
    compression_parser = parser.add_mutually_exclusive_group(required=False)
    compression_parser.add_argument('--%s' % name, dest=name, action='store_true',
                    help='enable %s' % name)
    compression_parser.add_argument('--no-%s' % name, dest=name, action='store_false',
                    help='disable %s' % name)
    parser.set_defaults(**{name: True})
    return parser

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Perform Ceph RBD incremental replication using export-diff and import-diff.")
    
    parser.add_argument('--debug', dest='debug', action='store_const',
                    const=True, default=False,
                    help='enable debug level output')
    parser.add_argument('-c', dest='config_file', action='store',
                    type=str, required=True,
                    help='Config file')
    parser.add_argument('--resume', dest='resume', action='store',
                    type=str,
                    help='Name of an image to resume from; any image encountered before this one is skipped.')
    parser.add_argument('--sleep', dest='sleep', action='store',
                    type=int, default=30,
                    help="experimental - seconds to sleep between each backup")
 
    boolarg(parser, "compression")
    boolarg(parser, "nice")

    global args
    args = parser.parse_args()
    global debug
    debug = args.debug
   
    do_import(args.config_file)
    
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
            run()
    finally:
        if got_lock:
            os.remove(lockFile)
