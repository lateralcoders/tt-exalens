#!/usr/bin/env python3
"""
debuda parses the build output files and probes the silicon to determine status of a buda run.
"""
STUB_HELP = "This tool requires debuda-stub. You can build debuda-stub with bin/build-debuda-stub.sh. It also requires zeromq (sudo apt install -y libzmq3-dev)."

import yaml, sys, os, argparse, time, traceback, re, pickle
import atexit, fnmatch, importlib

from tabulate import tabulate

# Tenstorrent classes
import util
import device, grayskull
import objects, stream

parser = argparse.ArgumentParser(description=__doc__ + STUB_HELP)
parser.add_argument('output_dir', type=str, help='Output directory of a buda run')
parser.add_argument('--netlist',  type=str, required=True, help='Netlist file to import')
parser.add_argument('--commands', type=str, required=False, help='Execute a set of commands separated by ;')
parser.add_argument('--stream-cache', action='store_true', default=False, help=f'If file "{stream.STREAM_CACHE_FILE_NAME}" exists, the stream data will be laoded from it. If the file does not exist, it will be crated and populated with the stream data')
parser.add_argument('--debug-debuda-stub', action='store_true', default=False, help=f'Prints all transactions on PCIe. Also, starts debuda-stub with --debug to print transfers.')
args = parser.parse_args()
import pprint
pp = pprint.PrettyPrinter(indent=4)

# Constants

# IDs of NOCs
NOC0 = 0
NOC1 = 1

# Returns an array of [r,c] pairs for the operation
def get_op_coords (op):
    locations = []
    opr = op['grid_loc'][0]
    opc = op['grid_loc'][1]
    for r in range(op['grid_size'][1]):
        for c in range(op['grid_size'][0]):
            locations.append ( [ opr + r, opc + c ] )
    return locations

# Returns the op name mapped to a given NOC location
def core_coord_to_op_name (graph_name, noc0_x, noc0_y):
    r, c = grayskull.noc0_to_rc (noc0_x, noc0_y)
    graph = NETLIST["graphs"][graph_name]
    for op_name in graph.keys():
        if op_name not in ['target_device', 'input_count']:
            op = graph[op_name]
            op_locations = get_op_coords(op)
            if [ r, c ] in op_locations:
                return f"{graph_name}/{op_name}:{op['type']}"



# Returns blob data where all values get converted to strings
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
            ret_val[lastidx][f"source"] = f'{util.CLR_INFO}{k}{util.CLR_END}'

    return ret_val

# Find occurrences of buffer with ID 'buffer_id' across all epochs, and print the structures that reference them
# Supply current_epoch_id=None, to show details in all epochs
def print_buffer_data (buffer_id, current_epoch_id = None):
    for epoch_id in EPOCH_TO_PIPEGEN_YAML_MAP:
        for dct in EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id]:
            d = EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id][dct]
            if ("input_list" in d and buffer_id in d["input_list"]) or ("output_list" in d and buffer_id in d["output_list"]) or ("buffer" in dct and "uniqid" in d and buffer_id == d["uniqid"]):
                if current_epoch_id is None or current_epoch_id == epoch_id:
                    util.print_columnar_dicts ([d], [f"{util.CLR_INFO}Epoch {epoch_id} - {dct}{util.CLR_END}"])
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
                    util.print_columnar_dicts ([d], [f"{util.CLR_INFO}Epoch {epoch_id} - {dct}{util.CLR_END}"])
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
                            util.print_columnar_dicts ([dct[strm]], [f"{util.CLR_INFO}Epoch {epoch_id} - BLOB - {strm}{util.CLR_END}"])
                        else:
                            print (f"Pipe is also used in epoch {epoch_id}. Details suppressed.")

