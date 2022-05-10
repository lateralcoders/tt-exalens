#!/bin/bash
TMP_OUT_FILE=build/test/dbd-out.tmp

if [ $1 = "skip-build" ]; then
    echo Skipping build
else
    echo make build_hwq
    make build_hw
    echo Building verif/op_tests ...
    make verif/op_tests > $TMP_OUT_FILE
fi

echo Running op_tests/test_op ...
./build/test/verif/op_tests/test_op --netlist verif/op_tests/netlists/netlist_matmul_op_with_fd.yaml --seed 0 --silicon --timeout 500 > $TMP_OUT_FILE
if [ $? -ne 0 ]; then
    echo Error in running ./build/test/verif/op_tests/test_op
    exit 1
fi

dbd/debuda.py  tt_build/test_op_6142509188972423790 --netlist verif/op_tests/netlists/netlist_matmul_op_with_fd.yaml --commands "s 1 1 24;exit"
if [ $? -ne 0 ]; then
    echo Error in running dbd/debuda.py
    exit 2
fi
