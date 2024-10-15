// SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
//
// SPDX-License-Identifier: Apache-2.0
#include "yaml_communication.h"

#include <string>

#include "dbdserver/requests.h"

void yaml_communication::process(const tt::dbd::request& request) {
    switch (request.type) {
        case tt::dbd::request_type::ping:
        case tt::dbd::request_type::get_runtime_data:
        case tt::dbd::request_type::get_cluster_description:
        case tt::dbd::request_type::get_device_ids:
        case tt::dbd::request_type::get_buda_run_dirpath:
            respond(serialize(request));
            break;

        case tt::dbd::request_type::pci_write32:
            respond(serialize(static_cast<const tt::dbd::pci_write32_request&>(request)));
            break;
        case tt::dbd::request_type::pci_read32:
            respond(serialize(static_cast<const tt::dbd::pci_read32_request&>(request)));
            break;
        case tt::dbd::request_type::pci_read:
            respond(serialize(static_cast<const tt::dbd::pci_read_request&>(request)));
            break;
        case tt::dbd::request_type::pci_write:
            respond(serialize(static_cast<const tt::dbd::pci_write_request&>(request)));
            break;
        case tt::dbd::request_type::pci_read32_raw:
            respond(serialize(static_cast<const tt::dbd::pci_read32_raw_request&>(request)));
            break;
        case tt::dbd::request_type::pci_write32_raw:
            respond(serialize(static_cast<const tt::dbd::pci_write32_raw_request&>(request)));
            break;
        case tt::dbd::request_type::dma_buffer_read32:
            respond(serialize(static_cast<const tt::dbd::dma_buffer_read32_request&>(request)));
            break;
        case tt::dbd::request_type::pci_read_tile:
            respond(serialize(static_cast<const tt::dbd::pci_read_tile_request&>(request)));
            break;
        case tt::dbd::request_type::get_harvester_coordinate_translation:
            respond(serialize(static_cast<const tt::dbd::get_harvester_coordinate_translation_request&>(request)));
            break;
        case tt::dbd::request_type::get_device_arch:
            respond(serialize(static_cast<const tt::dbd::get_device_arch_request&>(request)));
            break;
        case tt::dbd::request_type::get_device_soc_description:
            respond(serialize(static_cast<const tt::dbd::get_device_soc_description_request&>(request)));
            break;
        case tt::dbd::request_type::get_file:
            respond(serialize(static_cast<const tt::dbd::get_file_request&>(request)));
            break;
        case tt::dbd::request_type::arc_msg:
            respond(serialize(static_cast<const tt::dbd::arc_msg_request&>(request)));
            break;
        default:
            respond("NOT_IMPLEMENTED_YAML_SERIALIZATION for " + std::to_string(static_cast<int>(request.type)));
            break;
    }
}

std::string yaml_communication::serialize(const tt::dbd::request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type));
}

std::string yaml_communication::serialize(const tt::dbd::pci_read32_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id) + "\n  noc_x: " + std::to_string(request.noc_x) +
           "\n  noc_y: " + std::to_string(request.noc_y) + "\n  address: " + std::to_string(request.address);
}

std::string yaml_communication::serialize(const tt::dbd::pci_write32_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id) + "\n  noc_x: " + std::to_string(request.noc_x) +
           "\n  noc_y: " + std::to_string(request.noc_y) + "\n  address: " + std::to_string(request.address) +
           "\n  data: " + std::to_string(request.data);
}

std::string yaml_communication::serialize(const tt::dbd::pci_read_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id) + "\n  noc_x: " + std::to_string(request.noc_x) +
           "\n  noc_y: " + std::to_string(request.noc_y) + "\n  address: " + std::to_string(request.address) +
           "\n  size: " + std::to_string(request.size);
}

std::string yaml_communication::serialize(const tt::dbd::pci_write_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id) + "\n  noc_x: " + std::to_string(request.noc_x) +
           "\n  noc_y: " + std::to_string(request.noc_y) + "\n  address: " + std::to_string(request.address) +
           "\n  size: " + std::to_string(request.size) + "\n  data: " + serialize_bytes(request.data, request.size);
}

std::string yaml_communication::serialize(const tt::dbd::pci_read32_raw_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id) + "\n  address: " + std::to_string(request.address);
}

std::string yaml_communication::serialize(const tt::dbd::pci_write32_raw_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id) + "\n  address: " + std::to_string(request.address) +
           "\n  data: " + std::to_string(request.data);
}

std::string yaml_communication::serialize(const tt::dbd::dma_buffer_read32_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id) + "\n  address: " + std::to_string(request.address) +
           "\n  channel: " + std::to_string(request.channel);
}

std::string yaml_communication::serialize(const tt::dbd::pci_read_tile_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id) + "\n  noc_x: " + std::to_string(request.noc_x) +
           "\n  noc_y: " + std::to_string(request.noc_y) + "\n  address: " + std::to_string(request.address) +
           "\n  size: " + std::to_string(request.size) + "\n  data_format: " + std::to_string(request.data_format);
}

std::string yaml_communication::serialize(const tt::dbd::get_harvester_coordinate_translation_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id);
}

std::string yaml_communication::serialize(const tt::dbd::get_device_arch_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id);
}

std::string yaml_communication::serialize(const tt::dbd::get_device_soc_description_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id);
}

std::string yaml_communication::serialize(const tt::dbd::get_file_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) + "\n  size: " + std::to_string(request.size) +
           "\n  path: " + std::string(request.data, request.size);
}

std::string yaml_communication::serialize(const tt::dbd::arc_msg_request& request) {
    return "- type: " + std::to_string(static_cast<int>(request.type)) +
           "\n  chip_id: " + std::to_string(request.chip_id) + "\n  msg_code: " + std::to_string(request.msg_code) +
           "\n  wait_for_done: " + std::to_string(request.wait_for_done) + "\n  arg0: " + std::to_string(request.arg0) +
           "\n  arg1: " + std::to_string(request.arg1) + "\n  timeout: " + std::to_string(request.timeout);
}

std::string yaml_communication::serialize_bytes(const uint8_t* data, size_t size) {
    std::string bytes;

    for (size_t i = 0; i < size; i++) {
        if (!bytes.empty()) {
            bytes += ", ";
        }
        bytes += std::to_string(data[i]);
    }
    return "[" + bytes + "]";
}
