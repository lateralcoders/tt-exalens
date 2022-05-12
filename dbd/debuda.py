#!/usr/bin/env python3
"""
debuda parses the build output files and probes the silicon to determine status of a buda run.
"""
STUB_HELP = "This tool requires debuda-stub. You can build debuda-stub with bin/build-debuda-stub.sh. It also requires zeromq (sudo apt install -y libzmq3-dev)."

import yaml, sys, os, argparse, time, traceback, re, pickle
import atexit, fnmatch, importlib

from tabulate import tabulate

# Tenstorrent classes
import tt_util as util
import tt_device, tt_grayskull as grayskull
import tt_objects, tt_stream

parser = argparse.ArgumentParser(description=__doc__ + STUB_HELP)
parser.add_argument('output_dir', type=str, help='Output directory of a buda run')
parser.add_argument('--netlist',  type=str, required=True, help='Netlist file to import')
parser.add_argument('--commands', type=str, required=False, help='Execute a set of commands separated by ;')
parser.add_argument('--stream-cache', action='store_true', default=False, help=f'If file "{tt_stream.STREAM_CACHE_FILE_NAME}" exists, the stream data will be loaded from it. If the file does not exist, it will be crated and populated with the stream data')
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
# Supply ui_state['current_epoch_id']=None, to show details in all epochs
def print_buffer_data (cmd, context):
    buffer_id = int(cmd[1])

    for epoch_id in context.netlist.epoch_ids():
        graph_name = context.netlist.epoch_id_to_graph_name(epoch_id)
        graph = context.netlist.graph(graph_name)
        buffer = graph.get_buffer(buffer_id)
        if buffer:
            util.print_columnar_dicts ([buffer.root], [f"{util.CLR_INFO}Epoch {epoch_id}{util.CLR_END}"])

        navigation_suggestions = [ ]
        for p in graph.pipes:
            pipe = graph.get_pipe(p)
            if buffer_id in pipe.root["input_list"]:
                print (f"( {util.CLR_BLUE}Input{util.CLR_END} of pipe {pipe.id()} )")
                navigation_suggestions.append ({ 'cmd' : f"p {pipe.id()}", 'description' : "Show pipe" })
            if buffer_id in pipe.root["output_list"]:
                print (f"( {util.CLR_BLUE}Output{util.CLR_END} of pipe {pipe.id()} )")
                navigation_suggestions.append ({ 'cmd' : f"p {pipe.id()}", 'description' : "Show pipe" })

    return navigation_suggestions

