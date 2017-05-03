#!/bin/bash
#
# Remove all remote snaps except the latest that is on backup

# TODO: make one ssh connection and reuse it


verbose=0
dryrun=0

log_debug() {
    if [ "$verbose" != 0 ]; then
        echo "DEBUG: $@"
    fi
}

log_warn() {
    if [ "$verbose" != 0 ]; then
        echo "DEBUG: $@"
    fi
}

for arg in "$@"; do
    if [ "$arg" = "-v" ]; then
        verbose=1
    elif [ "$arg" = "-n" ]; then
        dryrun=1
    fi
    shift
done

echo -n "reading snap list..."
IFS=$'\n'
snap_data=($(
    ssh ceph1 '
            for image in $(rbd ls proxmox); do
                for snap in $(rbd snap ls proxmox/"$image" | awk '"'"'NR!=1{print $2}'"'"' | sort -r); do
                    echo "${image}@${snap}"
                done
            done
        '
))
echo "done"

list_images(){
    printf "%s\n" "${snap_data[@]}" | awk -F@ '{print $1}' | uniq
}
list_snaps(){
    local image="$1"
    printf "%s\n" "${snap_data[@]}" | grep -E "^${image}@" | awk -F@ '{print $2}'
}

(
if [ "$dryrun" != 1 ] && ! flock -n 9; then
    log_warn "can't get lock... quitting"
    exit 1
fi

for image in $(list_images); do
    log_debug "image = $image"
    
    # look for the newest remote snap that has a local copy, which we plan to keep, and remove others
    remote_local_match=
    for snap in $(list_snaps "$image" | sort -r); do
        log_debug "    remote = ${image}@${snap}... "
        if [ -e "/data/ceph-repl/proxmox/${image}/${snap}" ]; then
            log_debug " local found: /data/ceph-repl/proxmox/${image}/${snap}"
            remote_local_match="$snap"
            break
        elif rbd snap ls backup-ceph-proxmox/${image} | awk 'NR!=1{print $2}' | grep -q "$snap"; then
            log_debug " local found: backup-ceph-proxmox/${image}@${snap}"
            remote_local_match="$snap"
            break
        else
            log_debug " local not found"
        fi
    done
    
    # build a list of non-matching remote snaps which we plan to remove; this excludes the one found above
    # this assumes you want to keep only one remote snap, and remove all others, older or newer than backup
    list=()
    for snap in $(list_snaps "$image" | sort -r); do
        if [ "$snap" = "$remote_local_match" ]; then
            continue
        fi
        echo "    removing ${image}@${snap}"
        list+=("$snap")
    done
    
    if [ "${#list[@]}" = 0 ]; then
        continue
    fi

    # do the actual removal
    if [ "$dryrun" = 1 ]; then
        echo "echo DRY RUN not removing list = ${list[@]}"
    else
        echo "${list[@]}" | ssh ceph1 "
            for x in $(cat); do
                rbd snap rm "proxmox/${image}@\${x}"
            done
            "
    fi
done
) 9>/var/run/ceph_repl.lock

