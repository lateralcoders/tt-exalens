import tt_util as util, os
import tt_device

phase_state_map = {
    0: "PHASE_START",
    1: "PHASE_AUTO_CONFIG",
    2: "PHASE_AUTO_CONFIG_SENT",
    3: "PHASE_ADVANCE_WAIT",
    4: "PHASE_PREV_DATA_FLUSH_WAIT",
    5: "PHASE_FWD_DATA"
    }

dest_state_map = {
    0 : "DEST_IDLE",
    1 : "DEST_REMOTE",
    2 : "DEST_LOCAL_RDY_WAIT",
    3 : "DEST_LOCAL_HS",
    4 : "DEST_LOCAL",
    5 : "DEST_ENDPOINT",
    6 : "DEST_NO_FWD"
    }

dest_ready_state_map = {
    0 : "DEST_READY_IDLE",
    1 : "DEST_READY_SEND_FIRST",
    2 : "DEST_READY_WAIT_DATA",
    3 : "DEST_READY_SEND_SECOND",
    4 : "DEST_READY_FWD_DATA"
    }

src_ready_state_map = {
    0 : "SRC_READY_IDLE",
    1 : "SRC_READY_WAIT_CFG",
    2 : "SRC_READY_DEST_READY_TABLE_RD",
    3 : "SRC_READY_SEND_UPDATE",
    4 : "SRC_READY_WAIT_ALL_DESTS",
    5 : "SRC_READY_FWD_DATA"
    }

src_state_map = {
    0 : "SRC_IDLE",
    1 : "SRC_REMOTE",
    2 : "SRC_LOCAL",
    3 : "SRC_ENDPOINT"
    }

def test_rc_to_noc0 ():
    for r in range (0,10):
        for c in range (0,10):
            nx, ny = rc_to_noc0(r, c)
            print (f"Checking rc {r},{c} -> {nx}-{ny}", end='')
            nr, nc = noc0_to_rc(nx, ny)
            if not (c == nc and r == nr):
                print ("Not good: ", r, c, nr, nc)
            else:
                print()

# Returns a stream type based on KERNEL_OPERAND_MAPPING_SCHEME
def stream_type (stream_id):
    # From src/firmware/riscv/grayskull/stream_io_map.h
    # Kernel operand mapping scheme:
    KERNEL_OPERAND_MAPPING_SCHEME = [
        { "id_min" : 0,  "id_max" : 7,  "stream_id_min" : 0, "short" : "??", "long" : "????? => streams 0-7" }, # FIX THIS
        { "id_min" : 0,  "id_max" : 7,  "stream_id_min" : 8, "short" : "input", "long" : "(inputs, unpacker-only) => streams 8-15" },
        { "id_min" : 8,  "id_max" : 15, "stream_id_min" : 16, "short" : "param", "long" : "(params, unpacker-only) => streams 16-23" },
        { "id_min" : 16, "id_max" : 23, "stream_id_min" : 24, "short" : "output", "long" : "(outputs, packer-only) => streams 24-31" },
        { "id_min" : 24, "id_max" : 31, "stream_id_min" : 32, "short" : "intermediate", "long" : "(intermediates, packer/unpacker) => streams 32-39" },
        { "id_min" : 32, "id_max" : 63, "stream_id_min" : 32, "short" : "op-relay", "long" : "(operand relay?) => streams 40-63" }, # CHECK THIS
    ]
    for ko in KERNEL_OPERAND_MAPPING_SCHEME:
        s_id_min = ko["stream_id_min"]
        s_id_count = ko["id_max"] - ko["id_min"]
        if stream_id >= s_id_min and stream_id < s_id_min + s_id_count:
            return ko
    util.WARN ("no desc for stream_id=%s" % stream_id)
    return "-"

