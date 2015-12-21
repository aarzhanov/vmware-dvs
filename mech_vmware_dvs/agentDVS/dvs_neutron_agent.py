import sys
import signal
import time

from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging

from neutron.common import topics
from neutron.common import utils
from neutron.common import config as common_config
from neutron.common import constants as n_const
from neutron.i18n import _LE, _LI
from neutron.agent import rpc as agent_rpc
from neutron.agent import securitygroups_rpc as sg_rpc
from neutron.agent.common import polling
from neutron.agent.linux import ip_lib
from neutron import context
from neutron.plugins.common import constants
from oslo_service import loopingcall

from mech_vmware_dvs import exceptions
from mech_vmware_dvs import util

LOG = logging.getLogger(__name__)
cfg.CONF.import_group('AGENT', 'mech_vmware_dvs.agentDVS.vmware_conf')


class ExtendAPI(object):

    def create_network(self, context, current, segment):
        self.create_network_precommit(current, segment)

    def delete_network(self, context, current, segment):
        self.delete_network_postcommit(current, segment)

    def update_network(self, context, current, segment, original):
        self.update_network_precommit(current, segment, original)

    def bind_port(self, context, network_current, network_segments, current):
        self.book_port(network_current, network_segments, current)

    def post_update_port(self, context, current, original, segment, sg_info):
        self.update_port_postcommit(current, original, segment, sg_info)

    def delete_port(self, context, current, original, segment, sg_info):
        self.delete_port_postcommit(current, original, segment, sg_info)


class DVSPluginApi(agent_rpc.PluginApi):
    pass


