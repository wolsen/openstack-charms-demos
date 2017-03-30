#!/usr/bin/env python
#
# Upgrade the OpenStack with Clustering Status Control.
# Usage: $ ./do-upgrade.py -o cloud:xenial-newton [application-name]
#

import argparse
import logging
import re
import six
import subprocess
import time
import yaml


logging.basicConfig(
    filename='os-upgrade.log',
    level=logging.DEBUG,
    format=('%(asctime)s %(levelname)s '
            '(%(funcName)s) %(message)s'))

log = logging.getLogger('os_upgrader')
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
handler.setFormatter(formatter)
log.addHandler(handler)

JUJU_VERSION = 1


class UnknownCharmNameError(Exception):
    """Raised when the charm name cannot be parsed."""
    pass


def determine_charm_name(properties):
    if 'charm-name' in properties:
        return properties['charm-name']

    if 'charm' in properties:
        match = re.match(r'\w*:(?:\w*/){0,1}(?P<name>\w*)-\d+',
                         self['charm'])
        if not match or not match.group('name'):
            raise UnknownCharmNameError("Unable to parse charm name "
                                        "from: %s" % self['charm'])
        return match.group('name')
    else:
        raise UnknownCharmNameError("Unable to determine charm name.")


class Juju(dict):

    def get_service(self, name):
        if JUJU_VERSION == 1:
            key = 'services'
        else:
            key = 'applications'

        if name not in self[key]:
            return None

        service_properties = self[key][name]
        charm_name = determine_charm_name(service_properties)
        service_type = SERVICE_TYPES.get(charm_name, Service)
        svc = service_type(self[key][name])
        svc['name'] = name
        svc['charm-name'] = charm_name

        return svc

    @classmethod
    def set_config_value(self, service, key, value):
        try:
            setting = '%s=%s' % (key, value)
            if JUJU_VERSION == 1:
                cmd = ['juju', 'set', service, setting]
            else:
                cmd = ['juju', 'config', service, setting]
            subprocess.check_output(cmd)
            return True
        except subprocess.CalledProcessError as e:
            # If the value is already set to the current value
            # return True to let the calling code know that it
            # can expect the value is set.
            print(e)
            if e.message.lower().index('already') >= 0:
                return True

            log.error(e)
            return False

    @classmethod
    def run_action(cls, unit_name, action):
        try:
            if JUJU_VERSION == 1:
                cmd = ['juju', 'action', 'do', unit_name, action]
            else:
                cmd = ['juju', 'run-action', unit_name, action]

            output = subprocess.check_output(cmd)
            return output.split(':')[1].strip()
        except subprocess.CalledProcessError as e:
            log.error(e)
            raise e

    @classmethod
    def enumerate_actions(cls, service):
        try:
            if JUJU_VERSION == 1:
                cmd = ['juju', 'action', 'defined', service]
            else:
                cmd = ['juju', 'actions', '--schema', '--format=json',
                       service]

            output = subprocess.check_output(cmd)
            actions = yaml.safe_load(output)
            return actions.keys()
        except subprocess.CalledProcessError as e:
            log.error(e)
            raise e

    @classmethod
    def is_action_done(cls, act_id):
        """Determines if the action by the action id is currently done or not.

        :param act_id: the action id to query the juju service on the status.
        :return boolean: True if the actino is done, False otherwise.
        """
        try:
            if JUJU_VERSION == 1:
                cmd = ['juju', 'action', 'fetch', act_id]
            else:
                cmd = ['juju', 'show-action-output', act_id]
            output = subprocess.check_output(cmd)
            results = yaml.safe_load(output)
            return results['status'] in ['completed', 'failed']
        except subprocess.CalledProcessError as e:
            log.error(e)
            raise e

    @classmethod
    def current(cls, service=None):
        global JUJU_VERSION
        output = subprocess.check_output(['juju', '--version'])
        if output.strip().startswith('1.'):
            JUJU_VERSION = 1
        else:
            JUJU_VERSION = 2

        cmd = ['juju', 'status']
        if service:
            cmd.append(service)

        if JUJU_VERSION == 2:
            cmd.append('--format=yaml')

        output = subprocess.check_output(cmd)
        parsed = yaml.safe_load(output)
        return Juju(parsed)

    @classmethod
    def run_on_service(cls, service, command):
        if JUJU_VERSION == 1:
            where = '--service'
        else:
            where = '--application'
        cmd = ['juju', 'run', where, service, command]
        output = subprocess.check_output(cmd)
        parsed = yaml.safe_load(output)
        return parsed

    @classmethod
    def run_on_unit(cls, unit, command):
        cmd = ['juju', 'run', '--unit', unit, command]
        output = subprocess.check_output(cmd)
        return output