# Populates a dict with register names and current values on core x-y for stream with id 'stream_id'
def read_stream_regs(chip, x, y, stream_id):
    reg = {}
    reg["STREAM_ID"] =                                            get_stream_reg_field(chip, x, y, stream_id, 224+5, 24, 6)
    reg["PHASE_AUTO_CFG_PTR (word addr)"] =                       get_stream_reg_field(chip, x, y, stream_id, 12, 0, 24)
    reg["CURR_PHASE"] =                                           get_stream_reg_field(chip, x, y, stream_id, 11, 0, 20)
    reg["CURR_PHASE_NUM_MSGS_REMAINING"] =                        get_stream_reg_field(chip, x, y, stream_id, 36, 12, 12)
    reg["NUM_MSGS_RECEIVED"] =                                    get_stream_reg_field(chip, x, y, stream_id, 224+5, 0, 24)
    reg["NEXT_MSG_ADDR"] =                                        get_stream_reg_field(chip, x, y, stream_id, 224+6, 0, 32)
    reg["NEXT_MSG_SIZE"] =                                        get_stream_reg_field(chip, x, y, stream_id, 224+7, 0, 32)
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
            reg["MCAST_DEST_NUM"] =                               get_stream_reg_field(chip, x, y, stream_id, 14, 0, 16)
            for i in range(0, 31):
                reg["DEST_BUF_SPACE_AVAILABLE[{i:d}]"] =          get_stream_reg_field(chip, x, y, stream_id, 64+i, 0, 32)
        else:
            reg["DEST_BUF_SPACE_AVAILABLE[0]"] =                  get_stream_reg_field(chip, x, y, stream_id, 64, 0, 32)

    if local_sources_connected == 1:
        local_src_mask_lo =                                       get_stream_reg_field(chip, x, y, stream_id, 48, 0, 32)
        local_src_mask_hi =                                       get_stream_reg_field(chip, x, y, stream_id, 49, 0, 32)
        local_src_mask =                                          (local_src_mask_hi << 32) | local_src_mask_lo
        reg["LOCAL_SRC_MASK"] =                                   local_src_mask
        reg["MSG_ARB_GROUP_SIZE"] =                               get_stream_reg_field(chip, x, y, stream_id, 15, 0, 3)
        reg["MSG_SRC_IN_ORDER_FWD"] =                             get_stream_reg_field(chip, x, y, stream_id, 15, 3, 1)
        reg["STREAM_MSG_SRC_IN_ORDER_FWD_NUM_MSREG_INDEX"] =      get_stream_reg_field(chip, x, y, stream_id, 16, 0, 24)
    else:
        reg["BUF_START (word addr)"] =                            get_stream_reg_field(chip, x, y, stream_id, 6, 0, 16)
        reg["BUF_SIZE (words)"] =                                 get_stream_reg_field(chip, x, y, stream_id, 7, 0, 16)
        reg["BUF_RD_PTR (word addr)"] =                           get_stream_reg_field(chip, x, y, stream_id, 24, 0, 16)
        reg["BUF_WR_PTR (word addr)"] =                           get_stream_reg_field(chip, x, y, stream_id, 25, 0, 16)
        reg["MSG_INFO_PTR (word addr)"] =                         get_stream_reg_field(chip, x, y, stream_id, 8, 0, 16)
        reg["MSG_INFO_WR_PTR (word addr)"] =                      get_stream_reg_field(chip, x, y, stream_id, 26, 0, 16)
        reg["STREAM_BUF_SPACE_AVAILABLE_REG_INDEX (word addr)"] = get_stream_reg_field(chip, x, y, stream_id, 28, 0, 16)
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
        if i == 8:
            phase_state = get_stream_reg_field(chip, x, y, stream_id, 224+i, 0, 4)
            src_ready_state = get_stream_reg_field(chip, x, y, stream_id, 224+i, 4, 3)
            dest_ready_state = get_stream_reg_field(chip, x, y, stream_id, 224+i, 7, 3)
            src_side_phase_complete = get_stream_reg_field(chip, x, y, stream_id, 224+i, 10, 1)
            dest_side_phase_complete = get_stream_reg_field(chip, x, y, stream_id, 224+i, 11, 1)
            src_state = get_stream_reg_field(chip, x, y, stream_id, 224+i, 16, 4)
            dest_state = get_stream_reg_field(chip, x, y, stream_id, 224+i, 20, 3)
            # IMPROVE: add back the interpretation in get_as_str
            reg["PHASE_STATE"] = phase_state
            reg["SRC_READY_STATE"] = src_ready_state
            reg["DEST_READY_STATE"] = dest_ready_state
            reg["SRC_SIDE_PHASE_COMPLETE"] = src_side_phase_complete
            reg["DEST_SIDE_PHASE_COMPLETE"] = dest_side_phase_complete
            reg["SRC_STATE"] = src_state
            reg["DEST_STATE"] = dest_state

    return reg