class DVSAgent(sg_rpc.SecurityGroupAgentRpcCallbackMixin, ExtendAPI):

    target = oslo_messaging.Target(version='1.2')

    def __init__(self, vsphere_hostname, vsphere_login, vsphere_password,
                 bridge_mappings, polling_interval, veth_mtu=None,
                 minimize_polling=False, quitting_rpc_timeout=None):
        super(DVSAgent, self).__init__()
        self.veth_mtu = veth_mtu

        self.agent_state = {
            'binary': 'neutron-dvs-agent',
            'host': cfg.CONF.host,
            'topic': n_const.L2_AGENT_TOPIC,
            'configurations': {'bridge_mappings': bridge_mappings,
                               'vsphere_hostname': vsphere_hostname,
                               'log_agent_heartbeats':
                                   cfg.CONF.AGENT.log_agent_heartbeats},
            'agent_type': 'DVS agent',
            'start_flag': True}

        self.setup_rpc()
        report_interval = cfg.CONF.AGENT.report_interval
        if report_interval:
            heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            heartbeat.start(interval=report_interval)

        self.polling_interval = polling_interval
        self.minimize_polling = minimize_polling
        # Security group agent support
        self.sg_agent = sg_rpc.SecurityGroupAgentRpc(
            self.context, self.sg_plugin_rpc, defer_refresh_firewall=True)
        self.run_daemon_loop = True
        self.iter_num = 0
        self.fullsync = True
        # The initialization is complete; we can start receiving messages
        self.connection.consume_in_threads()

        self.quitting_rpc_timeout = quitting_rpc_timeout
        self.network_map = util.create_network_map_from_config(
            cfg.CONF.ML2_VMWARE)

    @util.wrap_retry
    def create_network_precommit(self, current, segment):
        try:
            dvs = self._lookup_dvs_for_context(segment)
        except (exceptions.NoDVSForPhysicalNetwork,
                exceptions.NotSupportedNetworkType) as e:
            LOG.info(_LI('Network %(id)s not created. Reason: %(reason)s') % {
                'id': current['id'],
                'reason': e.message})
        except exceptions.InvalidNetwork:
            pass
        else:
            dvs.create_network(current, segment)

    @util.wrap_retry
    def delete_network_postcommit(self, current, segment):
        try:
            dvs = self._lookup_dvs_for_context(segment)
        except (exceptions.NoDVSForPhysicalNetwork,
                exceptions.NotSupportedNetworkType) as e:
            LOG.info(_LI('Network %(id)s not deleted. Reason: %(reason)s') % {
                'id': current['id'],
                'reason': e.message})
        except exceptions.InvalidNetwork:
            pass
        else:
            dvs.delete_network(current)

    @util.wrap_retry
    def update_network_precommit(self, current, segment, original):
        try:
            dvs = self._lookup_dvs_for_context(segment)
        except (exceptions.NoDVSForPhysicalNetwork,
                exceptions.NotSupportedNetworkType) as e:
            LOG.info(_LI('Network %(id)s not updated. Reason: %(reason)s') % {
                'id': current['id'],
                'reason': e.message})
        except exceptions.InvalidNetwork:
            pass
        else:
            dvs.update_network(current, original)

    @util.wrap_retry
    def book_port(self, network_segments, network_current, current):
        for segment in network_segments:
            dvs = self._lookup_dvs_for_context(segment)
            # TODO(ekosareva): port_key need to send back to server
            dvs.book_port(network_current, current['id'])

    @util.wrap_retry
    def update_port_postcommit(self, current, original, segment, sg_info):
        try:
            dvs = self._lookup_dvs_for_context(segment)
        except exceptions.NotSupportedNetworkType as e:
            LOG.info(_LI('Port %(id)s not updated. Reason: %(reason)s') % {
                'id': current['id'],
                'reason': e.message})
        except exceptions.NoDVSForPhysicalNetwork:
            raise exceptions.InvalidSystemState(details=_(
                'Port %(port_id)s belong to VMWare VM, but there is '
                'no mapping from network to DVS.') % {'port_id': current['id']}
            )
        else:
            self._update_admin_state_up(dvs, original, current)
            force = original['status'] == n_const.PORT_STATUS_DOWN
            self._update_security_groups(dvs, current, original, sg_info,
                                         force=force)

    @util.wrap_retry
    def delete_port_postcommit(self, current, original, segment, sg_info):
        try:
            dvs = self._lookup_dvs_for_context(segment)
        except exceptions.NotSupportedNetworkType as e:
            LOG.info(_LI('Port %(id)s not deleted. Reason: %(reason)s') % {
                'id': current['id'],
                'reason': e.message})
        except exceptions.NoDVSForPhysicalNetwork:
            raise exceptions.InvalidSystemState(details=_(
                'Port %(port_id)s belong to VMWare VM, but there is '
                'no mapping from network to DVS.') % {'port_id': current['id']}
            )
        else:
            self._update_security_groups(
                dvs, current, original, sg_info, force=True)
            dvs.release_port(current)

    def _lookup_dvs_for_context(self, segment):
        if segment['network_type'] == constants.TYPE_VLAN:
            physical_network = segment['physical_network']
            try:
                return self.network_map[physical_network]
            except KeyError:
                LOG.debug('No dvs mapped for physical '
                          'network: %s' % physical_network)
                raise exceptions.NoDVSForPhysicalNetwork(
                    physical_network=physical_network)
        else:
            raise exceptions.NotSupportedNetworkType(
                network_type=segment['network_type'])

    @util.wrap_retry
    def _update_security_groups(self, dvs, current, original, sg_info, force):
        if not dvs:
            return
        current_sg = set(current['security_groups'])
        if force:
            changed_sg = current_sg
        else:
            original_sg = set(original.get('security_groups', []))
            changed_sg = current_sg.symmetric_difference(original_sg)

        if changed_sg or force:
            devices = sg_info['devices']
            security_groups = sg_info['security_groups']
            sg_member_ips = sg_info['sg_member_ips']

            sg_to_update = set()
            ports_to_update = set()

            if current['id'] not in devices:
                sg_to_update = sg_to_update.union(changed_sg)
            else:
                ports_to_update.add(current['id'])

            for sg_id, rules in security_groups.items():
                for rule in rules:
                    try:
                        remote_group_id = rule['remote_group_id']
                    except KeyError:
                        pass
                    else:
                        if remote_group_id in changed_sg:
                            sg_to_update.add(sg_id)
                        if sg_id in changed_sg.union(sg_to_update):
                            ip_set = sg_member_ips[remote_group_id][
                                rule['ethertype']]
                            rule['ip_set'] = ip_set

            for id, port in devices.iteritems():
                # TODO(ekosareva): removed one more condition(is it needed?):
                # 'dvs_port_key' in port['binding:vif_details']
                if (port['binding:vif_type'] == util.DVS and
                        sg_to_update & set(port['security_groups'])):
                    ports_to_update.add(id)

            if ports_to_update:
                ports = []
                for port_id in ports_to_update:
                    port = devices[port_id]
                    for sec_group_id in port['security_groups']:
                        try:
                            rules = security_groups[sec_group_id]
                        except KeyError:
                            # security_group doesn't has rules
                            pass
                        else:
                            port['security_group_rules'].extend(rules)
                    ports.append(port)
                dvs.update_port_rules(ports)

    def _update_admin_state_up(self, dvs, original, current):
        try:
            original_admin_state_up = original['admin_state_up']
        except KeyError:
            pass
        else:
            current_admin_state_up = current['admin_state_up']
            perform = current_admin_state_up != original_admin_state_up
            if perform:
                dvs.switch_port_blocked_state(current)

    def _report_state(self):
        try:
            agent_status = self.state_rpc.report_state(self.context,
                                                       self.agent_state,
                                                       True)
            if agent_status == n_const.AGENT_REVIVED:
                LOG.info(_LI('Agent has just revived. Do a full sync.'))
                self.fullsync = True
            self.agent_state.pop('start_flag', None)
        except Exception:
            LOG.exception(_LE("Failed reporting state!"))

    def setup_rpc(self):
        self.agent_id = 'dvs-agent-%s' % cfg.CONF.host
        self.topic = topics.AGENT
        self.plugin_rpc = DVSPluginApi(topics.PLUGIN)
        # self.agentside_rpc = ServerAPI()
        self.sg_plugin_rpc = sg_rpc.SecurityGroupServerRpcApi(topics.PLUGIN)
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.REPORTS)

        # RPC network init
        self.context = context.get_admin_context_without_session()
        # Handle updates from service
        self.endpoints = [self]
        # Define the listening consumers for the agent
        consumers = [[topics.PORT, topics.UPDATE],
                     [topics.PORT, topics.DELETE],
                     [topics.NETWORK, topics.DELETE],
                     [topics.SECURITY_GROUP, topics.UPDATE],
                     [util.DVS, topics.UPDATE]]
        self.connection = agent_rpc.create_consumers(self.endpoints,
                                                     self.topic,
                                                     consumers,
                                                     start_listening=False)

    def _handle_sigterm(self, signum, frame):
        LOG.debug("Agent caught SIGTERM, quitting daemon loop.")
        self.run_daemon_loop = False

    def daemon_loop(self):
        with polling.get_polling_manager(self.minimize_polling) as pm:
            self.rpc_loop(polling_manager=pm)

    def rpc_loop(self, polling_manager=None):
        if not polling_manager:
            polling_manager = polling.get_polling_manager(
                minimize_polling=False)
        while self.run_daemon_loop:
            start = time.time()
            if self.fullsync:
                LOG.info(_LI("Agent out of sync with plugin!"))
                self.fullsync = False
                polling_manager.force_polling()
            if self._agent_has_updates(polling_manager):
                LOG.debug("Agent rpc_loop - update")
            self.loop_count_and_wait(start)

    def _agent_has_updates(self, polling_manager):
        return (polling_manager.is_polling_required or
                self.sg_agent.firewall_refresh_needed())

    def loop_count_and_wait(self, start_time):
        # sleep till end of polling interval
        elapsed = time.time() - start_time
        LOG.debug("Agent rpc_loop - iteration:%(iter_num)d "
                  "completed. Elapsed:%(elapsed).3f",
                  {'iter_num': self.iter_num,
                   'elapsed': elapsed})
        if elapsed < self.polling_interval:
            time.sleep(self.polling_interval - elapsed)
        else:
            LOG.debug("Loop iteration exceeded interval "
                      "(%(polling_interval)s vs. %(elapsed)s)!",
                      {'polling_interval': self.polling_interval,
                       'elapsed': elapsed})
        self.iter_num = self.iter_num + 1

    def process_ports(self):
        pass

    def port_update(self, context, **kwargs):
        pass

    def port_delete(self, context, **kwargs):
        pass