# Prints information on DRAM queues
def print_dram_queue_summary_for_graph (graph, chip_array):
    epoch_id = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph]["epoch_id"]
    chip_id = GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP[graph]["target_device"]
    chip = chip_array[chip_id]

    PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[epoch_id]

    print (f"{util.CLR_INFO}DRAM queues for epoch %d{util.CLR_END}" % epoch_id)

    table = []
    for b in PIPEGEN:
        if "buffer" in b:
            buffer=PIPEGEN[b]
            if buffer["dram_buf_flag"] != 0 or buffer["dram_io_flag"] != 0 and buffer["dram_io_flag_is_remote"] == 0:
                dram_chan = buffer["dram_chan"]
                dram_addr = buffer['dram_addr']
                dram_loc = grayskull.CHANNEL_TO_DRAM_LOC[dram_chan]
                rdptr = device.pci_read_xy (chip, dram_loc[0], dram_loc[1], 0, dram_addr)
                wrptr = device.pci_read_xy (chip, dram_loc[0], dram_loc[1], 0, dram_addr + 4)
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
                    # print (f"{util.CLR_WARN}Found host queue %s{util.CLR_END}" % pp.pformat(buffer))
                    rdptr = device.host_dma_read (dram_addr)
                    wrptr = device.host_dma_read (dram_addr + 4)
                    slot_size_bytes = buffer["size_tiles"] * buffer["tile_size"]
                    queue_size_bytes = slot_size_bytes * buffer["q_slots"]
                    occupancy = (wrptr - rdptr) if wrptr >= rdptr else wrptr - (rdptr - buffer["q_slots"])
                    table.append ([ b, buffer["dram_buf_flag"], buffer["dram_io_flag"], f"0x{dram_addr:x}", f"{rdptr}", f"{wrptr}", occupancy, buffer["q_slots"], queue_size_bytes ])

    print (f"{util.CLR_INFO}Host queues (where dram_io_flag_is_remote!=0) for epoch %d {util.CLR_END}" % epoch_id)
    if len(table) > 0:
        print (tabulate(table, headers=["Buffer name", "dram_buf_flag", "dram_io_flag", "Address", "RD ptr", "WR ptr", "Occupancy", "Q slots", "Q Size [bytes]"] ))
    else:
        print ("No host queues found")

# Prints epoch queues
def print_epoch_queue_summary (chip_array, x_coords, y_coords):
    dram_chan = 0 # This queue is always in channel 0
    dram_loc = grayskull.CHANNEL_TO_DRAM_LOC[dram_chan]

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
        print (f"{util.CLR_INFO}Epoch queues for device %d{util.CLR_END}" % chip_id)
        chip_id += 1
        for x in y_coords:
            for y in x_coords:
                EPOCH_QUEUE_START_ADDR = reserved_size_bytes
                offset = (16 * x + y) * ((EPOCH_Q_NUM_SLOTS*2+8)*4)
                dram_addr = EPOCH_QUEUE_START_ADDR + offset
                rdptr = device.pci_read_xy (chip, dram_loc[0], dram_loc[1], 0, dram_addr)
                wrptr = device.pci_read_xy (chip, dram_loc[0], dram_loc[1], 0, dram_addr + 4)
                occupancy = (wrptr - rdptr) if wrptr >= rdptr else wrptr - (rdptr - EPOCH_Q_NUM_SLOTS)
                if occupancy > 0:
                    table.append ([ f"{x}-{y}", f"0x{dram_addr:x}", f"{rdptr}", f"{wrptr}", occupancy ])
    if len(table) > 0:
        print (tabulate(table, headers=["Location", "Address", "RD ptr", "WR ptr", "Occupancy" ] ))
    else:
        print ("No epoch queues have occupancy > 0")

# A helper to print the result of a single PCI read
def print_a_read (x, y, addr, val, comment=""):
    print(f"{x}-{y} 0x{addr:08x} => 0x{val:08x} ({val:d}) {comment}")

# Perform a burst of PCI reads and print results.
# If burst_type is 1, read the same location for a second and print a report
# If burst_type is 2, read an array of locations once and print a report
def print_burst_read_xy (chip, x, y, noc_id, addr, burst_type = 1):
    if burst_type == 1:
        values = {}
        t_end = time.time() + 1
        print ("Sampling for 1 second...")
        while time.time() < t_end:
            val = device.pci_read_xy(chip, x, y, noc_id, addr)
            if val not in values:
                values[val] = 0
            values[val] += 1
        for val in values.keys():
            print_a_read(x, y, addr, val, f"- {values[val]} times")
    elif burst_type >= 2:
        for k in range(0, burst_type):
            val = device.pci_read_xy(chip, x, y, noc_id, addr + 4*k)
            print_a_read(x,y,addr + 4*k, val)

