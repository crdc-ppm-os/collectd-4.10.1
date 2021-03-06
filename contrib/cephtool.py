import collectd
import json
import os
import popen2
import random
import string
import sys
import time

g_cephtool_path = ""
g_ceph_config = ""

# How long to wait before complaining about PGs lingering in bad states.
g_linger_timeout = 300

def cephtool_config(config):
    global g_cephtool_path, g_ceph_config, g_linger_timeout
    for child in config.children:
        if child.key == "cephtool":
            g_cephtool_path = child.values[0]
        elif child.key == "config":
            g_ceph_config = child.values[0]
        elif child.key == "linger_timeout":
            g_linger_timeout = int(child.values[0])
    collectd.info("cephtool_config: g_cephtool_path='%s', g_ceph_config='%s',\
g_linger_timeout=%d" % (g_cephtool_path, g_ceph_config, g_linger_timeout))
    if g_cephtool_path == "":
        raise Exception("You must configure the path to cephtool.")
    if not os.path.exists(g_cephtool_path):
        raise Exception("Cannot locate cephtool. cephtool is configured as \
'%s', but that does not exist." % g_cephtool_path)

def cephtool_subprocess(more_args):
    args = [g_cephtool_path]
    if (g_ceph_config != ""):
        args.extend(["-c", g_ceph_config])
    args.extend(more_args)
    args.extend(["-o", "-"])
    pstdout, pstdin = popen2.popen2(args)
    return pstdout.read()

def find_first_json_line(lines):
    line_idx = 0
    for line in lines:
        if ((len(line) > 0) and ((line[0] == '{') or (line[0] == '['))):
            return line_idx
        line_idx = line_idx + 1
    raise Exception("failed to find the first JSON line in the output!")

def cephtool_get_json_sections(num_sections, more_args):
    info = cephtool_subprocess(more_args)
    lines = info.splitlines()
    jsonobjs = []
    for i in range(0, num_sections):
        first_json_line = find_first_json_line(lines)
        jsonstr = "\n".join(lines[first_json_line:])
        json_decoder = json.JSONDecoder()
        jsonobj, end_idx = json_decoder.raw_decode(jsonstr)
        jsonobjs.append(jsonobj)
        lines = jsonstr[end_idx:].splitlines()
    return jsonobjs

# { bad_state_name -> { pgid -> time_pgid_entered_state } }
g_bad_states_to_pgs = {
    "crashed" : {},
    "creating" : {},
    "degraded" : {},
    "down" : {},
    "inconsistent" : {},
    "peering" : {},
    "repair" : {},
    "replay" : {},
    "stray" : {},
}

def register_pg_in_state(curtime, pgid, state):
    global g_bad_states_to_pgs
    if (not g_bad_states_to_pgs.has_key(state)):
        return
    phash = g_bad_states_to_pgs[state]
    if (not phash.has_key(pgid)):
        phash[pgid] = curtime
        return
    if phash[pgid] > curtime:
        phash[pgid] = curtime

def count_lingering_bad_pgs(curtime):
    global g_bad_states_to_pgs
    global g_linger_timeout
    num_lingering_bad_pgs = {}
    for bad_state_name in g_bad_states_to_pgs.keys():
        num_lingering_bad_pgs[bad_state_name] = 0
    for bad_state_name, bad_pgs in g_bad_states_to_pgs.items():
        for pgid, etime in bad_pgs.items():
            if (etime > curtime):
                continue
            diff = curtime - etime
            if (diff > g_linger_timeout):
                num_lingering_bad_pgs[bad_state_name] = num_lingering_bad_pgs[bad_state_name] + 1
    for bad_state_name, num_lingering_bad_pgs in num_lingering_bad_pgs.items():
        collectd.Values(plugin="cephtool",\
            type=('num_lingering_' + bad_state_name + "_pgs"),\
            values=[num_lingering_bad_pgs]\
        ).dispatch()

def count_old_bad_pgs(curtime):
    global g_bad_states_to_pgs
    global g_linger_timeout
    pg_to_bad_time = { }
    for bad_state_name, bad_pgs in g_bad_states_to_pgs.items():
        for pgid, etime in bad_pgs.items():
            if (etime > curtime):
                continue
            diff = curtime - etime
            if (diff > g_linger_timeout):
                if (pg_to_bad_time.has_key(pgid)):
                    if (pg_to_bad_time.has_key[pgid] >= etime):
                        pg_to_bad_time[pgid] = etime
                else:
                    pg_to_bad_time[pgid] = etime
    num_old_bad_pgs = {
        "old" : 0,
        "older" : 0,
        "oldest" : 0,
    }
    for pgid, etime in pg_to_bad_time.items():
        if (etime > curtime):
            continue
        diff = curtime - etime
        if (diff > 3 * g_linger_timeout):
            num_old_bad_pgs["oldest"] = num_old_bad_pgs["oldest"] + 1
        elif (diff > 2 * g_linger_timeout):
            num_old_bad_pgs["older"] = num_old_bad_pgs["older"] + 1
        elif (diff > g_linger_timeout):
            num_old_bad_pgs["old"] = num_old_bad_pgs["old"] + 1
    for desc, num in num_old_bad_pgs.items():
        collectd.Values(plugin="cephtool",\
            type=('num_' + desc + "_bad_pgs"),\
            values=[num]\
        ).dispatch()

