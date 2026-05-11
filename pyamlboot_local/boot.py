#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import time
from . import proto


def parse_cmdline():
    parser = argparse.ArgumentParser(description="USB boot tool for Amlogic G12 SoCs",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--version', '-v', action='version', version='%(prog)s 1.0')
    parser.add_argument('binary',  action='store',
                        help="binary to load or name of board")
    args = parser.parse_args()

    return args


def main(file_path):
    dev = proto.AmlogicSoC()

    loadAddr = 0xfffa0000
    with open(file_path, "rb") as f:
        seq = 0
        data = f.read()

        dev.writeLargeMemory(0xfffa0000, data[0:0x10000], 4096)
        dev.run(0xfffa0000)

        time.sleep(2)

        prevLength = -1
        prevOffset = -1
        while True:
            (length, offset) = dev.getBootAMLC()

            if length == prevLength and offset == prevOffset:
                break

            prevLength = length
            prevOffset = offset

            dev.writeAMLCData(seq, offset, data[offset:offset + length])

            seq = seq + 1


if __name__ == '__main__':
    args = parse_cmdline()
    bpath = args.binary
    main(bpath)
