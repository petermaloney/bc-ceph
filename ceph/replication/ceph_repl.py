#!/usr/bin/env python3

# TODO: 
# - use nice and ionice
# - handle failure during import; we want some data saying that it started or finished, and if it is started and not finished, then rollback and retry previous rather than adding a new snapshot on top
# - handle excludes/includes
# - separate config

# Think of a way to handle failures and keep track of what is sent and what is not

#########################
# config TODO: move to separate file
#########################

"""
direction = "pull"

#########
# source
#########
src_cluster = "ceph"
src_pool = "rbd"

#######
# dest
#######
dest_cluster = "ceph"
"""

#########################

import datetime
import socket
import subprocess
import sys
import json
import argparse

def log_error(message):
    print("ERROR: %s" % message)

def log_debug(message):
    if debug:
        print("DEBUG: %s" % message)

def log_info(message):
    print("INFO: %s" % message)
    

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

def set_direction(host, args):
    # it is expected that when direction+host means "me" the host/xxx_host values are None
    if direction == "pull" and host == src_host:
        args = ["ssh", host] + args
    elif direction == "pull" and host == dest_host:
        pass
    elif direction == "push" and host == src_host:
        pass
    elif direction == "push" and host == dest_host:
        args = ["ssh", host] + args
    else: 
        raise Exception("unexpected direction = %s, host = %s" % (direction, host))
    
    log_debug("host = %s, args = %s" % (host, args))
    return args


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
            raise Exception("Rounding error... sizeMB \"%s\" -> \"%s\" handling not implemented" % (sizeMB, ret))
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


# TODO: use set_direction(...) here
def repl(snap_path, dest_image_path, prev_snap_name=None):
    log_info("Starting replication for snap src \"%s\" prev snap \"%s\" dest \"%s\"" 
        % (snap_path, prev_snap_name, dest_image_path))
    
    args = ["rbd", "export-diff"]
    if prev_snap_name:
        args += ["--from-snap", prev_snap_name]
    args += [snap_path, "-"]
    args = set_direction(src_host, args)
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    args = set_direction(dest_host, ["rbd", "import-diff", "-", dest_image_path])
    p2 = subprocess.Popen(args, stdin=p.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    p2.wait()
    if( p2.returncode == 0 ):
        log_info("replication successful \"%s\" -> \"%s\"" % (snap_path, dest_image_path))
        return
    raise Exception("failed to export/import diff the stream, src \"%s\" prev snap \"%s\" dest \"%s\":\n%s" % 
                    (snap_path, prev_snap_name, dest_image_path, read_file(p2.stderr)) )

def do_import(config_file):
    if config_file.endswith(".py"):
        config_file = config_file[:len(config_file)-3]
    cfg = __import__(config_file, globals(), locals())

    global direction, src_cluster, src_pool, dest_cluster
    
    direction = cfg.direction
    src_cluster = cfg.src_cluster
    src_pool = cfg.src_pool
    dest_cluster = cfg.dest_cluster

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Perform Ceph RBD incremental replication using export-diff and import-diff.")
    
    parser.add_argument('--debug', dest='debug', action='store_const',
                    const=True, default=False,
                    help='enable debug level output')
    parser.add_argument('-c', dest='config_file', action='store',
                    type=str, default=None,
                    help='Config file')

    args = parser.parse_args()
    global debug
    debug = args.debug
    
    do_import(args.config_file)
    
    now = datetime.datetime.now(datetime.timezone.utc)
    nowstr = now.strftime("%Y-%m-%dT%H:%M:%S")
    snapname = "replication-%s" % nowstr

    if hasattr(subprocess, "DEVNULL"):
        subprocess_devnull = subprocess.DEVNULL
    else:
        # python 3.2.3 (Ubuntu 12.04) doesn't have DEVNULL... so use PIPE
        subprocess_devnull = subprocess.PIPE

    if direction == "pull":
        src_host = findhost(src_cluster)
        dest_host = None
    else:
        src_host = None
        dest_host = findhost(dest_cluster)

    # destpool should be "backup-${src_cluster}-${src_pool}, eg. backup-ceph-rbd
    dest_pool = "backup-%s-%s" % (src_cluster, src_pool)

    for image in get_images(src_pool, src_host):
        # TODO: do some better way of excluding things
        if image == "manjaro-bak":
            continue
        
        src_snap_path = "%s/%s@%s" % (src_pool, image, snapname)
        dest_snap_path = "%s/%s@%s" % (dest_pool, image, snapname)
        src_image_path = "%s/%s" % (src_pool, image)
        dest_image_path = "%s/%s" % (dest_pool,image)
        
        log_info("Making snapshot: %s" % src_snap_path)
        snap_create(src_snap_path, src_host)

        src_size = get_size(src_image_path, src_host)
        
        try:
            dest_size = get_size(dest_image_path, dest_host)
        except:
            dest_size = None

        log_debug("src size = %s, dest size = %s" % (src_size, dest_size))
        
        if not dest_size:
            #TODO: save state meaning "write in progress"
            rbd_create(dest_image_path, src_size, host=dest_host)
            repl(src_snap_path, dest_image_path)
            
            # TODO: do I need dest_snap_create?
            #snap_create(dest_snap_path)
        elif dest_size != src_size:
            log_error("ERROR: incremental mode but src and dest are different size... untested")
            exit(1)
            # TODO: support growing, but not shrinking automatically ... or does import-diff already do this?
        else:
            #TODO: save state meaning "write in progress"
            
            # figure out prev_snap_name
            prev_snap_name = get_latest_snap(dest_image_path, dest_host)
            
            #TODO: import-diff on dest
            repl(src_snap_path, dest_image_path, prev_snap_name=prev_snap_name)

        #TODO: send all snapshots in between too, not just the one made now?
        
        # when doing it in bash, it seemed that I needed this...
        # for some reason, I don't any more; rbd import-diff seems to do it.
        # Maybe only the first send required it?
        #dest_snap_create(dest_snap_path)
