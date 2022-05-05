#!/usr/bin/env python3
"""
debuda parses the build output files and probes the silicon to determine status of a buda run.
"""
STUB_HELP = "This tool requires debuda-stub. You can build debuda-stub with bin/build-debuda-stub.sh. It also requires zeromq (sudo apt install -y libzmq3-dev)."

import yaml, sys, os, struct, argparse, time, traceback, subprocess, signal, re, pickle
import atexit, fnmatch, importlib

from tabulate import tabulate
import zmq # For communication with Buda or debuda stub

# Get path of this script. 'frozen' means packaged with pyinstaller.
def application_path ():
    if getattr(sys, 'frozen', False):
        application_path = os.path.dirname(sys.executable)
    elif __file__:
        application_path = os.path.dirname(__file__)
    return application_path

STREAM_CACHE_FILE_NAME = "dbd_streams_cache.pkl"

parser = argparse.ArgumentParser(description=__doc__ + STUB_HELP)
parser.add_argument('output_dir', type=str, help='Output directory of a buda run')
parser.add_argument('--netlist',  type=str, required=True, help='Netlist file to import')
parser.add_argument('--commands', type=str, required=False, help='Execute a set of commands separated by ;')
parser.add_argument('--stream-cache', action='store_true', default=False, help=f'If file "{STREAM_CACHE_FILE_NAME}" exists, the stream data will be laoded from it. If the file does not exist, it will be crated and populated with the stream data')
parser.add_argument('--debug-debuda-stub', action='store_true', default=False, help=f'Prints all transactions on PCIe. Also, starts debuda-stub with --debug to print transfers.')
args = parser.parse_args()
import pprint
pp = pprint.PrettyPrinter(indent=4)

# Colors
CLR_RED = '\033[31m'
CLR_GREEN = '\033[32m'
CLR_BLUE = '\033[34m'
CLR_ORANGE = '\033[38:2:205:106:0m'
CLR_END = '\033[0m'

CLR_ERR = CLR_RED
CLR_WARN = CLR_ORANGE
CLR_INFO = CLR_BLUE
CLR_PROMPT = CLR_GREEN

# Global variables
EPOCH_TO_PIPEGEN_YAML_MAP={}
EPOCH_TO_BLOB_YAML_MAP={}
GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP={}
EPOCH_ID_TO_CHIP_ID={}
EPOCH_ID_TO_GRAPH_NAME={}
PIPEGEN=None # Points to currently selected entry inside EPOCH_TO_PIPEGEN_YAML_MAP (selected by current_epoch)
BLOB=None    # Points to currently selected entry inside EPOCH_TO_BLOB_YAML_MAP (selected by current_epoch)

ZMQ_SOCKET=None

# When given short=True, function will only print this many entries
SHORT_PRINT_LINE_LIMIT = 10

def reverse_mapping_list(l):
    ret = [0] * len(l)
    for idx, val in enumerate(l):
        ret[val] = idx
    return ret

# From src/firmware/riscv/grayskull/stream_io_map.h
# Kernel operand mapping scheme:
KERNEL_OPERAND_MAPPING_SCHEME = [
    { "id_min" : 0,  "id_max" : 7,  "stream_id_min" : 8, "short" : "input", "long" : "(inputs, unpacker-only) => streams 8-15" },
    { "id_min" : 8,  "id_max" : 15, "stream_id_min" : 16, "short" : "param", "long" : "(params, unpacker-only) => streams 16-23" },
    { "id_min" : 16, "id_max" : 23, "stream_id_min" : 24, "short" : "output", "long" : "(outputs, packer-only) => streams 24-31" },
    { "id_min" : 24, "id_max" : 31, "stream_id_min" : 32, "short" : "intermediate", "long" : "(intermediates, packer/unpacker) => streams 32-39" },
    { "id_min" : 32, "id_max" : 63, "stream_id_min" : 32, "short" : "op-relay", "long" : "(operand relay?) => streams 40-63" }, # CHECK THIS
]

def stream_id_descriptor (stream_id):
    for ko in KERNEL_OPERAND_MAPPING_SCHEME:
        s_id_min = ko["stream_id_min"]
        s_id_count = ko["id_max"] - ko["id_min"]
        if stream_id >= s_id_min and stream_id < s_id_min + s_id_count:
            return ko
    print ("no desc for stream_id=%s" % stream_id)

# FIX: Move this to chip.py in t6py
GS_CHANNEL_TO_DRAM_LOC = [(1, 0), (1, 6), (4, 0), (4, 6), (7, 0), (7, 6), (10, 0), (10, 6)]

GS_PHYS_X_TO_NOC_0_X = [ 0, 12, 1, 11, 2, 10, 3, 9, 4, 8, 5, 7, 6 ]
GS_PHYS_Y_TO_NOC_0_Y = [ 0, 11, 1, 10, 2, 9,  3, 8, 4, 7, 5, 6 ]
GS_PHYS_X_TO_NOC_1_X = [ 12, 0, 11, 1, 10, 2, 9, 3, 8, 4, 7, 5, 6 ]
GS_PHYS_Y_TO_NOC_1_Y = [ 11, 0, 10, 1, 9,  2, 8, 3, 7, 4, 6, 5 ]
GS_NOC_0_X_TO_PHYS_X = reverse_mapping_list (GS_PHYS_X_TO_NOC_0_X)
GS_NOC_0_Y_TO_PHYS_Y = reverse_mapping_list (GS_PHYS_Y_TO_NOC_0_Y)
GS_NOC_1_X_TO_PHYS_X = reverse_mapping_list (GS_PHYS_X_TO_NOC_1_X)
GS_NOC_1_Y_TO_PHYS_Y = reverse_mapping_list (GS_PHYS_Y_TO_NOC_1_Y)

# IDs of NOCs
NOC0 = 0
NOC1 = 1

def GS_physical_to_noc (phys_x, phys_y, noc_id=0):
    if noc_id == 0:
        return (GS_PHYS_X_TO_NOC_0_X[phys_x], GS_PHYS_Y_TO_NOC_0_Y[phys_y])
    else:
        return (GS_PHYS_X_TO_NOC_1_X[phys_x], GS_PHYS_Y_TO_NOC_1_Y[phys_y])

def GS_noc_to_physical (noc_x, noc_y, noc_id=0):
    if noc_id == 0:
        return (GS_NOC_0_X_TO_PHYS_X[noc_x], GS_NOC_0_Y_TO_PHYS_Y[noc_y])
    else:
        return (GS_NOC_1_X_TO_PHYS_X[noc_x], GS_NOC_1_Y_TO_PHYS_Y[noc_y])

# Converts NOC0 to NOC1 coords
def GS_noc0_to_noc1 (noc_x, noc_y):
    phys_x, phys_y = GS_noc_to_physical (noc_x, noc_y, noc_id=0)
    return GS_physical_to_noc (phys_x, phys_y, noc_id=1)

# Converts NOC1 to NOC0 coords
def GS_noc1_to_noc0 (noc_x, noc_y):
    #print (f"noc_x = {noc_x}  noc_y = {noc_y}")
    phys_x, phys_y = GS_noc_to_physical (noc_x, noc_y, noc_id=1)
    return GS_physical_to_noc (phys_x, phys_y, noc_id=0)

# FIX: Check if this is correct
def GS_noc0_to_rc (noc0_x, noc0_y):
    row = noc0_y - 1
    col = noc0_x - 1
    return row, col

def GS_rc_to_noc0 (row, col):
    noc0_y = row + 1
    noc0_x = col + 1
    return noc0_x, noc0_y

# Returns an array of [r,c] pairs for the operation
def get_op_locations (op):
    locations = []
    opr = op['grid_loc'][0]
    opc = op['grid_loc'][1]
    for r in range(op['grid_size'][1]):
        for c in range(op['grid_size'][0]):
            locations.append ( [ opr + r, opc + c ] )
    return locations

# Prints the op name based on the core noc location
def core_to_op_name (graph_name, noc0_x, noc0_y):
    r, c = GS_noc0_to_rc (noc0_x, noc0_y)
    graph = NETLIST["graphs"][graph_name]
    for op_name in graph.keys():
        if op_name not in ['target_device', 'input_count']:
            op = graph[op_name]
            op_locations = get_op_locations(op)
            if [ r, c ] in op_locations:
                return f"{graph_name}/{op_name}:{op['type']}"

def pci_read_xy(chip_id, x, y, z, reg_addr):
    # print (f"Reading {x}-{y} 0x{reg_addr:x}")
    # ZMQ_SOCKET.send(struct.pack ("ccccci", b'\x02', chip_id, x, y, z, reg_addr))
    ZMQ_SOCKET.send(struct.pack ("cccccII", b'\x02', chip_id.to_bytes(1, byteorder='big'), x.to_bytes(1, byteorder='big'), y.to_bytes(1, byteorder='big'), z.to_bytes(1, byteorder='big'), reg_addr, 0))
    ret_val = struct.unpack ("I", ZMQ_SOCKET.recv())[0]
    return ret_val

def pci_read_tile(chip_id, x, y, z, reg_addr, msg_size, data_format):
    # print (f"Reading {x}-{y} 0x{reg_addr:x}")
    # ZMQ_SOCKET.send(struct.pack ("ccccci", b'\x05', chip_id, x, y, z, reg_addr, data_format<<16 + message_size))
    data = data_format * 2**16 + msg_size
    ZMQ_SOCKET.send(struct.pack ("cccccII", b'\x05', chip_id.to_bytes(1, byteorder='big'), x.to_bytes(1, byteorder='big'), y.to_bytes(1, byteorder='big'), z.to_bytes(1, byteorder='big'), reg_addr, data))
    ret = ZMQ_SOCKET.recv()
    return ret

def pci_write_xy(chip_id, x, y, z, reg_addr, data):
    # print (f"Reading {x}-{y} 0x{reg_addr:x}")
    # ZMQ_SOCKET.send(struct.pack ("ccccci", b'\x02', chip_id, x, y, z, reg_addr))
    ZMQ_SOCKET.send(struct.pack ("cccccII", b'\x04', chip_id.to_bytes(1, byteorder='big'), x.to_bytes(1, byteorder='big'), y.to_bytes(1, byteorder='big'), z.to_bytes(1, byteorder='big'), reg_addr, data))
    ret_val = struct.unpack ("I", ZMQ_SOCKET.recv())[0]
    assert data == ret_val

def host_dma_read (dram_addr):
    # print ("host_dma_read 0x%x" % dram_addr)
    ZMQ_SOCKET.send(struct.pack ("cccccI", b'\x03', b'\x00', b'\x00', b'\x00', b'\x00', dram_addr))
    ret_val = struct.unpack ("I", ZMQ_SOCKET.recv())[0]
    return ret_val

def read_print_noc_reg(chip_id, x, y, noc_id, reg_name, reg_index):
    reg_addr = 0xffb20000 + (noc_id*0x10000) + 0x200 + (reg_index*4)
    val = pci_read_xy(chip_id, x, y, 0, reg_addr)
    print(f"Tensix x={x:02d},y={y:02d} => NOC{noc_id:d} {reg_name:s} = 0x{val:08x} ({val:d})")

