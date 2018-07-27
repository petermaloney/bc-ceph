#!/usr/bin/env python3

import argparse
import subprocess
import json
import socketserver
import socket
import hashlib
import binascii
import time
import threading
import sys
import traceback
import math
import os

server_list = None
json_nan_regex = None

#listen_address = "localhost"
listen_address = "0.0.0.0"
port = 9871

hostname = socket.gethostname()

# hostname -> last nonce accepted
nonces = {}
start_time = int(time.time())

#====================
# logging
#====================

# TODO: 
# - instead of console output, use syslog from python
# - signal handler for HUP so it will reopen the log file
# - logrotate file
# - rsyslog rule file
# - change init script so it only handles the console log, and logging here is to the regular file

import logging
import logging.handlers

# Log levels:
# TRACE   = 5
# DEBUG   = 10
# VERBOSE = 15
# INFO    = 20
# WARN    = 30 (aka WARNING)
# ERROR   = 40
# FATAL   = 50 (aka CRITICAL)

logging.VERBOSE = 15
def log_verbose(self, message, *args, **kws):
    if self.isEnabledFor(logging.VERBOSE):
        self.log(logging.VERBOSE, message, *args, **kws)

logging.addLevelName(logging.VERBOSE, "VERBOSE")
logging.Logger.verbose = log_verbose