# Function to print a full dump of a location x-y
def full_dump_xy(chip_id, x, y):
    for stream_id in range (0, 64):
        print()
        stream = read_stream_regs(chip_id, x, y, stream_id)
        for reg, value in stream.items():
            print(f"Tensix x={x:02d},y={y:02d} => stream {stream_id:02d} {reg} = {value}")

    for noc_id in range (0, 2):
        print()
        read_print_noc_reg(chip_id, x, y, noc_id, "nonposted write reqs sent", 0xA)
        read_print_noc_reg(chip_id, x, y, noc_id, "posted write reqs sent", 0xB)
        read_print_noc_reg(chip_id, x, y, noc_id, "nonposted write words sent", 0x8)
        read_print_noc_reg(chip_id, x, y, noc_id, "posted write words sent", 0x9)
        read_print_noc_reg(chip_id, x, y, noc_id, "write acks received", 0x1)
        read_print_noc_reg(chip_id, x, y, noc_id, "read reqs sent", 0x5)
        read_print_noc_reg(chip_id, x, y, noc_id, "read words received", 0x3)
        read_print_noc_reg(chip_id, x, y, noc_id, "read resps received", 0x2)
        print()
        read_print_noc_reg(chip_id, x, y, noc_id, "nonposted write reqs received", 0x1A)
        read_print_noc_reg(chip_id, x, y, noc_id, "posted write reqs received", 0x1B)
        read_print_noc_reg(chip_id, x, y, noc_id, "nonposted write words received", 0x18)
        read_print_noc_reg(chip_id, x, y, noc_id, "posted write words received", 0x19)
        read_print_noc_reg(chip_id, x, y, noc_id, "write acks sent", 0x10)
        read_print_noc_reg(chip_id, x, y, noc_id, "read reqs received", 0x15)
        read_print_noc_reg(chip_id, x, y, noc_id, "read words sent", 0x13)
        read_print_noc_reg(chip_id, x, y, noc_id, "read resps sent", 0x12)
        print()
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x out vc full credit out vc stall", 0x24)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y out vc full credit out vc stall", 0x22)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port niu out vc full credit out vc stall", 0x20)
        print()
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC14 & VC15 dbg", 0x3d)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC12 & VC13 dbg", 0x3c)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC10 & VC11 dbg", 0x3b)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC8 & VC9 dbg", 0x3a)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC6 & VC7 dbg", 0x39)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC4 & VC5 dbg", 0x38)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC2 & VC3 dbg", 0x37)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port x VC0 & VC1 dbg", 0x36)
        print()
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC14 & VC15 dbg", 0x35)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC12 & VC13 dbg", 0x34)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC10 & VC11 dbg", 0x33)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC8 & VC9 dbg", 0x32)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC6 & VC7 dbg", 0x31)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC4 & VC5 dbg", 0x30)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC2 & VC3 dbg", 0x2f)
        read_print_noc_reg(chip_id, x, y, noc_id, "router port y VC0 & VC1 dbg", 0x2e)
        print()
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
    tt_device.PCI_IFC.pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    test_val1 = tt_device.PCI_IFC.pci_read_xy(chip_id, x, y, 0, 0xffb1205c)
    rd_sel = 1
    tt_device.PCI_IFC.pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    test_val2 = tt_device.PCI_IFC.pci_read_xy(chip_id, x, y, 0, 0xffb1205c)

    rd_sel = 0
    sig_sel = 2*5
    tt_device.PCI_IFC.pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    brisc_pc = tt_device.PCI_IFC.pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    # Doesn't work - looks like a bug for selecting inputs > 31 in daisy stop
    # rd_sel = 0
    # sig_sel = 2*16
    # tt_device.PCI_IFC.pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    # nrisc_pc = tt_device.PCI_IFC.pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    rd_sel = 0
    sig_sel = 2*10
    tt_device.PCI_IFC.pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    trisc0_pc = tt_device.PCI_IFC.pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    rd_sel = 0
    sig_sel = 2*11
    tt_device.PCI_IFC.pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    trisc1_pc = tt_device.PCI_IFC.pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    rd_sel = 0
    sig_sel = 2*12
    tt_device.PCI_IFC.pci_write_xy(chip_id, x, y, 0, 0xffb12054, ((en << 29) | (rd_sel << 25) | (daisy_sel << 16) | (sig_sel << 0)))
    trisc2_pc = tt_device.PCI_IFC.pci_read_xy(chip_id, x, y, 0, 0xffb1205c) & pc_mask

    # IH: Commented out to reduce chatter
    print()
    print(f"Tensix x={x:02d},y={y:02d} => dbus_test_val1 (expect 7)={test_val1:x}, dbus_test_val2 (expect A5A5A5A5)={test_val2:x}")
    print(f"Tensix x={x:02d},y={y:02d} => brisc_pc=0x{brisc_pc:x}, trisc0_pc=0x{trisc0_pc:x}, trisc1_pc=0x{trisc1_pc:x}, trisc2_pc=0x{trisc2_pc:x}")

    tt_device.PCI_IFC.pci_write_xy(chip_id, x, y, 0, 0xffb12054, 0)

