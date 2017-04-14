#!/usr/bin/env python3

import sys
import subprocess
import re

#====================
# global variables
#====================

# pg_stat column in `ceph pg dump`, for finding the end of the pg list to ignore whatever is after it
re_pg_stat = re.compile("^[0-9]+\.[0-9a-z]+")

osds = {}
avg_old = 0
avg_new = 0


#====================

def ceph_osd_df():
    p = subprocess.Popen(["ceph", "osd", "df"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        lines = out.decode("UTF-8").splitlines()
        return lines
    else:
        raise Exception("ceph osd df command failed; err = %s" % str(err))


def ceph_pg_dump():
    #bc-ceph-pg-dump -a -s

    p = subprocess.Popen(["ceph", "pg", "dump"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        lines = out.decode("UTF-8").splitlines()
        
        # find the header row
        header = 0
        for line in lines:
            if line[0:8] == "pg_stat\t":
                break
            header+=1
            
        # find the last pg
        last_pg = header
        for line in lines[header+1:]:
            if not re_pg_stat.match(line[0:8]):
                break
            last_pg+=1
    
        return lines[header:last_pg]
    else:
        raise Exception("pg dump command failed; err = %s" % str(err))


def ceph_osd_reweight(osd_id, weight):
    p = subprocess.Popen(["ceph", "osd", "reweight", str(osd_id), str(weight)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        return
    else:
        raise Exception("ceph osd df command failed; err = %s" % str(err))


# weighted average, based on bytes and weight
def refresh_average():
    global osds
    global avg_old
    global avg_new
    
    total_old = 0
    total_new = 0
    count = 0
    
    for osd in osds.values():
        total_old += osd.bytes_old / osd.weight
        total_new += osd.bytes_new / osd.weight
        count += 1
    
    avg_old = total_old/count
    avg_new = total_new/count

    #print("avg_old = %s" % avg_old)
    #print("avg_new = %s" % avg_new)


class Osd:
    def __init__(self, osd_id):
        self.osd_id = osd_id
        self.weight = None
        self.reweight = None
        self.bytes_old = None
        self.bytes_new = None
        self.var_old = None
        self.var_new = None


def refresh_weight():
    global osds
    
    for line in ceph_osd_df():
        line = line.split()
        if line[0] == "ID" or line[0] == "TOTAL" or line[0] == "MIN/MAX":
            # ignore header and other things
            continue
        osd_id = int(line[0])
        
        if osd_id in osds:
            osd = osds[osd_id]
        else:
            osd = Osd(osd_id)
            osds[osd_id] = osd
        
        osd.weight = float(line[1])
        osd.reweight = float(line[2])


def refresh_bytes():
    global avg_old
    global avg_new
    
    for line in ceph_pg_dump():
        line = line.split()
        
        #print("line = %s \"%s\"" % (type(line), line))
        if line[0] == "pg_stat":
            # ignore header
            continue
        
        #   0          1          2      3       4       5      6        7      8          9        10 11         12    13          14    15            16                
        # ['pg_stat', 'objects', 'mip', 'degr', 'misp', 'unf', 'bytes', 'log', 'disklog', 'state', 'state_stamp', 'v', 'reported', 'up', 'up_primary', 'acting', 'acting_primary', 'last_scrub', 'scrub_stamp', 'last_deep_scrub', 'deep_scrub_stamp']


        size = int(line[6])
        up = line[14]
        acting = line[16]
        
        #print("DEBUG: size = %s, up = %s, acting = %s" % (size,up,acting))
        osds_old = acting.replace("[", "").replace("]", "").split(",")
        osds_new = up.replace("[", "").replace("]", "").split(",")
        
        osds_old = list(map(int, osds_old))
        osds_new = list(map(int, osds_new))

        #print("DEBUG: osds_old = %s, osds_new = %s" % (osds_old, osds_new))
        
        for osd_id in osds_old:
            osd_id = int(osd_id)
            osd = osds[osd_id]
            if not osd.bytes_old:
                osd.bytes_old = 0
            osd.bytes_old += size

        for osd_id in osds_new:
            osd_id = int(osd_id)
            osd = osds[osd_id]
            if not osd.bytes_new:
                osd.bytes_new = 0
            osd.bytes_new += size


def refresh_var():
    global osds
    
    for osd in osds.values():
        osd.var_old = osd.bytes_old / osd.weight / avg_old
        osd.var_new = osd.bytes_new / osd.weight / avg_new


def refresh_all():
    refresh_weight()
    refresh_bytes()
    refresh_average()
    refresh_var()


def print_report():
    global avg_old
    global avg_new
    
    print("%-3s %-7s %-7s %-14s %-5s %-14s %-5s" % ("osd", "weight", "reweight", "old size", "var", "new size", "var"))
    for osd in osds.values():
        print("%3d %7.5f %7.5f %14d %5.5f %14d %5.5f" % 
              (osd.osd_id, osd.weight, osd.reweight, osd.bytes_old, osd.var_old, osd.bytes_new, osd.var_new))

def adjust():
    lowest = osds[0]
    highest = osds[0]
    
    for osd in osds.values():
        if osd.var_new < lowest.var_new:
            lowest = osd
        if osd.var_new > highest.var_new:
            highest = osd
    
    print("DEBUG: lowest = %s %s" % (lowest.osd_id, lowest.var_new))
    print("DEBUG: highest = %s %s" % (highest.osd_id, highest.var_new))

    if lowest.reweight != 1 and lowest.var_new < 0.97:
        if lowest.var_new < 0.9:
            increment = 0.02
        else:
            increment = 0.005
        new = round(round(lowest.reweight,3) + increment, 4)
        if new > 1:
            new = 1
        print("Doing reweight: osd_id = %s, old = %s, new = %s" % (lowest.osd_id, lowest.reweight, new))
        ceph_osd_reweight(lowest.osd_id, new)
    if highest.reweight != 1 and highest.var_new > 1.03:
        if highest.var_new > 1.10:
            increment = 0.02
        else:
            increment = 0.005
        new = round(round(highest.reweight,3) - increment, 4)
        print("Doing reweight: osd_id = %s, old = %s, new = %s" % (highest.osd_id, highest.reweight, new))
        ceph_osd_reweight(highest.osd_id, new)
    

if __name__ == "__main__":
    refresh_all()
    print_report()
    if len(sys.argv) > 1 and sys.argv[1] == "-a":
        adjust()
