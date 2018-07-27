usage:
-------

initial installation (first mon and puppet):
--------------------
- configure the module's ceph.conf (and blank ceph.client.admin.keyring, ceph.mon.keyring and monmap file)
    vim /etc/modules/puppet/ceph/files/etc/ceph/ceph.conf
        [global]
        fsid = {cluster-id}
        mon initial members = {hostname}
        mon host = {ip-address}
        public network = {network}
        cluster network = {network}
        auth cluster required = cephx
        auth service required = cephx
        auth client required = cephx
        osd journal size = 1024
        osd pool default size = 3
        osd pool default min size = 2
        osd pool default pg num = 64
        osd pool default pgp num = 64
        osd crush chooseleaf type = 1
    touch /etc/modules/puppet/ceph/files/etc/ceph/ceph.client.admin.keyring
- possibly update the repo pubkey file
    wget -O- 'https://download.ceph.com/keys/release.asc' > /etc/puppet/modules/ceph/files/etc/ceph/ceph.asc
- in site.pp, include ceph on first mon node, but not ceph::mon
- run puppet agent on first mon node; now ceph, ceph.conf, and the helper scripts are installed... 
- manually go to the first mon and run the mon create script (which also starts the mon):
    bc-ceph-create-first-mon
- manually copy the files from that machine to the module's files/etc/ceph/ dir
   scp -3 root@ceph1:/var/lib/ceph/tmp/client.admin.keyring root@puppet:/etc/puppet/modules/ceph/files/etc/ceph/
   scp -3 root@ceph1:/var/lib/ceph/tmp/ceph.mon.keyring root@puppet:/etc/puppet/modules/ceph/files/etc/ceph/
   scp -3 root@ceph1:/var/lib/ceph/tmp/monmap root@puppet:/etc/puppet/modules/ceph/files/etc/ceph/

preparing new machines:
--------------------
prepare osds:
- on each osd machine, make empty GPT partition tables on the SSDs for journals
    parted /dev/nvme0n1 mktable gpt
- on each osd machine, make a partition for bcache and set a GPT label that ends with "_bcache" or "-bcache"
    parted /dev/sda mkpart ssd1_bcache ${start} ${end}
    parted /dev/sdb mkpart ssd2_bcache ${start} ${end}

new mon and osd daemons:
--------------------

more mons:
- in site.pp, include ceph::mon on any mons you want; it is redundant and does nothing on the first mon until files change that need redeployment
- after they are up, manually add them to ceph.conf

osd:
- in site.pp, include ceph::osd in each node
- note that because we use osd device serial numbers in the journal device name, when an osd disk is replaced, there is a new journal name too
- in site.pp, add one call to ceph::create_osd (function) per osd disk
    # TODO: this puppet function and script below need updating since adding bcache and syntax changes in the bc-ceph-create-osd script

    # use this script to generate the "ceph::create_osd" calls, and then modify them
    journals=($(stat -c %n /dev/disk/by-id/nvme-INTEL_SSD* | grep -vE -- "-part[0-9]+$"))
    bcaches=($(
        # This line for SAS/SATA only
        #for d in $(lsbcache | awk '$1 == "cache" && $4 !~ /nvme/ {print $4}'); do
        # This line for NVMe only
        for d in $(lsbcache | awk '$1 == "cache" && $4 ~ /nvme/ {print $4}'); do
            for l in /dev/disk/by-id/ata-* /dev/disk/by-id/scsi-* /dev/disk/by-id/nvme-*; do
                if readlink -f "$l" | grep -q "$d"; then
                    echo "$l"
                fi
            done
        done
    ))

    jn=0
    bn=0
    for d in /dev/sd[a-z]; do
        serial=$(smartctl -i "$d" | grep -i serial | awk '{print $NF}')
        printf "ceph::create_osd{\"%s\": journal=>\"%s\", bcache=>\"%s\"}\n" "$serial" "${journals[$jn]}" "${bcaches[$bn]}"
        let jn++
        let bn++
        if [ "$jn" -ge "${#journals[@]}" ]; then
            jn=0
        fi
        if [ "$bn" -ge "${#bcaches[@]}" ]; then
            bn=0
        fi
    done

mds:
- in site.pp, include ceph::mds

note about simplified/hardcoded stuff:
- cluster name is hardcoded "ceph" in the scripts
- creating any daemon type requires the admin keyring installed... right now this is fine for us, but would be better separate if all our hosts weren't also mons
- journals are always on partitions, named like journal_serialnumber
- bcache is on a partition, named like *-bcache