class Service(dict):

    @property
    def name(self):
        return self['name']

    @property
    def charm_name(self):
        if 'charm-name' in self:
            return self['charm-name']
        else:
            return None

    def has_relation(self, rel_name):
        return rel_name in self['relations']

    def set_config(self, key, value):
        return Juju.set_config_value(self.name, key, value)

    def units(self):
        units = []
        for name, info in self['units'].iteritems():
            unit = self._create_unit(name, info)
            units.append(unit)
        return units

    def _create_unit(self, name, properties):
        unit = Unit(properties)
        unit['name'] = name
        unit['service'] = self
        return unit

    def run(self, command):
        return Juju.run_on_service(self.name, command)


class APIService(Service):
    """A service which provides an API service.

    An API service uses HAProxy to load balance incoming API requests to the
    backend service and will be drained before pausing the unit in an effort
    to prevent service disruption.
    """

    def _create_unit(self, name, properties):
        unit = APIUnit(properties)
        unit['name'] = name
        unit['service'] = self
        return unit

    def _set_haproxy_backend_state(self, backend_server, state):
        """Sets the state of the haproxy backend server.

        :param backend_server: the name of the backend server to
            change the state of
        :param state: the state to change the backend server to
        """
        for u in self.units():
            backends = u.run("awk '/^backend/ {{ print $2 }}' /etc/haproxy/haproxy.cfg")
            backends = backends.split('\n')

            commands = []
            for backend in backends:
                cmd = ("set server {backend}/{server} "
                       "state {state}").format(backend=backend,
                                               server=backend_server,
                                               state=state)
                commands.append(cmd)
                cmd = ';'.join(commands)
                to_run = ('echo "{cmd}" | sudo nc -U '
                          '/var/run/haproxy/admin.sock').format(cmd=cmd)
                log.debug("%s: Issuing command: %s" % (u.name, to_run))
                try:
                    u.run(to_run)
                except subprocess.CalledProcessError as e:
                    log.error("Failed to set backend %s to state %s on unit "
                              "%s: %s", backend, state, u.name, e.output)


# Defines the Service implementations to use based on the name
# of the charm. If a service is not listed below, it is given
# the default Service implementation.
# Key => Charm Name, Value => Service class
SERVICE_TYPES = {
    'nova-cloud-controller': APIService,
    'keystone': APIService,
    'glance': APIService,
    'cinder': APIService,
    'glance': APIService,
    'heat': APIService,
    'neutron-api': APIService,
    'openstack-dashboard': APIService,
}


class Unit(dict):
    @property
    def name(self):
        return self['name']

    @property
    def workload_status(self):
        if 'workload-status' in self:
            return Status(self['workload-status'])
        else:
            return None

    @property
    def agent_status(self):
        if 'agent-status' in self:
            return Status(self['agent-status'])
        else:
            return None

    def is_upgrading(self):
        wl_status = self.workload_status
        if wl_status is None:
            return False
        return wl_status.is_upgrading()

    def run_action(self, action):
        try:
            action_id = Juju.run_action(self.name, action)
            while not Juju.is_action_done(action_id):
                time.sleep(2)
        except subprocess.CalledProcessError as e:
            log.error(e)
            raise e

    def run(self, command):
        return Juju.run_on_unit(self.name, command)

    def get_hacluster_subordinate_unit(self):
        if not self['subordinates']:
            return None
        for k in self['subordinates']:
            if k.find('hacluster') >= 0:
                # TODO(wolsen) hack!
                return Unit({'name': k})

        return None

    def pre_pause(self):
        pass

    def pause(self):
        log.info(' Pausing service on unit: %s' % self.name)
        self.run_action('pause')
        log.info(' Service on unit %s is paused.' % self.name)

    def post_pause(self):
        pass

    def pre_resume(self):
        pass

    def resume(self):
        log.info(' Resuming service on unit: %s' % self.name)
        self.run_action('resume')
        log.info(' Service on unit %s has resumed.' % self.name)

    def post_resume(self):
        pass

    def upgrade_openstack(self):
        log.info(' Upgrading OpenStack for unit: %s' % self.name)
        self.run_action('openstack-upgrade')
        log.info(' Completed upgrade for unit: %s' % self.name)


