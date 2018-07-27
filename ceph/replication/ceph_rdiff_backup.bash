#!/bin/bash -u
#
# Backup using only temporary snapshots
# This is because using persistent RBD snapshots makes the cluster very slow.

# TODO: somehow make sure rbd snaps are removed, like when this script exits early
# TODO: maybe write my own rdiff implementatino... it makes no sense to read the full file locally once for the sig plus once for the patch if I'm only modifying a bit of it
# TODO: make the inplace+rdiff-patch thing faster...
#      maybe instead of inplace reading the new file, and patch reading the old file, make a special inplace.py that reads only the old file, and reads the stream from rdiff patch; if the content is unchanged, don't write... if it changed, write

host=ceph1
base_dest_ds=data/ceph-backup
base_dest_dir=/data/ceph-backup

log_info() {
    echo "$(date +%Y-%m-%dT%H:%M:%S) INFO: $@"
}
log_warn() {
    echo "$(date +%Y-%m-%dT%H:%M:%S) WARN: $@"
}

die() {
    echo "ERROR: $@"
    exit 1
}

get_signature() {
    local path="$1"
    if [ -e "$path".raw.sig ]; then
        cat "$path".raw.sig
    else
        rdiff signature "$path".raw -
    fi
}
 
backup() {
    # rbd pool/image_name
    local image="$1"
    local snap_name="$2"

    local pool=$(dirname "$image")
    local image_name=$(basename "$image")

    local dest_ds="${base_dest_ds}/${pool}/${image_name}"
    local dest_file="${base_dest_dir}/${pool}/${image_name}/${image_name}.raw"

    log_info "backing up snap: ${image}@${snap_name}"
    if [ ! -e "${dest_file}" ]; then
        echo "Doing full pull"
        zfs create -p "${dest_ds}" || die "failed to create dest dataset: ${dest_ds}"
        ssh "$host" rbd export "${image}@${snap_name}" - \
            | tee >(rdiff signature - "${dest_file}.sig") \
            | dd of="${dest_file}" conv=sparse bs=1M \
        && zfs snapshot "${dest_ds}@${snap_name}"
    else
        echo "Doing incremental pull"

        local temp_snap_name=replication-temp
        if zfs list -t snapshot "${dest_ds}@$temp_snap_name" >/dev/null 2>&1; then
            zfs destroy "${dest_ds}@$temp_snap_name"
        fi
        zfs snapshot "${dest_ds}@${temp_snap_name}" || die "failed to create snapshot: $temp_snap_name"
        local temp_snap_path="${base_dest_dir}/${pool}/${image_name}/.zfs/snapshot/${temp_snap_name}"

        /usr/local/bin/inplace.py \
            <( \
                rdiff patch "${temp_snap_path}/${image_name}.raw" \
                    <(get_signature "${temp_snap_path}/${image_name}" \
                        | ssh "$host" rdiff delta - \<\(rbd export "${image}@${snap_name}" -\) -) \
                    - \
                    | tee >(rdiff signature - "${dest_file}.sig") \
            ) \
            "${dest_file}" \
        && zfs snapshot "${dest_ds}@${snap_name}"

        zfs destroy "${dest_ds}@${temp_snap_name}"
    fi
}

list_snaps() {
    local image="$1"
    ssh "$host" rbd snap ls "${image}"
    zfs list -t snapshot -r "${base_dest_ds}/${image}"
}

list_images() {
    local pool="$1"
    echo "DEBUG: listing only a few" >&2
    ssh "$host" rbd ls "${pool}" | grep vm-107-disk-2 #grep -E "101|102|106|107|109"
}

create_snap_name() {
    echo "replication-$(date +%Y-%m-%dT%H:%M:%S)"
}

create_snap() {
    local image="$1"
    local snap_name="$2"
    log_info "creating remote snap: ${image}@${snap_name}"
    ssh "$host" rbd snap create "${image}@${snap_name}"
}

destroy_snap() {
    local image="$1"
    local snap_name="$2"
    log_info "destroying remote snap: ${image}@${snap_name}"
    ssh "$host" rbd snap rm "${image}@${snap_name}"
}

snap_and_backup() {
    local pool="$1"
    local image="$2"

    (
        if ! flock -n 9; then
            log_warn "can't get lock... quitting"
            exit 1
        fi

        local snap_name=$(create_snap_name)
        create_snap "${pool}/${image}" "$snap_name"
        backup "${pool}/${image}" "$snap_name"
        destroy_snap "${pool}/${image}" "$snap_name"
    ) 9>"/var/lock/ceph_repl_${pool}_${image}.lock"
}

if [ "$(basename "$0")" = "list" ]; then
    image="$1"
    if grep -q ".raw" <<< "$image"; then
        image=$(sed -r 's|(.*).raw|\1|' <<< "$image")
    fi
    list_snaps "$image"
else
    # just in case it's not already set... otherwise backup would be very slow (rdiff reads input file 10x)
    zfs set primarycache=all data

    if grep -q "/" <<< "$1"; then
        pool=$(dirname "$1")
        image=$(basename "$1")

        snap_and_backup "$pool" "$image"
    else
        pool="$1"
        for image in $(list_images "$pool"); do
            snap_and_backup "$pool" "$image"
        done
    fi
fi