def get_stream_reg_field(chip_id, x, y, stream_id, reg_index, start_bit, num_bits):
    reg_addr = 0xFFB40000 + (stream_id*0x1000) + (reg_index*4)
    val = pci_read_xy(chip_id, x, y, 0, reg_addr)
    mask = (1 << num_bits) - 1
    val = (val >> start_bit) & mask
    return val

def print_blank_line():
    print ("")

# The field names we want to show as hexadecimal numbers
HEX_FIELDS = {
    "buf_addr",
    "PHASE_AUTO_CFG_PTR (word addr)",
    "NEXT_MSG_ADDR",
    "NEXT_MSG_SIZE",
    "LOCAL_SRC_MASK",
    "BUF_START (word addr)",
    "BUF_SIZE (words)",
    "BUF_RD_PTR (word addr)",
    "BUF_WR_PTR (word addr)",
    "MSG_INFO_PTR (word addr)",
    "MSG_INFO_WR_PTR (word addr)",
    "STREAM_BUF_SPACE_AVAILABLE_REG_INDEX (word addr)",
    "dram_buf_noc_addr"
}

# The field names we want to show as 0-padded hexadecimal numbers
HEX0_FIELDS = { f"DEBUG_STATUS[{i:d}]" for i in range (0,10) }.union ({ f"SCRATCH_REG{i}" for i in range (0,6) })

# Converts field value to string (hex or decimal...)
def get_as_str (fld, val):
    if fld in HEX_FIELDS:
        if fld == "dram_buf_noc_addr":
            return f"{(val>>32) & 0x3f}-{(val>>38) & 0x3f} 0x{val&0xffffffff:x}"
        else:
            return (f"0x{val:x}")
    elif fld in HEX0_FIELDS:
        return (f"0x{val:08x}")
    else:
        return f"{val:d}"

# Populates a dict with register names and current values on core x-y for stream with id 'stream_id'
def read_stream_regs(chip, x, y, stream_id):
    reg = {}
    reg["STREAM_ID"] =                                            get_stream_reg_field(chip, x, y, stream_id, 224+9, 0, 6)
    reg["PHASE_AUTO_CFG_PTR (word addr)"] =                       get_stream_reg_field(chip, x, y, stream_id, 12, 0, 24)
    reg["CURR_PHASE"] =                                           get_stream_reg_field(chip, x, y, stream_id, 11, 0, 20)
    reg["CURR_PHASE_NUM_MSGS_REMAINING"] =                        get_stream_reg_field(chip, x, y, stream_id, 35, 0, 12)
    reg["NUM_MSGS_RECEIVED"] =                                    get_stream_reg_field(chip, x, y, stream_id, 224+5, 0, 16)
    reg["NEXT_MSG_ADDR"] =                                        get_stream_reg_field(chip, x, y, stream_id, 224+6, 0, 16)
    reg["NEXT_MSG_SIZE"] =                                        get_stream_reg_field(chip, x, y, stream_id, 224+6, 16, 16)
    reg["OUTGOING_DATA_NOC"] =                                    get_stream_reg_field(chip, x, y, stream_id, 10, 1, 1)
    local_sources_connected =                                     get_stream_reg_field(chip, x, y, stream_id, 10, 3, 1)
    reg["LOCAL_SOURCES_CONNECTED"] =                              local_sources_connected
    reg["SOURCE_ENDPOINT"] =                                      get_stream_reg_field(chip, x, y, stream_id, 10, 4, 1)
    remote_source =                                               get_stream_reg_field(chip, x, y, stream_id, 10, 5, 1)
    reg["REMOTE_SOURCE"] =                                        remote_source
    reg["RECEIVER_ENDPOINT"] =                                    get_stream_reg_field(chip, x, y, stream_id, 10, 6, 1)
    reg["LOCAL_RECEIVER"] =                                       get_stream_reg_field(chip, x, y, stream_id, 10, 7, 1)
    remote_receiver =                                             get_stream_reg_field(chip, x, y, stream_id, 10, 8, 1)
    reg["REMOTE_RECEIVER"] =                                      remote_receiver
    reg["NEXT_PHASE_SRC_CHANGE"] =                                get_stream_reg_field(chip, x, y, stream_id, 10, 12, 1)
    reg["NEXT_PHASE_DST_CHANGE"] =                                get_stream_reg_field(chip, x, y, stream_id, 10, 13, 1)

    if remote_source == 1:
        reg["INCOMING_DATA_NOC"] =                                get_stream_reg_field(chip, x, y, stream_id, 10, 0, 1)
        reg["REMOTE_SRC_X"] =                                     get_stream_reg_field(chip, x, y, stream_id, 0, 0, 6)
        reg["REMOTE_SRC_Y"] =                                     get_stream_reg_field(chip, x, y, stream_id, 0, 6, 6)
        reg["REMOTE_SRC_STREAM_ID"] =                             get_stream_reg_field(chip, x, y, stream_id, 0, 12, 6)
        reg["REMOTE_SRC_UPDATE_NOC"] =                            get_stream_reg_field(chip, x, y, stream_id, 10, 2, 1)
        reg["REMOTE_SRC_PHASE"] =                                 get_stream_reg_field(chip, x, y, stream_id, 1, 0, 20)
        reg["REMOTE_SRC_DEST_INDEX"] =                            get_stream_reg_field(chip, x, y, stream_id, 0, 18, 6)
        reg["REMOTE_SRC_IS_MCAST"] =                              get_stream_reg_field(chip, x, y, stream_id, 10, 16, 1)

    if remote_receiver == 1:
        reg["OUTGOING_DATA_NOC"] =                                get_stream_reg_field(chip, x, y, stream_id, 10, 1, 1)
        reg["REMOTE_DEST_STREAM_ID"] =                            get_stream_reg_field(chip, x, y, stream_id, 2, 12, 6)
        reg["REMOTE_DEST_X"] =                                    get_stream_reg_field(chip, x, y, stream_id, 2, 0, 6)
        reg["REMOTE_DEST_Y"] =                                    get_stream_reg_field(chip, x, y, stream_id, 2, 6, 6)
        reg["REMOTE_DEST_BUF_START"] =                            get_stream_reg_field(chip, x, y, stream_id, 3, 0, 16)
        reg["REMOTE_DEST_BUF_SIZE"] =                             get_stream_reg_field(chip, x, y, stream_id, 4, 0, 16)
        reg["REMOTE_DEST_BUF_WR_PTR"] =                           get_stream_reg_field(chip, x, y, stream_id, 5, 0, 16)
        reg["REMOTE_DEST_MSG_INFO_WR_PTR"] =                      get_stream_reg_field(chip, x, y, stream_id, 9, 0, 16)
        reg["DEST_DATA_BUF_NO_FLOW_CTRL"] =                       get_stream_reg_field(chip, x, y, stream_id, 10, 15, 1)
        mcast_en =                                                get_stream_reg_field(chip, x, y, stream_id, 13, 12, 1)
        reg["MCAST_EN"] =                                         mcast_en
        if mcast_en == 1:
            reg["MCAST_END_X"] =                                  get_stream_reg_field(chip, x, y, stream_id, 13, 0, 6)
            reg["MCAST_END_Y"] =                                  get_stream_reg_field(chip, x, y, stream_id, 13, 6, 6)
            reg["MCAST_LINKED"] =                                 get_stream_reg_field(chip, x, y, stream_id, 13, 13, 1)
            reg["MCAST_VC"] =                                     get_stream_reg_field(chip, x, y, stream_id, 13, 14, 1)
            reg["MCAST_DEST_NUM"] =                               get_stream_reg_field(chip, x, y, stream_id, 15, 0, 16)

    if local_sources_connected == 1:
        local_src_mask_lo =                                       get_stream_reg_field(chip, x, y, stream_id, 48, 0, 32)
        local_src_mask_hi =                                       get_stream_reg_field(chip, x, y, stream_id, 49, 0, 32)
        local_src_mask =                                          (local_src_mask_hi << 32) | local_src_mask_lo
        reg["LOCAL_SRC_MASK"] =                                   local_src_mask
        reg["MSG_ARB_GROUP_SIZE"] =                               get_stream_reg_field(chip, x, y, stream_id, 13, 16, 3)
        reg["MSG_SRC_IN_ORDER_FWD"] =                             get_stream_reg_field(chip, x, y, stream_id, 13, 19, 1)
        reg["STREAM_MSG_SRC_IN_ORDER_FWD_NUM_MSGS_REG_INDEX"] =   get_stream_reg_field(chip, x, y, stream_id, 14, 0, 24)
    else:
        reg["BUF_START (word addr)"] =                            get_stream_reg_field(chip, x, y, stream_id, 6, 0, 16)
        reg["BUF_SIZE (words)"] =                                 get_stream_reg_field(chip, x, y, stream_id, 7, 0, 16)
        reg["BUF_RD_PTR (word addr)"] =                           get_stream_reg_field(chip, x, y, stream_id, 23, 0, 16)
        reg["BUF_WR_PTR (word addr)"] =                           get_stream_reg_field(chip, x, y, stream_id, 24, 0, 16)
        reg["MSG_INFO_PTR (word addr)"] =                         get_stream_reg_field(chip, x, y, stream_id, 8, 0, 16)
        reg["MSG_INFO_WR_PTR (word addr)"] =                      get_stream_reg_field(chip, x, y, stream_id, 25, 0, 16)
        reg["STREAM_BUF_SPACE_AVAILABLE_REG_INDEX (word addr)"] = get_stream_reg_field(chip, x, y, stream_id, 27, 0, 16)
        reg["DATA_BUF_NO_FLOW_CTRL"] =                            get_stream_reg_field(chip, x, y, stream_id, 10, 14, 1)
        reg["UNICAST_VC_REG"] =                                   get_stream_reg_field(chip, x, y, stream_id, 10, 18, 3)
        reg["REG_UPDATE_VC_REG"] =                                get_stream_reg_field(chip, x, y, stream_id, 10, 21, 3)

    reg["SCRATCH_REG0"] =                                         get_stream_reg_field(chip, x, y, stream_id, 248, 0, 32)
    reg["SCRATCH_REG1"] =                                         get_stream_reg_field(chip, x, y, stream_id, 249, 0, 32)
    reg["SCRATCH_REG2"] =                                         get_stream_reg_field(chip, x, y, stream_id, 250, 0, 32)
    reg["SCRATCH_REG3"] =                                         get_stream_reg_field(chip, x, y, stream_id, 251, 0, 32)
    reg["SCRATCH_REG4"] =                                         get_stream_reg_field(chip, x, y, stream_id, 252, 0, 32)
    reg["SCRATCH_REG5"] =                                         get_stream_reg_field(chip, x, y, stream_id, 253, 0, 32)
    for i in range(0, 10):
        reg[f"DEBUG_STATUS[{i:d}]"] =                             get_stream_reg_field(chip, x, y, stream_id, 224+i, 0, 32)

    return reg