def create_agent_config_map(config):
    """Create a map of agent config parameters.

    :param config: an instance of cfg.CONF
    :returns: a map of agent configuration parameters
    """
    try:
        bridge_mappings = utils.parse_mappings(config.ML2_VMWARE.network_maps)
    except ValueError as e:
        raise ValueError(_("Parsing network_maps failed: %s.") % e)

    kwargs = dict(
        vsphere_hostname=config.ML2_VMWARE.vsphere_hostname,
        vsphere_login=config.ML2_VMWARE.vsphere_login,
        vsphere_password=config.ML2_VMWARE.vsphere_password,
        bridge_mappings=bridge_mappings,
        polling_interval=config.AGENT.polling_interval,
        minimize_polling=config.AGENT.minimize_polling,
        veth_mtu=config.AGENT.veth_mtu,
        quitting_rpc_timeout=config.AGENT.quitting_rpc_timeout,
    )
    return kwargs


def main():

    cfg.CONF.register_opts(ip_lib.OPTS)
    common_config.init(sys.argv[1:])
    common_config.setup_logging()
    utils.log_opt_values(LOG)

    try:
        agent_config = create_agent_config_map(cfg.CONF)
    except ValueError as e:
        LOG.error(_LE('%s Agent terminated!'), e)
        sys.exit(1)

    try:
        agent = DVSAgent(**agent_config)
    except RuntimeError as e:
        LOG.error(_LE("%s Agent terminated!"), e)
        sys.exit(1)
    signal.signal(signal.SIGTERM, agent._handle_sigterm)

    # Start everything.
    LOG.info(_LI("Agent initialized successfully, now running... "))
    agent.daemon_loop()

if __name__ == "__main__":
    main()
