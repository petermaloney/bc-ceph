#!/usr/bin/env python3
#
#TODO:
#    modify ceph_snaprotator.merge_snaps so if you set /dev/null, it won't name it .tmp, and just dump to /dev/null
#    add a way to checksum?
#        or will this do nothing since we can't compare export-diff output. 
#        Not sure how to do it other than import-diff and then export.
#        at least I should remove the header when comparing

import argparse
import ceph_snaprotator
import glob
import os

def list_snaps(image_path):
    ret = []
    for snap in sorted(glob.iglob(image_path+"/replication*"), key=os.path.basename):
        ret += [snap.split("/")[-1]]
    return ret

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test diff integrity by merging them. But you probably still need to import into ceph to compare checksums (this is export-diff not export).")

    parser.add_argument('image_paths', metavar='image_paths', type=str, nargs='+',
                    help='paths of images to test')
    
    parser.add_argument('--debug', dest='debug', action='store_const',
                    const=True, default=False,
                    help='enable debug level output')

    args = parser.parse_args()
    
    # TODO: arg, list
    #image_path="/data/ceph-repl/proxmox/vm-100-disk-1"
    
    outfile="/data/ceph-repl/ceph-repl-merge-testfile"
    
    for image_path in args.image_paths:
        snaps = list_snaps(image_path)
        if len(snaps) < 2:
            print("less than 2 snaps... skipping image %s" % image_path)
            continue
        ceph_snaprotator.merge_snaps(image_path, snaps, outfile=outfile, remove_merged=False)
