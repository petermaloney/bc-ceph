install steps

your desktop:
    cd ~/projects/bc-it-admin/ceph/bc-ceph-auto-reboot

    if ssh ceph1 which systemctl; then
        # for Ubuntu 16.04
        for h in ceph{1..5}; do scp bc-ceph-auto-reboot.{py,rsyslog,logrotate,service} ${h}:; done
    else
        # for Ubuntu 14.04
        for h in ceph{1..5}; do scp bc-ceph-auto-reboot.{py,rsyslog,logrotate,init} ${h}:; done
    fi

ceph*:
    mv ~peter/bc-ceph-auto-reboot.py /usr/local/bin/
    chown root:root /usr/local/bin/bc-ceph-auto-reboot.py
    chmod +rx /usr/local/bin/bc-ceph-auto-reboot.py

    mv ~peter/bc-ceph-auto-reboot.logrotate /etc/logrotate.d/bc-ceph-auto-reboot
    mv ~peter/bc-ceph-auto-reboot.rsyslog /etc/rsyslog.d/bc-ceph-auto-reboot.conf
    chown root:root /etc/logrotate.d/bc-ceph-auto-reboot /etc/rsyslog.d/bc-ceph-auto-reboot.conf
    chmod u=rw,go=r /etc/logrotate.d/bc-ceph-auto-reboot /etc/rsyslog.d/bc-ceph-auto-reboot.conf
    service rsyslog restart

    if which systemctl; do
        mv ~peter/bc-ceph-auto-reboot.service /etc/systemd/system
        chown root:root /etc/systemd/system/bc-ceph-auto-reboot.service
        chmod +r /etc/systemd/system/bc-ceph-auto-reboot.service
        systemctl daemon-reload
        systemctl enable bc-ceph-auto-reboot
        systemctl start bc-ceph-auto-reboot
    else
        mv ~peter/bc-ceph-auto-reboot.init /etc/init.d/bc-ceph-auto-reboot
        chown root:root /etc/init.d/bc-ceph-auto-reboot
        chmod +rx /etc/init.d/bc-ceph-auto-reboot
        for n in 2 3 4 5; do
            ln -s /etc/init.d/bc-ceph-auto-reboot /etc/rc${n}.d/S99-bc-ceph-auto-reboot
        done
        service bc-ceph-auto-reboot start
    fi