def convert_to_noc_0 (x, y, noc_id):
    if noc_id == 0:
        return (x, y)
    else:
        return (GS_noc1_to_noc0 (x,y))

# Given a dict returned by read_stream_regs, convert to strings (and colorize)
def convert_reg_dict_to_strings(chip, regs, x, y, stream_id):
    string_regs = {}
    for k in regs:
        # Convert to strings
        string_regs[k] = get_as_str (k, regs[k])
        # If on noc-1, convert the coords no noc-1 coords
        if "REMOTE_SRC_UPDATE_NOC" in regs and regs["REMOTE_SRC_UPDATE_NOC"] > 0:
            try:
                if k == "REMOTE_SRC_X":
                    noc0_x, noc0_y = GS_noc1_to_noc0 (regs[k], 0)
                    string_regs[k] += f"({CLR_INFO}{noc0_x}{CLR_END})"
                if k == "REMOTE_SRC_Y":
                    noc0_x, noc0_y = GS_noc1_to_noc0 (0, regs[k])
                    string_regs[k] += f"({CLR_INFO}{noc0_y}{CLR_END})"
            except:
                print (f"{CLR_ERR}Invalid coordinate passed k={k} regs[k]={regs[k]} {CLR_END}")

    return string_regs

# Returns a summary of non-idle streams within a core
def get_core_stream_summary (chip, x, y):
    all_streams_summary = {}
    navigation_suggestions = [ ]
    for stream_id in range (0, 64):
        val = ""
        # Check if idle
        regs = read_stream_regs (chip, x, y, stream_id)
        reg_strings = convert_reg_dict_to_strings(chip, regs, x, y, stream_id)
        idle = is_stream_idle (regs)

        # Construct the navigation suggestions, and stream idle status
        if regs["REMOTE_SOURCE"] !=0 and 'REMOTE_SRC_X' in regs:
            val += f"RS-{reg_strings['REMOTE_SRC_X']}-{reg_strings['REMOTE_SRC_Y']}-{reg_strings['REMOTE_SRC_STREAM_ID']} "
            noc0_x, noc0_y = convert_to_noc_0 (regs['REMOTE_SRC_X'], regs['REMOTE_SRC_Y'], regs['REMOTE_SRC_UPDATE_NOC'])
            navigation_suggestions.append (\
                { 'stream_id' : stream_id, 'type' : 'src', "noc0_x" : noc0_x, "noc0_y" : noc0_y, \
                'cmd' : f"s {noc0_x} {noc0_y} {reg_strings['REMOTE_SRC_STREAM_ID']}" })
        if regs["REMOTE_RECEIVER"] !=0 and 'REMOTE_DEST_X' in regs:
            val += f"RR-{reg_strings['REMOTE_DEST_X']}-{reg_strings['REMOTE_DEST_Y']}-{reg_strings['REMOTE_DEST_STREAM_ID']} "
            noc0_x, noc0_y = convert_to_noc_0 (regs['REMOTE_DEST_X'], regs['REMOTE_DEST_Y'], regs['OUTGOING_DATA_NOC'])
            navigation_suggestions.append (\
                { 'stream_id' : stream_id, 'type' : 'dest', "noc0_x" : noc0_x, "noc0_y" : noc0_y, \
                'cmd' : f"s {noc0_x} {noc0_y} {reg_strings['REMOTE_DEST_STREAM_ID']}" })
        if regs["LOCAL_SOURCES_CONNECTED"]!=0:
            val += "LSC "

        if not idle:
            all_streams_summary[f"{stream_id}"] = val
        else:
            val += "idle"
    return all_streams_summary, navigation_suggestions

# For all cores given by _coords, read all 64 streams and populate the 'streams' dict
# streams[x][y][stream_id] will contain a dictionary of all register values as strings formatted to show in UI
def get_all_streams_ui_data (chip, x_coords, y_coords):
    # Use cache
    if os.path.exists (STREAM_CACHE_FILE_NAME) and args.stream_cache:
        print (f"{CLR_WARN}Loading streams from file {STREAM_CACHE_FILE_NAME}{CLR_END}")
        with open(STREAM_CACHE_FILE_NAME, 'rb') as f:
            streams = pickle.load(f)
            return streams

    streams = {}
    for x in x_coords:
        streams[x] = {}
        for y in y_coords:
            streams[x][y] = {}
            for stream_id in range (0, 64):
                regs = read_stream_regs (chip, x, y, stream_id)
                streams[x][y][stream_id] = convert_reg_dict_to_strings(chip, regs, x, y, stream_id)

    if args.stream_cache:
        print (f"{CLR_WARN}Saving streams to file {STREAM_CACHE_FILE_NAME}{CLR_END}")
        with open(STREAM_CACHE_FILE_NAME, 'wb') as f:
            pickle.dump(streams, f)

    return streams

def full_dump_xy(chip_id, x, y):
    for stream_id in range (0, 64):
        print_blank_line()
        stream = read_stream_regs(chip_id, x, y, stream_id)
        for reg, value in stream.items():
            print(f"Tensix x={x:02d},y={y:02d} => stream {stream_id:02d} {reg} = {value}")

    for noc_id in range (0, 2):
        print_blank_line()
        read_print_noc_reg(chip_id, x, y, noc_id, "nonposted write reqs sent", 0xA)
        read_print_noc_reg(chip_id, x, y, noc_id, "posted write reqs sent", 0xB)
        read_print_noc_reg(chip_id, x, y, noc_id, "nonposted write words sent", 0x8)
        read_print_noc_reg(chip_id, x, y, noc_id, "posted write words sent", 0x9)
        read_print_noc_reg(chip_id, x, y, noc_id, "write acks received", 0x1)
        read_print_noc_reg(chip_id, x, y, noc_id, "read reqs sent", 0x5)
        read_print_noc_reg(chip_id, x, y, noc_id, "read words received", 0x3)
        read_print_noc_reg(chip_id, x, y, noc_id, "read resps received", 0x2)
        print_blank_line()
        read_print_noc_reg(chip_id, x, y, noc_id, "nonposted write reqs received", 0x1A)
        read_print_noc_reg(chip_id, x, y, noc_id, "posted write reqs received", 0x1B)
        read_print_noc_reg(chip_id, x, y, noc_id, "nonposted write words received", 0x18)
        read_print_noc_reg(chip_id, x, y, noc_id, "posted write words received", 0x19)
        read_print_noc_reg(chip_id, x, y, noc_id, "write acks sent", 0x10)
        read_print_noc_reg(chip_id, x, y, noc_id, "read reqs received", 0x15)
        read_print_noc_reg(chip_id, x, y, noc_id, "read words sent", 0x13)
        read_print_noc_reg(chip_id, x, y, noc_id, "read resps sent", 0x12)
        print_blank_line()
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x out vc full credit out vc stall", 0x24)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y out vc full credit out vc stall", 0x22)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port niu out vc full credit out vc stall", 0x20)
        print_blank_line()
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC14 & VC15 dbg", 0x3d)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC12 & VC13 dbg", 0x3c)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC10 & VC11 dbg", 0x3b)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC8 & VC9 dbg", 0x3a)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC6 & VC7 dbg", 0x39)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC4 & VC5 dbg", 0x38)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC2 & VC3 dbg", 0x37)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC0 & VC1 dbg", 0x36)
        print_blank_line()
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC14 & VC15 dbg", 0x35)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC12 & VC13 dbg", 0x34)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC10 & VC11 dbg", 0x33)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC8 & VC9 dbg", 0x32)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC6 & VC7 dbg", 0x31)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC4 & VC5 dbg", 0x30)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC2 & VC3 dbg", 0x2f)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC0 & VC1 dbg", 0x2e)
        print_blank_line()
        read_print_noc_reg(chip_id, x, y, noc_id, "router port niu VC6 & VC7 dbg", 0x29)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port niu VC4 & VC5 dbg", 0x28)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port niu VC2 & VC3 dbg", 0x27)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port niu VC0 & VC1 dbg", 0x26)

    en = 1
    rd_sel = 0
    pc_mask = 0x7fffffff
    daisy_sel = 7

    sig_sel = 0xff
    rd_sel = 0
    pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    test_val1 = pci_read_xy(chip_id, x, y, 0, 0xffb1205c)
    rd_sel = 1
    pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    test_val2 = pci_read_xy(chip_id, x, y, 0, 0xffb1205c)

    rd_sel = 0
    sig_sel = 2*9
    pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    brisc_pc = pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    # Doesn't work - looks like a bug for selecting inputs > 31 in daisy stop
    # rd_sel = 0
    # sig_sel = 2*16
    # pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    # nrisc_pc = pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    rd_sel = 0
    sig_sel = 2*10
    pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    trisc0_pc = pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    rd_sel = 0
    sig_sel = 2*11
    pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    trisc1_pc = pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    rd_sel = 0
    sig_sel = 2*12
    pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    trisc2_pc = pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    # IH: Commented out to reduce chatter
    print_blank_line()
    print(f"Tensix x={x:02d},y={y:02d} => dbus_test_val1 (expect 7)={test_val1:x}, dbus_test_val2 (expect A5A5A5A5)={test_val2:x}")
    print(f"Tensix x={x:02d},y={y:02d} => brisc_pc=0x{brisc_pc:x}, trisc0_pc=0x{trisc0_pc:x}, trisc1_pc=0x{trisc1_pc:x}, trisc2_pc=0x{trisc2_pc:x}")

    pci_write_xy(chip_id, x, y, 0, 0xffb12054, 0)

def blob_data_to_string (blb_data):
    ret_val = {}
    for k in blb_data:
        try:
            ret_val[k] = get_as_str(k, blb_data[k])
        except:
            ret_val[k] = blb_data[k] # get_as_str(k, blb_data[k])
    return ret_val

# Given a stream, return the stream's data as recorded in blob.yaml
def get_streams_from_blob (chip, x, y, id):
    stream_name = f"chip_{chip}__y_{y}__x_{x}__stream_id_{id}"
#    print (f"get_stream of {stream_name}")
    ret_val = [ ]
    for k in BLOB:
        v = BLOB[k]
        if stream_name in v:
            # print (f"v[stream_name]: {v[stream_name]}")
            if 0 in v[stream_name]: # Hack: dram lists
                v_data = blob_data_to_string(v[stream_name][0])
            else:
                v_data = blob_data_to_string(v[stream_name])
            # print (f"APPENDING: {v_data}")
            ret_val.append (v_data)
            lastidx = len(ret_val)-1
            # Attach a source field
            ret_val[lastidx][f"source"] = f'{CLR_INFO}{k}{CLR_END}'

    return ret_val

# Converts a shallow dict to a table. A table is an array that can be consumed by tabulate.py
def dict_to_table (dct):
    if dct:
        table = [ [k, dct[k]] for k in dct ]
    else:
        table = [ [ "", "" ] ]
    return table

