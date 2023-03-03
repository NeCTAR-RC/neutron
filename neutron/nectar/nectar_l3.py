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
#

from midonet.neutron.services.l3 import l3_midonet
from neutron_lib.callbacks import registry
from neutron_lib import constants as n_const
from neutron_lib.plugins import constants as plugin_constants
from neutron_lib.services import base as service_base
from oslo_config import cfg
from oslo_log import log

from neutron.db.availability_zone import router as router_az_db
from neutron.db import dns_db
from neutron.db import extraroute_db
from neutron.db import l3_fip_pools_db
from neutron.db import l3_fip_port_details
from neutron.db import l3_fip_qos
from neutron.db import l3_gwmode_db
from neutron.db.models import l3 as l3_models
from neutron.nectar import utils as nectar_utils
from neutron.quota import resource_registry
from neutron.services.ovn_l3 import plugin as ovn_plugin


LOG = log.getLogger(__name__)


@registry.has_registry_receivers
class NectarL3Plugin(service_base.ServicePluginBase,
                     extraroute_db.ExtraRoute_dbonly_mixin,
                     l3_gwmode_db.L3_NAT_db_mixin,
                     dns_db.DNSDbMixin,
                     l3_fip_port_details.Fip_port_details_db_mixin,
                     router_az_db.RouterAvailabilityZoneMixin,
                     l3_fip_qos.FloatingQoSDbMixin,
                     l3_fip_pools_db.FloatingIPPoolsMixin,
                     ):

    supported_extension_aliases = ["router", "extraroute", "ext-gw-mode",
                                   "router-interface-fip", "fip64"]

    @resource_registry.tracked_resources(router=l3_models.Router,
                                         floatingip=l3_models.FloatingIP)
    def __init__(self):
        self.ovn = ovn_plugin.OVNL3RouterPlugin()
        self.midonet = l3_midonet.MidonetL3ServicePlugin()

    @staticmethod
    def disable_qos_fip_extension_by_extension_drivers(aliases):
        ovn_plugin.OVNL3RouterPlugin()\
                  .disable_qos_fip_extension_by_extension_drivers(aliases)

    @property
    def _ovn_client(self):
        return self.ovn._ovn_client

    @property
    def port_forwarding(self):
        return self.ovn.port_forwarding

    def get_plugin_type(self):
        return plugin_constants.L3

    def get_plugin_description(self):
        """returns string description of the plugin."""
        return ("L3 Router Service Plugin for basic L3 forwarding"
                " using Nectar")

    def get_real_driver(self, context):
        if nectar_utils.use_legacy(context, cfg.CONF):
            return self.midonet
        return self.ovn

    def create_router_precommit(self, resource, event, trigger, payload):
        driver = self.get_real_driver(payload.context)
        return driver.create_router_precommit(resource, event, trigger,
                                              payload)

    def create_router(self, context, router):
        driver = self.get_real_driver(context)
        return driver.create_router(context, router)

    def update_router(self, context, id, router):
        driver = self.get_real_driver(context)
        return driver.update_router(context, id, router)

    def delete_router(self, context, id):
        driver = self.get_real_driver(context)
        return driver.delete_router(context, id)

    def add_router_interface(self, context, router_id, interface_info=None):
        driver = self.get_real_driver(context)
        return driver.add_router_interface(context, router_id,
                                           interface_info=interface_info)

    def remove_router_interface(self, context, router_id, interface_info):
        driver = self.get_real_driver(context)
        return driver.remove_router_interface(context, router_id,
                                              interface_info)

    def create_floatingip_precommit(self, resource, event, trigger, payload):
        driver = self.get_real_driver(payload.context)
        driver.create_floatingip_precommit(resource, event, trigger, payload)

    def create_floatingip(self, context, floatingip,
                          initial_status=n_const.FLOATINGIP_STATUS_DOWN):
        driver = self.get_real_driver(context)
        return driver.create_floatingip(context, floatingip)

    def delete_floatingip(self, context, id):
        driver = self.get_real_driver(context)
        driver.delete_floatingip(context, id)

    def update_floatingip(self, context, id, floatingip):
        driver = self.get_real_driver(context)
        return driver.update_floatingip(context, id, floatingip)

    # OVN only below
    def update_floatingip_status(self, context, floatingip_id, status):
        driver = self.get_real_driver(context)
        return driver.update_floatingip_status(context, floatingip_id, status)

    def disassociate_floatingips(self, context, port_id, do_notify=True):
        driver = self.get_real_driver(context)
        return driver.disassociate_floatingips(context, port_id,
                                               do_notify=do_notify)

    #####
    def update_router_gateway_port_bindings(self, router, host):
        return self.ovn.update_router_gateway_port_bindings(router, host)

    #####
    def schedule_unhosted_gateways(self, event_from_chassis=None):
        return self.ovn.schedule_unhosted_gateways(
            event_from_chassis=event_from_chassis)

    #####
    def get_router_availability_zones(self, router):
        return self.ovn.get_router_availability_zones(router)

    def validate_availability_zones(self, context, resource_type,
                                    availability_zones):
        driver = self.get_real_driver(context)
        return driver.validate_availability_zones(context, resource_type,
                                                  availability_zones)

    # Private method called by ovn sync
    def _add_neutron_router_interface(self, context, router_id, interface_info,
                                      may_exist=False):
        driver = self.get_real_driver(context)
        # This is only for OVN
        if driver == self.ovn:
            driver._add_neutron_router_interface(
                context, router_id, interface_info, may_exist=may_exist)