# Reads and immediately prints a value of a given NOC register
def read_print_noc_reg(chip_id, x, y, noc_id, reg_name, reg_index):
    reg_addr = 0xffb20000 + (noc_id*0x10000) + 0x200 + (reg_index*4)
    val = tt_device.PCI_IFC.pci_read_xy(chip_id, x, y, 0, reg_addr)
    print(f"Tensix x={x:02d},y={y:02d} => NOC{noc_id:d} {reg_name:s} = 0x{val:08x} ({val:d})")

# Extracts and returns a single field of a stream register
def get_stream_reg_field(chip_id, x, y, stream_id, reg_index, start_bit, num_bits):
    reg_addr = 0xFFB40000 + (stream_id*0x1000) + (reg_index*4)
    val = tt_device.PCI_IFC.pci_read_xy(chip_id, x, y, 0, reg_addr)
    mask = (1 << num_bits) - 1
    val = (val >> start_bit) & mask
    return val

# Returns whether the stream is configured
def is_stream_configured(stream_data):
    return int(stream_data['CURR_PHASE']) > 0

def is_stream_idle(stream_data):
    return (stream_data["DEBUG_STATUS[7]"] & 0xfff) == 0xc00
def is_stream_active (stream_data):
    return int (stream_data["CURR_PHASE"]) != 0 and int (stream_data["NUM_MSGS_RECEIVED"]) > 0
def is_bad_stream (stream_data):
    return \
        (stream_data["DEBUG_STATUS[1]"] != 0) or \
        (stream_data["DEBUG_STATUS[2]"] & 0x7) == 0x4 or \
        (stream_data["DEBUG_STATUS[2]"] & 0x7) == 0x2
def is_gsync_hung (chip, x, y):
    return tt_device.PCI_IFC.pci_read_xy(chip, x, y, 0, 0xffb2010c) == 0xB0010000
def is_ncrisc_done (chip, x, y):
    return tt_device.PCI_IFC.pci_read_xy(chip, x, y, 0, 0xffb2010c) == 0x1FFFFFF1

NCRISC_STATUS_REG_ADDR=0xFFB2010C
BRISC_STATUS_REG_ADDR=0xFFB3010C