# Find occurrences of pipe with ID 'pipe_id' across all epochs, and print the structures that reference them
# Supply current_epoch_id=None, to show details in all epochs
def print_pipe_data (cmd, context):
    pipe_id = int(cmd[1])

    for epoch_id in context.netlist.epoch_ids():
        graph_name = context.netlist.epoch_id_to_graph_name(epoch_id)
        graph = context.netlist.graph(graph_name)
        pipe = graph.get_pipe(pipe_id)
        if pipe:
            util.print_columnar_dicts ([pipe.root], [f"{util.CLR_INFO}Epoch {epoch_id}{util.CLR_END}"])

        navigation_suggestions = [ ]
        for input_buffer in pipe.root['input_list']:
            navigation_suggestions.append ({ 'cmd' : f"b {input_buffer}", 'description' : "Show src buffer" })
        for input_buffer in pipe.root['output_list']:
            navigation_suggestions.append ({ 'cmd' : f"b {input_buffer}", 'description' : "Show dest buffer" })

    return navigation_suggestions

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
def print_dram_queue_summary (cmd, context, ui_state = None): # graph, chip_array):
    if ui_state is not None:
        epoch_id_list = [ ui_state["current_epoch_id"] ]
    else:
        epoch_id_list = context.netlist.epoch_ids()

    table = []
    for epoch_id in epoch_id_list:
        print (f"{util.CLR_INFO}DRAM queues for epoch %d{util.CLR_END}" % epoch_id)
        graph_name = context.netlist.epoch_id_to_graph_name (epoch_id)
        graph = context.netlist.graph(graph_name)
        device_id = context.netlist.graph_name_to_device_id(graph_name)

        for buffer_id, buffer in graph.buffers.items():
            buffer_data = buffer.root
            if buffer_data["dram_buf_flag"] != 0 or buffer_data["dram_io_flag"] != 0 and buffer_data["dram_io_flag_is_remote"] == 0:
                dram_chan = buffer_data["dram_chan"]
                dram_addr = buffer_data['dram_addr']
                dram_loc = grayskull.CHANNEL_TO_DRAM_LOC[dram_chan]
                rdptr = tt_device.pci_read_xy (device_id, dram_loc[0], dram_loc[1], 0, dram_addr)
                wrptr = tt_device.pci_read_xy (device_id, dram_loc[0], dram_loc[1], 0, dram_addr + 4)
                slot_size_bytes = buffer_data["size_tiles"] * buffer_data["tile_size"]
                queue_size_bytes = slot_size_bytes * buffer_data["q_slots"]
                occupancy = (wrptr - rdptr) if wrptr >= rdptr else wrptr - (rdptr - buffer_data["q_slots"])
                table.append ([ buffer_id, buffer_data["dram_buf_flag"], buffer_data["dram_io_flag"], dram_chan, f"0x{dram_addr:x}", f"{rdptr}", f"{wrptr}", occupancy, buffer_data["q_slots"], queue_size_bytes ])

    print (tabulate(table, headers=["Buffer name", "dram_buf_flag", "dram_io_flag", "Channel", "Address", "RD ptr", "WR ptr", "Occupancy", "Q slots", "Q Size [bytes]"] ))

# Prints the queues residing in host's memory.
def print_host_queue (cmd, context, ui_state):
    if ui_state is not None:
        epoch_id_list = [ ui_state["current_epoch_id"] ]
    else:
        epoch_id_list = context.netlist.epoch_ids()

    table = []
    for epoch_id in epoch_id_list:
        print (f"{util.CLR_INFO}DRAM queues for epoch %d{util.CLR_END}" % epoch_id)
        graph_name = context.netlist.epoch_id_to_graph_name (epoch_id)
        graph = context.netlist.graph(graph_name)
        device_id = context.netlist.graph_name_to_device_id(graph_name)

        for buffer_id, buffer in graph.buffers.items():
            buffer_data = buffer.root
            if buffer_data["dram_io_flag_is_remote"] != 0:
                dram_addr = buffer_data['dram_addr']
                if dram_addr >> 29 == device_id:
                    rdptr = tt_device.host_dma_read (dram_addr)
                    wrptr = tt_device.host_dma_read (dram_addr + 4)
                    slot_size_bytes = buffer_data["size_tiles"] * buffer_data["tile_size"]
                    queue_size_bytes = slot_size_bytes * buffer_data["q_slots"]
                    occupancy = (wrptr - rdptr) if wrptr >= rdptr else wrptr - (rdptr - buffer_data["q_slots"])
                    table.append ([ buffer_id, buffer_data["dram_buf_flag"], buffer_data["dram_io_flag"], f"0x{dram_addr:x}", f"{rdptr}", f"{wrptr}", occupancy, buffer_data["q_slots"], queue_size_bytes ])

    print (f"{util.CLR_INFO}Host queues (where dram_io_flag_is_remote!=0) for epoch %d {util.CLR_END}" % epoch_id)
    if len(table) > 0:
        print (tabulate(table, headers=["Buffer name", "dram_buf_flag", "dram_io_flag", "Address", "RD ptr", "WR ptr", "Occupancy", "Q slots", "Q Size [bytes]"] ))
    else:
        print ("No host queues found")

