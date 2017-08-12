#! /usr/bin/python
from __future__ import print_function

from onos_sdn_topology import OnosSdnTopology

SDN_CLI_DESCRIPTION = '''CLI-based interface to the various sdn_topology implementations.
Useful for configuring an SDN controller to install flows without having to manually type them out
and properly format them.'''

# @author: Kyle Benson
# (c) Kyle Benson 2017

import argparse
import logging as log
#from os.path import isdir
#from os import listdir
#from getpass import getpass
#password = getpass('Enter password: ')

def parse_args(args):
##################################################################################
#################      ARGUMENTS       ###########################################
# ArgumentParser.add_argument(name or flags...[, action][, nargs][, const][, default][, type][, choices][, required][, help][, metavar][, dest])
# action is one of: store[_const,_true,_false], append[_const], count
# nargs is one of: N, ?(defaults to const when no args), *, +, argparse.REMAINDER
# help supports %(var)s: help='default value is %(default)s'
# Mutually exclusive arguments:
# group = parser.add_mutually_exclusive_group()
# group.add_argument(...)
##################################################################################

    parser = argparse.ArgumentParser(description=SDN_CLI_DESCRIPTION,
                                     #formatter_class=argparse.RawTextHelpFormatter,
                                     #epilog='Text to display at the end of the help print',
                                     #parents=[parent1,...], # add parser args from these ArgumentParsers
                                     # NOTE: for multiple levels of arg
                                     # scripts, will have to use add_help=False
                                     # or may consider using parse_known_args()
                                     )

    # Configuring the manager itself
    parser.add_argument('--type', type=str, default='onos',
                        help='''type of SDN topology manager to run operations on (default=%(default)s)''')
    parser.add_argument('--ip', type=str, default='localhost',
                        help='''SDN controller's host address (default=%(default)s)''')
    parser.add_argument('--port', '-p', type=int, default=8181,
                        help='''SDN controller's REST API port (default=%(default)s)''')

    # Displaying info
    parser.add_argument('command', default=['hosts'], nargs='*',
                        help='''command to execute can be one of (default=%(default)s):
                        hosts (include_attributes)    - print the available hosts (with attributes if optional argument is yes/true)
                        path (src, dst) - build and install a path between src and dst using flow rules
                        mcast (addr, src, dst1, dst2, ...)  - build and install a multicast tree for IP address 'addr'
                               from src to all of dst1,2... using flow rules
                        del-flows (no args)   - deletes all flow rules
                        del-groups (no args)  - deletes all groups
                        ''')


    # joins logging facility with argparse
    # parser.add_argument('--debug', '-d', type=str, default='info', nargs='?', const='debug',
    #                     help=''set debug level for logging facility (default=%(default)s, %(const)s when specified with no arg)'')


    return parser.parse_args(args)

if __name__ == "__main__":
    import sys
    args = parse_args(sys.argv[1:])

    if args.type == 'onos':
        topo = OnosSdnTopology(ip=args.ip, port=args.port)
    else:
        raise ValueError("unrecognized / unsupported SdnTopology implementation of type: %s" % args.type)

    cmd = args.command[0]
    cmd_args = args.command[1:] if len(args.command) > 0 else None
    if cmd == 'hosts':
        include_attrs = True if (cmd_args and cmd_args[0].lower() in ('y', 'yes', 't', 'true')) else False
        if include_attrs:
            print("Hosts:\n%s" % '\n'.join(str(h) for h in topo.get_hosts(attributes=True)))
        else:
            print("Hosts:\n%s" % '\n'.join(topo.get_hosts()))

    elif cmd == 'path':
        assert len(cmd_args) == 2, "path command must have exactly 2 hosts specified!"
        path = topo.get_path(cmd_args[0], cmd_args[1])
        print("Installing Path:", path)
        rules = topo.build_flow_rules_from_path(path)
        rules.extend(topo.build_flow_rules_from_path(list(reversed(path))))
        for rule in rules:
            assert topo.install_flow_rule(rule), "error installing rule: %s" % rule

    elif cmd == 'mcast' or cmd == 'multicast':
        assert len(cmd_args) >= 3, "must specify an IP address and at least 2 host IDs to build a multicast tree!"
        # TODO: validate that first arg is an IP address...
        src_host = cmd_args[1]
        mcast_tree = topo.get_multicast_tree(src_host, cmd_args[2:])
        address = cmd_args[0]

        print("Installing multicast tree:", list(mcast_tree.nodes()))
        matches = topo.build_matches(ipv4_src=topo.get_ip_address(src_host), ipv4_dst=address, eth_type='0x0800')
        gflows, flows = topo.build_flow_rules_from_multicast_tree(mcast_tree, src_host, matches)

        print("installing groups:", gflows)
        for gf in gflows:
            assert topo.install_group(gf)

        print("installing flows:", flows)
        for flow in flows:
            assert topo.install_flow_rule(flow), "problem installing flow: %s" % flow

    elif cmd == 'del-flows':
        topo.remove_all_flow_rules()
    elif cmd == 'del-groups':
        topo.remove_all_groups()


    # enables logging for all classes
    # log_level = log.getLevelName(args.debug.upper())
    # log.basicConfig(format='%(levelname)s:%(message)s', level=log_level)