def get_status_register_desc(register_address, reg_value_on_chip):
    STATUS_REG = {
        NCRISC_STATUS_REG_ADDR : [ #ncrisc
            { "reg_val":[0xA8300000,0xA8200000,0xA8100000], "description" : "Prologue queue header load",                                   "mask":0xFFFFF000, "ver": 0 },
            { "reg_val":[0x11111111],                       "description" : "Main loop begin",                                              "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xC0000000],                       "description" : "Load queue pointers",                                          "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xD0000000],                       "description" : "Which stream id will read queue",                              "mask":0xFFFFF000, "ver": 0 },
            { "reg_val":[0xD1000000],                       "description" : "Queue has data to read",                                       "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xD2000000],                       "description" : "Queue has l1 space",                                           "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xD3000000],                       "description" : "Queue read in progress",                                       "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xE0000000],                       "description" : "Which stream has data in l1 available to push",                "mask":0xFFFFF000, "ver": 0 },
            { "reg_val":[0xE1000000],                       "description" : "Push in progress",                                             "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xF0000000],                       "description" : "Which stream will write queue",                                "mask":0xFFFFF000, "ver": 0 },
            { "reg_val":[0xF0300000],                       "description" : "Waiting for stride to be ready before updating wr pointer",    "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xF1000000],                       "description" : "Needs to write data to dram",                                  "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xF2000000],                       "description" : "Ready to write data to dram",                                  "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xF3000000],                       "description" : "Has data to write to dram",                                    "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xF4000000],                       "description" : "Writing to dram",                                              "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0x20000000],                       "description" : "Amount of written tiles that needs to be cleared",             "mask":0xFFFFF000, "ver": 0 },
            { "reg_val":[0x22222222,0x33333333,0x44444444], "description" : "Epilogue",                                                     "mask":0xFFFFFFFF, "ver": 1 },
            { "reg_val":[0x10000006,0x10000001],            "description" : "Waiting for next epoch",                                       "mask":0xFFFFFFFF, "ver": 1 },
        ],
        BRISC_STATUS_REG_ADDR : [ #brisc
            { "reg_val":[0xB0000000],                       "description" : "Stream restart check",                                         "mask":0xFFFFF000, "ver": 0 },
            { "reg_val":[0xC0000000],                       "description" : "Check whether unpack stream has data",                         "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xD0000000],                       "description" : "Clear unpack stream",                                          "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xE0000000],                       "description" : "Check and push pack stream that has data (TM ops only)",       "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xF0000000],                       "description" : "Reset intermediate streams",                                   "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0xF1000000],                       "description" : "Wait until all streams are idle",                              "mask":0xFFFFFFFF, "ver": 0 },
            { "reg_val":[0x21000000],                       "description" : "Waiting for next epoch",                                       "mask":0xFFFFF000, "ver": 1 },
            { "reg_val":[0x10000001],                       "description" : "Waiting for next epoch",                                       "mask":0xFFFFFFFF, "ver": 1 },
        ]
    }

    if register_address in STATUS_REG:
        reg_value_desc_list = STATUS_REG[register_address]
        for reg_value_desc in reg_value_desc_list:
            mask = reg_value_desc["mask"]
            for reg_val_in_desc in reg_value_desc["reg_val"]:
                if (reg_value_on_chip & mask == reg_val_in_desc):
                    return [reg_value_on_chip, reg_value_desc["description"], reg_value_desc["ver"]]
        return [reg_value_on_chip, "", 2]
    return []

def status_register_summary(device_id, coords, addr, ver = 0):
    status_descs = {}
    for loc in coords:
        status_descs[loc] = get_status_register_desc(addr, tt_device.PCI_IFC.pci_read_xy(device_id, loc[0], loc[1], 0, addr))

    # Print register status
    status_descs_rows = []
    for loc in coords:
        if status_descs[loc] and status_descs[loc][2] <= ver:
            status_descs_rows.append([f"{loc[0]:d}-{loc[1]:d}",f"{status_descs[loc][0]:08x}", f"{status_descs[loc][1]}"]);
    return status_descs_rows

