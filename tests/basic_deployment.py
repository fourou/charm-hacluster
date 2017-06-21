#!/usr/bin/env python
#
# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import time

import amulet

from charmhelpers.contrib.openstack.amulet.deployment import (
    OpenStackAmuletDeployment
)
from charmhelpers.contrib.openstack.amulet.utils import (
    OpenStackAmuletUtils,
    DEBUG,
    # ERROR
)

# Use DEBUG to turn on debug logging
u = OpenStackAmuletUtils(DEBUG)
seconds_to_wait = 600

# Set number of primary units and cluster-count for hacluster
NUM_UNITS = 3

PY_CRM_GET_PROPERTY = """cd hooks;
python -c 'import pcmk;
try:
    print(pcmk.get_property(\"maintenance-mode\"))
except pcmk.PropertyNotFound:
    print(\"false\")
'
"""


class HAClusterBasicDeployment(OpenStackAmuletDeployment):

    def __init__(self, series=None, openstack=None, source=None, stable=False):
        """Deploy the entire test environment."""
        super(HAClusterBasicDeployment, self).__init__(series, openstack,
                                                       source, stable)
        env_var = 'AMULET_OS_VIP'
        self._vip = os.getenv(env_var, None)
        if not self._vip:
            amulet.raise_status(amulet.SKIP, msg="No vip provided with '%s' - "
                                "skipping tests" % (env_var))

        self._add_services()
        self._add_relations()
        self._configure_services()
        self._deploy()

        u.log.info('Waiting on extended status checks...')
        exclude_services = []

        # Wait for deployment ready msgs, except exclusions
        self._auto_wait_for_status(exclude_services=exclude_services)

        self.d.sentry.wait()
        self._initialize_tests()

    def _add_services(self):
        this_service = {'name': 'hacluster'}
        other_services = [
            {'name': 'percona-cluster', 'constraints': {'mem': '3072M'}},
            {'name': 'keystone', 'units': NUM_UNITS},
        ]
        super(HAClusterBasicDeployment, self)._add_services(this_service,
                                                            other_services)

    def _add_relations(self):
        relations = {'keystone:shared-db': 'percona-cluster:shared-db',
                     'hacluster:ha': 'keystone:ha'}
        super(HAClusterBasicDeployment, self)._add_relations(relations)

    def _configure_services(self):
        keystone_config = {
            'admin-password': 'openstack',
            'admin-token': 'ubuntutesting',
            'debug': 'true',
            'verbose': 'true',
            'vip': self._vip,
        }

        if self._get_openstack_release() >= self.xenial_mitaka:
            keystone_config.update({'ha-bindiface': 'ens2'})

        pxc_config = {
            'dataset-size': '25%',
            'max-connections': 1000,
            'root-password': 'ChangeMe123',
            'sst-password': 'ChangeMe123',
        }
        hacluster_config = {
            'debug': 'true',
            'cluster_count': NUM_UNITS,
        }

        configs = {
            'keystone': keystone_config,
            'hacluster': hacluster_config,
            'percona-cluster': pxc_config,
        }
        super(HAClusterBasicDeployment, self)._configure_services(configs)

    def _initialize_tests(self):
        """Perform final initialization before tests get run."""
        # Access the sentries for inspecting service units
        self.pxc_sentry = self.d.sentry['percona-cluster'][0]
        self.keystone_sentry = self.d.sentry['keystone'][0]
        # NOTE: the hacluster unit id may not correspond with its parent unit
        #       id.
        self.hacluster_sentry = self.d.sentry['hacluster'][0]

        u.log.debug('openstack release val: {}'.format(
            self._get_openstack_release()))
        u.log.debug('openstack release str: {}'.format(
            self._get_openstack_release_string()))

        # Authenticate keystone admin
        u.log.debug('Authenticating keystone admin against VIP: '
                    '{}'.format(self._vip))
        self.keystone = u.authenticate_keystone_admin(self.keystone_sentry,
                                                      user='admin',
                                                      password='openstack',
                                                      tenant='admin')

        # Create a demo tenant/role/user
        u.log.debug('Creating keystone demo tenant, role and user against '
                    'VIP: {}'.format(self._vip))
        self.demo_tenant = 'demoTenant'
        self.demo_role = 'demoRole'
        self.demo_user = 'demoUser'
        if not u.tenant_exists(self.keystone, self.demo_tenant):
            tenant = self.keystone.tenants.create(tenant_name=self.demo_tenant,
                                                  description='demo tenant',
                                                  enabled=True)
            self.keystone.roles.create(name=self.demo_role)
            self.keystone.users.create(name=self.demo_user,
                                       password='password',
                                       tenant_id=tenant.id,
                                       email='demo@demo.com')

        # Authenticate keystone demo
        u.log.debug('Authenticating keystone demo user against VIP: '
                    '{}'.format(self._vip))
        self.keystone_demo = u.authenticate_keystone_user(
            self.keystone,
            user=self.demo_user,
            password='password',
            tenant=self.demo_tenant)

    def _toggle_maintenance_and_wait(self, expected):
        SLEEP = 10
        TIMEOUT = 900  # secs

        crm_get_prop_cmd = PY_CRM_GET_PROPERTY
        self.d.configure('hacluster', {'maintenance-mode': expected})

        stime = time.time()
        ha_unit = self.d.sentry['hacluster'][0]
        while time.time() - stime <= TIMEOUT:
            time.sleep(SLEEP)
            (output, exit_code) = ha_unit.run(crm_get_prop_cmd)
            if output == expected:
                u.log.debug('maintenance-mode enabled: %s' % output)
                break

        assert output == expected, 'maintenance-mode is: %s, expected: %s' \
            % (output, expected)

    def test_910_pause_and_resume(self):
        """The services can be paused and resumed. """
        u.log.debug('Checking pause and resume actions...')
        unit = self.hacluster_sentry

        assert u.status_get(unit)[0] == "active"

        action_id = u.run_action(unit, "pause")
        assert u.wait_on_action(action_id), "Pause action failed."
        assert u.status_get(unit)[0] == "maintenance"

        action_id = u.run_action(unit, "resume")
        assert u.wait_on_action(action_id), "Resume action failed."
        assert u.status_get(unit)[0] == "active"
        u.log.debug('OK')

    def test_920_put_in_maintenance(self):
        """Put pacemaker in maintenance mode"""
        return
        u.log.debug('Setting cluster in maintenance mode')

        self._toggle_maintenance_and_wait('true')
        self._toggle_maintenance_and_wait('false')