# Given two tables 'a' and 'b' merge them into a wider table
def merge_tables_side_by_side (a, b):
    width_a = len(a[0])
    width_b = len(b[0])
    t = [ ]
    for i in range (max (len(a), len(b))):
        row = [ None ] * (width_a + width_b)

        for j in range (width_a):
            row [j] = "" if i >= len(a) else a[i][j]

        for j in range (width_b):
            row [j + width_a] = "" if i >= len(b) else b[i][j]

        t.append (row)
    return t

# Given an array of dicts, and their titles. Print a flattened version of all the dicts as a big table.
def print_columnar_dicts (dict_array, title_array):
    final_table = [ ]
    for idx, dct in enumerate(dict_array):
        assert isinstance(dct, dict)
        current_table = dict_to_table(dct)
        if idx == 0:
            final_table = current_table
        else:
            final_table = merge_tables_side_by_side (final_table, current_table)

    titles = [ ]
    for t in title_array:
        titles += [ t ]
        titles += [ "" ]

    print (tabulate(final_table, headers=titles))

#
# Analysis functions
#

# This is shown in the 'Non-idle streams' column in stream view
def is_stream_idle(stream_data):
    return (stream_data["DEBUG_STATUS[7]"] & 0xfff) == 0xc00
# Used to show "Not idle" in stream_summary
def is_stream_active (stream_data):
    return int (stream_data["CURR_PHASE"]) != 0 and int (stream_data["NUM_MSGS_RECEIVED"]) > 0
# Used in stream_summary
def is_bad_stream (stream_data):
    return \
        (int (stream_data["DEBUG_STATUS[1]"], base=16) != 0) or \
        (int (stream_data["DEBUG_STATUS[2]"], base=16) & 0x7) == 0x4 or \
        (int (stream_data["DEBUG_STATUS[2]"], base=16) & 0x7) == 0x2
# Used in stream_summary
def is_gsync_hung (chip, x, y):
    return pci_read_xy(chip, x, y, 0, 0xffb2010c) == 0xB0010000
# Used in stream_summary
def is_ncrisc_done (chip, x, y):
    return pci_read_xy(chip, x, y, 0, 0xffb2010c) == 0x1FFFFFF1

def stream_summary(chip, x_coords, y_coords, streams, short=False):
    active_streams = {}
    bad_streams = []
    gsync_hung = {}
    ncrisc_done = {}

    # Detect problems
    for x in x_coords:
        active_streams[x] = {}
        gsync_hung[x] = {}
        ncrisc_done[x] = {}
        for y in y_coords:
            active_streams[x][y] = []
            for stream_id in range (0, 64):
                if is_stream_active(streams[x][y][stream_id]):
                    active_streams[x][y].append(stream_id)
                if is_bad_stream(streams[x][y][stream_id]):
                    bad_streams.append([x,y,stream_id])
            gsync_hung[x][y] = is_gsync_hung(chip, x, y)
            ncrisc_done[x][y] = is_ncrisc_done(chip, x, y)

    # Print streams that are not idle
    all_streams_done = True
    headers = [ "X-Y", "Op", "Stream", "Type", "Epoch", "Phase", "State", "CURR_PHASE_NUM_MSGS_REMAINING", "NUM_MSGS_RECEIVED" ]
    rows = []

    num_entries_to_show_remaining = SHORT_PRINT_LINE_LIMIT
    for x in x_coords:
        for y in y_coords:
            if len(active_streams[x][y]) != 0:
                first_stream = True

                for i in range(len(active_streams[x][y])):
                    xy = f"{x}-{y}" if first_stream else ""
                    first_stream = False
                    stream_id=active_streams[x][y][i]
                    current_phase = int(streams[x][y][stream_id]['CURR_PHASE'])
                    epoch_id = current_phase>>10
                    stream_type = stream_id_descriptor(stream_id)["short"]
                    op = core_to_op_name(EPOCH_ID_TO_GRAPH_NAME[epoch_id], x, y)
                    row = [ xy, op, stream_id, stream_type, epoch_id, current_phase, f"{CLR_WARN}Active{CLR_END}", int(streams[x][y][stream_id]['CURR_PHASE_NUM_MSGS_REMAINING']), int(streams[x][y][stream_id]['NUM_MSGS_RECEIVED']) ]

                    num_entries_to_show_remaining -= 1
                    if short and num_entries_to_show_remaining == 0:
                        for i in range (len(row)):
                            row[i] = "..."
                        rows.append (row)
                        break
                    if short and num_entries_to_show_remaining < 0:
                        break

                    rows.append (row)
                    all_streams_done = False

    if not all_streams_done:
        print (tabulate(rows, headers=headers))
    if all_streams_done:
        print("  No streams appear hung. If the test hung, some of the streams possibly did not get any tiles.")

    # Print streams in bad state
    if len(bad_streams) != 0:
        num_entries_to_show_remaining = SHORT_PRINT_LINE_LIMIT
        print_blank_line()
        print("The following streams are in a bad state (have an assertion in DEBUG_STATUS[1], or DEBUG_STATUS[2] indicates a hang):")
        for i in range(len(bad_streams)):
            bad_stream_x = bad_streams[i][0]
            bad_stream_y = bad_streams[i][1]
            bad_stream_id = bad_streams[i][2]
            print(f"\t x={bad_stream_x:02d}, y={bad_stream_y:02d}, stream_id={bad_stream_id:02d}")
            if short and num_entries_to_show_remaining == 0:
                for i in range (len(row)):
                    print ("...")
            if short and num_entries_to_show_remaining < 0:
                break
            num_entries_to_show_remaining -= 1


    # Print gsync_hung cores
    for x in x_coords:
        for y in y_coords:
            if gsync_hung[x][y]:
                print(f"Global sync hang: x={x:02d}, y={y:02d}")

    # Print NC Riscs that are not idle
    if all_streams_done: # Only do this if all streams are done
        ncriscs_not_idle_count = 0
        for y in y_coords:
            for x in x_coords:
                if not ncrisc_done[x][y]:
                    if ncriscs_not_idle_count == 0: # First output
                        print("NCRISCs not idle: ")
                    ncriscs_not_idle_count += 1
                    print(f"{x:02d}-{y:02d}", end=" ")
                    if ncriscs_not_idle_count % 12 == 0:
                        print_blank_line()
        if ncriscs_not_idle_count > 0:
            print_blank_line()

# Prints a single stream
def print_stream (current_chip, x, y, stream_id, current_epoch_id):
    regs = read_stream_regs (current_chip, x, y, stream_id)
    stream_regs = convert_reg_dict_to_strings(current_chip, regs, x, y, stream_id)
    streams_from_blob = get_streams_from_blob (current_chip, x, y, stream_id)
    stream_epoch_id = (regs["CURR_PHASE"] >> 10)
    current_epoch_id = stream_epoch_id

    all_stream_summary, navigation_suggestions = get_core_stream_summary (current_chip, x, y)
    data_columns = [ all_stream_summary ] if len(all_stream_summary) > 0 else []
    title_columns = [ f"{CLR_WARN}Non-idle streams{CLR_END}" ] if len(all_stream_summary) > 0 else []

    data_columns.append (stream_regs)
    title_columns.append ("Registers")

    # 1. Append blobs
    buffer_id_strings = set()
    non_active_phases = dict()
    for stream_from_blob in streams_from_blob:
        buf_id = stream_from_blob["buf_id"] if stream_from_blob and "buf_id" in stream_from_blob else None
        if f"{regs['CURR_PHASE']}" in stream_from_blob["source"]:
            if buf_id is not None:
                buffer_str = f"buffer_{buf_id}"
                buffer_id_strings.add (buffer_str)
            data_columns.append (stream_from_blob)
            title_columns.append ("Stream (blob.yaml)")
        else:
            non_active_phases[stream_from_blob["source"]] = "-"

    # 1a. Print Non Active phases, if any
    if len(non_active_phases) > 0:
        title_columns.append ("non-active phases")
        data_columns.append (non_active_phases)

    # 2. Append buffers
    for buffer_id_string in buffer_id_strings:
        data_columns.append (PIPEGEN[buffer_id_string] if buffer_id_string in PIPEGEN else { "-": "-" })
        title_columns.append (buffer_id_string)

    # 3. Append relevant pipes
    for buffer_id_string in buffer_id_strings:
        buffer_id = int (buffer_id_string[7:], 0) # HACK: to skip the "buffer_" string
        # FIX: below is mostly copied from print_buffer_data()
        for epoch_id in EPOCH_TO_PIPEGEN_YAML_MAP:
            for dct in EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id]:
                d = EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id][dct]
                if ("input_list" in d and buffer_id in d["input_list"]) or ("output_list" in d and buffer_id in d["output_list"]):
                    data_columns.append (d)
                    title_columns.append ("Pipe")

    print_columnar_dicts (data_columns, title_columns)

    if current_epoch_id != stream_epoch_id:
        print (f"{CLR_WARN}Current epoch is {current_epoch_id}, while the stream is in epoch {stream_epoch_id} {CLR_END}. To switch to epoch {stream_epoch_id}, enter {CLR_PROMPT}e {stream_epoch_id}{CLR_END}")

    # 4. TODO: Print forks

    return navigation_suggestions, stream_epoch_id


# Find occurrences of buffer with ID 'buffer_id' across all epochs, and print the structures that reference them
# Supply current_epoch_id=None, to show details in all epochs
def print_buffer_data (buffer_id, current_epoch_id = None):
    for epoch_id in EPOCH_TO_PIPEGEN_YAML_MAP:
        for dct in EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id]:
            d = EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id][dct]
            if ("input_list" in d and buffer_id in d["input_list"]) or ("output_list" in d and buffer_id in d["output_list"]) or ("buffer" in dct and "uniqid" in d and buffer_id == d["uniqid"]):
                if current_epoch_id is None or current_epoch_id == epoch_id:
                    print_columnar_dicts ([d], [f"{CLR_INFO}Epoch {epoch_id} - {dct}{CLR_END}"])
                else:
                    print (f"Buffer is also used in epoch {epoch_id}. Details suppressed.")

# Find occurrences of pipe with ID 'pipe_id' across all epochs, and print the structures that reference them
# Supply current_epoch_id=None, to show details in all epochs
def print_pipe_data (pipe_id, current_epoch_id = None):
    for epoch_id in EPOCH_TO_PIPEGEN_YAML_MAP:
        for dct in EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id]:
            d = EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id][dct]
            if ("pipe" in dct and "id" in d and pipe_id == d["id"]):
                if current_epoch_id is None or current_epoch_id == epoch_id:
                    print_columnar_dicts ([d], [f"{CLR_INFO}Epoch {epoch_id} - {dct}{CLR_END}"])
                else:
                    print (f"Pipe is also used in epoch {epoch_id}. Details suppressed.")

    for epoch_id in EPOCH_TO_BLOB_YAML_MAP:
        for dram_blob_or_phase in EPOCH_TO_BLOB_YAML_MAP[epoch_id]:
            dct = EPOCH_TO_BLOB_YAML_MAP[epoch_id][dram_blob_or_phase]
            for strm in dct:
                if dram_blob_or_phase == "dram_blob":
                    for i in strm:
                        pass # No pipe info in dram_blobs at the moment
                else:
                    if "pipe_id" in dct[strm] and dct[strm]["pipe_id"] == pipe_id:
                        if current_epoch_id is None or current_epoch_id == epoch_id:
                            print_columnar_dicts ([dct[strm]], [f"{CLR_INFO}Epoch {epoch_id} - BLOB - {strm}{CLR_END}"])
                        else:
                            print (f"Pipe is also used in epoch {epoch_id}. Details suppressed.")