def cephtool_read_pg_states(pg_json):
    curtime = time.time()
    stateinfo = {
        "active" : 0,
        "clean" : 0,
        "crashed" : 0,
        "creating" : 0,
        "degraded" : 0,
        "down" : 0,
        "inconsistent" : 0,
        "peering" : 0,
        "repair" : 0,
        "replay" : 0,
        "scanning" : 0,
        "scrubbing" : 0,
        "scrubq" : 0,
        "splitting" : 0,
        "stray" : 0,
    }
    for pg in pg_json:
        state = pg["state"]
        slist = string.split(state, "+")
        for s in slist:
            if not s in stateinfo:
                collectd.error("PG %s has unknown state %s" % \
                    (pg["pgid"], s))
            else:
                stateinfo[s] = stateinfo[s] + 1
            register_pg_in_state(curtime, pg["pgid"], s)
    for k,v in stateinfo.items():
        collectd.Values(plugin="cephtool",\
            type=('num_pgs_' + k),\
            values=[v]\
        ).dispatch()
    count_lingering_bad_pgs(curtime)
    count_old_bad_pgs(curtime)

def cephtool_read_osd(osd_json):
    num_in = 0
    num_up = 0
    total = 0
    for osd in osd_json:
        total = total + 1
        if osd["in"] == 1:
            num_in = num_in + 1
        if osd["up"] == 1:
            num_up = num_up + 1
    collectd.Values(plugin="cephtool",\
        type="num_osds_in",\
        values=[num_in],\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type="num_osds_out",\
        values=[total - num_in],\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type="num_osds_up",\
        values=[num_up],\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type="num_osds_down",\
        values=[total - num_up],\
    ).dispatch()

def cephtool_read(data=None):
    osd_json, pg_json, mon_json = cephtool_get_json_sections(3,
                                        ["osd", "dump", "--format=json", ";",
                                        "pg", "dump", "--format=json", ";",
                                        "mon", "dump", "--format=json"])

    collectd.Values(plugin="cephtool",\
        type='num_osds',\
        values=[len(osd_json["osds"])]\
    ).dispatch()
    cephtool_read_osd(osd_json["osds"])

    collectd.Values(plugin="cephtool",\
        type='osd_stats_kb_used',\
        values=[pg_json["osd_stats_sum"]["kb_used"]]\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type='osd_stats_kb_avail',\
        values=[pg_json["osd_stats_sum"]["kb_avail"]]\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type='osd_stats_snap_trim_queue_len',\
        values=[pg_json["osd_stats_sum"]["snap_trim_queue_len"]]\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type='osd_stats_num_snap_trimming',\
        values=[pg_json["osd_stats_sum"]["num_snap_trimming"]]\
    ).dispatch()

    collectd.Values(plugin="cephtool",\
        type='num_pgs',\
        values=[len(pg_json["pg_stats"])]\
    ).dispatch()

    cephtool_read_pg_states(pg_json["pg_stats"])

    collectd.Values(plugin="cephtool",\
        type='num_pools',\
        values=[len(pg_json["pool_stats"])]\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type='num_objects',\
        values=[pg_json["pg_stats_sum"]["stat_sum"]["num_objects"]]\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type='pg_stats_sum_num_bytes',\
        values=[pg_json["pg_stats_sum"]["stat_sum"]["num_bytes"]]\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type='num_objects_missing_on_primary',\
        values=[pg_json["pg_stats_sum"]["stat_sum"]["num_objects_missing_on_primary"]]\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type='num_objects_degraded',\
        values=[pg_json["pg_stats_sum"]["stat_sum"]["num_objects_degraded"]]\
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type='num_objects_unfound',\
        values=[pg_json["pg_stats_sum"]["stat_sum"]["num_objects_unfound"]]\
    ).dispatch()

    collectd.Values(plugin="cephtool",\
        type='num_monitors',\
        values=[len(mon_json["mons"])],
    ).dispatch()
    collectd.Values(plugin="cephtool",\
        type='num_monitors_in_quorum',\
        values=[len(mon_json["quorum"])],
    ).dispatch()

collectd.register_config(cephtool_config)
collectd.register_read(cephtool_read)
