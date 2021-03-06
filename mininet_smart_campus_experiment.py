#! /usr/bin/env python

# @author: Kyle Benson
# (c) Kyle Benson 2017

import errno
import logging
import os
import random
from subprocess import Popen

import topology_manager
from seismic_warning_test.seismic_alert_common import SEISMIC_PICK_TOPIC, IOT_GENERIC_TOPIC
from smart_campus_experiment import SmartCampusExperiment, DISTANCE_METRIC

log = logging.getLogger(__name__)
LOGGERS_TO_DISABLE = ('sdn_topology', 'topology_manager.sdn_topology', 'connectionpool', 'urllib3.connectionpool')

import json
import argparse
import time
import ipaddress

from mininet.net import Mininet
from mininet.node import RemoteController, Host, OVSKernelSwitch
from mininet.node import Switch  # these just used for types in docstrings
from mininet.cli import CLI
from mininet.link import TCLink

from topology_manager.networkx_sdn_topology import NetworkxSdnTopology

from config import *
from ride.config import *


class MininetSmartCampusExperiment(SmartCampusExperiment):
    """
    Version of SmartCampusExperiment that runs the experiment in Mininet emulation.
    This includes some background traffic and....

    It outputs the following files (where * is a string representing a summary of experiment parameters):
      - results_*.json : the results file output by this experiment that contains all of the parameters
          and information about publishers/subscribers/the following output locations for each experimental run
      - outputs_*/client_events_{$HOST_ID}.json : contains events sent/recvd by seismic client
      - logs_*/{$HOST_ID} : log files storing seismic client/server's stdout/stderr
      NOTE: the folder hierarchy is important as the results_*.json file contains relative paths pointing
          to the other files from its containing directory.

    NOTE: there are 3 different versions of the network topology stored in this object:
      1) self.topo is the NetworkxSdnTopology that we read from a file and use to populate the networks
      2) self.net is the Mininet topology consisting of actual Mininet Hosts and Links
      3) self.topology_adapter is the concrete SdnTopology (e.g. OnosSdnTopology) instance that interfaces with the
         SDN controller responsible for managing the emulated Mininet nodes.
      Generally, the nodes stored as fields of this class are Mininet Switches and the hosts are Mininet Hosts.  Links
      are expressed simply as (host1.name, host2.name) pairs.  Various helper functions exist to help convert between
      these representations and ensure the proper formatting is used as arguments to e.g. launching processes on hosts.
    """

    def __init__(self, controller_ip=CONTROLLER_IP, controller_port=CONTROLLER_REST_API_PORT,
                 # need to save these two params to pass to RideD
                 tree_choosing_heuristic=DEFAULT_TREE_CHOOSING_HEURISTIC, max_alert_retries=None,
                 topology_adapter=DEFAULT_TOPOLOGY_ADAPTER,
                 n_traffic_generators=0, traffic_generator_bandwidth=10,
                 show_cli=False, comparison=None,
                 *args, **kwargs):
        """
        Mininet and the SdnTopology adapter will be started by this constructor.
        NOTE: you must start the remote SDN controller before constructing/running the experiment!
        :param controller_ip: IP address of SDN controller that we point RideD towards: it must be accessible by the server Mininet host!
        :param controller_port: REST API port of SDN controller
        :param tree_choosing_heuristic: explicit in this version since we are running an
         actual emulation and so cannot check all the heuristics at once
        :param max_alert_retries: passed to Ride-D to control # times it retries sending alerts
        :param topology_adapter: type of REST API topology adapter we use: one of 'onos', 'floodlight'
        :param n_traffic_generators: number of background traffic generators to run iperf on
        :param traffic_generator_bandwidth: bandwidth (in Mbps; using UDP) to set the iperf traffic generators to
        :param show_cli: display the Mininet CLI in between each run (useful for debugging)
        :param comparison: disable RIDE-D and use specified comparison strategy (unicast or oracle)
        :param args: see args of superclass
        :param kwargs: see kwargs of superclass
        """

        # We want this parameter overwritten in results file for the proper configuration.
        self.comparison = comparison
        if comparison is not None:
            assert comparison in ('oracle', 'unicast'), "Uncrecognized comparison method: %s" % comparison
            kwargs['tree_construction_algorithm'] = (comparison,)

        super(MininetSmartCampusExperiment, self).__init__(*args, **kwargs)
        # save any additional parameters the Mininet version adds
        self.results['params']['experiment_type'] = 'mininet'
        self.results['params']['tree_choosing_heuristic'] = self.tree_choosing_heuristic = tree_choosing_heuristic
        self.results['params']['max_alert_retries'] = self.max_alert_retries = max_alert_retries
        self.results['params']['n_traffic_generators'] = self.n_traffic_generators = n_traffic_generators
        self.results['params']['traffic_generator_bandwidth'] = self.traffic_generator_bandwidth = traffic_generator_bandwidth

        self.controller_ip = controller_ip
        self.controller_port = controller_port
        self.topology_adapter_type = topology_adapter
        # set later as it needs resetting between runs and must be created after the network starts up
        self.topology_adapter = None
        # This gets passed to seismic hosts
        self.debug_level = kwargs.get('debug', 'error')

        # These will all be filled in by calling setup_topology()
        # NOTE: make sure you reset these between runs so that you don't collect several runs worth of e.g. hosts!
        self.hosts = []
        self.switches = []
        self.cloud_gateways = []
        # XXX: see note in setup_topology() about replacing cloud hosts with a switch to ease multi-homing
        self.cloud_switches = []
        self.links = []
        self.net = None
        self.controller = None
        self.nat = None

        self.server_switch = None
        # Save Popen objects to later ensure procs terminate before exiting Mininet
        # or we'll end up with hanging procs.
        self.popens = []
        # Need to save client/server iperf procs separately as we need to terminate the server ones directly.
        self.client_iperfs = []
        self.server_iperfs = []

        # We'll optionally drop to a CLI after the experiment completes for further poking around
        self.show_cli = show_cli

        # HACK: We just manually allocate IP addresses rather than adding a controller API to request them.
        # NOTE: we also have to specify a unique UDP src port for each tree so that responses can be properly routed
        # back along the same tree (otherwise each MDMT would generate the same flow rules and overwrite each other!).
        base_addr = ipaddress.IPv4Address(MULTICAST_ADDRESS_BASE)
        self.mcast_address_pool = [(str(base_addr + i), MULTICAST_ALERT_BASE_SRC_PORT + i) for i in range(kwargs['ntrees'])]

        # Disable some of the more verbose and unnecessary loggers
        for _logger_name in LOGGERS_TO_DISABLE:
            l = logging.getLogger(_logger_name)
            l.setLevel(logging.WARNING)

    @classmethod
    def get_arg_parser(cls, parents=(SmartCampusExperiment.get_arg_parser(),), add_help=True):
        """
        Argument parser that can be combined with others when this class is used in a script.
        Need to not add help options to use that feature, though.
        :param tuple[argparse.ArgumentParser] parents:
        :param add_help: if True, adds help command (set to False if using this arg_parser as a parent)
        :return argparse.ArgumentParser arg_parser:
        """

        # argument parser that can be combined with others when this class is used in a script
        # need to not add help options to use that feature, though
        # TODO: document some behavior that changes with the Mininet version:
        # -- pubs/subs are actual client processes
        arg_parser = argparse.ArgumentParser(parents=parents, add_help=add_help)
        # experimental treatment parameters: all taken from parents
        # background traffic generation
        arg_parser.add_argument('--ngenerators', '-g', default=0, dest='n_traffic_generators', type=int,
                                help='''number of hosts that generate random traffic to cause congestion (default=%(default)s)''')
        arg_parser.add_argument('--generator-bandwidth', '-bw', default=10, dest='traffic_generator_bandwidth', type=float,
                                help='''bandwidth (in Mbps) of iperf for congestion traffic generating hosts (default=%(default)s)''')
        arg_parser.add_argument('--cli', '-cli', dest='show_cli', action='store_true',
                                help='''displays the Mininet CLI after running the experiment. This is useful for
                                debugging problems as it prevents the OVS/controller state from being wiped after
                                the experiment and keeps the network topology up.''')
        arg_parser.add_argument('--comparison', default=None,
                                help='''use the specified comparison strategy rather than RIDE-D.  Can be one of:
                                 unicast (send individual unicast packets to each subscriber),
                                 oracle (modifies experiment duration to allow server to retransmit aggregated
                                 packets enough times that the SDN controller should detect failures and recover paths).''')

        return arg_parser

    @classmethod
    def build_default_results_file_name(cls, args, dirname='results'):
        """
        :param args: argparse object (or plain dict) with all args info (not specifying ALL args is okay)
        :param dirname: directory name to place the results files in
        :return: string representing the output_filename containing a parameter summary for easy identification
        """
        # HACK: we need to add the additional parameters this experiment version bring in
        # We add them at the end, though we'll replace the choosing_heuristic with the comparison metric if specified.
        output_filename = super(MininetSmartCampusExperiment, cls).build_default_results_file_name(args, dirname)
        if isinstance(args, argparse.Namespace):
            choosing_heuristic = args.tree_choosing_heuristic if args.comparison is None else args.comparison
        else:
            choosing_heuristic = args.get('tree_choosing_heuristic', DEFAULT_TREE_CHOOSING_HEURISTIC)\
                if args.get('comparison', None) is None else args['comparison']
        replacement = '_%s.json' % choosing_heuristic
        output_filename = output_filename.replace('.json', replacement)
        return output_filename

    def set_interrupt_signal(self):
        # ignore it so we can terminate Mininet commands without killing Mininet
        # TODO: something else?
        return

    def setup_topology(self):
        """
        Builds the Mininet network, including all hosts, servers, switches, links, and NATs.
        This relies on reading the topology file using a NetworkxSdnTopology helper.

        NOTE: we assume that the topology file was generated by (or follows the same naming conventions as)
        the campus_topo_gen.py module.  In particular, the naming conventions is used to identify different
        types of hosts/switches as well as to assign MAC/IP addresses in a more legible manner.  i.e.
        Hosts are assigned IP addresses with the format "10.[131/200 for major/minor buildings respectively].building#.host#".
        Switch DPIDs (MAC addresses) are assigned with first letter being type (minor buildings are 'a' and
         the server switch is 'e') and the last digits being its #.
        :param str topology_file: file name of topology to import
        """
        self.net = Mininet(topo=None,
                           build=False,
                           ipBase=IP_SUBNET,
                           autoSetMacs=True,
                           # autoStaticArp=True
                           )

        log.info('*** Adding controller')
        self.controller = self.net.addController(name='c0',
                                         controller=RemoteController,
                                         ip=self.controller_ip,
                                         port=OPENFLOW_CONTROLLER_PORT,
                                         )

        # import the switches, hosts, and server(s) from our specified file
        self.topo = NetworkxSdnTopology(self.topology_filename)

        for switch in self.topo.get_switches():
            mac = get_mac_for_switch(switch)
            s = self.net.addSwitch(switch, dpid=mac, cls=OVSKernelSwitch)
            log.debug("adding switch %s at DPID %s" % (switch, s.dpid))
            self.switches.append(s)
            if self.topo.is_cloud_gateway(switch):
                self.cloud_gateways.append(s)

        for host in self.topo.get_hosts():
            _ip, _mac = get_ip_mac_for_host(host)
            h = self.net.addHost(host, ip=_ip, mac=_mac)
            self.hosts.append(h)

        for server in self.topo.get_servers():
            # HACK: we actually add a switch in case the server is multi-homed since it's very
            # difficult to work with multiple interfaces on a host (e.g. ONOS can only handle
            # a single MAC address per host).
            server_switch_name = server.replace('s', 'e')
            server_switch_dpid = get_mac_for_switch(server_switch_name, is_server=True)
            # Keep server name for switch so that the proper links will be added later.
            self.server_switch = self.net.addSwitch(server, dpid=server_switch_dpid, cls=OVSKernelSwitch)
            host = 'h' + server
            _ip, _mac = get_ip_mac_for_host(host)
            s = self.net.addHost(host, ip=_ip, mac=_mac)
            # ENHANCE: handle multiple servers
            self.server = s
            self.net.addLink(self.server_switch, self.server)

        for cloud in self.topo.get_clouds():
            # Only consider the cloud special if we've enabled doing so
            if self.with_cloud:
                # HACK: Same hack with adding local server
                cloud_switch_name = cloud.replace('x', 'f')
                cloud_switch_dpid = get_mac_for_switch(cloud_switch_name, is_cloud=True)
                # Keep server name for switch so that the proper links will be added later.
                cloud_switch = self.net.addSwitch(cloud, dpid=cloud_switch_dpid, cls=OVSKernelSwitch)
                self.cloud_switches.append(cloud_switch)
                # ENHANCE: handle multiple clouds
                host = 'h' + cloud
                _ip, _mac = get_ip_mac_for_host(host)
                self.cloud = self.net.addHost(host, ip=_ip, mac=_mac)
                self.net.addLink(cloud_switch, self.cloud)
            # otherwise just add a host to prevent topology errors
            else:
                self.net.addHost(cloud)
                self.cloud = self.net.addHost(cloud)

        for link in self.topo.get_links():
            from_link = link[0]
            to_link = link[1]
            log.debug("adding link from %s to %s" % (from_link, to_link))

            # Get link attributes for configuring realistic traffic control settings
            # For configuration options, see mininet.link.TCIntf.config()
            attributes = link[2]
            _bw = attributes.get('bw', 10)  # in Mbps
            _delay = '%fms' % attributes.get('latency', 10)
            # TODO: increase jitter for cloud!
            _jitter = '1ms'
            _loss = self.error_rate

            l = self.net.addLink(self.net.get(from_link), self.net.get(to_link),
                                 cls=TCLink, bw=_bw, delay=_delay, jitter=_jitter, loss=_loss
                                 )
            self.links.append(l)

        # add NAT so the server can communicate with SDN controller's REST API
        # NOTE: because we didn't add it to the actual SdnTopology, we don't need
        # to worry about it getting failed.  However, we do need to ensure it
        # connects directly to the server to avoid failures disconnecting it.
        # HACK: directly connect NAT to the server, set a route for it, and
        # handle this hacky IP address configuration
        nat_ip = NAT_SERVER_IP_ADDRESS % 2
        srv_ip = NAT_SERVER_IP_ADDRESS % 3
        self.nat = self.net.addNAT(connect=self.server)
        self.nat.configDefault(ip=nat_ip)

        # Now we set the IP address for the server's new interface.
        # NOTE: we have to set the default route after starting Mininet it seems...
        srv_iface = sorted(self.server.intfNames())[-1]
        self.server.intf(srv_iface).setIP(srv_ip)

    # HACK: because self.topo stores nodes by just their string name, we need to
    # convert them into actual Mininet hosts for use by this experiment.

    def _get_mininet_nodes(self, nodes):
        """
        Choose the actual Mininet Hosts (rather than just strings) that will
        be subscribers.
        :param List[str] nodes:
        :return List[Node] mininet_nodes:
        """
        return [self.net.get(n) for n in nodes]

    def choose_publishers(self):
        """
        Choose the actual Mininet Hosts (rather than just strings) that will
        be publishers.
        :return List[Host] publishers:
        """
        return self._get_mininet_nodes(super(MininetSmartCampusExperiment, self).choose_publishers())

    def choose_subscribers(self):
        """
        Choose the actual Mininet Hosts (rather than just strings) that will
        be subscribers.
        :return List[Host] subscribers:
        """
        return self._get_mininet_nodes(super(MininetSmartCampusExperiment, self).choose_subscribers())

    def choose_server(self):
        """
        Choose the actual Mininet Host (rather than just strings) that will
        be the server.
        :return Host server:
        """
        # HACK: call the super version of this so that we increment the random number generator correctly
        super(MininetSmartCampusExperiment, self).choose_server()
        return self.server

    def get_failed_nodes_links(self):
        fnodes, flinks = super(MininetSmartCampusExperiment, self).get_failed_nodes_links()
        # NOTE: we can just pass the links as strings
        return self._get_mininet_nodes(fnodes), flinks

    def run_experiment(self):
        """
        Configures all appropriate settings, runs the experiment, and
        finally tears it down before returning the results.
        (Assumes Mininet has already been started).

        Returned results is a dict containing the 'logs_dir' and 'outputs_dir' for
        this run as well as lists of 'subscribers' and 'publishers' (their app IDs
        (Mininet node names), which will appear in the name of their output file).

        :rtype dict:
        """

        log.info('*** Starting network')
        log.debug("Building Network...")
        self.net.build()
        log.debug("Network built; starting...")
        self.net.start()
        log.debug("Started!  Waiting for switch connections...")
        self.net.waitConnected()  # ensure switches connect
        log.debug("Switches connected!")

        # give controller time to converge topology so pingall works
        time.sleep(5)

        # setting the server's default route for controller access needs to be
        # done after the network starts up
        nat_ip = self.nat.IP()
        srv_iface = self.server.intfNames()[-1]
        self.server.setDefaultRoute('via %s dev %s' % (nat_ip, srv_iface))

        # We also have to manually configure the routes for the multicast addresses
        # the server will use.
        for a, p in self.mcast_address_pool:
            self.server.setHostRoute(a, self.server.intf().name)

        # this needs to come after starting network or no interfaces/IP addresses will be present
        log.debug("\n".join("added host %s at IP %s" % (host.name, host.IP()) for host in self.net.hosts))
        log.debug('links: %s' % [(l.intf1.name, l.intf2.name) for l in self.net.links])

        # May need to ping the hosts again if links start up too late...
        hosts = [h for h in self.net.hosts if h != self.nat]
        def ping_hosts(hosts):
            log.info('*** Pinging hosts so controller can gather IP addresses...')
            # don't want the NAT involved as hosts won't get a route to it
            # comms and the whole point of this is really just to establish the hosts in the
            # controller's topology.  ALSO: we need to either modify this or call ping manually
            # because having error_rate > 0 leads to ping loss, which could results in a host
            # not being known!
            loss = 0
            if ALL_PAIRS:
                loss = self.net.ping(hosts=hosts, timeout=2)
            else:
                for h in hosts:
                    loss += self.net.ping((h, self.server), timeout=2)
                loss /= len(hosts)

            if loss > 0:
                log.warning("ping had a loss of %f" % loss)

            # This needs to occur AFTER pingAll as the exchange of ARP messages
            # is used by the controller (ONOS) to learn hosts' IP addresses
            # Similarly to ping, we don't need all-pairs... just for the hosts to/from edge/cloud servers
            if ALL_PAIRS:
                self.net.staticArp()
            else:
                server_ip = self.server.IP()
                server_mac = self.server.MAC()
                cloud_ip = self.cloud.IP()
                cloud_mac = self.cloud.MAC()
                for src in hosts:
                    src.setARP(ip=server_ip, mac=server_mac)
                    src.setARP(ip=cloud_ip, mac=cloud_mac)
                    self.cloud.setARP(ip=src.IP(), mac=src.MAC())
                    self.server.setARP(ip=src.IP(), mac=src.MAC())

        ping_hosts(hosts)
        # Need to sleep so that the controller has a chance to converge its topology again...
        time.sleep(5)

        # Now connect the SdnTopology and verify that all the non-NAT hosts, links, and switches are available through it
        expected_nhosts = len(hosts)  # ignore NAT, but include servers
        # Don't forget that we added switches for the servers to easily multi-home them
        expected_nlinks = self.topo.topo.number_of_edges() + (1 if self.server else 0) + (1 if self.cloud else 0)
        expected_nswitches = len(self.topo.get_switches()) + (1 if self.server else 0) + (1 if self.cloud else 0)
        n_sdn_links = 0
        n_sdn_switches = 0
        n_sdn_hosts = 0
        ntries = 1
        while n_sdn_hosts != expected_nhosts or n_sdn_links != expected_nlinks or n_sdn_switches != expected_nswitches:
            self.setup_topology_manager()

            n_sdn_hosts = len(self.topology_adapter.get_hosts())
            n_sdn_links = self.topology_adapter.topo.number_of_edges()
            n_sdn_switches = len(self.topology_adapter.get_switches())

            success = True
            if n_sdn_hosts != expected_nhosts:
                log.warning("topology adapter didn't find all the hosts!  It only got %d/%d.  Trying topology adapter again..." % (n_sdn_hosts, len(hosts)))
                success = False
            if expected_nlinks != n_sdn_links:
                log.warning("topology adapter didn't find all the links!  Only got %d/%d.  Trying topology adapter again..." % (n_sdn_links, expected_nlinks))
                success = False
            if expected_nswitches != n_sdn_switches:
                log.warning("topology adapter didn't find all the switches!  Only got %d/%d.  Trying topology adapter again..." % (n_sdn_switches, expected_nswitches))
                success = False

            time.sleep(2 if success else 10)

            # Sometimes this hangs forever... we should probably try configuring hosts again
            if ntries % 5 == 0 and not success:
                log.warning("pinging hosts again since we still aren't ready with the complete topology...")
                ping_hosts(hosts)
            ntries += 1

        log.info('*** Network set up!\n*** Configuring experiment...')

        self.setup_traffic_generators()
        # NOTE: it takes a second or two for the clients to actually start up!
        # log.debug('*** Starting clients at time %s' % time.time())

        ##    STARTING EXPERIMENT

        logs_dir, outputs_dir = self.setup_seismic_test(self.publishers, self.subscribers, self.server)
        # log.debug('*** Done starting clients at time %s' % time.time())

        ####    FAILURE MODEL     ####

        exp_start_time = time.time()
        log.info('*** Configuration done!  Experiment started at %f; now waiting for failure events...' % exp_start_time)
        # ENCHANCE: instead of just 1 sec before, should try to figure out how long
        # it'll take for different machines/configurations and time it better...
        time.sleep(SEISMIC_EVENT_DELAY)
        quake_time = None

        ###    FAIL / RECOVER DATA PATHS
        # According to the specified configuration, we update each requested DataPath link to the specified status
        # up(recover) / down(fail), sleeping between each iteration to let the system adapt to the changes.

        # XXX: because RideC assigns all publishers to the 'highest priority' (lowest alphanumerically) DP,
        # each iteration should just fail the one with highest priority here to observe the fail-over.
        #
        # Fail ALL DataPaths!  then recover one...
        data_path_changes = [(dpl[0], dpl[1], 'down', TIME_BETWEEN_SEISMIC_EVENTS)
                             for dpl in sorted(self.data_path_links)[1:]]
        # XXX: the first one should happen immediately!
        first_dpl = sorted(self.data_path_links)[0]
        data_path_changes.insert(0, (first_dpl[0], first_dpl[1], 'down', 0))
        data_path_changes.append((first_dpl[0], first_dpl[1], 'up', TIME_BETWEEN_SEISMIC_EVENTS))
        # XXX: since the failure configs can sometimes take a little bit, we should explicitly record when each happened
        output_dp_changes = []

        # We'll fail the first DataPath, then fail the second along with the local links (main earthquake),
        # then eventually recover one of the DataPaths
        for i, (cloud_gw, cloud_switch, new_status, delay) in enumerate(data_path_changes):

            log.debug("waiting for DataPath change...")
            time.sleep(delay)
            dp_change_time = time.time()
            output_dp_changes.append((cloud_gw, new_status, dp_change_time))
            log.debug("%s DataPath link (%s--%s) at time %f" %
                      ("failing" if new_status == 'down' else "recovering", cloud_gw, cloud_switch, dp_change_time))
            self.net.configLinkStatus(cloud_gw, cloud_switch, new_status)

            # First DataPath failure wasn't a 'local earthquake', the second is and will fail part of local topology
            if i == 1:
                # Apply actual failure model: we schedule these to fail when the earthquake hits
                # so there isn't time for the topology to update on the controller,
                # which would skew the results incorrectly. Since it may take a few cycles
                # to fail a lot of nodes/links, we schedule the failures for a second before.
                quake_time = time.time()
                log.info('*** Earthquake at %s!  Applying failure model...' % quake_time)
                for link in self.failed_links:
                    log.debug("failing link: %s" % str(link))
                    self.net.configLinkStatus(link[0], link[1], 'down')
                for node in self.failed_nodes:
                    node.stop(deleteIntfs=False)
                log.debug("done applying failure model at %f" % time.time())

        # wait for the experiment to finish by sleeping for the amount of time we haven't used up already
        remaining_time = exp_start_time + EXPERIMENT_DURATION - time.time()
        log.info("*** Waiting %f seconds for experiment to complete..." % remaining_time)
        if remaining_time > 0:
            time.sleep(remaining_time)

        return {'outputs_dir': outputs_dir, 'logs_dir': logs_dir,
                'quake_start_time': quake_time,
                'data_path_changes': output_dp_changes,
                'publishers': {p.IP(): p.name for p in self.publishers},
                'subscribers': {s.IP(): s.name for s in self.subscribers}}

    def setup_topology_manager(self):
        """
        Starts a SdnTopology for the given controller (topology_manager) type.  Used for setting
        routes, clearing flows, etc.
        :return:
        """
        kwargs = self._get_topology_manager_config()
        self.topology_adapter = topology_manager.build_topology_adapter(**kwargs)

    def _get_topology_manager_config(self):
        """Get configuration parameters for the topology adapter as a dict."""
        kwargs = dict(topology_adapter_type=self.topology_adapter_type,
                      controller_ip=self.controller_ip, controller_port=self.controller_port)
        if self.topology_adapter_type == 'onos':
            kwargs['username'] = ONOS_API_USER
            kwargs['password'] = ONOS_API_PASSWORD
        return kwargs

    def setup_traffic_generators(self):
        """Each traffic generating host starts an iperf process aimed at
        (one of) the server(s) in order to generate random traffic and create
        congestion in the experiment.  Traffic is all UDP because it sets the bandwidth.

        NOTE: iperf v2 added the capability to tell the server when to exit after some time.
        However, we explicitly terminate the server anyway to avoid incompatibility issues."""

        generators = self._get_mininet_nodes(self._choose_random_hosts(self.n_traffic_generators))

        # TODO: include the cloud_server as a possible traffic generation/reception
        # point here?  could also use other hosts as destinations...
        srv = self.server

        log.info("*** Starting background traffic generators")
        # We enumerate the generators to fill the range of ports so that the server
        # can listen for each iperf client.
        for n, g in enumerate(generators):
            log.info("iperf from %s to %s" % (g, srv))
            # can't do self.net.iperf([g,s]) as there's no option to put it in the background
            i = g.popen('iperf -p %d -t %d -u -b %dM -c %s &' % (IPERF_BASE_PORT + n, EXPERIMENT_DURATION,
                                                                 self.traffic_generator_bandwidth, srv.IP()))
            self.client_iperfs.append(i)
            i = srv.popen('iperf -p %d -t %d -u -s &' % (IPERF_BASE_PORT + n, EXPERIMENT_DURATION))
            self.server_iperfs.append(i)


    def setup_seismic_test(self, sensors, subscribers, server):
        """
        Sets up the seismic sensing test scenario in which each sensor reports
        a sensor reading to the server, which will aggregate them together and
        multicast the result back out to each subscriber.  The server uses RIDE-D:
        a reliable multicast method in which several maximally-disjoint multicast
        trees (MDMTs) are installed in the SDN topology and intelligently
        choosen from at alert-time based on various heuristics.
        :param List[Host] sensors:
        :param List[Host] subscribers:
        :param Host server:
        :returns logs_dir, outputs_dir: the directories (relative to the experiment output
         file) in which the logs and output files, respectively, are stored for this run
        """

        delay = SEISMIC_EVENT_DELAY  # seconds before sensors start picking
        quit_time = EXPERIMENT_DURATION

        # HACK: Need to set PYTHONPATH since we don't install our Python modules directly and running Mininet
        # as root strips this variable from our environment.
        env = os.environ.copy()
        ride_dir = os.path.dirname(os.path.abspath(__file__))
        if 'PYTHONPATH' not in env:
            env['PYTHONPATH'] = ride_dir + ':'
        else:
            env['PYTHONPATH'] = env['PYTHONPATH'] + ':' + ride_dir

        # The logs and output files go in nested directories rooted
        # at the same level as the whole experiment's output file.
        # We typically name the output file as results_$PARAMS.json, so cut off the front and extension
        root_dir = os.path.dirname(self.output_filename)
        base_dirname = os.path.splitext(os.path.basename(self.output_filename))[0]
        if base_dirname.startswith('results_'):
            base_dirname = base_dirname[8:]
        if WITH_LOGS:
            logs_dir = os.path.join(root_dir, 'logs_%s' % base_dirname, 'run%d' % self.current_run_number)
            try:
                os.makedirs(logs_dir)
                # XXX: since root is running this, we need to adjust the permissions, but using mode=0777 in os.mkdir()
                # doesn't work for some systems...
                os.chmod(logs_dir, 0777)
            except OSError:
                pass
        else:
            logs_dir = None
        outputs_dir =  os.path.join(root_dir, 'outputs_%s' % base_dirname, 'run%d' % self.current_run_number)
        try:
            os.makedirs(outputs_dir)
            os.chmod(outputs_dir, 0777)
        except OSError:
            pass

        ##############################
        ### SETUP EDGE / CLOUD SERVERS
        ##############################

        server_ip = server.IP()
        assert server_ip != '127.0.0.1', "ERROR: server.IP() returns localhost!"

        log.info("Seismic server on host %s with IP %s" % (server.name, server_ip))

        #### COMPARISON CONFIGS

        # First, we need to set static unicast routes to subscribers for unicast comparison config.
        # This HACK avoids the controller recovering failed paths too quickly due to Mininet's zero latency
        # control plane network.
        # NOTE: because we only set static routes when not using RideD multicast, this shouldn't
        # interfere with other routes.
        # NOTE: using ntrees=0 to imply unicast let's us easily compare the unicast approach with other #s MDMTs
        use_unicast = (self.comparison is not None and self.comparison == 'unicast') or self.ntrees == 0
        use_multicast = not use_unicast
        if use_unicast:
            for sub in subscribers:
                try:
                    # HACK: we get the route from the NetworkxTopology in order to have the same
                    # as other experiments, but then need to convert these paths into one
                    # recognizable by the actual SDN Controller Topology manager.
                    # HACK: since self.server is a new Mininet Host not in original topo, we do this:
                    original_server_name = self.topo.get_servers()[0]
                    route = self.topo.get_path(original_server_name, sub.name, weight=DISTANCE_METRIC)
                    # Next, convert the NetworkxTopology nodes to the proper ID
                    route = self._get_mininet_nodes(route)
                    route = [self.get_node_dpid(n) for n in route]
                    # Then we need to modify the route to account for the real Mininet server 'hs0'
                    route.insert(0, self.get_host_dpid(self.server))
                    log.debug("Installing static route for subscriber %s: %s" % (sub, route))

                    flow_rules = self.topology_adapter.build_flow_rules_from_path(route, priority=STATIC_PATH_FLOW_RULE_PRIORITY)
                    if not self.topology_adapter.install_flow_rules(flow_rules):
                        log.error("problem installing batch of flow rules for subscriber %s: %s" % (sub, flow_rules))
                except Exception as e:
                    log.error("Error installing flow rules for static subscriber routes: %s" % e)
                    raise e
        # For the oracle comparison config we just extend the quit time so the controller has plenty
        # of time to detect and recover from the failures.
        elif self.comparison is not None and self.comparison == 'oracle':
            raise NotImplementedError("no current implementation for 'oracle' comparison: just use calculated result in this run...")

        sdn_topology_cfg = self._get_topology_manager_config()
        # XXX: use controller IP specified in config.py if the default localhost was left
        if sdn_topology_cfg['controller_ip'] == '127.0.0.1':
            sdn_topology_cfg['controller_ip'] = CONTROLLER_IP

        ride_d_cfg = None if not self.with_ride_d else make_scale_config_entry(name="RideD", multicast=use_multicast,
                                                                  class_path="seismic_warning_test.ride_d_event_sink.RideDEventSink",
                                                                  # RideD configurations
                                                                  addresses=self.mcast_address_pool, ntrees=self.ntrees,
                                                                  tree_construction_algorithm=self.tree_construction_algorithm,
                                                                  tree_choosing_heuristic=self.tree_choosing_heuristic,
                                                                  max_retries=self.max_alert_retries,
                                                                  dpid=self.get_host_dpid(self.server),
                                                                  topology_mgr=sdn_topology_cfg,
                                                                  )
        seismic_alert_server_cfg = '' if not self.with_ride_d else make_scale_config_entry(
            class_path="seismic_warning_test.seismic_alert_server.SeismicAlertServer",
            output_events_file=os.path.join(outputs_dir, 'srv'),
            name="EdgeSeismicServer")

        _srv_apps = seismic_alert_server_cfg
        if self.with_ride_c:
            # To run RideC, we need to configure it with the necessary information to register each DataPath under
            # consideration: an ID, the gateway switch DPID, the cloud server's DPID, and the probing source port.
            # The source port will be used to distinguish the different DataPathMonitor probes from each other and
            # route them through the correct gateway using static flow rules.
            # NOTE: because we pass these parameters as tuples in a list, with each tuple containing all info
            # necessary to register a DataPath, we can assume the order remains constant.

            src_ports = range(PROBE_BASE_SRC_PORT, PROBE_BASE_SRC_PORT + len(self.cloud_gateways))
            data_path_args = [[gw.name, self.get_switch_dpid(gw), self.get_host_dpid(self.cloud), src_port] for
                          gw, src_port in zip(self.cloud_gateways, src_ports)]
            log.debug("RideC-managed DataPath arguments are: %s" % data_path_args)

            # We have two different types of IoT data flows (generic and seismic) so we use two different CoAP clients
            # on the publishers to distinguish the traffic, esp. since generic data is sent non-CON!
            publisher_args = [(h.IP(), pub_port) for h in sensors for pub_port in (COAP_CLIENT_BASE_SRC_PORT, COAP_CLIENT_BASE_SRC_PORT+1)]

            _srv_apps += make_scale_config_entry(class_path="seismic_warning_test.ride_c_application.RideCApplication",
                                                 name="RideC", topology_mgr=sdn_topology_cfg, data_paths=data_path_args,
                                                 edge_server=self.get_host_dpid(server),
                                                 cloud_server=self.get_host_dpid(self.cloud),
                                                 publishers=publisher_args,
                                                 reroute_policy=self.reroute_policy,
                                                 )

            # Now set the static routes for probes to travel through the correct DataPath Gateway.
            for gw, src_port in zip(self.cloud_gateways, src_ports):
                gw_dpid = self.get_switch_dpid(gw)
                edge_gw_route = self.topology_adapter.get_path(self.get_host_dpid(self.server), gw_dpid,
                                                               weight=DISTANCE_METRIC)
                gw_cloud_route = self.topology_adapter.get_path(gw_dpid, self.get_host_dpid(self.cloud),
                                                                weight=DISTANCE_METRIC)
                route = self.topology_adapter.merge_paths(edge_gw_route, gw_cloud_route)

                # Need to modify the 'matches' used to include the src/dst_port!
                dst_port = ECHO_SERVER_PORT

                matches = dict(udp_src=src_port, udp_dst=dst_port)
                frules = self.topology_adapter.build_flow_rules_from_path(route, add_matches=matches, priority=STATIC_PATH_FLOW_RULE_PRIORITY)

                # NOTE: need to do the other direction to ensure responses come along same path!
                route.reverse()
                matches = dict(udp_dst=src_port, udp_src=dst_port)
                frules.extend(self.topology_adapter.build_flow_rules_from_path(route, add_matches=matches, priority=STATIC_PATH_FLOW_RULE_PRIORITY))

                # log.debug("installing probe flow rules for DataPath (port=%d)\nroute: %s\nrules: %s" %
                #           (src_port, route, frules))
                if not self.topology_adapter.install_flow_rules(frules):
                    log.error("problem installing batch of flow rules for RideC probes via gateway %s: %s" % (gw, frules))

        srv_cfg = make_scale_config(sinks=ride_d_cfg,
                                    networks=None if not self.with_ride_d else \
                                        make_scale_config_entry(name="CoapServer", events_root="/events/",
                                                                class_path="coap_server.CoapServer"),
                                    # TODO: also run a publisher for that bugfix?
                                    applications=_srv_apps
                                    )

        base_args = "-q %d --log %s" % (quit_time, self.debug_level)
        cmd = SCALE_CLIENT_BASE_COMMAND % (base_args + srv_cfg)

        if WITH_LOGS:
            cmd += " > %s 2>&1" % os.path.join(logs_dir, 'srv')

        log.debug(cmd)
        p = server.popen(cmd, shell=True, env=env)
        self.popens.append(p)

        if self.with_cloud:
            # Now for the cloud, which differs only by the facts that it doesn't run RideC, is always unicast alerting
            # via RideD, and also runs a UdpEchoServer to respond to RideC's DataPath probes
            ride_d_cfg = None if not self.with_ride_d else make_scale_config_entry(name="RideD", multicast=False,
                                                                  class_path="seismic_warning_test.ride_d_event_sink.RideDEventSink",
                                                                  dpid=self.get_host_dpid(self.cloud), addresses=None,
                                                                  )
            seismic_alert_cloud_cfg = '' if not self.with_ride_d else make_scale_config_entry(
                class_path="seismic_warning_test.seismic_alert_server.SeismicAlertServer",
                output_events_file=os.path.join(outputs_dir, 'cloud'),
                name="CloudSeismicServer")
            cloud_apps = seismic_alert_cloud_cfg

            cloud_net_cfg = make_scale_config_entry(class_path='udp_echo_server.UdpEchoServer',
                                                    name='EchoServer', port=ECHO_SERVER_PORT)
            if self.with_ride_d:
                cloud_net_cfg += make_scale_config_entry(name="CoapServer", events_root="/events/",
                                                         class_path="coap_server.CoapServer")

            cloud_cfg = make_scale_config(applications=cloud_apps, sinks=ride_d_cfg, networks=cloud_net_cfg,)

            cmd = SCALE_CLIENT_BASE_COMMAND % (base_args + cloud_cfg)
            if WITH_LOGS:
                cmd += " > %s 2>&1" % os.path.join(logs_dir, 'cloud')

            log.debug(cmd)
            p = self.cloud.popen(cmd, shell=True, env=env)
            self.popens.append(p)

        # XXX: to prevent the 0-latency control plane from resulting in the controller immediately routing around
        # quake-induced failures, we set static routes from each cloud gateway to the subscribers based on their
        # destination IP address.
        # TODO: what to do about cloud server --> cloud gateway?????? how do we decide which gateway (DP) should be used?
        # NOTE: see comments above when doing static routes for unicast comparison configuration about why we
        # should be careful about including a server in the path...
        for sub in subscribers:

            sub_ip = sub.IP()
            matches = self.topology_adapter.build_matches(ipv4_dst=sub_ip, ipv4_src=self.cloud.IP())

            for gw in self.cloud_gateways:
                path = self.topo.get_path(gw.name, sub.name)
                log.debug("installing static route for subscriber: %s" % path)
                path = [self.get_node_dpid(n) for n in self._get_mininet_nodes(path)]
                # XXX: since this helper function assumes the first node is a host, it'll skip over installing
                # rules on it.  Hence, we add the cloud switch serving that gateway as the 'source'...
                path.insert(0, self.get_node_dpid(self.cloud_switches[0]))
                # TODO: what to do with this?  we can't add the cloud or the last gw to be handled will be the one routed through...
                # path.insert(0, self.get_node_dpid(self.cloud))
                frules = self.topology_adapter.build_flow_rules_from_path(path, matches, priority=STATIC_PATH_FLOW_RULE_PRIORITY)

                if not self.topology_adapter.install_flow_rules(frules):
                    log.error("problem installing batch of flow rules for subscriber %s via gateway %s: %s" % (sub, gw, frules))

        ####################
        ###  SETUP CLIENTS
        ####################

        sensors = set(sensors)
        subscribers = set(subscribers)
        # BUGFIX HACK: server only sends data to subs if it receives any, so we run an extra
        # sensor client on the server host so the server process always receives at least one
        # publication.  Otherwise, if no publications reach it the reachability is 0 when it
        # may actually be 1.0! This is used mainly for comparison vs. NetworkxSmartCampusExperiment.
        # TODO: just run the same seismic sensor on the server so that it will always publish SOMETHING
        # sensors.add(server)

        log.info("Running seismic test client on %d subscribers and %d sensors" % (len(subscribers), len(sensors)))

        # If we aren't using the cloud, publishers will just send to the edge and subscribers only have 1 broker
        cloud_ip = server_ip
        alerting_brokers = [server_ip]
        if self.with_cloud:
            cloud_ip = self.cloud.IP()
            alerting_brokers.append(cloud_ip)

        for client in sensors.union(subscribers):
            client_id = client.name

            # Build the cli configs for the two client types
            subs_cfg = make_scale_config(
                networks=make_scale_config_entry(name="CoapServer", class_path="coap_server.CoapServer",
                                                 events_root="/events/"),
                applications=make_scale_config_entry(
                    class_path="seismic_warning_test.seismic_alert_subscriber.SeismicAlertSubscriber",
                    name="SeismicSubscriber", remote_brokers=alerting_brokers,
                    output_file=os.path.join(outputs_dir, 'subscriber_%s' % client_id)))
            pubs_cfg = make_scale_config(
                sensors=make_scale_config_entry(name="SeismicSensor", event_type=SEISMIC_PICK_TOPIC,
                                                dynamic_event_data=dict(seq=0),
                                                class_path="dummy.dummy_virtual_sensor.DummyVirtualSensor",
                                                output_events_file=os.path.join(outputs_dir,
                                                                                'publisher_%s' % client_id),
                                                # Need to start at specific time, not just delay, as it takes a few
                                                # seconds to start up each process.
                                                # TODO: Also spread out the reports a little bit, but we should spread
                                                # out the failures too if we do so: + random.uniform(0, 1)
                                                start_time=time.time() + delay,
                                                sample_interval=TIME_BETWEEN_SEISMIC_EVENTS) +
                # for congestion traffic
                        make_scale_config_entry(name="IoTSensor", event_type=IOT_GENERIC_TOPIC,
                                                dynamic_event_data=dict(seq=0),
                                                class_path="dummy.dummy_virtual_sensor.DummyVirtualSensor",
                                                output_events_file=os.path.join(outputs_dir,
                                                                                'congestor_%s' % client_id),
                                                # give servers a chance to start; spread out their reports too
                                                start_delay=random.uniform(5, 10),
                                                sample_interval=IOT_CONGESTION_INTERVAL)
                ,  # always sink the picks as confirmable, but deliver the congestion traffic best-effort
                sinks=make_scale_config_entry(class_path="remote_coap_event_sink.RemoteCoapEventSink",
                                              name="SeismicCoapEventSink", hostname=cloud_ip,
                                              src_port=COAP_CLIENT_BASE_SRC_PORT,
                                              topics_to_sink=(SEISMIC_PICK_TOPIC,)) +
                      make_scale_config_entry(class_path="remote_coap_event_sink.RemoteCoapEventSink",
                                              name="GenericCoapEventSink", hostname=cloud_ip,
                                              # make sure we distinguish the coapthon client instances from each other!
                                              src_port=COAP_CLIENT_BASE_SRC_PORT + 1,
                                              topics_to_sink=(IOT_GENERIC_TOPIC,), confirmable_messages=False)
                # Can optionally enable this to print out each event in its entirety.
                # + make_scale_config_entry(class_path="log_event_sink.LogEventSink", name="LogSink")
            )

            # Build up the final cli configs, merging the individual ones built above if necessary
            args = base_args
            if client in sensors:
                args += pubs_cfg
            if client in subscribers:
                args += subs_cfg
            cmd = SCALE_CLIENT_BASE_COMMAND % args

            if WITH_LOGS:
                unique_filename = ''
                if client in sensors and client in subscribers:
                    unique_filename = 'ps'
                elif client in sensors:
                    unique_filename = 'p'
                elif client in subscribers:
                    unique_filename = 's'
                unique_filename = '%s_%s' % (unique_filename, client_id)
                cmd += " > %s 2>&1" % os.path.join(logs_dir, unique_filename)

            # the node.sendCmd option in mininet only allows a single
            # outstanding command at a time and cancels any current
            # ones when net.CLI is called.  Hence, we need popen.
            log.debug(cmd)
            p = client.popen(cmd, shell=True, env=env)
            self.popens.append(p)

        # make the paths relative to the root directory in which the whole experiment output file is stored
        # as otherwise the paths are dependent on where the cwd is
        logs_dir = os.path.relpath(logs_dir, root_dir) if WITH_LOGS else None
        outputs_dir = os.path.relpath(outputs_dir, root_dir)
        return logs_dir, outputs_dir

    def teardown_experiment(self):
        log.info("*** Experiment complete! Waiting for all host procs to exit...")

        # need to check if the programs have finished before we exit mininet!
        # NOTE: need to wait more than 10 secs, which is default 'timeout' for CoapServer.listen()
        # TODO: set wait_time to 30? 60?
        time.sleep(20)
        def wait_then_kill(proc, timeout = 1, wait_time = 2):
            assert isinstance(proc, Popen)  # for typing
            ret = None
            for i in range(wait_time/timeout):
                ret = proc.poll()
                if ret is not None:
                    break
                time.sleep(timeout)
            else:
                log.error("process never quit: killing it...")
                proc.kill()
                ret = proc.wait()
                log.error("now it exited with code %d" % ret)
            return ret

        # Inspect the clients first, then the server so it has a little more time to finish up closing
        client_popen_start_idx = 1 if not self.with_cloud else 2

        for p in self.popens[client_popen_start_idx:]:
            ret = wait_then_kill(p)
            if ret is None:
                log.error("Client proc never quit!")
            elif ret != 0:
                # TODO: we'll need to pipe this in from the scale client?
                if ret == errno.ENETUNREACH:
                    # TODO: handle this error appropriately: record failed clients in results?
                    log.error("Client proc failed due to unreachable network!")
                else:
                    log.error("Client proc exited with code %d" % p.returncode)

        ret = wait_then_kill(self.popens[0])
        if ret != 0:
            log.error("server proc exited with code %d" % ret)

        if self.with_cloud:
            ret = wait_then_kill(self.popens[1])
            if ret != 0:
                log.error("cloud proc exited with code %d" % ret)

        # Clean up traffic generators:
        # Clients should terminate automatically, but the server won't do so unless
        # a high enough version of iperf is used so we just do it explicitly.
        for p in self.client_iperfs:
            p.wait()
        for p in self.server_iperfs:
            try:
                wait_then_kill(p)
            except OSError:
                pass  # must have already terminated
        self.popens = []
        self.server_iperfs = []
        self.client_iperfs = []

        # XXX: somehow there still seem to be client processes surviving the .kill() commands; this finishes them off:
        p = Popen(CLEANUP_SCALE_CLIENTS, shell=True)
        p.wait()

        log.debug("*** All processes exited!")

        # But first, give a chance to inspect the experiment state before quitting Mininet.
        if self.show_cli:
            CLI(self.net)

        # BUG: This might error if a process (e.g. iperf) didn't finish exiting.
        try:
            log.debug("Stopping Mininet...")
            self.net.stop()
        except OSError as e:
            log.error("Stopping Mininet failed, but we'll keep going.  Reason: %s" % e)

        # We seem to still have process leakage even after the previous call to stop Mininet,
        # so let's do an explicit clean between each run.
        log.debug("Cleaning up Mininet...")
        p = Popen('sudo mn -c > /dev/null 2>&1', shell=True)
        time.sleep(10 if not TESTING else 2)
        p.wait()

        # Clear out all the flows/groups from controller
        # XXX: this method is quicker/more reliable than going through the REST API since that requires deleting each
        # group one at a time!
        if self.topology_adapter_type == 'onos':
            log.debug("Resetting controller for next run...")
            # XXX: for some reason, doing 'onos wipe-out please' doesn't actually clear out switches!  Hence, we need to
            # fully reset ONOS before the next run and wait for it to completely restart by checking if the API is up.
            p = Popen("%s %s" % (CONTROLLER_RESET_CMD, IGNORE_OUTPUT), shell=True)
            p.wait()

            p = Popen(CONTROLLER_SERVICE_RESTART_CMD, shell=True)
            p.wait()
            onos_running = False

            # We also seem to need to fully reset OVS sometimes for larger topologies
            p = Popen(RESET_OVS, shell=True)
            p.wait()
            p = Popen(RUN_OVS, shell=True)
            p.wait()

            while not onos_running:
                try:
                    # wait first so that if we get a 404 error we'll wait to try again
                    time.sleep(10)
                    ret = self.topology_adapter.rest_api.get_hosts()
                    # Once we get back from the API an empty list of hosts, we know that ONOS is fully-booted.
                    if ret == []:
                        onos_running = True
                        log.debug("ONOS fully-booted!")

                        # Check to make sure the switches were actually cleared...
                        uncleared_switches = self.topology_adapter.rest_api.get_switches()
                        if uncleared_switches:
                            log.error("Why do we still have switches after restarting ONOS??? they are: %s" % uncleared_switches)
                    else:
                        log.debug("hosts not cleared out of ONOS yet...")
                except IOError:
                    log.debug("still waiting for ONOS to fully restart...")

        elif self.topology_adapter is not None:
            log.debug("Removing groups and flows via REST API.  This could take a while while we wait for the transactions to commit...")
            self.topology_adapter.remove_all_flow_rules()

            # We loop over doing this because we want to make sure the groups have been fully removed
            # before continuing to the next run or we'll have serious problems.
            # NOTE: some flows will still be present so we'd have to check them after
            # filtering only those added by REST API, hence only looping over groups for now...
            ngroups = 1
            while ngroups > 0:
                self.topology_adapter.remove_all_groups()
                time.sleep(1)
                leftover_groups = self.topology_adapter.get_groups()
                ngroups = len(leftover_groups)
                # len(leftover_groups) == 0, "Not all groups were cleared after experiment! Still left: %s" % leftover_groups
        else:
            log.warning("No topology adapter!  Cannot reset it between runs...")

        # Reset all of our collections of topology components, processes, etc.  This is copied straight from __init__,
        # but we don't put it in a separate helper function called from there as Pycharm would complain about it...
        self.hosts = []
        self.switches = []
        self.cloud_gateways = []
        self.cloud_switches = []
        self.links = []
        self.net = None
        self.controller = None
        self.nat = None
        self.server_switch = None
        self.popens = []
        self.client_iperfs = []
        self.server_iperfs = []

        # Sleep for a bit so the controller/OVS can finish resetting
        log.debug("*** Done cleaning up the run!  Waiting %dsecs for changes to propagate to OVS/SDN controller..." % SLEEP_TIME_BETWEEN_RUNS)
        time.sleep(SLEEP_TIME_BETWEEN_RUNS)

    def record_result(self, result):
        """Save additional results outputs (or convert to the right format) before outputting them."""
        # Need to save node names rather than actual Mininet nodes for JSON serializing.
        self.failed_nodes = [n.name for n in self.failed_nodes]

        # We'll also record the 'oracle' heuristic now so that we know how many pubs/subs should have been reachable
        # by/to the edge/cloud servers
        ftopo = self.get_failed_topology(self.topo.topo, self.failed_nodes, self.failed_links)
        subscriber_names = result['subscribers'].values()
        publisher_names = result['publishers'].values()
        # XXX: just hard-coding the names since we made them e.g. hs0
        server_name = 's0'
        cloud_name = 'x0'

        result['oracle_edge_subs'] = SmartCampusExperiment.get_oracle_reachability(subscriber_names, server_name, ftopo)
        result['oracle_edge_pubs'] = SmartCampusExperiment.get_oracle_reachability(publisher_names, server_name, ftopo)
        if self.with_cloud:
            # we need to remove the first DP link since it'd be failed:
            # XXX: we can just hack the gateway off that we know is always there
            ftopo.remove_node('g0')
            result['oracle_cloud_subs'] = SmartCampusExperiment.get_oracle_reachability(subscriber_names, cloud_name, ftopo)
            result['oracle_cloud_pubs'] = SmartCampusExperiment.get_oracle_reachability(publisher_names, cloud_name, ftopo)

        super(MininetSmartCampusExperiment, self).record_result(result)

    ####   Helper functions for working with Mininet nodes/links    ####

    def get_host_dpid(self, host):
        """
        Returns the data plane ID for the given host that is recognized by the
        particular SDN controller currently in use.
        :param Host host:
        :return:
        """
        if self.topology_adapter_type == 'onos':
            # TODO: verify this vibes with ONOS properly; might need VLAN??
            dpid = host.defaultIntf().MAC().upper() + '/None'
        elif self.topology_adapter_type == 'floodlight':
            dpid = host.IP()
        else:
            raise ValueError("Unrecognized topology adapter type %s" % self.topology_adapter_type)
        return dpid

    def get_switch_dpid(self, switch):
        """
        Returns the data plane ID for the given switch that is recognized by the
        particular SDN controller currently in use.
        :param Switch switch:
        :return:
        """
        if self.topology_adapter_type == 'onos':
            dpid = 'of:' + switch.dpid
        elif self.topology_adapter_type == 'floodlight':
            raise NotImplementedError()
        else:
            raise ValueError("Unrecognized topology adapter type %s" % self.topology_adapter_type)
        return dpid

    def get_node_dpid(self, node):
        """
        Returns the data plane ID for the given node by determining whether it's a
        Switch or Host first.
        :param node:
        :return:
        """
        if isinstance(node, Switch):
            return self.get_switch_dpid(node)
        elif isinstance(node, Host):
            return self.get_host_dpid(node)
        else:
            raise TypeError("Unrecognized node type for: %s" % node)

    @property
    def data_path_links(self):
        """Returns a collection of (gateway Switch, cloud Switch) pairs to represent DataPath links
        or None if no clouds/DataPath exist"""
        if self.cloud_switches is None:
            return None
        # XXX: since we only have one cloud server, we don't need to figure out which one corresponds to each GW
        return [(gw.name, self.cloud_switches[0].name) for gw in self.cloud_gateways]