def print_dram_queue_summary_for_graph (graph, chip_array):
    epoch_id = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph]["epoch_id"]
    chip_id = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph]["target_device"]
    chip = chip_array[chip_id]

    PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id]

    print (f"{CLR_INFO}DRAM queues for epoch %d{CLR_END}" % epoch_id)

    table = []
    for b in PIPEGEN:
        if "buffer" in b:
            buffer=PIPEGEN[b]
            if buffer["dram_buf_flag"] != 0 or buffer["dram_io_flag"] != 0 and buffer["dram_io_flag_is_remote"] == 0:
                dram_chan = buffer["dram_chan"]
                dram_addr = buffer['dram_addr']
                dram_loc = GS_CHANNEL_TO_DRAM_LOC[dram_chan]
                rdptr = pci_read_xy (chip, dram_loc[0], dram_loc[1], 0, dram_addr)
                wrptr = pci_read_xy (chip, dram_loc[0], dram_loc[1], 0, dram_addr + 4)
                slot_size_bytes = buffer["size_tiles"] * buffer["tile_size"]
                queue_size_bytes = slot_size_bytes * buffer["q_slots"]
                occupancy = (wrptr - rdptr) if wrptr >= rdptr else wrptr - (rdptr - buffer["q_slots"])
                table.append ([ b, buffer["dram_buf_flag"], buffer["dram_io_flag"], dram_chan, f"0x{dram_addr:x}", f"{rdptr}", f"{wrptr}", occupancy, buffer["q_slots"], queue_size_bytes ])

    print (tabulate(table, headers=["Buffer name", "dram_buf_flag", "dram_io_flag", "Channel", "Address", "RD ptr", "WR ptr", "Occupancy", "Q slots", "Q Size [bytes]"] ))

# Prints the queues residing in host's memory.
def print_host_queue_for_graph (graph):
    epoch_id = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph]["epoch_id"]
    chip_id = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph]["target_device"]

    PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id]

    table = []
    for b in PIPEGEN:
        if "buffer" in b:
            buffer=PIPEGEN[b]
            if buffer["dram_io_flag_is_remote"] != 0:
                # dram_chan = buffer["dram_chan"]
                dram_addr = buffer['dram_addr']
                if dram_addr >> 29 == chip_id:
                    # print (f"{CLR_WARN}Found host queue %s{CLR_END}" % pp.pformat(buffer))
                    rdptr = host_dma_read (dram_addr)
                    wrptr = host_dma_read (dram_addr + 4)
                    slot_size_bytes = buffer["size_tiles"] * buffer["tile_size"]
                    queue_size_bytes = slot_size_bytes * buffer["q_slots"]
                    occupancy = (wrptr - rdptr) if wrptr >= rdptr else wrptr - (rdptr - buffer["q_slots"])
                    table.append ([ b, buffer["dram_buf_flag"], buffer["dram_io_flag"], f"0x{dram_addr:x}", f"{rdptr}", f"{wrptr}", occupancy, buffer["q_slots"], queue_size_bytes ])

    print (f"{CLR_INFO}Host queues (where dram_io_flag_is_remote!=0) for epoch %d {CLR_END}" % epoch_id)
    if len(table) > 0:
        print (tabulate(table, headers=["Buffer name", "dram_buf_flag", "dram_io_flag", "Address", "RD ptr", "WR ptr", "Occupancy", "Q slots", "Q Size [bytes]"] ))
    else:
        print ("No host queues found")

# Prints epoch queues
def print_epoch_queue_summary (chip_array, x_coords, y_coords):
    dram_chan = 0 # This queue is always in channel 0
    dram_loc = GS_CHANNEL_TO_DRAM_LOC[dram_chan]

    # From tt_epoch_dram_manager::tt_epoch_dram_manager and following the constants
    GridSizeRow = 16
    GridSizeCol = 16
    EPOCH_Q_NUM_SLOTS = 32
    epoch0_start_table_size_bytes = GridSizeRow*GridSizeCol*(EPOCH_Q_NUM_SLOTS*2+8)*4
    DRAM_CHANNEL_CAPACITY_BYTES  = 1024 * 1024 * 1024
    DRAM_PERF_SCRATCH_SIZE_BYTES =   40 * 1024 * 1024
    DRAM_HOST_MMIO_SIZE_BYTES    =  256 * 1024 * 1024
    reserved_size_bytes = DRAM_PERF_SCRATCH_SIZE_BYTES - epoch0_start_table_size_bytes

    chip_id = 0
    for chip in chip_array:
        table = []
        print (f"{CLR_INFO}Epoch queues for device %d{CLR_END}" % chip_id)
        chip_id += 1
        for x in y_coords:
            for y in x_coords:
                EPOCH_QUEUE_START_ADDR = reserved_size_bytes
                offset = (16 * x + y) * ((EPOCH_Q_NUM_SLOTS*2+8)*4)
                dram_addr = EPOCH_QUEUE_START_ADDR + offset
                rdptr = pci_read_xy (chip, dram_loc[0], dram_loc[1], 0, dram_addr)
                wrptr = pci_read_xy (chip, dram_loc[0], dram_loc[1], 0, dram_addr + 4)
                occupancy = (wrptr - rdptr) if wrptr >= rdptr else wrptr - (rdptr - EPOCH_Q_NUM_SLOTS)
                if occupancy > 0:
                    table.append ([ f"{x}-{y}", f"0x{dram_addr:x}", f"{rdptr}", f"{wrptr}", occupancy ])
    if len(table) > 0:
        print (tabulate(table, headers=["Location", "Address", "RD ptr", "WR ptr", "Occupancy" ] ))
    else:
        print ("No epoch queues have occupancy > 0")

def print_a_read (x, y, addr, val, comment=""):
    print(f"{x}-{y} 0x{addr:08x} => 0x{val:08x} ({val:d}) {comment}")

def burst_read_xy (chip, x, y, noc_id, addr, burst_type = 1):
    if burst_type == 1:
        values = {}
        t_end = time.time() + 1
        print ("Sampling for 1 second...")
        while time.time() < t_end:
            val = pci_read_xy(chip, x, y, noc_id, addr)
            if val not in values:
                values[val] = 0
            values[val] += 1
        for val in values.keys():
            print_a_read(x, y, addr, val, f"- {values[val]} times")
    elif burst_type >= 2:
        for k in range(0, burst_type):
            val = pci_read_xy(chip, x, y, noc_id, addr + 4*k)
            print_a_read(x,y,addr + 4*k, val)

def print_available_commands (commands):
    rows = []
    for c in commands:
        desc = c['arguments_description'].split(':')
        row = [ f"{CLR_INFO}{c['short']}{CLR_END}", f"{CLR_INFO}{c['long']}{CLR_END}", f"{desc[0]}", f"{desc[1]}" ]
        rows.append(row)
    print (tabulate(rows, headers=[ "Short", "Long", "Arguments", "Description" ]))

def print_suggestions (graph_name, navigation_suggestions, current_stream_id):
    if navigation_suggestions:
        print ("Speed dial:")
        rows = []
        for i in range (len(navigation_suggestions)):
            stream_id = navigation_suggestions[i]['stream_id']
            clr = CLR_INFO if current_stream_id == stream_id else CLR_END
            row = [ f"{clr}{i}{CLR_END}", \
                f"{clr}Go to {navigation_suggestions[i]['type']} of stream {navigation_suggestions[i]['stream_id']}{CLR_END}", \
                f"{clr}{navigation_suggestions[i]['cmd']}{CLR_END}", \
                f"{clr}{core_to_op_name(graph_name, navigation_suggestions[i]['noc0_x'], navigation_suggestions[i]['noc0_y'])}{CLR_END}"
                ]
            rows.append (row)
        print(tabulate(rows, headers=[ "#", "Description", "Command", "Op name" ]))

def print_stream_summary (chip_array):
    # Finally check and print stream data
    for i, chip in enumerate (chip_array):
        print (f"{CLR_INFO}Reading and analyzing streams on device %d...{CLR_END}" % i)
        streams_ui_data = get_all_streams_ui_data (chip, GS_x_coords, GS_y_coords)
        stream_summary(chip, GS_x_coords, GS_y_coords, streams_ui_data)

def init_files (args):
    # Get paths to Pipegen and Blob YAML files for the Current epoch
    epoch = 0
    global EPOCH_TO_PIPEGEN_YAML_MAP   # This refers to a single pipegen.yaml file
    global EPOCH_TO_BLOB_YAML_MAP      # This refers to a single blob.yaml file
    global PIPEGEN   # This refers to a single pipegen.yaml file
    global BLOB      # This refers to a single blob.yaml file
    global NETLIST   # netlist yaml

    # Load netlist file
    print (f"Loading {args.netlist}")
    NETLIST = yaml.safe_load(open(args.netlist))

    # Load graph to epoch map
    global GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP
    try:
        graph_to_epoch_filename = f"{args.output_dir}/graph_to_epoch_map.yaml"
        print (f"Loading {graph_to_epoch_filename}")
        GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP = yaml.safe_load(open(graph_to_epoch_filename))
    except:
        print (f"{CLR_ERR}Error: cannot open graph_to_epoch_map.yaml {CLR_END}")
        sys.exit(1)

    # Cache epoch id to chip id
    global EPOCH_ID_TO_CHIP_ID
    global EPOCH_ID_TO_GRAPH_NAME
    for graph_name in GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP:
        epoch_id = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph_name]["epoch_id"]
        target_device = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph_name]["target_device"]
        EPOCH_ID_TO_CHIP_ID[epoch_id] = target_device
        EPOCH_ID_TO_GRAPH_NAME[epoch_id] = graph_name

    # Load BLOB and PIPEGEN data
    for graph in NETLIST["graphs"]:
        epoch_id = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph]["epoch_id"]
        GRAPH_DIR=f"{args.output_dir}/temporal_epoch_{epoch_id}"
        if not os.path.isdir(GRAPH_DIR):
            print (f"{CLR_ERR}Error: cannot find directory {GRAPH_DIR} {CLR_END}")
            sys.exit(1)
        PIPEGEN_FILE=f"{GRAPH_DIR}/overlay/pipegen.yaml"
        BLOB_FILE=f"{GRAPH_DIR}/overlay/blob.yaml"

        # Pipegen file contains multiple documents (separated by ---).
        # We merge them all into one map.
        print (f"Loading {PIPEGEN_FILE}")
        pipegen_yaml = {}
        for i in yaml.safe_load_all(open(PIPEGEN_FILE)):
            pipegen_yaml = { **pipegen_yaml, **i }
        EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id] = pipegen_yaml

        print (f"Loading {BLOB_FILE}")
        EPOCH_TO_BLOB_YAML_MAP[epoch_id] = yaml.safe_load(open(BLOB_FILE))