stream_formatter = logging.Formatter(
    fmt='%(asctime)-15s.%(msecs)03d %(levelname)s: %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(stream_formatter)

# syslog already adds date (and hostname), but we have to add script name and pid
# the script name hardcoded here can be matched with rsyslog conditions like: $programname == 'bc-ceph-auto-reboot'
syslog_formatter = logging.Formatter(
    fmt='bc-ceph-auto-reboot[%(process)d]: %(levelname)s: %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

syslog_handler = logging.handlers.SysLogHandler(address = '/dev/log')
syslog_handler.setFormatter(syslog_formatter)

logger = logging.getLogger("bc-ceph-auto-reboot")

# don't add a handler yet... we'll add based on argparse

#====================

# from https://stackoverflow.com/questions/6086976/how-to-get-a-complete-exception-stack-trace-in-python
def format_exception():
    exception_list = traceback.format_stack()
    exception_list = exception_list[:-2]
    exception_list.extend(traceback.format_tb(sys.exc_info()[2]))
    exception_list.extend(traceback.format_exception_only(sys.exc_info()[0], sys.exc_info()[1]))

    exception_str = "Traceback (most recent call last):\n"
    exception_str += "".join(exception_list)
    # Removing the last \n
    exception_str = exception_str[:-1]

    return exception_str


class JsonValueError(Exception):
    def __init__(self, cause):
        self.cause = cause
    

def ceph_health():
    p = subprocess.Popen(["ceph", "health"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        lines = out.decode("UTF-8")
        return lines.strip()
    else:
        raise Exception("ceph health command failed; err = %s" % str(err))


def ceph_osd_tree():
    p = subprocess.Popen(["ceph", "osd", "tree", "--format=json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        jsontxt = out.decode("UTF-8")
        try:
            return json.loads(jsontxt)
        except ValueError as e:
            # we expect this is because some osds are not fully added, so they have "-nan" in the output.
            # that's not valid json, so here's a quick fix without parsing properly (which is the json lib's job)
            try:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("DOING WORKAROUND. jsontxt = %s" % jsontxt)
                global json_nan_regex
                if not json_nan_regex:
                    json_nan_regex = re.compile("([^a-zA-Z0-9]+)(-nan)")
                jsontxt = json_nan_regex.sub("\\1\"-nan\"", jsontxt)
                return json.loads(jsontxt)
            except ValueError as e2:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("FAILED WORKAROUND. jsontxt = %s" % jsontxt)
                raise JsonValueError(e)
    else:
        raise Exception("ceph osd df command failed; err = %s" % str(err))


def get_uptime():
    with open('/proc/uptime', 'r') as f:
        uptime_seconds = float(f.readline().split()[0])
    uptime_days = uptime_seconds / 86400
    return uptime_days


def get_hosts():
    j = ceph_osd_tree()
    ret = []
    nodes = j["nodes"]
    for n in nodes:
        if n["type"] == "host":
            ret += [n["name"]]

    return ret


# All this does is delete the client.reboottest key. Any attempt to use the key will make a new one, so that's all is needed.
# Deleting the key often seems paranoid, but it doesn't cost much, so maybe we'll do it.
def reset_security_key():
    p = subprocess.Popen(["ceph", "auth", "del", "client.reboottest"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        return
    else:
        raise Exception("ceph auth del command failed; err = %s" % str(err))


# This generates the hash used for the security check; this function is used by server and client.
# To prove the reboot request comes from a real ceph node, we make a nonce (UNIX timestamp), and get a key (client.reboottest) from ceph auth, and we require a hash of nonce+key to match on both sides.
# FIXME: this assumes the ceph osd hosts have the admin key.
def get_security_check_hash(nonce):
    p = subprocess.Popen(["ceph", "auth", "get-or-create", "client.reboottest"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        txt = out.decode("UTF-8")

        keyline = txt.splitlines()[1].split()
        key = keyline[0]
        equals = keyline[1]
        value = keyline[2]

        if key != "key" or equals != "=":
            raise Exception("ceph reboot test command failed; output format wasn't understood")

        m = hashlib.sha256()
        m.update(str(nonce).encode("utf-8"))
        m.update(value.encode("utf-8"))
        local_hash_bytes = m.digest()
        return local_hash_bytes
    else:
        raise Exception("ceph reboot test command failed; err = %s" % str(err))


def reboot_security_check(client_address, nonce, remote_hash_hex_string):
    remote_hash_hex_string_bytes = remote_hash_hex_string.encode("utf-8")
    remote_host = client_address[0]
    
    if nonce < start_time:
        logger.error("security check failed. invalid nonce = %s. nonce must be the UNIX timestamp after the start of this program which was %s." % (nonce, start_time))
        return None
    
    # a proper nonce is > the previously used nonce... enforce that
    # so we record script start time, and a map of host -> last nonce given, and require all nonces to be > either of those
    if remote_host in nonces:
        prev_nonce = nonces[remote_host]
        if nonce <= prev_nonce:
            logger.error("hancling client %s, invalid nonce = %s. nonce must be higher than previously used nonce for that host. prev_nonce = %s." % (client_address, nonce, prev_nonce))
            return False

    nonces[remote_host] = nonce
    
    local_hash_bytes = get_security_check_hash(nonce)
    if not local_hash_bytes:
        logger.error("hancling client %s, security check failed. invalid nonce" % (client_address))
        return False

    local_hash_hex_string_bytes = binascii.hexlify(local_hash_bytes)

    if remote_hash_hex_string_bytes != local_hash_hex_string_bytes:
        logger.error("security check failed. nonce = \"%s\", remote_hash = \"%s\", local_hash = \"%s\"" % (nonce, remote_hash_hex_string, str(local_hash_hex_string_bytes, "utf-8")))
        return False

    return True


def do_reboot():
    # health check
    health = ceph_health()
    if health != "HEALTH_OK":
        logger.info("health is not ok... skpping reboot. Health = %s" % health)
        return
    
    # uptime >= max_uptime check
    uptime = get_uptime()
    if uptime < args.max_uptime:
        logger.info("uptime %s is below max %s... skipping reboot." % (uptime, args.max_uptime))
        return
        
    # ceph osd set noout ??
    # I guess we can just let the auto out code handle any faliures caused by rebooting a machine

    if args.dry_run:
        logger.info("(DRY RUN) shutdown and reboot was scheduled at time: %s" % args.shutdown_time)
    else:
        #if hostname != "tceph1" and hostname != "tceph2" and hostname != "tceph3":
        #    logger.error("TESTING disabled reboot safety triggered - skipping reboot")
        #    return
            
        # start reboot process
        # shutdown -r +10
        p = subprocess.Popen(["shutdown", "-r", args.shutdown_time],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        out, err = p.communicate()
        if( p.returncode == 0 ):
            logger.info("shutdown and reboot was scheduled at time: %s" % args.shutdown_time)
        else:
            logger.error("shutdown and reboot failed to schedule at time: %s" % args.shutdown_time)


class ListenerHandler(socketserver.StreamRequestHandler):
    def handle_request(self, request_text):
        client_address = str(self.client_address)
        logger.info("handling client %s, request = %s" % (client_address, request_text))
        request_split = request_text.split()
        
        if len(request_text) == 0:
            logger.info("handling client %s, empty request..." % (client_address))
        elif request_text == "get_uptime":
            uptime = get_uptime()

            logger.info("handling client %s, uptime = %s" % (client_address, uptime))

            message_text = "%s %s %s" % (hostname, uptime, args.max_uptime)
            message_bytes = message_text.encode("utf-8")
            self.request.send(message_bytes)
        elif request_split[0] == "do_reboot":
            nonce = int(request_split[1])
            remote_hash = request_split[2]

            # security check...real master? maybe save an object in rados? or give a hash of a shared secret + nonce?
            if not reboot_security_check(client_address, nonce, remote_hash):
                logger.error("handling client %s, security check failed... skpping reboot." % (client_address))
                
                message_text = "%s %s" % (False, "security check failed")
                message_bytes = message_text.encode("utf-8")
                self.request.send(message_bytes)
                
                return
            if not time_is_allowed():
                logger.info("handling client %s, reboot is not allowed now; allowed = %s %s" % (client_address, args.days, args.times))
                
                message_text = "%s %s" % (False, "reboot is not allowed now")
                message_bytes = message_text.encode("utf-8")
                self.request.send(message_bytes)
                
                return

            message_text = "%s %s" % (True, "ok")
            message_bytes = message_text.encode("utf-8")
            self.request.send(message_bytes)

            logger.info("handling client %s, calling do_reboot()" % (client_address))
            do_reboot()
        
    # For handling input
    def handle(self):
        client_address = str(self.client_address)
        logger.info("handling client %s" % (client_address))

        request = self.rfile.readline().strip()
        request_text = str(request, "utf-8")

        self.handle_request(request_text)

        self.request.close()


class ListenerServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


client_thread = None

def run_server():
    socketserver.ThreadingMixIn.allow_reuse_address = True
    socketserver.TCPServer.allow_reuse_address = True

    server = None
    try:
        server = ListenerServer((listen_address, port), ListenerHandler)
        logger.info("Starting server... hit ctrl+c to exit")
        server.serve_forever()
    except KeyboardInterrupt as e:
        logger.info("Stopping server...")
        server.shutdown()
        
        if client_thread:
            client_thread.signal_stop = True
        
        exit(0)


def get_uptime_remote(server):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((server, port))
        sock.send("get_uptime\n".encode("utf-8"))
        reply = sock.recv(1024)
        sock.close()
        
        reply_text = reply.decode("utf-8")
        remote_hostname, uptime, remote_max_uptime = reply_text.split()
        
        return float(uptime), float(remote_max_uptime)
    except ConnectionRefusedError as e:
        logger.warn("failed to get uptime from %s: %s" % (server, e))
    except Exception as e:
        if logger.isEnabledFor(logging.DEBUG):
            s = format_exception()
            logger.debug("failed to get uptime from %s: %s" % (server, s))
        else:
            logger.info("failed to get uptime from %s: %s" % (server, e))
        return None


def split_status(line):
    i = line.index(" ")
    return line[0:i], line[i+1:]


def request_reboot_remote(server):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((server, port))
        
        # TODO: add nonce and hash
        nonce = int(time.time())
        local_hash_bytes = get_security_check_hash(nonce)
        local_hash_hex_string = str(binascii.hexlify(local_hash_bytes), "utf-8")
        
        message_text = "do_reboot %s %s\n" % (nonce, local_hash_hex_string)
        
        sock.send(message_text.encode("utf-8"))
        reply = sock.recv(1024)
        sock.close()
        
        reply_text = reply.decode("utf-8")
        status, message = split_status(reply_text)
        
        status = status == "True"
        
        return status, message
    except ConnectionRefusedError as e:
        logger.warn("failed to get uptime from %s: %s" % (server, e))
    except Exception as e:
        if logger.isEnabledFor(logging.DEBUG):
            s = format_exception()
            logger.debug("failed to get uptime from %s: %s" % (server, s))
        else:
            logger.info("failed to get uptime from %s: %s" % (server, e))
    return None, None


def sort_uptimes_key(uptime_tuple):
    uptime = uptime_tuple[0]
    max_uptime = uptime_tuple[1]
    
    #print("%s -> %s %s" % (uptime_tuple, type(uptime), uptime))
    
    excess = uptime - max_uptime
    
    return excess


day_names = ["sun", "mon", "tue", "wed", "thurs", "fri", "sat"]
def get_day_num(name):
    if name == "tues":
        name = "tue"
    return day_names.index(name)


# return true if the current calendar time matches the --times-allowed and --days-allowed arguments
def time_is_allowed():
    global args
    
    now = time.localtime()
    
    # converting here myself instead of using %a because I don't want to use locales... the same command run on 2 machines should work the same
    day = int(time.strftime("%w", now))
    # time
    hour = int(time.strftime("%H", now))
    minute = int(time.strftime("%M", now))
    
    minute_of_day = hour * 60 + minute
    
    # split by comma
    # define min and max for ranges
    # compare each split group's min and max

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("day = %s, hour = %s, minute = %s, minute_of_day = %s" % (day, hour, minute, minute_of_day))

    day_ok = False
    for g in args.days.split(","):
        g = g.strip()
        start_name, end_name = g.split("-") if "-" in g else (g,g)
        
        start_num = get_day_num(start_name)
        end_num = get_day_num(end_name)
        
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s to %s" % (start_name, end_name))
            
        if day >= start_num and day <= end_num:
            day_ok = True
            break

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("day_ok = %s" % day_ok)
    
    if not day_ok:
        return False
    
    time_ok = False
    for g in args.times.split(","):
        g = g.strip()
        start_time, end_time = g.split("-")
        
        start_hour, start_minute = start_time.split(":")
        start_minute_of_day = int(start_hour) * 60 + int(start_minute)
        
        end_hour, end_minute = end_time.split(":")
        end_minute_of_day = int(end_hour) * 60 + int(end_minute)
        
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s to %s / %s to %s" % (start_time, end_time, start_minute_of_day, end_minute_of_day))
        
        if minute_of_day >= start_minute_of_day and minute_of_day <= end_minute_of_day:
            time_ok = True
            break
    
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("time_ok = %s" % time_ok)
    
    return time_ok


class MasterClientThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.signal_stop = False
        self.stopped = False
    
    # sleeps a tiny bit at a time, checking for signal_stop each time, and sleeping the whole amount given
    # this makes handling ctrl+c faster and consistent (not different time depending on sleep amount)
    def sleep(self, sleep_amount):
        sleep_start = time.time()
        sleep_remaining = sleep_amount
        
        logger.debug("sleep(). sleep_amount = %s" % (sleep_amount))
            
        while True:
            if self.signal_stop:
                break
            
            sleep_remaining = sleep_amount - (time.time() - sleep_start)
            #logger.debug("sleep(). sleep_amount = %s, sleep_start = %s, now = %s, sleep_remaining = %s" % (sleep_amount, sleep_start, time.time(), sleep_remaining))
            
            sleep_time = min(sleep_remaining, 0.1)
            time.sleep(sleep_time)
            
            if time.time() - sleep_amount >= sleep_start:
                break

    def run(self):
        # TODO: instead of hardcoded sleep times, adjust them based on the next expected reboot time (calculated based on master inputs, ignoring remote max_uptime,days,times), limited to some large amount like 1h
        # but there are different conditions... a host refusing to reboot would have a shorter time
        loop_sleep_time = 10
        loop_sleep_time_after_reboot1 = 30
        loop_sleep_time_after_reboot2 = 10
        
        if args.shutdown_time == "now":
            loop_sleep_time_after_reboot1 = 5
        elif args.shutdown_time.startswith("+"):
            t = int(args.shutdown_time[1:])
            loop_sleep_time_after_reboot1 = t*60+5
            
        while not self.signal_stop:
            if not time_is_allowed():
                logger.info("master: reboot is not allowed now; allowed = %s %s; not checking status" % (args.days, args.times))
                self.sleep(loop_sleep_time)
                continue
                
            failure = False
            logger.info("master: checking status")
            
            server_list = sorted(get_hosts())
            uptimes = {}
            for server in server_list:
                uptime_tuple = None
                if server == hostname:
                    uptime_tuple  = get_uptime(), args.max_uptime
                else:
                    uptime_tuple = get_uptime_remote(server)
                    
                if uptime_tuple:
                    uptimes[server] = uptime_tuple
                else:
                    # checking a host failed... so we won't do any reboots
                    failure = True
                
            highest = max(uptimes, key=lambda x: sort_uptimes_key(uptimes[x]))

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("master: uptimes = %s, highest = %s" % (uptimes, highest))
            
            if self.signal_stop:
                break
            
            if failure:
                # we don't run the reboot code if any host failed....maybe that one is already rebooting.
                logger.info("master: due to failure, not requesting any reboots")
                self.sleep(loop_sleep_time)
                continue
            
            health = ceph_health()
            if health != "HEALTH_OK":
                logger.info("master: health is %s, so not doing reboots" % health)
                self.sleep(loop_sleep_time)
                continue
            
            high_uptime = uptimes[highest][0]
            high_max_uptime = uptimes[highest][1]
            if high_uptime > high_max_uptime:
                logger.info("master: requesting reboot of host %s" % highest)
                
                # send reboot request
                status, message = request_reboot_remote(highest)
                logger.info("master: host %s responded: %s %s" % (highest, status, message))
                
                if status == False:
                    logger.warn("master: host %s refused to reboot..." % (highest))
                    self.sleep(loop_sleep_time)
                    continue
                
                if highest == hostname:
                    # TODO: handle local machine differently?
                    while not self.signal_stop:
                        logger.info("master: waiting for the master to reboot...")
                        self.sleep(loop_sleep_time_after_reboot1)
                else:
                    reboot_start_time = time.time()
                    logger.info("master: waiting for host %s to reboot..." % (highest))
                    self.sleep(loop_sleep_time_after_reboot2)
                    
                    seemed_down = False
                    while not self.signal_stop:
                        uptime_tuple = get_uptime_remote(highest)
                        uptime = uptime_tuple[0] if uptime_tuple else None
                        
                        if not uptime_tuple:
                            logger.info("master: waiting for host %s to come back since %s..." % (highest, reboot_start_time))
                            self.sleep(loop_sleep_time_after_reboot2)
                            seemed_down = True
                        elif uptime > high_uptime and seemed_down:
                            # if it seems unlikely to actually reboot, try sending reboot command again.
                            logger.info("master: host %s seems back, but still has high uptime... stopping waiting (since %s), and next loop should request a reboot again" % (highest, reboot_start_time))
                            break
                        elif uptime > high_uptime:
                            logger.info("master: still waiting for host %s to reboot since %s..." % (highest, reboot_start_time))
                            self.sleep(loop_sleep_time_after_reboot2)
                        elif uptime < high_uptime:
                            # if the new uptime is lower than the uptime before we requested reboot, then we assume it's done
                            logger.info("master: host %s is back with uptime %s..." % (highest, uptime))
                            break
                    
            else:
                logger.info("master: no reboot necessary. highest = %s, next reboot in %s days" % (highest, high_max_uptime - high_uptime))
            
            self.sleep(loop_sleep_time)
            if self.signal_stop:
                break

        logger.info("master: Stopping master client thread...")
        self.stopped = True

def run_client():
    global client_thread

    # paranoid security? reset the key?
    #reset_security_key()

    client_thread = MasterClientThread()
    logger.info("Starting master client... hit ctrl+c to exit")
    client_thread.start()
    logger.info("Done starting master client...")

    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Reweight OSDs so they have closer to equal space used.')
    parser.add_argument('-d', '--debug', action='store_const', const=True,
                    help='enable debug level logging')
    parser.add_argument('--logging', action='store', default="console", type=str,
                    help='comma separated list of logging methods: console, syslog (default console)')
    
    parser.add_argument('-n', '--dry-run', action='store_const', const=True,
                    help='enable dry run mode, so it will do everything except the reboot')
    parser.add_argument('-N', '--dry-run-pretend', action='store', type=str, default=None,
                    help='pretend certain values for uptime for certain nodes; comma separated pairs. eg. ceph1=50,ceph2=10')
    parser.add_argument('-x', '--max-uptime', action='store', type=float, default=30,
                    help='maximum uptime in days before a reboot is needed (default 30)')
    parser.add_argument('--shutdown-time', action='store', type=str, default="+10",
                    help='scheduled time for shutdown (for syntax, see man shutdown) (default +10)')
    
    parser.add_argument('--days', action='store', type=str, default="mon-thurs",
                    help='days when rebooting is allowed, comma separated names and ranges (eg. \"mon,wed-thurs\") (default \"mon-thurs\")')
    parser.add_argument('--times', action='store', type=str, default="10:00-16:00",
                    help='local time when rebooting is allowed, comma separated ranges of times (eg. \"09:00-16:00,17:00-17:30\") (default \"10:00-16:00\")')
    
    global args
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    # start this way so errors go to console, and later remove it if it wasn't selected
    logger.addHandler(stream_handler)
    
    args.logging = args.logging.split(",")
    for method in args.logging:
        if method == "syslog":
            logger.addHandler(syslog_handler)
        elif method == "console":
            pass
        else:
            logger.error("Unrecognized logging method: %s" % method)
            exit(1)

    if not "console" in args.logging:
        logger.removeHandler(stream_handler)
            
    # TODO: when communicating between nodes, use something for security
    # TODO: if a new server is added, or one is removed, update? maybe whoever adds a server has that job?

    h = ceph_health().strip()

    u = get_uptime()

    server_list = sorted(get_hosts())
    logger.info("server_list = %s" % server_list)
    master = server_list[0]
    
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("master = %s" % master)

    if hostname == master:
        run_client()
        
    run_server()
    
    # TODO: how to run server, and then keep going for a client?


    #     lock idea 1: when clients ask for lock...
    #         save it as pending lock
    #         this server goes to the next server in the name-sorted list and requests the lock (chained, not broadcast)
    #         if the last in the chain sees all requested the same lock, then it sets the lock as final
    #         if an intermediate in the chain sees the next one replied it's final, set the lock final
    #         if the first in the chain sees the next one replied it's final, set the lock final, and tell the client
    #     lock idea 2: first in the list is the master (chosen)
    #         it tells the others when to reboot
    #         if any are down, there is no master
    #     lock idea 3: use some common quorum library and make that choose a master

    # The process...
    # server:
    #     all nodes run this program, and it acts as a server
    #     the server waits for requests from the master
    #     master polls for uptime
    #         uptime in reply is adjusted by -N?
    #         reply format is: hostname uptime max_uptime
    #     master requests a reboot
    #         security check...real master? maybe save an object in rados?
    #         health check
    #         uptime >= max_uptime check
    #         start reboot process
    # client:
    #     only the master acts as a client
    #     master polls all other machines for uptime
    #     reboot_order = sort by uptime weighted by max_uptime
    #     if not healthy, or if max uptime is < max_uptime, wait
    #     choose largest in reboot_order, and request that node starts the reboot process


    # things we ensure:
    #  - only one machine can be down (due to this script, or otherwise)
    #      we ensure this by having one master that never changes, and it only sends shutdown requests if we have HEALTH_OK and all servers reply to get_uptime
    # - 
