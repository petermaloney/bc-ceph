#!/bin/bash
#
# Remove all remote snaps except the latest that is on backup

for image in $(ssh ceph1 rbd ls proxmox); do
    (
    if ! flock -n 9; then
        log_warn "can't get lock... quitting"
        exit 1
    fi
    
    remote_local_match=
    for snap in $(ssh ceph1 rbd snap ls proxmox/"$image" | awk 'NR!=1{print $2}' | sort -r); do
        echo "remote = ${image}@${snap}"
        if [ -e "/data/ceph-repl/proxmox/${image}/${snap}" ]; then
            echo "local found"
            remote_local_match="$snap"
            break
        else
            echo "local not found"
        fi
    done
    list=()
    for snap in $(ssh ceph1 rbd snap ls proxmox/"$image" | awk 'NR!=1{print $2}' | sort -r); do
        if [ "$snap" = "$remote_local_match" ]; then
            continue
        fi
        echo "removing $snap"
        list+=("$snap")
    done
    
    if [ "${#list[@]}" = 0 ]; then
        continue
    fi
    
    echo "${list[@]}" | ssh ceph1 "
        for x in $(cat); do
            rbd snap rm "proxmox/${image}@\${x}"
        done
        "
    ) 9>/var/run/ceph_repl.lock
done