# PSeudo code FOR TRAVERSAL
# def list_inputs (core_x, core_y):
#     for each stream:
#         if stream has in blob.yaml entry with input_index field exists AND not intermediary
#             then this is an input stream
#                 then use buffer_id or
#                 use REMOTE_SRC_X to figure out where the source is

# # We also have forks (fork_stream_ids - list of streams in the core (local))
# def list_output (core_x, core_y):
#     for each stream:
#         if stream has in blob.yaml entry with output_index field exists AND not intermediary
#             then this is an output stream
#                 then use buffer_id or (use REMOTE_DEST_X to figure out where the source is)

#         if stream has fork_stream_ids:
#             then also consider the stream ids in that list, but don't check for output_index

def find_buffer (buffer_id):
    for b in PIPEGEN:
        if "buffer" in b:
            buffer=PIPEGEN[b]
            if buffer["uniqid"] == buffer_id:
                return buffer
    return None

# Return noc0_X, noc0_Y given stream_full_name from blob.yaml
def get_stream_tuple_from_full_name (stream_full_name):
    # Example full name: chip_0__y_1__x_1__stream_id_8
    vals = re.findall(r'chip_(\d)+__y_(\d)+__x_(\d)+__stream_id_(\d)+', stream_full_name)
    return [ vals[0][2], vals[0][1], vals[0][3] ]

def find_stream_for_buffer_id (buffer_id):
    for phase in BLOB:
        for stream_full_name, stream_data in BLOB[phase].items():
            if "buf_id" in stream_data and stream_data["buf_id"] == buffer_id:
                return get_stream_tuple_from_full_name (stream_full_name)
    return None

def get_connected_buffers (buffer_id_list, connection="outputs"):
    if type(buffer_id_list) != list: buffer_id_list = [ buffer_id_list ] # If not a list, assume a single buffer id, and create a list from it

    dest_buffers = [ ]

    for p in PIPEGEN:
        if "pipe" in p:
            for buffer_id in buffer_id_list:
                if "output" in connection and buffer_id in PIPEGEN[p]["input_list"]:
                    dest_buffers += PIPEGEN[p]["output_list"]
                if "input" in connection and buffer_id in PIPEGEN[p]["output_list"]:
                    dest_buffers += PIPEGEN[p]["input_list"]
    return dest_buffers

def get_buff_core_coordinates_rc (buffer_id_list):
    if type(buffer_id_list) != list: buffer_id_list = [ buffer_id_list ] # If not a list, assume a single buffer id, and create a list from it
    buff_cores = set()
    for b in PIPEGEN:
        if "buffer" in b:
            if PIPEGEN[b]["uniqid"] in buffer_id_list:
                buff_cores.add (tuple(PIPEGEN[b]["core_coordinates"]))
    return list (buff_cores)

def print_buffer_info (buffer_id_list):
    if type(buffer_id_list) != list: buffer_id_list = [ buffer_id_list ] # If not a list, assume a single buffer id, and create a list from it
    for bid in buffer_id_list:
        b = find_buffer (bid)
        print (f"Buffer {bid} - {b['md_op_name']} rc:{b['core_coordinates']}")

def get_core_buffers (core_coordinates_list_rc):
    if type(core_coordinates_list_rc) != list: core_coordinates_list_rc = [ core_coordinates_list_rc ] # If not a list, assume a single buffer id, and create a list from it

    buffer_set = set()
    for b in PIPEGEN:
        if "buffer" in b:
            if tuple(PIPEGEN[b]["core_coordinates"]) in core_coordinates_list_rc:
                buffer_set.add (PIPEGEN[b]["uniqid"])
    return list(buffer_set)

def is_input_buffer(bid):
    for p in PIPEGEN:
        if "pipe" in p:
            if bid in PIPEGEN[p]["input_list"]: return True
    return False

def is_output_buffer(bid):
    for p in PIPEGEN:
        if "pipe" in p:
            if bid in PIPEGEN[p]["output_list"]: return True
    return False

def filter_buffers (buffer_list, filter):
    if filter == "input":
        return [ bid for bid in buffer_list if is_input_buffer(bid) ]
    elif filter == "output":
        return [ bid for bid in buffer_list if is_output_buffer(bid) ]
    else:
        raise (f"Exception: {CLR_ERR} Invalid filter '{filter}' {CLR_END}")

# Return (noc0_x,noc0_y,stream_id) tuple for all the input DRAM buffers
def get_dram_buffers(graph):
    epoch_id = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph]["epoch_id"]
    PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id]

    input_buffers = []
    for b in PIPEGEN:
        if "buffer" in b:
            buffer=PIPEGEN[b]
            if buffer["dram_buf_flag"] != 0 or buffer["dram_io_flag"] != 0:
                input_buffers.append (buffer["uniqid"])
    return input_buffers

