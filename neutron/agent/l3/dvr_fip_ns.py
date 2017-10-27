# Copyright (c) 2015 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import contextlib
import os

from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_utils import excutils

from neutron._i18n import _, _LE, _LW
from neutron.agent.l3 import fip_rule_priority_allocator as frpa
from neutron.agent.l3 import link_local_allocator as lla
from neutron.agent.l3 import namespaces
from neutron.agent.l3 import router_info
from neutron.agent.linux import ip_lib
from neutron.agent.linux import iptables_manager
from neutron.common import constants
from neutron.common import exceptions as n_exc
from neutron.common import utils as common_utils
from neutron.ipam import utils as ipam_utils

LOG = logging.getLogger(__name__)

FIP_NS_PREFIX = 'fip-'
FIP_EXT_DEV_PREFIX = 'fg-'
FIP_2_ROUTER_DEV_PREFIX = 'fpr-'
ROUTER_2_FIP_DEV_PREFIX = namespaces.ROUTER_2_FIP_DEV_PREFIX
# Route Table index for FIPs
FIP_RT_TBL = 16
# Rule priority range for FIPs
FIP_PR_START = 32768
FIP_PR_END = FIP_PR_START + 40000


class FipNamespace(namespaces.Namespace):

    def __init__(self, ext_net_id, agent_conf, driver, use_ipv6):
        name = self._get_ns_name(ext_net_id)
        super(FipNamespace, self).__init__(
            name, agent_conf, driver, use_ipv6)

        self._ext_net_id = ext_net_id
        self.agent_conf = agent_conf
        self.driver = driver
        self.use_ipv6 = use_ipv6
        self.agent_gateway_port = None
        self._subscribers = set()
        path = os.path.join(agent_conf.state_path, 'fip-priorities')
        self._rule_priorities = frpa.FipRulePriorityAllocator(path,
                                                              FIP_PR_START,
                                                              FIP_PR_END)
        self._iptables_manager = iptables_manager.IptablesManager(
            namespace=self.get_name(),
            use_ipv6=self.use_ipv6)
        path = os.path.join(agent_conf.state_path, 'fip-linklocal-networks')
        self.local_subnets = lla.LinkLocalAllocator(
            path, constants.DVR_FIP_LL_CIDR)
        self.destroyed = False
        self._stale_fips_checked = False

    @classmethod
    def _get_ns_name(cls, ext_net_id):
        return namespaces.build_ns_name(FIP_NS_PREFIX, ext_net_id)

    def get_name(self):
        return self._get_ns_name(self._ext_net_id)

    def get_ext_device_name(self, port_id):
        return (FIP_EXT_DEV_PREFIX + port_id)[:self.driver.DEV_NAME_LEN]

    def get_int_device_name(self, router_id):
        return (FIP_2_ROUTER_DEV_PREFIX + router_id)[:self.driver.DEV_NAME_LEN]

    def get_rtr_ext_device_name(self, router_id):
        return (ROUTER_2_FIP_DEV_PREFIX + router_id)[:self.driver.DEV_NAME_LEN]

    def has_subscribers(self):
        return len(self._subscribers) != 0

    def subscribe(self, external_net_id):
        is_first = not self.has_subscribers()
        self._subscribers.add(external_net_id)
        return is_first

    def unsubscribe(self, external_net_id):
        self._subscribers.discard(external_net_id)
        return not self.has_subscribers()

    def allocate_rule_priority(self, floating_ip):
        return self._rule_priorities.allocate(floating_ip)

    def deallocate_rule_priority(self, floating_ip):
        self._rule_priorities.release(floating_ip)

    @contextlib.contextmanager
    def _fip_port_lock(self, interface_name):
        # Use a namespace and port-specific lock semaphore to allow for
        # concurrency
        lock_name = 'port-lock-' + self.name + '-' + interface_name
        with lockutils.lock(lock_name, common_utils.SYNCHRONIZED_PREFIX):
            try:
                yield
            except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE('DVR: FIP namespace config failure '
                                  'for interface %s'), interface_name)

    def create_or_update_gateway_port(self, agent_gateway_port):
        interface_name = self.get_ext_device_name(agent_gateway_port['id'])

        # The lock is used to make sure another thread doesn't call to
        # update the gateway port before we are done initializing things.
        with self._fip_port_lock(interface_name):
            is_first = self.subscribe(agent_gateway_port['network_id'])
            if is_first:
                self._create_gateway_port(agent_gateway_port, interface_name)
            else:
                try:
                    self._update_gateway_port(
                        agent_gateway_port, interface_name)
                except Exception:
                    # If an exception occurs at this point, then it is
                    # good to clean up the namespace that has been created
                    # and reraise the exception in order to resync the router
                    with excutils.save_and_reraise_exception():
                        self.unsubscribe(agent_gateway_port['network_id'])
                        self.delete()
                        LOG.exception(_LE('DVR: Gateway update in '
                                          'FIP namespace failed'))

    def _create_gateway_port(self, ex_gw_port, interface_name):
        """Create namespace, request port creationg from Plugin,
           then configure Floating IP gateway port.
        """
        self.create()

        LOG.debug("DVR: adding gateway interface: %s", interface_name)
        ns_name = self.get_name()
        self.driver.plug(ex_gw_port['network_id'],
                         ex_gw_port['id'],
                         interface_name,
                         ex_gw_port['mac_address'],
                         bridge=self.agent_conf.external_network_bridge,
                         namespace=ns_name,
                         prefix=FIP_EXT_DEV_PREFIX,
                         mtu=ex_gw_port.get('mtu'))

        # Remove stale fg devices
        ip_wrapper = ip_lib.IPWrapper(namespace=ns_name)
        devices = ip_wrapper.get_devices()
        for device in devices:
            name = device.name
            if name.startswith(FIP_EXT_DEV_PREFIX) and name != interface_name:
                LOG.debug('DVR: unplug: %s', name)
                ext_net_bridge = self.agent_conf.external_network_bridge
                self.driver.unplug(name,
                                   bridge=ext_net_bridge,
                                   namespace=ns_name,
                                   prefix=FIP_EXT_DEV_PREFIX)

        ip_cidrs = common_utils.fixed_ip_cidrs(ex_gw_port['fixed_ips'])
        self.driver.init_l3(interface_name, ip_cidrs, namespace=ns_name,
                            clean_connections=True)

        self.agent_gateway_port = ex_gw_port

        cmd = ['sysctl', '-w', 'net.ipv4.conf.%s.proxy_arp=1' % interface_name]
        ip_wrapper.netns.execute(cmd, check_exit_code=False)

    def create(self):
        LOG.debug("DVR: add fip namespace: %s", self.name)
        # parent class will ensure the namespace exists and turn-on forwarding
        super(FipNamespace, self).create()
        # Somewhere in the 3.19 kernel timeframe ip_nonlocal_bind was
        # changed to be a per-namespace attribute.  To be backwards
        # compatible we need to try both if at first we fail.
        failed = ip_lib.set_ip_nonlocal_bind(
                value=1, namespace=self.name, log_fail_as_error=False)
        if failed:
            LOG.debug('DVR: fip namespace (%s) does not support setting '
                      'net.ipv4.ip_nonlocal_bind, trying in root namespace',
                      self.name)
            ip_lib.set_ip_nonlocal_bind(value=1)

        # no connection tracking needed in fip namespace
        self._iptables_manager.ipv4['raw'].add_rule('PREROUTING',
                                                    '-j CT --notrack')
        self._iptables_manager.apply()

    def delete(self):
        self.destroyed = True
        self._delete()
        self.agent_gateway_port = None

    @namespaces.check_ns_existence
    def _delete(self):
        ip_wrapper = ip_lib.IPWrapper(namespace=self.name)
        for d in ip_wrapper.get_devices(exclude_loopback=True,
                                        exclude_gre_devices=True):
            if d.name.startswith(FIP_2_ROUTER_DEV_PREFIX):
                # internal link between IRs and FIP NS
                ip_wrapper.del_veth(d.name)
            elif d.name.startswith(FIP_EXT_DEV_PREFIX):
                # single port from FIP NS to br-ext
                # TODO(carl) Where does the port get deleted?
                LOG.debug('DVR: unplug: %s', d.name)
                ext_net_bridge = self.agent_conf.external_network_bridge
                self.driver.unplug(d.name,
                                   bridge=ext_net_bridge,
                                   namespace=self.name,
                                   prefix=FIP_EXT_DEV_PREFIX)

        # TODO(mrsmith): add LOG warn if fip count != 0
        LOG.debug('DVR: destroy fip namespace: %s', self.name)
        super(FipNamespace, self).delete()

    def _check_for_gateway_ip_change(self, new_agent_gateway_port):

        def get_gateway_ips(gateway_port):
            gw_ips = {}
            if gateway_port:
                for subnet in gateway_port.get('subnets', []):
                    gateway_ip = subnet.get('gateway_ip', None)
                    if gateway_ip:
                        ip_version = ip_lib.get_ip_version(gateway_ip)
                        gw_ips[ip_version] = gateway_ip
            return gw_ips

        new_gw_ips = get_gateway_ips(new_agent_gateway_port)
        old_gw_ips = get_gateway_ips(self.agent_gateway_port)

        return new_gw_ips != old_gw_ips

    def get_fip_table_indexes(self, ip_version):
        ns_ipr = ip_lib.IPRule(namespace=self.get_name())
        ip_rules_list = ns_ipr.rule.list_rules(ip_version)
        tbl_index_list = []
        for ip_rule in ip_rules_list:
            tbl_index = ip_rule['table']
            if tbl_index in ['local', 'default', 'main']:
                continue
            tbl_index_list.append(tbl_index)
        return tbl_index_list

    def _add_default_gateway_for_fip(self, gw_ip, ip_device, tbl_index):
        """Adds default gateway for fip based on the tbl_index passed."""
        if tbl_index is None:
            ip_version = ip_lib.get_ip_version(gw_ip)
            tbl_index_list = self.get_fip_table_indexes(ip_version)
            for tbl_index in tbl_index_list:
                ip_device.route.add_gateway(gw_ip, table=tbl_index)
        else:
            ip_device.route.add_gateway(gw_ip, table=tbl_index)

    def _add_rtr_ext_route_rule_to_route_table(self, ri, fip_2_rtr,
                                               fip_2_rtr_name):
        """Creates external route table and adds routing rules."""
        # TODO(Swami): Rename the _get_snat_idx function to some
        # generic name that can be used for SNAT and FIP
        rt_tbl_index = ri._get_snat_idx(fip_2_rtr)
        interface_name = self.get_ext_device_name(
            self.agent_gateway_port['id'])
        try:
            # The lock is used to make sure another thread doesn't call to
            # update the gateway route before we are done initializing things.
            with self._fip_port_lock(interface_name):
                self._update_gateway_route(self.agent_gateway_port,
                                           interface_name,
                                           tbl_index=rt_tbl_index)
        except Exception:
            # If an exception occurs at this point, then it is
            # good to unsubscribe this external network so that
            # the next call will trigger the interface to be plugged.
            # We reraise the exception in order to resync the router.
            with excutils.save_and_reraise_exception():
                self.unsubscribe(self.agent_gateway_port['network_id'])
                # Reset the fip count so that the create_rtr_2_fip_link
                # is called again in this context
                ri.dist_fip_count = 0
                LOG.exception(_LE('DVR: Gateway update route in FIP namespace '
                                  'failed'))

        # Now add the filter match rule for the table.
        ip_rule = ip_lib.IPRule(namespace=self.get_name())
        ip_rule.rule.add(**{'ip': str(fip_2_rtr.ip),
                            'iif': fip_2_rtr_name,
                            'table': rt_tbl_index,
                            'priority': rt_tbl_index,
                            'from': '0.0.0.0/0'})

    def _update_gateway_port(self, agent_gateway_port, interface_name):
        if (self.agent_gateway_port and
            not self._check_for_gateway_ip_change(agent_gateway_port)):
                return
        # Caller already holding lock
        self._update_gateway_route(
            agent_gateway_port, interface_name, tbl_index=None)

        # Cache the agent gateway port after successfully updating
        # the gateway route, so that checking on self.agent_gateway_port
        # will be a valid check
        self.agent_gateway_port = agent_gateway_port

    def _update_gateway_route(self, agent_gateway_port,
                             interface_name, tbl_index):
        ns_name = self.get_name()
        ipd = ip_lib.IPDevice(interface_name, namespace=ns_name)
        # If the 'fg-' device doesn't exist in the namespace then trying
        # to send advertisements or configure the default route will just
        # throw exceptions.  Unsubscribe this external network so that
        # the next call will trigger the interface to be plugged.
        if not ipd.exists():
            LOG.warning(_LW('DVR: FIP gateway port with interface '
                            'name: %(device)s does not exist in the given '
                            'namespace: %(ns)s'), {'device': interface_name,
                                                   'ns': ns_name})
            msg = _('DVR: Gateway update route in FIP namespace failed, retry '
                    'should be attempted on next call')
            raise n_exc.FloatingIpSetupException(msg)

        for fixed_ip in agent_gateway_port['fixed_ips']:
            ip_lib.send_ip_addr_adv_notif(ns_name,
                                          interface_name,
                                          fixed_ip['ip_address'],
                                          self.agent_conf.send_arp_for_ha)

        for subnet in agent_gateway_port['subnets']:
            gw_ip = subnet.get('gateway_ip')
            if gw_ip:
                is_gateway_not_in_subnet = not ipam_utils.check_subnet_ip(
                                                subnet.get('cidr'), gw_ip)
                if is_gateway_not_in_subnet:
                    ipd.route.add_route(gw_ip, scope='link')
                self._add_default_gateway_for_fip(gw_ip, ipd, tbl_index)
            else:
                current_gateway = ipd.route.get_gateway()
                if current_gateway and current_gateway.get('gateway'):
                    ipd.route.delete_gateway(current_gateway.get('gateway'))

    def _add_cidr_to_device(self, device, ip_cidr):
        to = common_utils.cidr_to_ip(ip_cidr)
        if not device.addr.list(to=to):
            device.addr.add(ip_cidr, add_broadcast=False)

    def create_rtr_2_fip_link(self, ri):
        """Create interface between router and Floating IP namespace."""
        LOG.debug("Create FIP link interfaces for router %s", ri.router_id)
        rtr_2_fip_name = self.get_rtr_ext_device_name(ri.router_id)
        fip_2_rtr_name = self.get_int_device_name(ri.router_id)
        fip_ns_name = self.get_name()

        # add link local IP to interface
        if ri.rtr_fip_subnet is None:
            ri.rtr_fip_subnet = self.local_subnets.allocate(ri.router_id)
        rtr_2_fip, fip_2_rtr = ri.rtr_fip_subnet.get_pair()
        rtr_2_fip_dev = ip_lib.IPDevice(rtr_2_fip_name, namespace=ri.ns_name)
        fip_2_rtr_dev = ip_lib.IPDevice(fip_2_rtr_name, namespace=fip_ns_name)

        if not rtr_2_fip_dev.exists():
            ip_wrapper = ip_lib.IPWrapper(namespace=ri.ns_name)
            rtr_2_fip_dev, fip_2_rtr_dev = ip_wrapper.add_veth(rtr_2_fip_name,
                                                               fip_2_rtr_name,
                                                               fip_ns_name)
            mtu = ri.get_ex_gw_port().get('mtu')
            if mtu:
                rtr_2_fip_dev.link.set_mtu(mtu)
                fip_2_rtr_dev.link.set_mtu(mtu)
            rtr_2_fip_dev.link.set_up()
            fip_2_rtr_dev.link.set_up()

        self._add_cidr_to_device(rtr_2_fip_dev, str(rtr_2_fip))
        self._add_cidr_to_device(fip_2_rtr_dev, str(fip_2_rtr))
        self._add_rtr_ext_route_rule_to_route_table(ri, fip_2_rtr,
                                                    fip_2_rtr_name)

        # add default route for the link local interface
        rtr_2_fip_dev.route.add_gateway(str(fip_2_rtr.ip), table=FIP_RT_TBL)

    def scan_fip_ports(self, ri):
        # don't scan if not dvr or count is not None
        if ri.dist_fip_count is not None:
            return

        # scan system for any existing fip ports
        ri.dist_fip_count = 0
        rtr_2_fip_interface = self.get_rtr_ext_device_name(ri.router_id)
        device = ip_lib.IPDevice(rtr_2_fip_interface, namespace=ri.ns_name)
        if device.exists():
            ri.dist_fip_count = len(ri.get_router_cidrs(device))
            # On upgrade, there could be stale IP addresses configured, check
            # and remove them once.
            # TODO(haleyb): this can go away after a cycle or two
            if not self._stale_fips_checked:
                stale_cidrs = (
                    ip for ip in router_info.RouterInfo.get_router_cidrs(
                        ri, device)
                    if common_utils.is_cidr_host(ip))
                for ip_cidr in stale_cidrs:
                    LOG.debug("Removing stale floating ip %s from interface "
                              "%s in namespace %s",
                              ip_cidr, rtr_2_fip_interface, ri.ns_name)
                    device.delete_addr_and_conntrack_state(ip_cidr)
                self._stale_fips_checked = True