# Prints epoch queues
def print_epoch_queue_summary (cmd, context, ui_state):
    epoch_id = ui_state["current_epoch_id"]

    graph_name = context.netlist.epoch_id_to_graph_name (epoch_id)
    graph = context.netlist.graph(graph_name)
    device_id = context.netlist.graph_name_to_device_id(graph_name)
    epoch_device = context.devices[device_id]

    print (f"{util.CLR_INFO}Epoch queues for epoch %d, device id {device_id}{util.CLR_END}" % epoch_id)

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
    print (f"{util.CLR_INFO}Epoch queues for device %d{util.CLR_END}" % chip_id)
    chip_id += 1

    dram_chan = 0 # CHECK: This queue is always in channel 0
    dram_loc = epoch_device.get_block_locations (block_type = "dram")[dram_chan]

    table=[]
    for loc in epoch_device.get_block_locations (block_type = "functional_workers"):
        y, x = loc[0], loc[1] # FIX: This is backwards - check.
        EPOCH_QUEUE_START_ADDR = reserved_size_bytes
        offset = (16 * x + y) * ((EPOCH_Q_NUM_SLOTS*2+8)*4)
        dram_addr = EPOCH_QUEUE_START_ADDR + offset
        rdptr = tt_device.pci_read_xy (device_id, dram_loc[0], dram_loc[1], 0, dram_addr)
        wrptr = tt_device.pci_read_xy (device_id, dram_loc[0], dram_loc[1], 0, dram_addr + 4)
        occupancy = (wrptr - rdptr) if wrptr >= rdptr else wrptr - (rdptr - EPOCH_Q_NUM_SLOTS)
        if occupancy > 0:
            table.append ([ f"{x}-{y}", f"0x{dram_addr:x}", f"{rdptr}", f"{wrptr}", occupancy ])

    if len(table) > 0:
        print (tabulate(table, headers=["Location", "Address", "RD ptr", "WR ptr", "Occupancy" ] ))
    else:
        print ("No epoch queues have occupancy > 0")

    util.WARN ("WIP: This results of this function need to be verified")

# A helper to print the result of a single PCI read
def print_a_read (x, y, addr, val, comment=""):
    print(f"{x}-{y} 0x{addr:08x} => 0x{val:08x} ({val:d}) {comment}")

# Perform a burst of PCI reads and print results.
# If burst_type is 1, read the same location for a second and print a report
# If burst_type is 2, read an array of locations once and print a report
def print_burst_read_xy (device_id, x, y, noc_id, addr, burst_type = 1):
    if burst_type == 1:
        values = {}
        t_end = time.time() + 1
        print ("Sampling for 1 second...")
        while time.time() < t_end:
            val = tt_device.pci_read_xy(device_id, x, y, noc_id, addr)
            if val not in values:
                values[val] = 0
            values[val] += 1
        for val in values.keys():
            print_a_read(x, y, addr, val, f"- {values[val]} times")
    elif burst_type >= 2:
        for k in range(0, burst_type):
            val = tt_device.pci_read_xy(device_id, x, y, noc_id, addr + 4*k)
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
def print_navigation_suggestions (navigation_suggestions):
    if navigation_suggestions:
        print ("Speed dial:")
        rows = []
        for i in range (len(navigation_suggestions)):
            rows.append ([ f"{i}", f"{navigation_suggestions[i]['description']}", f"{navigation_suggestions[i]['cmd']}" ])
        print(tabulate(rows, headers=[ "#", "Description", "Command" ]))

# Prints all streams for all chips given by chip_array
def print_stream_summary (context):
    # Finally check and print stream data
    for device_id, device in enumerate (context.devices):
        print (f"{util.CLR_INFO}Reading and analyzing streams on device %d...{util.CLR_END}" % device_id)
        streams_ui_data = tt_device.read_all_stream_registers ()
        stream_summary(chip, grayskull.x_coords, grayskull.y_coords, streams_ui_data)