# dumps L1 cache memory on chip=chip and core=(x,y)
# starting from address addr until addr + size
def dump_memory(chip, x, y, addr, size):
    for k in range(0, size//4//16 + 1):
        row = []
        for j in range(0, 16):
            if (addr + k*64 + j* 4 < addr + size):
                val = pci_read_xy(chip, x, y, 0, addr + k*64 + j*4)
                row.append(f"0x{val:08x}")
        s = " ".join(row)
        print(f"{x}-{y} 0x{(addr + k*64):08x} => {s}")

# returns buffer_id from blob.yaml
def get_buffer_id(chip, x, y, stream_id):
    stream_name = f"chip_{chip}__y_{y}__x_{x}__stream_id_{stream_id}"
    for element in BLOB :
        phase = re.findall(r"phase_(\d+)", element)
        if (len(phase) > 0):
            for stream in BLOB[element]:
                if stream == stream_name:
                    if "buf_id" in BLOB[element][stream_name]:
                        return BLOB[element][stream_name]["buf_id"]
    return ""


# returns current phase for the stream from device
def get_current_phase(chip, x, y, stream_id):
    return get_stream_reg_field(chip, x, y, stream_id, 11, 0, 20)

# returns epoch id for the stream
def get_stream_epoch_id(chip, x, y, stream_id):
    current_phase = get_current_phase(chip, x ,y, stream_id)
    return (current_phase >> 10)

# converts data format to string
def get_data_format_from_string(str):
    data_format = {}
    data_format["Float32"]   = 0
    data_format["Float16"]   = 1
    data_format["Bfp8"]      = 2
    data_format["Bfp4"]      = 3
    data_format["Bfp2"]      = 11
    data_format["Float16_b"] = 5
    data_format["Bfp8_b"]    = 6
    data_format["Bfp4_b"]    = 7
    data_format["Bfp2_b"]    = 15
    data_format["Lf8"]       = 10
    data_format["UInt16"]    = 12
    data_format["Int8"]      = 14
    data_format["Tf32"]      = 4
    if str in data_format:
        return data_format[str]
    return None

# returns data fromat from netlist
# Format can be Float32, Float16, Float16_b...
def get_data_format(chip, x, y, stream_id):

    buf_id = get_buffer_id(chip, x, y, stream_id)
    if buf_id == "":
        return None

    input = is_input_buffer(buf_id)
    output = is_output_buffer(buf_id)

    if input == output :
        return None

    is_in_df = input
    epoch_id = get_stream_epoch_id(chip, x, y, stream_id)
    graph_name = EPOCH_ID_TO_GRAPH_NAME[epoch_id]

    if graph_name == "":
        return None

    # find operation and return data format of operation
    graph = NETLIST["graphs"][graph_name]

    for op in graph:
        if op != "input_count" and op != "target_device":
            operation = graph[op]
            grid_loc = operation["grid_loc"]
            grid_size = operation["grid_size"]

            if grid_loc[0]<= y - 1 and y -1 < grid_loc[0] + grid_size[0] and grid_loc[1] <= x -1  and x - 1 < grid_loc[1] + grid_size[1]:
                if is_in_df == True:
                    return get_data_format_from_string(operation["in_df"][0])
                else:
                    return get_data_format_from_string(operation["out_df"])
    return None

# Dumps tile in format received form tt_tile::get_string
def dump_tile(chip, x, y, addr, size, data_format):
    s = pci_read_tile(chip, x, y, 0, addr, size, data_format)
    print(s.decode("utf-8"))

# gets information about stream buffer in l1 cache from blob
def get_l1_buffer_info_from_blob(chip, x, y, stream_id, phase):
    stream_name = f"chip_{chip}__y_{y}__x_{x}__stream_id_{stream_id}"
    current_phase = "phase_" + phase
    buffer_addr = 0
    msg_size = 0
    buffer_size = 0
    for element in BLOB:
        if (element == current_phase):
            for stream in BLOB[element]:
                if (stream == stream_name):
                    if BLOB[element][stream].get("buf_addr"):
                        buffer_addr = BLOB[element][stream].get("buf_addr")
                        buffer_size = BLOB[element][stream].get("buf_size")
                        msg_size =BLOB[element][stream].get("msg_size")
    return buffer_addr, buffer_size, msg_size

# dump raw message or formated tile within message
# message consists of header, tile and padding
def dump_message_xy(chip, x, y, stream_id, message_id, is_tile):
    current_phase = str(get_stream_reg_field(chip, x, y, stream_id, 11, 0, 20))
    buffer_addr, buffer_size, msg_size = get_l1_buffer_info_from_blob(chip, x, y, stream_id, current_phase)
    print(f"{x}-{y} buffer_addr: 0x{(buffer_addr):08x} buffer_size: 0x{buffer_size:0x} msg_size:{msg_size}")
    if (buffer_addr >0 and buffer_size>0 and msg_size>0) :
        if (message_id> 0 and message_id <= buffer_size/msg_size):
            msg_addr = buffer_addr + (message_id - 1) * msg_size
            if (is_tile):
                data_format = get_data_format(chip, x, y, stream_id)
                dump_tile(chip, x, y, msg_addr, msg_size, data_format)
            else:
                dump_memory(chip, x, y, msg_addr, msg_size)
        else:
            print(f"Message id should be in range (1, {buffer_size/msg_size})")
    else:
        print("Not enough data in blob.yaml")

RECURSION_DEPTH = 0

# Computes all buffers that are feeding into the buffers from buffer_id_list
def fan_in_buffer_set(buffer_id_list, already_visited = set()):
    global RECURSION_DEPTH
    if RECURSION_DEPTH > 400:
        print (f"{CLR_ERR}Recursion limit reached{CLR_END}")
        return set()
    RECURSION_DEPTH=RECURSION_DEPTH+1
    fan_in_set = set ()

    if type(buffer_id_list) != list: buffer_id_list = [ buffer_id_list ]
    if len (buffer_id_list) == 0:
        return fan_in_set

    # Get direct fan-ins
    buff_core_coords = get_buff_core_coordinates_rc (buffer_id_list)
    # print (buff_core_coords)
    if (255,255) in buff_core_coords: buff_core_coords.remove ((255,255)) # Exclude DRAM
    buffer_id_list = get_core_buffers (buff_core_coords)
    # print (f"Looking for direct fan ins of {buffer_id_list}")
    # print_buffer_info(buffer_id_list)
    direct_fan_ins = set(get_connected_buffers (buffer_id_list, "input"))
    # print (f"direct_fan_ins = {direct_fan_ins}")

    # Filter out the buffers we already visited
    # Figure out the set of fan-ins that we have not already visited
    propagate_fan_in_set = direct_fan_ins - already_visited
    # print (f"propagate_fan_in_set = {propagate_fan_in_set}")
    # print (f"fan_in_set = {fan_in_set}")
    already_visited = already_visited | direct_fan_ins

    # print (f"already_visited: {already_visited}")

    return already_visited.union (fan_in_buffer_set(list (propagate_fan_in_set), already_visited))

def get_fanin_cores_rc (core_coordinates_list_rc):
    if type(core_coordinates_list_rc) != list: core_coordinates_list_rc = [ core_coordinates_list_rc ] # If not a list, assume a single buffer id, and create a list from it

    all_core_buffers = get_core_buffers (core_coordinates_list_rc)
    # print (f"all_core_buffers: {all_core_buffers}")
    core_buffers = list (fan_in_buffer_set(all_core_buffers))
    # print (f"get_fanin_cores_rc/core_buffers: {core_buffers}")
    fanin_cores_rc = get_buff_core_coordinates_rc (core_buffers)
    # print (f"get_fanin_cores_rc/fanin_cores_rc: {fanin_cores_rc}")
    if (255,255) in fanin_cores_rc: fanin_cores_rc.remove ((255,255)) # Exclude DRAM
    return fanin_cores_rc

def traverse_from_inputs (graph, chip_array, current_x, current_y):
    graph_buffs = get_dram_buffers (graph)
    # print (f"graph_buffs = {graph_buffs}")
    in_buffs = filter_buffers(graph_buffs, "input")
    # print (f"in_buffs = {in_buffs}")
    out_buffs = filter_buffers(graph_buffs, "output")
    # print (f"out_buffs = {out_buffs}")

    dest_buffers = get_connected_buffers (in_buffs, "outputs")
    core_coordinates = get_buff_core_coordinates_rc(dest_buffers)
    # print (f"get_buff_core_coordinates_rc: {core_coordinates}")
    core_buffers = get_core_buffers (core_coordinates)
    # print (f"core_buffers: {core_buffers}")
    core_output_buffers = filter_buffers (core_buffers, "output")
    # print (f"core_output_buffers: {core_output_buffers}")
    print_buffer_info (core_output_buffers)

    fan_in_set = fan_in_buffer_set(core_output_buffers)
    # print (f"fan_in_buffer_set of {core_output_buffers} are: {fan_in_set}")

def test(graph, chip_array, current_x, current_y):
    return traverse_from_inputs (graph, chip_array, current_x, current_y)

def analyze_blocked_streams (graph, chip_array, current_x, current_y):
    headers = [ "X-Y", "Op", "Stream", "Type", "Epoch", "Phase", "MSGS_REMAINING", "MSGS_RECEIVED", "Depends on", "State", "Flag" ]
    rows = []

    # 1. Read and analyze data
    chip_data = dict()
    active_streams = dict()
    empty_input_streams = dict()

    for i, chip in enumerate (chip_array):
        chip_data[i] = {
            "chip" : chip,
            "cores" : { }
        }
        # 1. Read all stream data
        streams = get_all_streams_ui_data (chip, GS_x_coords, GS_y_coords)

        # 2a. Analyze the data
        for x in GS_x_coords:
            chip_data[i]["cores"][x] = {}
            for y in GS_y_coords:
                has_active_stream = False
                has_empty_inputs = False

                for stream_id in range (0, 64):
                    if is_stream_active(streams[x][y][stream_id]):
                        has_active_stream = True
                        active_streams[(i, x, y, stream_id)] = streams
                    current_phase = int(streams[x][y][stream_id]['CURR_PHASE'])
                    if current_phase > 0: # Must be configured
                        stream_type = stream_id_descriptor(stream_id)["short"]
                        NUM_MSGS_RECEIVED = int(streams[x][y][stream_id]['NUM_MSGS_RECEIVED'])
                        if stream_type == "input" and NUM_MSGS_RECEIVED == 0:
                            has_empty_inputs = True
                            empty_input_streams[(i, x, y, stream_id)] = streams

                chip_data[i]["cores"][x][y] = {\
                    "fan_in_cores" : [],\
                    "has_active_stream" : has_active_stream,\
                    "has_empty_inputs" : has_empty_inputs\
                }

        # 2b. Find stream dependencies
        active_core_rc_list = [ GS_noc0_to_rc( active_stream[1], active_stream[2] ) for active_stream in active_streams ]
        active_core_noc0_list = [ ( active_stream[1], active_stream[2] ) for active_stream in active_streams ]
        for active_core_rc in active_core_rc_list:
            fan_in_cores_rc = get_fanin_cores_rc (active_core_rc)
            active_core_noc0 = GS_rc_to_noc0 (active_core_rc[0], active_core_rc[1])
            # print (f"fan_in_cores_rc for {active_core_rc}: {fan_in_cores_rc}")
            fan_in_cores_noc0 = [ GS_rc_to_noc0 (rc[0], rc[1]) for rc in fan_in_cores_rc ]
            chip_data[i]["cores"][active_core_noc0[0]][active_core_noc0[1]]["fan_in_cores"] = fan_in_cores_noc0

        # 3. Print the output
        last_core_loc = None
        for x in GS_x_coords:
            for y in GS_y_coords:
                has_active_stream = chip_data[i]["cores"][x][y]["has_active_stream"]
                has_empty_inputs = chip_data[i]["cores"][x][y]["has_empty_inputs"]
                if has_active_stream:
                    for stream_id in range (0, 64):
                        current_phase = int(streams[x][y][stream_id]['CURR_PHASE'])
                        if current_phase > 0:
                            epoch_id = current_phase>>10
                            stream_type = stream_id_descriptor(stream_id)["short"]
                            stream_active = is_stream_active(streams[x][y][stream_id])
                            NUM_MSGS_RECEIVED = int(streams[x][y][stream_id]['NUM_MSGS_RECEIVED'])
                            CURR_PHASE_NUM_MSGS_REMAINING = int(streams[x][y][stream_id]['CURR_PHASE_NUM_MSGS_REMAINING'])
                            graph_name = EPOCH_ID_TO_GRAPH_NAME[epoch_id]
                            op = core_to_op_name(graph_name, x, y)
                            core_loc = f"{x}-{y}"
                            fan_in_cores = chip_data[i]['cores'][x][y]['fan_in_cores']
                            fan_in_cores_str = ""
                            if last_core_loc != core_loc:
                                for fic_noc0 in fan_in_cores:
                                    if fic_noc0 in active_core_noc0_list:
                                        fan_in_cores_str += f"{fic_noc0[0]}-{fic_noc0[1]} "
                            flag = f"{CLR_WARN}All core inputs ready, but no output generated{CLR_END}" if not has_empty_inputs and last_core_loc != core_loc else ""
                            row = [ core_loc if last_core_loc != core_loc else "", op if last_core_loc != core_loc else "", stream_id, stream_type, epoch_id, current_phase, CURR_PHASE_NUM_MSGS_REMAINING, NUM_MSGS_RECEIVED, fan_in_cores_str, f"Active" if stream_active else "", flag ]
                            last_core_loc = core_loc
                            rows.append (row)
    print (tabulate(rows, headers=headers))

def main(chip_array, args):
    # If chip_array is not an array, make it one
    if not isinstance(chip_array, list):
       chip_array = [ chip_array ]

    init_files (args)

    cmd_raw = ""

    # Set initial state
    current_epoch_id = len(EPOCH_TO_PIPEGEN_YAML_MAP.keys())-1
    current_x, current_y, current_stream_id = None, None, None
    current_prompt = "" # Based on the current x,y,stream_id tuple
    global PIPEGEN
    PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[current_epoch_id]
    global BLOB
    BLOB = EPOCH_TO_BLOB_YAML_MAP[current_epoch_id]

    # Print the summary
    global GS_x_coords
    global GS_y_coords
    GS_x_coords = list (range (1, 13))
    GS_y_coords = list (range (1, 6)) + list (range (7, 12))

    for graph in GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP:
        print_host_queue_for_graph(graph)

    for graph in GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP:
        print_dram_queue_summary_for_graph(graph, chip_array)

    # print_stream_summary (chip_array)
    # print_epoch_queue_summary(chip_array, GS_x_coords, GS_y_coords)

    commands = [
        { "long" : "exit",
          "short" : "x",
          "expected_argument_count" : 0,
          "arguments_description" : ": exit the program"
        },
        { "long" : "help",
          "short" : "h",
          "expected_argument_count" : 0,
          "arguments_description" : ": prints command documentation"
        },
        { "long" : "epoch",
          "short" : "e",
          "expected_argument_count" : 1,
          "arguments_description" : "epoch_id : switch to epoch epoch_id"
        },
        { "long" : "stream-summary",
          "short" : "ss",
          "expected_argument_count" : 0,
          "arguments_description" : " : reads and analyzes all streams"
        },
        { "long" : "stream",
          "short" : "s",
          "expected_argument_count" : 3,
          "arguments_description" : "x y stream_id : show stream 'stream_id' at core 'x-y'"
        },
        { "long" : "dump-message-xy",
          "short" : "m",
          "expected_argument_count" : 1,
          "arguments_description" : "message_id: prints message for current stream in currently active phase"
        },
        { "long" : "dump-tile-xy",
          "short" : "t",
          "expected_argument_count" : 1,
          "arguments_description" : "tile_id: prints tile for current stream in currently active phase"
        },
        { "long" : "buffer",
          "short" : "b",
          "expected_argument_count" : 1,
          "arguments_description" : "buffer_id : prints details on the buffer with ID buffer_id"
        },
        { "long" : "pipe",
          "short" : "p",
          "expected_argument_count" : 1,
          "arguments_description" : "pipe_id : prints details on the pipe with ID pipe_id"
        },
        {
          "long" : "dram-queue",
          "short" : "dq",
          "expected_argument_count" : 0,
          "arguments_description" : ": prints DRAM queue summary"
        },
        {
          "long" : "host-queue",
          "short" : "hq",
          "expected_argument_count" : 0,
          "arguments_description" : ": prints Host queue summary"
        },
        {
          "long" : "epoch-queue",
          "short" : "eq",
          "expected_argument_count" : 0,
          "arguments_description" : ": prints Epoch queue summary"
        },
        {
          "long" : "pci-read-xy",
          "short" : "rxy",
          "expected_argument_count" : 3,
          "arguments_description" : "x y addr : read data from address 'addr' at noc0 location x-y of the chip associated with current epoch"
        },
        {
          "long" : "burst-read-xy",
          "short" : "brxy",
          "expected_argument_count" : 4,
          "arguments_description" : "x y addr burst_type : burst read data from address 'addr' at noc0 location x-y of the chip associated with current epoch. \nNCRISC status code address=0xffb2010c, BRISC status code address=0xffb3010c"
        },
        {
          "long" : "pci-write-xy",
          "short" : "wxy",
          "expected_argument_count" : 4,
          "arguments_description" : "x y addr value : writes value to address 'addr' at noc0 location x-y of the chip associated with current epoch"
        },
        {
          "long" : "full-dump",
          "short" : "fd",
          "expected_argument_count" : 0,
          "arguments_description" : ": performs a full dump at current x-y"
        },
        {
          "long" : "analyze-blocked-streams",
          "short" : "abs",
          "expected_argument_count" : 0,
          "arguments_description" : ": analyzes the streams and hightlights the ones that are not progressing. Blocked streams that have all inputs ready are highlighted."
        },
        {
          "long" : "test",
          "short" : "t",
          "expected_argument_count" : 0,
          "arguments_description" : ": test for development"
        }
    ]

    import_commands (commands)

    def epoch_id_to_chip_id (epoch_id):
        return EPOCH_ID_TO_CHIP_ID[epoch_id]

    non_interactive_commands=args.commands.split(";") if args.commands else []

    current_x = 1
    current_y = 1
    current_stream_id = 8
    current_epoch_id = 0
    current_graph_name = EPOCH_ID_TO_GRAPH_NAME[current_epoch_id]

    navigation_suggestions = None
    while cmd_raw != 'exit' and cmd_raw != 'x':
        have_non_interactive_commands=len(non_interactive_commands) > 0
        if current_x is not None and current_y is not None and current_epoch_id is not None:
            row, col = GS_noc0_to_rc (current_x, current_y)
            current_prompt = f"core:{CLR_PROMPT}{current_x}-{current_y}{CLR_END} rc:{CLR_PROMPT}{row},{col}{CLR_END} op:{CLR_PROMPT}{core_to_op_name(current_graph_name, current_x, current_y)}{CLR_END} stream:{CLR_PROMPT}{current_stream_id}{CLR_END} "
        try:
            current_chip_id = epoch_id_to_chip_id(current_epoch_id)
            current_chip = chip_array[current_chip_id]
            current_graph_name = EPOCH_ID_TO_GRAPH_NAME[current_epoch_id]

            print_suggestions (current_graph_name, navigation_suggestions, current_stream_id)

            if have_non_interactive_commands:
                cmd_raw = non_interactive_commands[0].strip()
                if cmd_raw == 'exit' or cmd_raw == 'x':
                    continue
                non_interactive_commands=non_interactive_commands[1:]
                if len(cmd_raw)>0:
                    print (f"{CLR_INFO}Executing command: %s{CLR_END}" % cmd_raw)
            else:
                prompt = f"Current epoch:{CLR_PROMPT}{current_epoch_id}{CLR_END} chip:{CLR_PROMPT}{current_chip_id}{CLR_END} {current_prompt}> "
                cmd_raw = input(prompt)

            try: # To get a a command from the speed dial
                cmd_int = int(cmd_raw)
                cmd_raw = navigation_suggestions[cmd_int]["cmd"]
            except:
                pass

            cmd = cmd_raw.split ()
            if len(cmd) > 0:
                cmd_string = cmd[0]
                found_command = None

                # Look for command to execute
                for c in commands:
                    if c["short"] == cmd_string or c["long"] == cmd_string:
                        found_command = c
                        # Check arguments
                        if len(cmd)-1 != found_command["expected_argument_count"]:
                            print (f"{CLR_ERR}Command '{found_command['long']}' requires {found_command['expected_argument_count']} argument{'s' if found_command['expected_argument_count'] != 1 else ''}: {found_command['arguments_description']}{CLR_END}")
                            found_command = 'invalid-args'
                        break

                if found_command == None:
                    # Print help on invalid commands
                    print (f"{CLR_ERR}Invalid command '{cmd_string}'{CLR_END}\nAvailable commands:")
                    print_available_commands (commands)
                elif found_command == 'invalid-args':
                    # This was handled earlier
                    pass
                else:
                    if found_command["long"] == "epoch":
                        new_epoch_id = int(cmd[1])

                        if new_epoch_id in EPOCH_ID_TO_GRAPH_NAME:
                            current_epoch_id = new_epoch_id
                            PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[current_epoch_id]
                            BLOB = EPOCH_TO_BLOB_YAML_MAP[current_epoch_id]
                        else:
                            print (f"{CLR_ERR}Invalid epoch id {new_epoch_id}{CLR_END}")
                    elif found_command["long"] == "stream-summary":
                        print_stream_summary(chip_array)
                    elif found_command["long"] == "stream":
                        current_x, current_y, current_stream_id = int(cmd[1]), int(cmd[2]), int(cmd[3])
                        navigation_suggestions, stream_epoch_id = print_stream (current_chip, current_x, current_y, current_stream_id, current_epoch_id)
                        if stream_epoch_id != current_epoch_id:
                            if stream_epoch_id >=0 and stream_epoch_id < len(BLOB.keys()):
                                current_epoch_id = stream_epoch_id
                                PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[current_epoch_id]
                                BLOB = EPOCH_TO_BLOB_YAML_MAP[current_epoch_id]
                                print (f"{CLR_WARN}Automatically switched to epoch {current_epoch_id}{CLR_END}")
                    elif found_command["long"] == "buffer":
                        buffer_id = int(cmd[1])
                        print_buffer_data (buffer_id, current_epoch_id)
                    elif found_command["long"] == "pipe":
                        buffer_id = int(cmd[1])
                        print_pipe_data (buffer_id, current_epoch_id)
                    elif found_command["long"] == "dram-queue":
                        print_dram_queue_summary_for_graph (current_graph_name, chip_array)
                    elif found_command["long"] == "host-queue":
                        print_host_queue_for_graph (current_graph_name)
                    elif found_command["long"] == "epoch-queue":
                        print_epoch_queue_summary(chip_array, GS_x_coords, GS_y_coords)
                    elif found_command["long"] == "pci-read-xy" or found_command["long"] == "burst-read-xy" or found_command["long"] == "pci-write-xy":
                        x = int(cmd[1],0)
                        y = int(cmd[2],0)
                        addr = int(cmd[3],0)
                        if found_command["long"] == "pci-read-xy":
                            data = pci_read_xy (current_chip_id, x, y, NOC0, addr)
                            print_a_read (x, y, addr, data)
                        elif found_command["long"] == "burst-read-xy":
                            burst_type = int(cmd[4],0)
                            burst_read_xy (current_chip_id, x, y, NOC0, addr, burst_type=burst_type)
                        elif found_command["long"] == "pci-write-xy":
                            pci_write_xy (current_chip_id, x, y, NOC0, addr, data = int(cmd[4],0))
                        else:
                            print (f"{CLR_ERR} Unknown {found_command['long']} {CLR_END}")
                    elif found_command["long"] == "dump-message-xy" or found_command["long"] == "dump-tile-xy":
                        message_id = int(cmd[1])
                        is_tile = found_command["long"] == "dump-tile-xy"
                        dump_message_xy(current_chip_id, current_x, current_y, current_stream_id, message_id, is_tile)
                    elif found_command["long"] == "full-dump":
                        full_dump_xy(current_chip_id, current_x, current_y)
                    elif found_command["long"] == "exit":
                        pass # Exit is handled in the outter loop
                    elif found_command["long"] == "help":
                        print_available_commands (commands)
                    elif found_command["long"] == "test":
                        test (current_graph_name, chip_array, current_x, current_y)
                    elif found_command["long"] == "analyze-blocked-streams":
                        analyze_blocked_streams (current_graph_name, chip_array, current_x, current_y)
                    else:
                        found_command["module"].run(cmd[1:], globals())

        except Exception as e:
            print (f"Exception: {CLR_ERR} {e} {CLR_END}")
            print(traceback.format_exc())
            if have_non_interactive_commands:
                raise
            else:
                raise
    return 0

# Communication with Buda (or debuda-stub)
# See struct BUDA_READ_REQ
DEBUDA_STUB_PROCESS=None
def init_comm_client ():
    DEBUDA_STUB_PORT=5555

    print ("Spawning debuda-stub.")
    debuda_stub_path = application_path() + "/debuda-stub"
    try:
        global DEBUDA_STUB_PROCESS
        debuda_stub_args = [ "--debug" ] if args.debug_debuda_stub else [ ]
        # print ("debuda_stub_cmd = %s" % ([debuda_stub_path] + debuda_stub_args))
        DEBUDA_STUB_PROCESS=subprocess.Popen([debuda_stub_path] + debuda_stub_args, preexec_fn=os.setsid)
    except:
        print (f"Exception: {CLR_ERR} Cannot find {debuda_stub_path}. {STUB_HELP} {CLR_END}")
        raise

    context = zmq.Context()
    global ZMQ_SOCKET

    #  Socket to talk to server
    print("Connecting to debuda-stub...")
    ZMQ_SOCKET = context.socket(zmq.REQ)
    ZMQ_SOCKET.connect(f"tcp://localhost:{DEBUDA_STUB_PORT}")
    print("Connected to debuda-stub.")

    ZMQ_SOCKET.send(struct.pack ("c", b'\x01')) # PING

    reply = ZMQ_SOCKET.recv_string()
    if "PONG" not in reply:
        print (f"Expected PONG but received {reply}") # Shoud print PONG

    time.sleep (0.1)

def terminate_comm_client_callback ():
    os.killpg(os.getpgid(DEBUDA_STUB_PROCESS.pid), signal.SIGTERM)
    print (f"Terminated debuda-stub with pid:{DEBUDA_STUB_PROCESS.pid}")
# Get path of this script. 'frozen' means: packaged with pyinstaller.
def application_path ():
    if getattr(sys, 'frozen', False):
        application_path = os.path.dirname(sys.executable)
    elif __file__:
        application_path = os.path.dirname(__file__)
    return os.path.abspath (application_path)

def import_commands (command_metadata_array):
    command_files = []
    for root, dirnames, filenames in os.walk(application_path () + '/debuda-commands'):
        for filename in fnmatch.filter(filenames, '*.py'):
            command_files.append(os.path.join(root, filename))

    sys.path.append(application_path() + '/debuda-commands')

    for cmdfile in command_files:
        module_path = os.path.splitext(os.path.basename(cmdfile))[0]
        my_cmd_module = importlib.import_module (module_path)
        command_metadata = my_cmd_module.command_metadata
        command_metadata["module"] = my_cmd_module
        command_metadata["long"] = my_cmd_module.__name__

        command_metadata_array.append (command_metadata)

init_comm_client ()

# Make sure to terminate communication client (debuda-stub) on exit
atexit.register (terminate_comm_client_callback)

main([ 0 ], args)

# TODO:
# - Up down history/browsing in prompt
# - Easily list all streams from a fork list
# - Automatic problem tracing
# - WH support
# - Use RC coordinates: core_coordinates              [0, 1]
# - Some kind of grep command or open blob.yaml in VSCODE