class APIUnit(Unit):
    """A Unit which provides an API service"""

    has_haproxy_admin_socket = None

    def pre_pause(self):
        """Runs actions before the pause of the service

        Pausing an API unit will first place the haproxy backend server
        into a drain state, which will remove the backend server from the
        load balancing consideration. Next, it will wait the amount of
        time specified in the draintime parameter to allow the backend
        server to be able to finish any outstanding requests. After the
        draintime has elapsed, the haproxy backend server will be placed
        into maintenance mode. Only once this has completed, the pause
        action will be invoked on the unit itself.
        """
        # Log where the VIP is for informational purposes.
        log.info('Locating VIP...')
        output = self.run('sudo crm status | grep IPaddr2')
        log.info(output.strip())

        if self._is_haproxy_admin_socket_available():
            self._set_backend_server_state('drain')
            log.info("Waiting %s seconds for the API requests to complete",
                     args.draintime)
            time.sleep(args.draintime)
            self._set_backend_server_state('maint')
        else:
            log.warning("Unable to drain API requests from haproxy queue. "
                        "API requests may be interrupted.")

    def post_resume(self):
        """Resumes the unit.

        Resuming an API unit will issue the resume action to the unit itself
        and then wait for 30 seconds to allow the backend to come up fully.
        After the 30 seconds have elapsed, the haproxy backends will be set
        back to the ready state to allow the backend to be a target again.
        """
        if self._is_haproxy_admin_socket_available():
            log.info("Waiting for 30 seconds to allow backend services to "
                     "initialize...")
            time.sleep(30)
            self._set_backend_server_state('ready')

    def _is_haproxy_admin_socket_available(self):
        if self.has_haproxy_admin_socket is None:
            try:
                self.run('echo "help" | '
                         'sudo nc -U /var/run/haproxy/admin.sock')
                self.has_haproxy_admin_socket = True
            except subprocess.CalledProcessError as e:
                log.debug('Unit %s does not appear to have the haproxy admin '
                          'socket enabled.', self.name)
                self.has_haproxy_admin_socket = False
        return self.has_haproxy_admin_socket

    def _set_backend_server_state(self, state):
        backend_server = self.name.replace('/', '-')
        service = self['service']
        if '_set_backend_server_state' in service:
            service._set_backend_server_state(backend_server, state)


class Status(dict):

    @property
    def current(self):
        return self['current']

    @property
    def message(self):
        return self['message']

    def is_upgrading(self):
        return self.message.lower().find('upgrad') >= 0


# The 15.10 charm versions support the big bang upgrade scenario
# or the rollinng upgrade within a specific service (e.g. all
# units of a given service are upgraded at the same time).

SERVICES = [
    # Identity and Image
    'keystone',
    'glance',

    # Upgrade nova
    'nova-cloud-controller',
    'nova-compute',

    # Neutron upgrades
    'neutron-api',
    'neutron-gateway',

    # Backend block-storage upgrade.
    # Note: just upgrade cinder service.
    'cinder',

    # Upgrade dashboard
    'openstack-dashboard',
]


# Not all charms use the openstack-origin. The openstack specific
# charms do, but some of the others use an alternate origin key
# depending on who the author was.
ORIGIN_KEYS = {
    'ceph': 'source',
    'ceph-osd': 'source',
    'ceph-mon': 'source',
    'ceph-radosgw': 'source',
}


def is_rollable(service):
    """Determines if the service provided is eligible for a rolling
    upgrade or not.

    :param service <Service>: the service object describing the service
                              that should be tested for rollable upgrades
    :return <bool>: True if the service is rollable, false if not.
    """
    if 'openstack-upgrade' not in Juju.enumerate_actions(service.name):
        # If the service does not have an openstack-upgrade action,
        # then the service cannot be upgraded in a rollable fashion.
        return False

    if len(service.units()) <= 1:
        # If there's not multiple units, no need to do the rolling
        # upgrade. Go for the big bang.
        return False

    if service.name.lower().find('ceph') > 0:
        # The ceph charms incorporate their own upgrade process by
        # simply setting the source so let it do the "big-bang" style
        # upgrade.
        # TODO(wolsen) this should check the charm in juju 2 rather
        # than rely on the service/application name.
        return False

    if not service.set_config('action-managed-upgrade', True):
        log.warning('Failed to enable action-managed-upgrade mode.')
        return False

    return True


def order_units(service, units):
    """Orders the units by ensuring that the leader is the first unit.

    Queries Juju in order to determine which unit is the leader, and
    places that unit at the top of the list.

    :param service <Service>: the service to order the units by
    :param units list<Unit>: the list of units to sort
    :return list<Unit>: the sorted list of units.
    """
    log.info('Determining ordering for service: %s' % service.name)
    ordered = []

    is_leader_data = Juju.run_on_service(service.name, 'is-leader')
    leader_info = filter(lambda u: u['Stdout'].strip() == 'True',
                         is_leader_data)
    leader_unit = leader_info[0]['UnitId']
    for unit in units:
        if unit.name == leader_unit:
            ordered.insert(0, unit)
        else:
            ordered.append(unit)

    log.info('Upgrade order is: %s' % [unit.name for unit in ordered])
    return ordered