# Print all commands (help)
def print_available_commands (commands):
    rows = []
    for c in commands:
        desc = c['arguments_description'].split(':')
        row = [ f"{util.CLR_INFO}{c['short']}{util.CLR_END}", f"{util.CLR_INFO}{c['long']}{util.CLR_END}", f"{desc[0]}", f"{desc[1]}" ]
        rows.append(row)
    print (tabulate(rows, headers=[ "Short", "Long", "Arguments", "Description" ]))

# Certain commands give suggestions for next step. This function formats and prints those suggestions.
def print_suggestions (graph_name, navigation_suggestions, current_stream_id):
    if navigation_suggestions:
        print ("Speed dial:")
        rows = []
        for i in range (len(navigation_suggestions)):
            stream_id = navigation_suggestions[i]['stream_id']
            clr = util.CLR_INFO if current_stream_id == stream_id else util.CLR_END
            row = [ f"{clr}{i}{util.CLR_END}", \
                f"{clr}Go to {navigation_suggestions[i]['type']} of stream {navigation_suggestions[i]['stream_id']}{util.CLR_END}", \
                f"{clr}{navigation_suggestions[i]['cmd']}{util.CLR_END}", \
                f"{clr}{core_coord_to_op_name(graph_name, navigation_suggestions[i]['noc0_x'], navigation_suggestions[i]['noc0_y'])}{util.CLR_END}"
                ]
            rows.append (row)
        print(tabulate(rows, headers=[ "#", "Description", "Command", "Op name" ]))

# Prints all streams for all chips given by chip_array
def print_stream_summary (chip_array):
    # Finally check and print stream data
    for i, chip in enumerate (chip_array):
        print (f"{util.CLR_INFO}Reading and analyzing streams on device %d...{util.CLR_END}" % i)
        streams_ui_data = read_all_stream_registers (chip, grayskull.x_coords, grayskull.y_coords)
        stream_summary(chip, grayskull.x_coords, grayskull.y_coords, streams_ui_data)

# Loads all files (blob, pipegen, netlist) and constructs maps for faster lookup
def load_files (args):
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
        print (f"{util.CLR_ERR}Error: cannot open graph_to_epoch_map.yaml {util.CLR_END}")
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
            print (f"{util.CLR_ERR}Error: cannot find directory {GRAPH_DIR} {util.CLR_END}")
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

