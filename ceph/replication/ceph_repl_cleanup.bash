#!/bin/bash
#
# Remove all remote snaps except the latest that is on backup

# TODO: make one ssh connection and reuse it


echo "reading snap list"
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

list_images(){
    printf "%s\n" "${snap_data[@]}" | awk -F@ '{print $1}' | uniq
}
list_snaps(){
    local image="$1"
    printf "%s\n" "${snap_data[@]}" | grep -E "^${image}@" | awk -F@ '{print $2}'
}

(
if ! flock -n 9; then
    log_warn "can't get lock... quitting"
    exit 1
fi
for image in $(list_images); do
    echo "image = $image"
    remote_local_match=
    for snap in $(list_snaps "$image" | sort -r); do
        echo -n "    remote = ${image}@${snap}... "
        if [ -e "/data/ceph-repl/proxmox/${image}/${snap}" ]; then
            echo " local found"
            remote_local_match="$snap"
            break
        else
            echo " local not found"
        fi
    done
    list=()
    for snap in $(list_snaps "$image" | sort -r); do
        if [ "$snap" = "$remote_local_match" ]; then
            continue
        fi
        echo "    removing $snap"
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
done
) 9>/var/run/ceph_repl.lock

