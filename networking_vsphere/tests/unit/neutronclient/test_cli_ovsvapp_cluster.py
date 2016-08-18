# Copyright (c) 2015 Hewlett-Packard Development Company, L.P.
#
# All Rights Reserved
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

import sys

import mock

from networking_vsphere.neutronclient import (
    _ovsvapp_cluster as ovsvapp_cluster)
from networking_vsphere.tests.unit.neutronclient import test_cli20

from neutronclient import shell


class CLITestV20ExtensionOVSvAppClusterJSON(test_cli20.CLITestV20Base):
    def setUp(self):
        # need to mock before super because extensions loaded on instantiation.
        self._mock_extension_loading()
        super(CLITestV20ExtensionOVSvAppClusterJSON, self).setUp(
            plurals={'tags': 'tag'})

    def _create_patch(self, name, func=None):
        patcher = mock.patch(name)
        thing = patcher.start()
        return thing

    def _mock_extension_loading(self):
        ext_pkg = 'neutronclient.common.extension'
        contrib = self._create_patch(ext_pkg + '._discover_via_entry_points')
        contrib.return_value = [("_ovsvapp_cluster",
                                 ovsvapp_cluster)]
        return contrib

    def test_ext_cmd_loaded(self):
        """Tests ovsvapp-cluster commands loaded."""
        shell.NeutronShell('2.0')
        ext_cmd = {'ovsvapp-cluster-list':
                   ovsvapp_cluster.OVSvAppClusterList,
                   'ovsvapp-cluster-show':
                   ovsvapp_cluster.OVSvAppClusterShow}
        self.assertDictContainsSubset(ext_cmd, shell.COMMANDS['2.0'])

    def test_create_ovsvapp_clusters(self):
        """Test Create OVSvApp clusters."""

        resources = "ovsvapp_cluster"
        vcenter_id = 'v1'
        clusters = ['c1']
        args = ['--vcenter_id', 'v1',
                '--clusters', 'c1']
        position_names = ['vcenter_id', 'clusters']
        position_values = [vcenter_id, clusters]
        cmd = ovsvapp_cluster.OVSvAppClusterCreate(
            test_cli20.MyApp(sys.stdout), None)
        self._test_create_resource(resources, cmd, vcenter_id, 'myid', args,
                                   position_names, position_values)

    def test_update_ovsvapp_clusters(self):
        """Test Update OVSvApp clusters."""

        resources = 'ovsvapp_cluster'
        cmd = ovsvapp_cluster.OVSvAppClusterUpdate(
            test_cli20.MyApp(sys.stdout), None)
        args = ['myid', '--vcenter_id', 'v1',
                '--clusters', 'c1']
        values = {'vcenter_id': 'v1',
                  'clusters': ['c1']}
        self._test_update_resource(resources, cmd, 'myid', args,
                                   values)

    def test_list_ovsvapp_clusters(self):
        """Test List OVSvApp clusters."""

        resources = "ovsvapp_clusters"
        contents = [{'vcenter-id': 'vcenter-123', }]
        cmd = ovsvapp_cluster.OVSvAppClusterList(
            test_cli20.MyApp(sys.stdout), None)
        self._test_list_resources(resources, cmd, True,
                                  response_contents=contents)

    def test_show_ovsvapp_cluster(self):
        """Test Show OVSvApp cluster."""

        resource = 'ovsvapp_cluster'
        cmd = ovsvapp_cluster.OVSvAppClusterShow(
            test_cli20.MyApp(sys.stdout), None)
        args = [self.test_id]
        self._test_show_resource(resource, cmd, self.test_id, args)