def perform_rolling_upgrade(service):
    """Performs a rolling upgrade for the specified service.

    Performs a rolling upgrade of the service by iterating through each
    of the units and runs a juju action do <unit_name> openstack-upgrade
    and waits for each unit to finish before continuing on to the next.

    :param service <Service>: the service object describing the juju service
                              that should be upgraded.
    """
    log.info('Performing a rolling upgrade for service: %s' % service.name)
    avail_actions = Juju.enumerate_actions(service.name)
    config_key = ORIGIN_KEYS.get(service.name, 'openstack-origin')
    service.set_config(config_key, args.origin)

    for unit in order_units(service, service.units()):
        log.info('Upgrading unit: %s' % unit.name)
        hacluster_unit = unit.get_hacluster_subordinate_unit()

        # TODO(wolsen) This is a temporary work around to allow the user
        # to evacuate a compute node during the upgrade procedure if
        # desired. This has the effect of pausing the upgrade script to
        # allow the user to manually intervene with the underlying cloud.
        # In the future, it would be nice to provide a mechanism to allow
        # the script to evacuate the node automatically (if desired).
        if args.evacuate and service.name == 'nova-compute':
            six.moves.input('Preparing to upgrade %s. Perform any additional '
                            'admin actions desired. Press ENTER to proceed.' %
                            unit.name)

        do_pause = args.pause and 'pause' in avail_actions
        if do_pause:
            unit.pre_pause()
            if hacluster_unit:
                hacluster_unit.pause()
            unit.pause()
            unit.post_pause()

        if 'openstack-upgrade' in avail_actions:
            unit.upgrade_openstack()

        if do_pause:
            unit.pre_resume()
            unit.resume()
            if hacluster_unit:
                hacluster_unit.resume()
            unit.post_resume()

        log.info(' Unit %s has finished the upgrade.' % unit.name)


def perform_bigbang_upgrade(service):
    """Performs a big-bang style upgrade for the specified service.

    In order to do the big-bang style upgrade, set the config value
    for the openstack-origin. Wait a few seconds until the upgrading
    message is reported.
    """
    log.info('Performing a big-bang upgrade for service: %s' % service.name)
    config_key = ORIGIN_KEYS.get(service.name, 'openstack-origin')
    service.set_config(config_key, args.origin)

    # Give the service a chance to invoke the config-changed hook
    # for the bigbang upgrade.
    time.sleep(5)

    upgrade_in_progress = True
    while upgrade_in_progress:
        service = Juju.current().get_service(service.name)
        unit_uip = [u.is_upgrading() for u in service.units()]
        upgrade_in_progress = any(unit_uip)
        if upgrade_in_progress:
            time.sleep(5)


def main():
    global args
    parser = argparse.ArgumentParser(
        description='Upgrades the currently running cloud.')
    parser.add_argument('-o', '--origin', type=str,
                        default='cloud:trusty-mitaka',
                        required=False, metavar='origin',
                        help='The Ubuntu Cloud Archive pocket to upgrade the '
                             'OpenStack cloud to. This is specified in the '
                             'form of cloud:<release_name>-<openstack_name>. '
                             'Examples include cloud:trusty-mitaka, '
                             'cloud:xenial-newton, etc.')
    parser.add_argument('-p', '--pause', action='store_true',
                        help='Pause units prior to upgrading')
    parser.add_argument('-e', '--evacuate', action='store_true',
                        help='Prompt before upgrading nova-compute units to '
                             'allow the compute host to be evacuated prior to '
                             'upgrading the unit.')
    parser.add_argument('-d', '--draintime', type=int, default=30,
                        help='Number of seconds to allow for a backend service to '
                             'drain the API connections. Default is 30 seconds.')
    parser.add_argument('app', metavar='app', type=str, nargs='*',
                        help='target app to upgrade')
    args = parser.parse_args()

    env = Juju.current()

    if args.app:
        to_upgrade = args.app
    else:
        to_upgrade = SERVICES

    for service in to_upgrade:
        log.info('Upgrading %s', service)
        svc = env.get_service(service)

        if not svc:
            log.error('Unable to find application %s', service)
            continue

        if is_rollable(svc):
            perform_rolling_upgrade(svc)
        else:
            perform_bigbang_upgrade(svc)


if __name__ == '__main__':
    main()