#
# Device
#
class WormholeDevice (tt_device.Device):
    def __init__(self):
        self.yaml_file = util.YamlFile ("device/wormhole_80_arch.yaml")

    # # Some of this can be read from architecture yaml file
    CHANNEL_TO_DRAM_LOC = [(0, 11), (5, 11), (5, 2), (5, 8), (5, 5), (0, 5)]

    # # Physical location mapping
    PHYS_X_TO_NOC_0_X = [ 0, 9, 1, 8, 2, 7, 3, 6, 4, 5 ]
    PHYS_Y_TO_NOC_0_Y = [ 0, 11, 1, 10, 2, 9,  3, 8, 4, 7, 5, 6 ]
    PHYS_X_TO_NOC_1_X = [ 9, 0, 8, 1, 7, 2, 6, 3, 5, 4 ]
    PHYS_Y_TO_NOC_1_Y = [ 11, 0, 10, 1, 9,  2, 8, 3, 7, 4, 6, 5 ]
    NOC_0_X_TO_PHYS_X = util.reverse_mapping_list (PHYS_X_TO_NOC_0_X)
    NOC_0_Y_TO_PHYS_Y = util.reverse_mapping_list (PHYS_Y_TO_NOC_0_Y)
    NOC_1_X_TO_PHYS_X = util.reverse_mapping_list (PHYS_X_TO_NOC_1_X)
    NOC_1_Y_TO_PHYS_Y = util.reverse_mapping_list (PHYS_Y_TO_NOC_1_Y)

    def noc0_to_rc (self, noc0_x, noc0_y):
        if noc0_x == 0 or noc0_x == 5:
            assert False, "NOC0 x=0 and x=5 do not have an RC coordinate"
        if noc0_y == 0 or noc0_y == 6:
            assert False, "NOC0 y=0 and y=6 do not have an RC coordinate"
        row = noc0_y - 1
        col = noc0_x - 1
        if noc0_x > 5: col-=1
        if noc0_y > 6: row-=1
        return row, col

    def rc_to_noc0 (self, row, col):
        noc0_y = row + 1
        noc0_x = col + 1
        if noc0_x >= 5: noc0_x+=1
        if noc0_y >= 6: noc0_y+=1
        return noc0_x, noc0_y

    def stream_type (self, stream_id): return stream_type (stream_id)
    def full_dump_xy(self, x, y): return full_dump_xy(self.id(), x, y)
    def is_stream_idle (self, regs): return is_stream_idle (regs)
    def as_noc_0 (self, x, y, noc_id):
        if noc_id == 0:
            return (x, y)
        else:
            return (self.noc1_to_noc0 (x,y))

    def is_bad_stream(self, regs): return is_bad_stream(regs)
    def is_gsync_hung(self, x, y): return is_gsync_hung(self.id(), x, y)
    def is_ncrisc_done(self, x, y): return is_ncrisc_done(self.id(), x, y)

    def read_stream_regs(self, noc0_loc, stream_id):
        return read_stream_regs (self.id(), noc0_loc[0], noc0_loc[1], stream_id)

    def is_stream_configured (self, stream_regs):
        return is_stream_configured (stream_regs)

    def is_stream_active (self, stream_regs):
        return is_stream_active (stream_regs)

    def stream_epoch (self, stream_regs):
        return int(stream_regs['CURR_PHASE']) >> 10


    def get_stream_phase (self, x, y, stream_id):
        return get_stream_reg_field(self.id(), x, y, stream_id, 11, 0, 20)

    NCRISC_STATUS_REG_ADDR=NCRISC_STATUS_REG_ADDR
    BRISC_STATUS_REG_ADDR=BRISC_STATUS_REG_ADDR
    def status_register_summary(self, addr, ver = 0):
        coords = self.get_block_locations ()
        return status_register_summary(self.id(), coords, addr, ver)

    def rows_with_no_functional_workers(self): return 2
    def cols_with_no_functional_workers(self): return 2
