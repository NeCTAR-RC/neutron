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

from keystoneauth1 import loading
from keystoneauth1 import session
from keystoneclient.v3 import client
from oslo_cache import core
from oslo_log import log

from neutron.common import cache_utils


LOG = log.getLogger(__name__)


def use_legacy(context, conf):
    if not context.project_id:
        return False
    project_tags = get_project_tags(context, conf)
    return 'legacy-networking' in project_tags


def get_project_tags(context, conf):
    cache = cache_utils.get_cache(conf)
    project_id = context.project_id
    cache_key = 'neutron-project-%s' % project_id
    project_tags = []
    if cache:
        project_tags = cache.get(cache_key)

    if not cache or project_tags == core.NO_VALUE:
        # Steal placement creds to talk to keystone
        auth_plugin = loading.load_auth_from_conf_options(
            conf, 'placement')
        sess = session.Session(auth=auth_plugin)
        project = client.Client(session=sess).projects.get(project_id)
        project_tags = project.tags
        if cache:
            LOG.debug("Setting tag cache for project %s", project_id)
            cache.set(cache_key, project_tags)
    return project_tags