# Prints contents of core's memory
def dump_memory(chip, x, y, addr, size):
    for k in range(0, size//4//16 + 1):
        row = []
        for j in range(0, 16):
            if (addr + k*64 + j* 4 < addr + size):
                val = tt_device.pci_read_xy(chip, x, y, 0, addr + k*64 + j*4)
                row.append(f"0x{val:08x}")
        s = " ".join(row)
        print(f"{x}-{y} 0x{(addr + k*64):08x} => {s}")

# gets information about stream buffer in l1 cache from blob
def get_l1_buffer_info_from_blob(device_id, graph, x, y, stream_id, phase):
    buffer_addr = 0
    msg_size = 0
    buffer_size = 0

    stream_loc = (device_id, x, y, stream_id, phase)
    stream = graph.streams[stream_loc]

    if stream.root.get("buf_addr"):
        buffer_addr = stream.root.get("buf_addr")
        buffer_size = stream.root.get("buf_size")
        msg_size =stream.root.get("msg_size")
    return buffer_addr, buffer_size, msg_size

# dumps message in hex format
def dump_message_xy(cmd, context, ui_state):
    message_id = int(cmd[1])
    device_id = ui_state['current_device_id']
    epoch_id = ui_state ['current_epoch_id']
    graph_name = context.netlist.epoch_id_to_graph_name(epoch_id)
    graph = context.netlist.graph(graph_name)
    current_device = context.devices[device_id]
    x, y, stream_id = ui_state['current_x'], ui_state['current_y'], ui_state['current_stream_id']
    current_phase = current_device.get_stream_phase (x, y, stream_id)
    buffer_addr, buffer_size, msg_size = get_l1_buffer_info_from_blob(device_id, graph, x, y, stream_id, current_phase)
    print(f"{x}-{y} buffer_addr: 0x{(buffer_addr):08x} buffer_size: 0x{buffer_size:0x} msg_size:{msg_size}")
    if (buffer_addr >0 and buffer_size>0 and msg_size>0) :
        if (message_id> 0 and message_id <= buffer_size/msg_size):
            dump_memory(device_id, x, y, buffer_addr + (message_id - 1) * msg_size, msg_size )
        else:
            print(f"Message id should be in range (1, {buffer_size//msg_size})")
    else:
        print("Not enough data in blob.yaml")

# Test command for development only
def test_command(cmd, context, ui_state):
    return 0

# Main
def main(args, context):
    cmd_raw = ""

    # Init commands
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
          "long" : "full-dump",
          "short" : "fd",
          "expected_argument_count" : 0,
          "arguments_description" : ": performs a full dump at current x-y"
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
          "long" : "test",
          "short" : "t",
          "expected_argument_count" : 0,
          "arguments_description" : ": test for development"
        }
    ]

    import_commands (commands)

    # Initialize current UI state
    ui_state = {
        "current_x": 1,           # Currently selected core (noc0 coordinates)
        "current_y": 1,
        "current_stream_id": 8,   # Currently selected stream_id
        "current_epoch_id": 0,    # Current epoch_id
        "current_graph_name": "", # Graph name for the current epoch
        "current_prompt": ""      # Based on the current x,y,stream_id tuple
    }

    navigation_suggestions = None

    def change_epoch (new_epoch_id):
        if context.netlist.epoch_id_to_graph_name(new_epoch_id) is not None:
            nonlocal ui_state
            ui_state["current_epoch_id"] = new_epoch_id
        else:
            print (f"{util.CLR_ERR}Invalid epoch id {new_epoch_id}{util.CLR_END}")

    # These commands will be executed right away (before allowing user input)
    non_interactive_commands=args.commands.split(";") if args.commands else []

    # Main command loop
    while cmd_raw != 'exit' and cmd_raw != 'x':
        have_non_interactive_commands=len(non_interactive_commands) > 0

        if ui_state['current_x'] is not None and ui_state['current_y'] is not None and ui_state['current_epoch_id'] is not None:
            row, col = grayskull.noc0_to_rc (ui_state['current_x'], ui_state['current_y'])
            ui_state['current_prompt'] = f"core:{util.CLR_PROMPT}{ui_state['current_x']}-{ui_state['current_y']}{util.CLR_END} rc:{util.CLR_PROMPT}{row},{col}{util.CLR_END} stream:{util.CLR_PROMPT}{ui_state['current_stream_id']}{util.CLR_END} "

        try:
            ui_state['current_graph_name'] = context.netlist.epoch_id_to_graph_name(ui_state['current_epoch_id'])
            ui_state['current_device_id'] = context.netlist.graph_name_to_device_id(ui_state['current_graph_name'])
            ui_state['current_device'] = context.devices[ui_state['current_device_id']]

            print_navigation_suggestions (navigation_suggestions)

            if have_non_interactive_commands:
                cmd_raw = non_interactive_commands[0].strip()
                if cmd_raw == 'exit' or cmd_raw == 'x':
                    continue
                non_interactive_commands=non_interactive_commands[1:]
                if len(cmd_raw)>0:
                    print (f"{util.CLR_INFO}Executing command: %s{util.CLR_END}" % cmd_raw)
            else:
                prompt = f"Current epoch:{util.CLR_PROMPT}{ui_state['current_epoch_id']}{util.CLR_END}({ui_state['current_graph_name']}) device:{util.CLR_PROMPT}{ui_state['current_device_id']}{util.CLR_END} {ui_state['current_prompt']}> "
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
                    if found_command["long"] == "exit":
                        pass # Exit is handled in the outter loop
                    elif found_command["long"] == "help":
                        print_available_commands (commands)
                    elif found_command["long"] == "test":
                        test_command (cmd, context, ui_state)
                    elif found_command["long"] == "epoch":
                        change_epoch (int(cmd[1]))
                    elif found_command["long"] == "buffer":
                        navigation_suggestions = print_buffer_data (cmd, context)
                    elif found_command["long"] == "pipe":
                        navigation_suggestions = print_pipe_data (cmd, context)
                    elif found_command["long"] == "pci-read-xy" or found_command["long"] == "burst-read-xy" or found_command["long"] == "pci-write-xy":
                        x = int(cmd[1],0)
                        y = int(cmd[2],0)
                        addr = int(cmd[3],0)
                        if found_command["long"] == "pci-read-xy":
                            data = tt_device.pci_read_xy (ui_state['current_device_id'], x, y, NOC0, addr)
                            print_a_read (x, y, addr, data)
                        elif found_command["long"] == "burst-read-xy":
                            burst_type = int(cmd[4],0)
                            print_burst_read_xy (ui_state['current_device_id'], x, y, NOC0, addr, burst_type=burst_type)
                        elif found_command["long"] == "pci-write-xy":
                            tt_device.pci_write_xy (ui_state['current_device_id'], x, y, NOC0, addr, data = int(cmd[4],0))
                        else:
                            print (f"{util.CLR_ERR} Unknown {found_command['long']} {util.CLR_END}")
                    elif found_command["long"] == "full-dump":
                        ui_state['current_device'].full_dump_xy(ui_state['current_x'], ui_state['current_y'])
                    elif found_command["long"] == "dram-queue":
                        print_dram_queue_summary (cmd, context, ui_state)
                    elif found_command["long"] == "host-queue":
                        print_host_queue (cmd, context, ui_state)
                    elif found_command["long"] == "epoch-queue":
                        print_epoch_queue_summary(cmd, context, ui_state)
                    elif found_command["long"] == "dump-message-xy":
                        dump_message_xy(cmd, context, ui_state)

                    elif found_command["long"] == "stream-summary":
                        print_stream_summary(cmd, context, ui_state)
                    elif found_command["long"] == "stream":
                        ui_state['current_x'], ui_state['current_y'], ui_state['current_stream_id'] = int(cmd[1]), int(cmd[2]), int(cmd[3])
                        navigation_suggestions, stream_epoch_id = print_stream (ui_state['current_device'], ui_state['current_x'], ui_state['current_y'], ui_state['current_stream_id'], ui_state['current_epoch_id'])
                        if stream_epoch_id != ui_state['current_epoch_id'] and stream_epoch_id >=0:
                            print (f"{util.CLR_WARN}Automatically switching to epoch {stream_epoch_id}{util.CLR_END}")
                            change_epoch (stream_epoch_id)
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
tt_device.init_comm_client (args.debug_debuda_stub)

# Make sure to terminate communication client (debuda-stub) on exit
atexit.register (tt_device.terminate_comm_client_callback)

# Create context
context = tt_objects.load (netlist_filepath = args.netlist, run_dirpath = args.output_dir)

# Main function
sys.exit (main(args, context))
