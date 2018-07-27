#!/usr/bin/env python3
#
# checks that every snapshot file's from-snap exists

import sys
import os
import traceback

debug = False
info = False

def log_debug(text):
    if debug:
        print("DEBUG: %s" % (text))
    
def log_info(text):
    if info:
        print("INFO: %s" % (text))
    
def log_warn(text):
    print("WARN: %s" % (text))
    
def load_files():
    files = []
    for file in sys.argv[1:]:
        if ".old" in file:
            continue
        files += [file]
    return files

def d(bytez):
    return bytez.decode('utf8')

def dint(bytez):
    ret = int(bytez[0])
    ret += int(bytez[1]<<8)
    ret += int(bytez[2]<<16)
    ret += int(bytez[3]<<24)
    return ret
    
def parse_diff(file):
    with open(file, "rb") as f:
        # "rbd diff v"
        x = f.read(10)
        
        if x != b'rbd diff v':
            raise Exception("hmm something's not right. file = %s, content = %s" % (file, str(x)))
        
        # version + 0x0a, eg. 0x31
        version = d(f.read(1))
        f.read(1)
        log_debug("version = %s" % version)
            
        # 0x74 -> "t" means "to snap" so there is no from snap
        type1 = d(f.read(1))
        log_debug("type1 = %s" % type1)
        
        if type1 != "t" and type1 != "f":
            raise Exception("unknown snap type %s" % type1)
        
        # 4 bytes? -> length of name to read
        len1 = dint(f.read(4))
        log_debug("len1 = %s" % len1)
        
        name1 = d(f.read(len1))
        log_debug("name1 = \"%s\"" % name1)
        
        if type1 == "t":
            return [None, name1]
        
        # 0x66 -> "f" means source snap
        type2 = d(f.read(1))
        log_debug("type2 = %s" % type1)
        
        # 4 bytes? -> length of name to read
        len2 = dint(f.read(4))
        log_debug("len2 = %s" % len2)

        name2 = d(f.read(len2))
        log_debug("name2 = \"%s\"" % name2)
        
        if type2 != "t" and type2 != "f":
            raise Exception("unknown snap type %s" % type2)
        
        return [name1, name2]
    
files = load_files()

for file in files:
    dirname = os.path.dirname(file)
    try:
        from_snap, to_snap = parse_diff(file)
    except:
        traceback.print_exc()
        continue

    if from_snap:
        from_exists = os.path.exists( os.path.join(dirname, from_snap) )
        if from_exists:
            fn=log_info
        else:
            fn=log_warn
        fn("%s, from_snap = %s, exists = %s" % (file, from_snap, from_exists))
    else:
        log_info("%s, from_snap = %s, first snap" % (file, from_snap))
    
    