# Prints contents of core's memory
def dump_memory(chip, x, y, addr, size):
    for k in range(0, size//4//16 + 1):
        row = []
        for j in range(0, 16):
            if (addr + k*64 + j* 4 < addr + size):
                val = device.pci_read_xy(chip, x, y, 0, addr + k*64 + j*4)
                row.append(f"0x{val:08x}")
        s = " ".join(row)
        print(f"{x}-{y} 0x{(addr + k*64):08x} => {s}")

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

# dumps message in hex format
def dump_message_xy(chip, x, y, stream_id, message_id):
    current_phase = str(grayskull.get_stream_reg_field(chip, x, y, stream_id, 11, 0, 20))
    buffer_addr, buffer_size, msg_size = get_l1_buffer_info_from_blob(chip, x, y, stream_id, current_phase)
    print(f"{x}-{y} buffer_addr: 0x{(buffer_addr):08x} buffer_size: 0x{buffer_size:0x} msg_size:{msg_size}")
    if (buffer_addr >0 and buffer_size>0 and msg_size>0) :
        if (message_id> 0 and message_id <= buffer_size/msg_size):
            dump_memory(chip, x, y, buffer_addr + (message_id - 1) * msg_size, msg_size )
        else:
            print(f"Message id should be in range (1, {buffer_size//msg_size})")
    else:
        print("Not enough data in blob.yaml")

# Test command for development only
def test(graph, chip_array, current_x, current_y):
    return test_traverse_from_inputs (graph, chip_array, current_x, current_y)

# Main
def main(chip_array, args, context):
    # If chip_array is not an array, make it an array
    if not isinstance(chip_array, list):
       chip_array = [ chip_array ]

    # 
    # load_files (args)

    cmd_raw = ""

    # Set initial state
    current_epoch_id = 0 # len(EPOCH_TO_PIPEGEN_YAML_MAP.keys())-1
    current_x, current_y, current_stream_id = None, None, None
    current_prompt = "" # Based on the current x,y,stream_id tuple
    # global PIPEGEN
    # PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[current_epoch_id]
    # global BLOB
    # BLOB = EPOCH_TO_BLOB_YAML_MAP[current_epoch_id]

    # Print the summary

    # for graph in GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP:
    #     print_host_queue_for_graph(graph)

    # for graph in GRAPH_NAME_TO_DEVICE_AND_EPOCH_MAP:
    #     print_dram_queue_summary_for_graph(graph, chip_array)

    # print_stream_summary (chip_array)
    # print_epoch_queue_summary(chip_array, grayskull.x_coords, grayskull.y_coords)

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
        {
          "long" : "dump-message-xy",
          "short" : "m",
          "expected_argument_count" : 1,
          "arguments_description" : "message_id: prints message for current stream in currently active phase"
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
          "long" : "analyze-blocked-streams-old",
          "short" : "abso",
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
        # return EPOCH_ID_TO_CHIP_ID[epoch_id]
        return 0

    non_interactive_commands=args.commands.split(";") if args.commands else []

    # Initialize current UI state
    current_x = 1
    current_y = 1
    current_stream_id = 8
    current_epoch_id = 0
    current_graph_name = "" # EPOCH_ID_TO_GRAPH_NAME[current_epoch_id]
    navigation_suggestions = None

    # Main command loop
    while cmd_raw != 'exit' and cmd_raw != 'x':
        have_non_interactive_commands=len(non_interactive_commands) > 0

        if current_x is not None and current_y is not None and current_epoch_id is not None:
            row, col = grayskull.noc0_to_rc (current_x, current_y)
            # current_prompt = f"core:{util.CLR_PROMPT}{current_x}-{current_y}{util.CLR_END} rc:{util.CLR_PROMPT}{row},{col}{util.CLR_END} op:{util.CLR_PROMPT}{core_coord_to_op_name(current_graph_name, current_x, current_y)}{util.CLR_END} stream:{util.CLR_PROMPT}{current_stream_id}{util.CLR_END} "
            current_prompt = f"core:{util.CLR_PROMPT}{current_x}-{current_y}{util.CLR_END} rc:{util.CLR_PROMPT}{row},{col}{util.CLR_END} stream:{util.CLR_PROMPT}{current_stream_id}{util.CLR_END} "
        try:
            current_chip_id = epoch_id_to_chip_id(current_epoch_id)
            current_chip = chip_array[current_chip_id]
            current_graph_name = "N/A" # EPOCH_ID_TO_GRAPH_NAME[current_epoch_id]

            print_suggestions (current_graph_name, navigation_suggestions, current_stream_id)

            if have_non_interactive_commands:
                cmd_raw = non_interactive_commands[0].strip()
                if cmd_raw == 'exit' or cmd_raw == 'x':
                    continue
                non_interactive_commands=non_interactive_commands[1:]
                if len(cmd_raw)>0:
                    print (f"{util.CLR_INFO}Executing command: %s{util.CLR_END}" % cmd_raw)
            else:
                prompt = f"Current epoch:{util.CLR_PROMPT}{current_epoch_id}{util.CLR_END} chip:{util.CLR_PROMPT}{current_chip_id}{util.CLR_END} {current_prompt}> "
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
                            print (f"{util.CLR_ERR}Command '{found_command['long']}' requires {found_command['expected_argument_count']} argument{'s' if found_command['expected_argument_count'] != 1 else ''}: {found_command['arguments_description']}{util.CLR_END}")
                            found_command = 'invalid-args'
                        break

                if found_command == None:
                    # Print help on invalid commands
                    print (f"{util.CLR_ERR}Invalid command '{cmd_string}'{util.CLR_END}\nAvailable commands:")
                    print_available_commands (commands)
                elif found_command == 'invalid-args':
                    # This was handled earlier
                    pass
                else:
                    if found_command["long"] == "epoch":
                        new_epoch_id = int(cmd[1])

                        if True: # new_epoch_id in EPOCH_ID_TO_GRAPH_NAME:
                            current_epoch_id = new_epoch_id
                            # PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[current_epoch_id]
                            # BLOB = EPOCH_TO_BLOB_YAML_MAP[current_epoch_id]
                        else:
                            print (f"{util.CLR_ERR}Invalid epoch id {new_epoch_id}{util.CLR_END}")
                    elif found_command["long"] == "stream-summary":
                        print_stream_summary(chip_array)
                    elif found_command["long"] == "stream":
                        current_x, current_y, current_stream_id = int(cmd[1]), int(cmd[2]), int(cmd[3])
                        navigation_suggestions, stream_epoch_id = print_stream (current_chip, current_x, current_y, current_stream_id, current_epoch_id)
                        if stream_epoch_id != current_epoch_id:
                            if stream_epoch_id >=0: # and stream_epoch_id < len(BLOB.keys()):
                                current_epoch_id = stream_epoch_id
                                # PIPEGEN = EPOCH_TO_PIPEGEN_YAML_MAP[current_epoch_id]
                                # BLOB = EPOCH_TO_BLOB_YAML_MAP[current_epoch_id]
                                print (f"{util.CLR_WARN}Automatically switched to epoch {current_epoch_id}{util.CLR_END}")
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
                        print_epoch_queue_summary(chip_array, grayskull.x_coords, grayskull.y_coords)
                    elif found_command["long"] == "pci-read-xy" or found_command["long"] == "burst-read-xy" or found_command["long"] == "pci-write-xy":
                        x = int(cmd[1],0)
                        y = int(cmd[2],0)
                        addr = int(cmd[3],0)
                        if found_command["long"] == "pci-read-xy":
                            data = device.pci_read_xy (current_chip_id, x, y, NOC0, addr)
                            print_a_read (x, y, addr, data)
                        elif found_command["long"] == "burst-read-xy":
                            burst_type = int(cmd[4],0)
                            print_burst_read_xy (current_chip_id, x, y, NOC0, addr, burst_type=burst_type)
                        elif found_command["long"] == "pci-write-xy":
                            device.pci_write_xy (current_chip_id, x, y, NOC0, addr, data = int(cmd[4],0))
                        else:
                            print (f"{util.CLR_ERR} Unknown {found_command['long']} {util.CLR_END}")
                    elif found_command["long"] == "dump-message-xy":
                        message_id = int(cmd[1])
                        dump_message_xy(current_chip_id, current_x, current_y, current_stream_id, message_id)
                    elif found_command["long"] == "full-dump":
                        grayskull.full_dump_xy(current_chip_id, current_x, current_y)
                    elif found_command["long"] == "exit":
                        pass # Exit is handled in the outter loop
                    elif found_command["long"] == "help":
                        print_available_commands (commands)
                    elif found_command["long"] == "test":
                        test (current_graph_name, chip_array, current_x, current_y)
                    elif found_command["long"] == "analyze-blocked-streams-old":
                        analyze_blocked_streams (current_graph_name, chip_array, current_x, current_y)
                    else:
                        found_command["module"].run(cmd[1:], context)

        except Exception as e:
            print (f"Exception: {util.CLR_ERR} {e} {util.CLR_END}")
            print(traceback.format_exc())
            if have_non_interactive_commands:
                raise
            else:
                raise
    return 0

# Import any 'plugin' comands from debuda-commands directory
def import_commands (command_metadata_array):
    command_files = []
    for root, dirnames, filenames in os.walk(util.application_path () + '/debuda-commands'):
        for filename in fnmatch.filter(filenames, '*.py'):
            command_files.append(os.path.join(root, filename))

    sys.path.append(util.application_path() + '/debuda-commands')

    for cmdfile in command_files:
        module_path = os.path.splitext(os.path.basename(cmdfile))[0]
        my_cmd_module = importlib.import_module (module_path)
        command_metadata = my_cmd_module.command_metadata
        command_metadata["module"] = my_cmd_module
        command_metadata["long"] = my_cmd_module.__name__
        print (f"Importing command '{my_cmd_module.__name__}'")

        command_metadata_array.append (command_metadata)

# Initialize communication with the client (debuda-stub)
device.init_comm_client (args.debug_debuda_stub)

# Make sure to terminate communication client (debuda-stub) on exit
atexit.register (device.terminate_comm_client_callback)

# Call Main

context = objects.load (netlist_filepath = args.netlist, run_dirpath = args.output_dir)

main([ 0 ], args, context)


# for dev in context.devices:
#     print (dev)