# TODO: import these from somewhere rather than repeating them here... BUT, note that we've done some bugfixes with these ones


def make_scale_config(applications=None, sensors=None, sinks=None, networks=None):
    """
    Builds a string to be used on the command line in order to run a scale client with the given configurations.
    NOTE: make sure to properly space your arguments and wrap any newlines in quotes so they aren't interpreted
    as the end of the command by the shell!
    """
    cfg = ""
    if applications is not None:
        cfg += ' --applications %s ' % applications
    if sensors is not None:
        cfg += ' --sensors %s ' % sensors
    if networks is not None:
        cfg += ' --networks %s ' % networks
    if sinks is not None:
        cfg += ' --event-sinks %s ' % sinks
    return cfg


def make_scale_config_entry(class_path, name, **kwargs):
    """Builds an individual entry for a single SCALE client module that can be fed to the CLI.
    NOTE: don't forget to add spaces around each entry if you use multiple!"""
    # d = dict(name=name, **kwargs)
    d = dict(**kwargs)
    # XXX: can't use 'class' as a kwarg in call to dict, so doing it this way...
    d['class'] = class_path
    # need to wrap the raw JSON in single quotes for use on command line as json.dumps wraps strings in double quotes
    # also need to escape these double quotes so that 'eval' (su -c) actually sees them in the args it passes to the final command
    return "'%s' " % json.dumps({name: d}).replace('"', r'\"')
    # return "'%s'" % json.dumps(d)


if __name__ == "__main__":
    import sys
    exp = MininetSmartCampusExperiment.build_from_args(sys.argv[1:])
    exp.run_all_experiments()

