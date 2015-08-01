# Copyright (c) 2015 Mirantis, Inc.
# All Rights Reserved.
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

import os

from oslo_utils import uuidutils
from pecan import set_config
from pecan.testing import load_test_app

from neutron.tests.unit import testlib_api


class PecanFunctionalTest(testlib_api.SqlTestCase):

    def setUp(self):
        self.setup_coreplugin('neutron.plugins.ml2.plugin.Ml2Plugin')
        super(PecanFunctionalTest, self).setUp()
        self.addCleanup(set_config, {}, overwrite=True)
        self.app = load_test_app(os.path.join(
            os.path.dirname(__file__),
            'config.py'
        ))


class TestV2Controller(PecanFunctionalTest):

    def test_get(self):
        response = self.app.get('/v2.0/ports.json')
        self.assertEqual(response.status_int, 200)

    def test_post(self):
        response = self.app.post_json('/v2.0/ports.json',
                                      params={'port': {'name': 'test'}})
        self.assertEqual(response.status_int, 200)

    def test_put(self):
        response = self.app.put_json('/v2.0/ports/44.json',
                                     params={'port': {'name': 'test'}})
        self.assertEqual(response.status_int, 200)

    def test_delete(self):
        response = self.app.delete('/v2.0/ports/44.json')
        self.assertEqual(response.status_int, 200)


class TestErrors(PecanFunctionalTest):

    def test_404(self):
        response = self.app.get('/assert_called_once', expect_errors=True)
        self.assertEqual(response.status_int, 404)

    def test_bad_method(self):
        response = self.app.patch('/v2.0/',
                                  expect_errors=True)
        self.assertEqual(response.status_int, 405)


class TestRequestID(PecanFunctionalTest):

    def test_request_id(self):
        response = self.app.get('/')
        self.assertIn('x-openstack-request-id', response.headers)
        self.assertTrue(
            response.headers['x-openstack-request-id'].startswith('req-'))
        id_part = response.headers['x-openstack-request-id'].split('req-')[1]
        self.assertTrue(uuidutils.is_uuid_like(id_part))
