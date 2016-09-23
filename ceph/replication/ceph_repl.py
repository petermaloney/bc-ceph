#!/usr/bin/env python3

# TODO: 
# - to take advantage of the json parsing, use python
# - use nice and ionice
# - check for space on mons... it seems to use a bunch when doing export
# - handle failure during import; we want some data saying that it started or finished, and if it is started and not finished, then rollback and retry previous rather than adding a new snapshot on top
# - handle excludes/includes

# Think of a way to handle failures and keep track of what is sent and what is not

#########################
# config TODO: move to separate file
#########################

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

#########################

import datetime
import socket
import subprocess
import sys
import json

now = datetime.datetime.now(datetime.timezone.utc)
nowstr = now.strftime("%Y-%m-%dT%H:%M:%S")
snapname = "replication-%s" % nowstr

if hasattr(subprocess, "DEVNULL"):
    subprocess_devnull = subprocess.DEVNULL
else:
    # python 3.2.3 (Ubuntu 12.04) doesn't have DEVNULL... so use PIPE
    subprocess_devnull = subprocess.PIPE


def log_error(message):
    print("ERROR: %s" % message)


def log_debug(message):
    print("DEBUG: %s" % message)


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

if direction == "pull":
    src_host = findhost(src_cluster)
    dest_host = None
else:
    src_host = None
    dest_host = findhost(dest_cluster)

# destpool should be "backup-${src_cluster}-${src_pool}, eg. backup-ceph-rbd
dest_pool = "backup-%s-%s" % (src_cluster, src_pool)

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
        return size/1024/1024
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


# TODO: handle prev_snap_name=None
def repl(snap_path, dest_image_path, prev_snap_name=None):
    print("Starting replication for snap src \"%s\" prev snap \"%s\" dest \"%s\"" 
        % (snap_path, prev_snap_name, dest_image_path))
    args = ["rbd", "export-diff", "--from-snap", prev_snap_name, snap_path, "-"]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    args = ["rbd", "import-diff", "-", dest_image_path]
    p2 = subprocess.Popen(args, stdin=p.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    p2.wait()
    if( p2.returncode == 0 ):
        print("replication successful \"%s\" -> \"%s\"" % (snap_path, dest_image_path))
        return
    raise Exception("failed to export/import diff the stream, src \"%s\" prev snap \"%s\" dest \"%s\"" % 
                    (snap_path, prev_snap_name, dest_image_path) )


if __name__ == "__main__":
    for image in get_images(src_pool, src_host):
        # TODO: do some better way of excluding things
        if image == "manjaro-bak":
            continue
        
        snap_path = "%s/%s@%s" % (src_pool, image, snapname)
        
        print("Making snapshot: %s" % snap_path)
        snap_create(snap_path, src_host)

        src_size = get_size(image, src_host)
        
        try:
            dest_size = get_size(image, dest_host)
        except:
            dest_size = None

        #log_debug("src size = %s, dest size = %s" % (src_size, dest_size))
        
        dest_image_path = "%s/%s" % (dest_pool,image)
        if not dest_size: #TODO:::::: YOU ARE HERE *******************************************
            #TODO: save state meaning "write in progress"
            repl(snap_path, dest_image_path)
            
            # TODO: do I need dest_snap_create?
            snap_create("%s/%s@%s" % (dest_pool, image, snapname), dest_host)
        elif dest_size != src_size:
            log_error("ERROR: incremental mode but src and dest are different size... untested")
            exit(1)
            # TODO: support growing, but not shrinking automatically ... or does import-diff already do this?
        else:
            #TODO: save state meaning "write in progress"
            
            # figure out prev_snap_name
            #prev_snap_name = dest_get_latest_snap("%s/%s" % (dest_pool, image))
            prev_snap_name = get_latest_snap("%s/%s" % (dest_pool, image), dest_host)
            
            #TODO: import-diff on dest
            repl(snap_path, dest_image_path, prev_snap_name=prev_snap_name)

        #TODO: send all snapshots in between too, not just the one made now?
        
        # when doing it in bash, it seemed that I needed this...
        # for some reason, I don't any more; rbd import-diff seems to do it.
        # Maybe only the first send required it?
        #dest_snap_create("%s/%s@%s" % (dest_pool, image, snapname), dest_host)

        print()
