#!/usr/bin/env python

# Copyright 2014 Red Hat, Inc.
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
"""
This script will generate the etc/tempest.conf file by applying a series of
specified options in the following order:

1. Values from etc/default-overrides.conf, if present. This file will be
provided by the distributor of the tempest code, a distro for example, to
specify defaults that are different than the generic defaults for tempest.

2. Values using the file provided by the --patch argument to the script.
Some required options differ among deployed clouds but the right values cannot
be discovered by the user. The file used here could be created by an installer,
or manually if necessary.

3. Values provided on the command line. These override all other values.

4. Discovery. Values that have not been provided in steps [1-3] will be
obtained by querying the cloud.
"""

import argparse
import ConfigParser
import glanceclient as glance_client
import keystoneclient.exceptions as keystone_exception
import keystoneclient.v2_0.client as keystone_client
import logging
import neutronclient.v2_0.client as neutron_client
import novaclient.client as nova_client
import os
import shutil
import subprocess
import sys
import urllib2

# Since tempest can be configured in different directories, we need to use
# the path starting at cwd.
sys.path.insert(0, os.getcwd())

from tempest.common import api_discovery

LOG = logging.getLogger(__name__)
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

TEMPEST_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

DEFAULTS_FILE = os.path.join(TEMPEST_DIR, "etc", "default-overrides.conf")
DEFAULT_IMAGE = "http://download.cirros-cloud.net/0.3.1/" \
                "cirros-0.3.1-x86_64-disk.img"

# services and their codenames
SERVICE_NAMES = {
    'baremetal': 'ironic',
    'compute': 'nova',
    'database': 'trove',
    'data_processing': 'sahara',
    'image': 'glance',
    'network': 'neutron',
    'object-store': 'swift',
    'orchestration': 'heat',
    'telemetry': 'ceilometer',
    'volume': 'cinder',
    'queuing': 'marconi',
}

# what API versions could the service have and should be enabled/disabled
# depending on whether they get discovered as supported. Services with only one
# version don't need to be here, neither do service versions that are not
# configurable in tempest.conf
SERVICE_VERSIONS = {
    'image': ['v1', 'v2'],
    'identity': ['v2', 'v3'],
    'volume': ['v1', 'v2'],
    'compute': ['v3'],
}

# Keep track of where the extensions are saved for that service.
# This is necessary because the configuration file is inconsistent - it uses
# different option names for service extension depending on the service.
SERVICE_EXTENSION_KEY = {
    'compute': 'discoverable_apis',
    'object-storage': 'discoverable_apis',
    'network': 'api_extensions',
    'volume': 'api_extensions',
}


def main():
    args = parse_arguments()
    logging.basicConfig(format=LOG_FORMAT)

    if args.verbose:
        LOG.setLevel(logging.INFO)
    if args.debug:
        LOG.setLevel(logging.DEBUG)

    conf = TempestConf()
    if os.path.isfile(DEFAULTS_FILE):
        LOG.info("Reading defaults from file '%s'", DEFAULTS_FILE)
        conf.read(DEFAULTS_FILE)
    if args.patch and os.path.isfile(args.patch):
        LOG.info("Adding options from patch file '%s'", args.patch)
        conf.read(args.patch)
    for section, key, value in args.overrides:
        conf.set(section, key, value, priority=True)

    uri = conf.get("identity", "uri")
    conf.set("identity", "uri_v3", uri.replace("v2.0", "v3"))
    if args.non_admin:
        conf.set("identity", "admin_username", "")
        conf.set("identity", "admin_tenant_name", "")
        conf.set("identity", "admin_password", "")
        conf.set("compute", "allow_tenant_isolation", "False")

    clients = ClientManager(conf, not args.non_admin)
    services = api_discovery.discover(clients.identity)
    if args.create:
        create_tempest_users(clients.identity, conf)
        if 'orchestration' in services:
            workaround_heat_role(clients.identity, conf)
    create_tempest_flavors(clients.compute, conf, args.create)
    create_tempest_images(clients.image, conf,
                          args.image, args.create)
    has_neutron = "network" in services
    create_tempest_networks(clients, conf, has_neutron, args.create)
    configure_discovered_services(conf, services)
    configure_boto(conf, services)
    configure_cli(conf)
    configure_horizon(conf)
    LOG.info("Creating configuration file %s" % os.path.abspath(args.out))
    with open(args.out, 'w') as f:
        conf.write(f)


def parse_arguments():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--create', action='store_true', default=False,
                        help='create default tempest resources')
    parser.add_argument('--out', default="etc/tempest.conf",
                        help='the tempest.conf file to write')
    parser.add_argument('--patch', default=None,
                        help="""A file in the format of tempest.conf that will
                                override the default values. The
                                patch file is an alternative to providing
                                key/value pairs. If there are also key/value
                                pairs they will be applied after the patch
                                file""")
    parser.add_argument('overrides', nargs='*', default=[],
                        help="""key value pairs to modify. The key is
                                section.key where section is a section header
                                in the conf file.
                                For example: identity.username myname
                                 identity.password mypass""")
    parser.add_argument('--debug', action='store_true', default=False,
                        help='Print debugging information')
    parser.add_argument('--verbose', '-v', action='store_true', default=False,
                        help='Print more information about the execution')
    parser.add_argument('--non-admin', action='store_true', default=False,
                        help='Run without admin creds')
    parser.add_argument('--image', default=DEFAULT_IMAGE,
                        help="""an image to be uploaded to glance. The name of
                                the image is the leaf name of the path which
                                can be either a filename or url. Default is
                                '%s'""" % DEFAULT_IMAGE)
    args = parser.parse_args()

    if args.create and args.non_admin:
        raise Exception("Options '--create' and '--non-admin' cannot be used"
                        " together, since creating" " resources requires"
                        " admin rights")
    args.overrides = parse_overrides(args.overrides)
    return args


def parse_overrides(overrides):
    """Manual parsing of positional arguments.

    TODO(mkollaro) find a way to do it in argparse
    """
    if len(overrides) % 2 != 0:
        raise Exception("An odd number of override options was found. The"
                        " overrides have to be in 'section.key value' format.")
    i = 0
    new_overrides = []
    while i < len(overrides):
        section_key = overrides[i].split('.')
        value = overrides[i + 1]
        if len(section_key) != 2:
            raise Exception("Missing dot. The option overrides has to come in"
                            " the format 'section.key value', but got '%s'."
                            % (overrides[i] + ' ' + value))
        section, key = section_key
        new_overrides.append((section, key, value))
        i += 2
    return new_overrides


class ClientManager(object):
    """Manager of various OpenStack API clients.

    Connections to clients are created on-demand, i.e. the client tries to
    connect to the server only when it's being requested.
    """
    _identity = None
    _compute = None
    _image = None
    _network = None

    def __init__(self, conf, admin):
        self.insecure = \
            conf.get('identity', 'disable_ssl_certificate_validation')
        self.auth_url = conf.get('identity', 'uri')
        if admin:
            self.username = conf.get('identity', 'admin_username')
            self.password = conf.get('identity', 'admin_password')
            self.tenant_name = conf.get('identity', 'admin_tenant_name')
        else:
            self.username = conf.get('identity', 'username', 'demo')
            self.password = conf.get('identity', 'password', 'secret')
            self.tenant_name = conf.get('identity', 'tenant_name', 'demo')

    @property
    def identity(self):
        if self._identity:
            return self._identity
        LOG.info("Connecting to Keystone at '%s' with username '%s',"
                 " tenant '%s', and password '%s'", self.auth_url,
                 self.username, self.tenant_name, self.password)
        self._identity = keystone_client.Client(username=self.username,
                                                password=self.password,
                                                tenant_name=self.tenant_name,
                                                auth_url=self.auth_url,
                                                insecure=self.insecure)
        return self._identity

    @property
    def compute(self):
        if self._compute:
            return self._compute
        LOG.debug("Connecting to Nova")
        self._compute = nova_client.Client('2', self.username, self.password,
                                           self.tenant_name, self.auth_url,
                                           insecure=self.insecure,
                                           no_cache=True)
        return self._compute

    @property
    def image(self):
        if not self._image:
            LOG.debug("Connecting to Glance")
            token = self.identity.auth_token
            catalog = self.identity.service_catalog
            endpoint = catalog.url_for(service_type='image',
                                       endpoint_type='publicURL')
            self._image = glance_client.Client("1", endpoint=endpoint,
                                               token=token,
                                               insecure=self.insecure)
        return self._image

    @property
    def network(self):
        if self._network:
            return self._network
        LOG.debug("Connecting to Neutron")
        self._network = neutron_client.Client(username=self.username,
                                              password=self.password,
                                              tenant_name=self.tenant_name,
                                              auth_url=self.auth_url,
                                              insecure=self.insecure)
        return self._network


class TempestConf(ConfigParser.SafeConfigParser):
    # causes the config parser to preserve case of the options
    optionxform = str

    # set of pairs `(section, key)` which have a higher priority (are
    # user-defined) and will usually not be overwritten by `set()`
    priority_sectionkeys = set()

    def set(self, section, key, value, priority=False):
        """Set value in configuration, similar to `SafeConfigParser.set`

        Creates non-existent sections. Keeps track of options which were
        specified by the user and should not be normally overwritten.

        :param priority: if True, always over-write the value. If False, don't
            over-write an existing value if it was written before with a
            priority (i.e. if it was specified by the user)
        :returns: True if the value was written, False if not (because of
            priority)
        """
        if not self.has_section(section):
            self.add_section(section)
        if not priority and (section, key) in self.priority_sectionkeys:
            LOG.debug("Option '[%s] %s = %s' was defined by user, NOT"
                      " overwriting into value '%s'", section, key,
                      self.get(section, key), value)
            return False
        if priority:
            self.priority_sectionkeys.add((section, key))
        LOG.debug("Setting [%s] %s = %s", section, key, value)
        ConfigParser.SafeConfigParser.set(self, section, key, value)
        return True


def create_tempest_users(identity_client, conf):
    """Create users necessary for Tempest if they don't exist already."""
    create_user_with_tenant(identity_client,
                            conf.get('identity', 'username'),
                            conf.get('identity', 'password'),
                            conf.get('identity', 'tenant_name'))

    give_role_to_user(identity_client,
                      conf.get('identity', 'admin_username'),
                      conf.get('identity', 'tenant_name'),
                      role_name='admin')

    create_user_with_tenant(identity_client,
                            conf.get('identity', 'alt_username'),
                            conf.get('identity', 'alt_password'),
                            conf.get('identity', 'alt_tenant_name'))


def give_role_to_user(identity_client, username, tenant_name, role_name):
    """Give the user a role in the project (tenant)."""
    user_id = identity_client.users.find(name=username)
    tenant_id = identity_client.tenants.find(name=tenant_name)
    role_id = identity_client.roles.find(name=role_name)
    try:
        identity_client.tenants.add_user(tenant_id, user_id, role_id)
        LOG.info("User '%s' was given the '%s' role in project '%s'",
                 username, role_name, tenant_name)
    except keystone_exception.Conflict:
        LOG.info("(no change) User '%s' already has the '%s' role in"
                 " project '%s'", username, role_name, tenant_name)


def create_user_with_tenant(identity_client, username, password, tenant_name):
    """Create user and tenant if he doesn't exist.

    Sets password even for existing user.
    """
    LOG.info("Creating user '%s' with tenant '%s' and password '%s'",
             username, tenant_name, password)
    tenant_description = "Tenant for Tempest %s user" % username
    email = "%s@test.com" % username
    # create tenant
    try:
        identity_client.tenants.create(tenant_name, tenant_description)
    except keystone_exception.Conflict:
        LOG.info("(no change) Tenant '%s' already exists", tenant_name)

    tenant = identity_client.tenants.find(name=tenant_name)
    # create user
    try:
        identity_client.users.create(name=username, password=password,
                                     email=email, tenant_id=tenant.id)
    except keystone_exception.Conflict:
        LOG.info("User '%s' already exists. Setting password to '%s'",
                 username, password)
        user = identity_client.users.find(name=username)
        identity_client.users.update_password(user.id, password)


def create_tempest_flavors(compute_client, conf, allow_creation):
    """Find or create flavors 'm1.nano' and 'm1.micro' and set them in conf.

    If 'flavor_ref' and 'flavor_ref_alt' are specified in conf, it will first
    try to find those - otherwise it will try finding or creating 'm1.nano' and
    'm1.micro' and overwrite those options in conf.

    :param allow_creation: if False, fail if flavors were not found
    """
    # m1.nano flavor
    flavor_id = None
    if conf.has_option('compute', 'flavor_ref'):
        flavor_id = conf.get('compute', 'flavor_ref')
    flavor_id = find_or_create_flavor(compute_client,
                                      flavor_id, 'm1.nano',
                                      allow_creation, ram=64)
    conf.set('compute', 'flavor_ref', flavor_id)

    # m1.micro flavor
    alt_flavor_id = None
    if conf.has_option('compute', 'flavor_ref_alt'):
        alt_flavor_id = conf.get('compute', 'flavor_ref_alt')
    alt_flavor_id = find_or_create_flavor(compute_client,
                                          alt_flavor_id, 'm1.micro',
                                          allow_creation, ram=128)
    conf.set('compute', 'flavor_ref_alt', alt_flavor_id)


def find_or_create_flavor(compute_client, flavor_id, flavor_name,
                          allow_creation, ram=64, vcpus=1, disk=0):
    """Try finding flavor by ID or name, create if not found.

    :param flavor_id: first try finding the flavor by this
    :param flavor_name: find by this if it was not found by ID, create new
        flavor with this name if not found at all
    :param allow_creation: if False, fail if flavors were not found
    :param ram: memory of created flavor in MB
    :param vcpus: number of VCPUs for the flavor
    :param disk: size of disk for flavor in GB
    """
    flavor = None
    # try finding it by the ID first
    if flavor_id:
        found = compute_client.flavors.findall(id=flavor_id)
        if found:
            flavor = found[0]
    # if not found previously, try finding it by name
    if flavor_name and not flavor:
        found = compute_client.flavors.findall(name=flavor_name)
        if found:
            flavor = found[0]

    if not flavor and not allow_creation:
        raise Exception("Flavor '%s' not found, but resource creation"
                        " isn't allowed. Either use '--create' or provide"
                        " an existing flavor" % flavor_name)

    if not flavor:
        LOG.info("Creating flavor '%s'", flavor_name)
        flavor = compute_client.flavors.create(flavor_name, ram, vcpus, disk)
    else:
        LOG.info("(no change) Found flavor '%s'", flavor.name)

    return flavor.id


def create_tempest_images(image_client, conf, image_path, allow_creation):
    qcow2_img_path = os.path.join(conf.get("scenario", "img_dir"),
                                  conf.get("scenario", "qcow2_img_file"))
    name = image_path[image_path.rfind('/') + 1:]
    alt_name = name + "_alt"
    image_id = None
    if conf.has_option('compute', 'image_ref'):
        image_id = conf.get('compute', 'image_ref')
    image_id = find_or_upload_image(image_client,
                                    image_id, name, allow_creation,
                                    image_source=image_path,
                                    image_dest=qcow2_img_path)
    alt_image_id = None
    if conf.has_option('compute', 'image_ref_alt'):
        alt_image_id = conf.get('compute', 'image_ref_alt')
    alt_image_id = find_or_upload_image(image_client,
                                        alt_image_id, alt_name, allow_creation,
                                        image_source=image_path,
                                        image_dest=qcow2_img_path)

    conf.set('compute', 'image_ref', image_id)
    conf.set('compute', 'image_ref_alt', alt_image_id)


def find_or_upload_image(image_client, image_id, image_name, allow_creation,
                         image_source='', image_dest=''):
    image = _find_image(image_client, image_id, image_name)
    if not image and not allow_creation:
        raise Exception("Image '%s' not found, but resource creation"
                        " isn't allowed. Either use '--create' or provide"
                        " an existing image_ref" % image_name)

    if image:
        LOG.info("(no change) Found image '%s'", image.name)
    else:
        LOG.info("Creating image '%s'", image_name)
        if image_source.startswith("http:") or \
           image_source.startswith("https:"):
                _download_file(image_source, image_dest)
        else:
            shutil.copyfile(image_source, image_dest)
        image = _upload_image(image_client, image_name, image_dest)
    return image.id


def create_tempest_networks(clients, conf, has_neutron, allow_creation):
    label = None
    if has_neutron:
        for router in clients.network.list_routers()['routers']:
            net_id = router['external_gateway_info']['network_id']
            if ('external_gateway_info' in router and net_id is not None):
                conf.set('network', 'public_network_id', net_id)
                conf.set('network', 'public_router_id', router['id'])
                break
        for network in clients.compute.networks.list():
            if network.id != net_id:
                label = network.label
                break
    else:
        networks = clients.compute.networks.list()
        if networks:
            label = networks[0].label
    if label:
        conf.set('compute', 'fixed_network_name', label)
    else:
        raise Exception('fixed_network_name could not be discovered and'
                        ' must be specified')


def configure_boto(conf, services):
    """Set boto URLs based on discovered APIs."""
    if 'ec2' in services:
        conf.set('boto', 'ec2_url', services['ec2']['url'])
    if 's3' in services:
        conf.set('boto', 's3_url', services['s3']['url'])


def configure_cli(conf):
    """Set cli_dir and others for Tempest CLI tests.

    Find locally installed "nova" and "nova-manage" commands and configure CLI
    based on their availability and paths.
    """
    cli_dir = get_program_dir("nova")
    if cli_dir:
        conf.set('cli', 'enabled', 'True')
        conf.set('cli', 'cli_dir', cli_dir)
    else:
        conf.set('cli', 'enabled', 'False')
    nova_manage_found = bool(get_program_dir("nova-manage"))
    conf.set('cli', 'has_manage', str(nova_manage_found))


def configure_horizon(conf):
    """Derive the horizon URIs from the identity's URI."""
    uri = conf.get('identity', 'uri')
    base = uri.rsplit(':', 1)[0]
    assert base.startswith('http:') or base.startswith('https:')
    has_horizon = True
    try:
        urllib2.urlopen(base)
    except urllib2.URLError:
        has_horizon = False
    conf.set('service_available', 'horizon', str(has_horizon))
    conf.set('dashboard', 'dashboard_url', base + '/')
    conf.set('dashboard', 'login_url', base + '/auth/login/')


def configure_discovered_services(conf, services):
    """Set service availability and supported extensions and versions.

    Set True/False per service in the [service_available] section of `conf`
    depending of wheter it is in services. In the [<service>-feature-enabled]
    section, set extensions and versions found in `services`.

    :param conf: ConfigParser configuration
    :param services: dictionary of discovered services - expects each service
        to have a dictionary containing 'extensions' and 'versions' keys
    """
    # set service availability
    for service, codename in SERVICE_NAMES.iteritems():
        # ceilometer is still transitioning from metering to telemetry
        if service == 'telemetry' and 'metering' in services:
            service = 'metering'
        conf.set('service_available', codename, str(service in services))

    # set service extensions
    for service, ext_key in SERVICE_EXTENSION_KEY.iteritems():
        if service in services:
            extensions = ','.join(services[service]['extensions'])
            conf.set(service + '-feature-enabled', ext_key, extensions)

    # set supported API versions for services with more of them
    for service, versions in SERVICE_VERSIONS.iteritems():
        supported_versions = services[service]['versions']
        section = service + '-feature-enabled'
        for version in versions:
            is_supported = any(version in item
                               for item in supported_versions)
            conf.set(section, 'api_' + version, str(is_supported))


def get_program_dir(program):
    """Get directory path of the external program.

    :param program: name of program, e.g. 'ls' or 'cat'
    :returns: None if it wasn't found, '/path/to/it/' if found
    """
    devnull = open(os.devnull, 'w')
    try:
        path = subprocess.check_output(["which", program], stderr=devnull)
        return os.path.dirname(path.strip())
    except subprocess.CalledProcessError:
        return None


def workaround_heat_role(identity_client, conf):
    """Work around Packstack bug #1139330.

    Give the admin user a role called 'heat_stack_owner' so he can use Heat.
    Ignore errors - e.g. if the role doesn't exist or if admin already has it,
    so it doesn't cause problems on system with a different configuration.
    """
    role = 'heat_stack_owner'
    try:
        identity_client.roles.find(name=role)  # check if it exists
        give_role_to_user(identity_client,
                          conf.get('identity', 'admin_username'),
                          conf.get('identity', 'tenant_name'),
                          role)
        LOG.info("Applied workaround for Packstack bug #1139330 - the admin"
                 " user was given the '%s' role", role)
    except keystone_client.exceptions.HTTPClientError as e:
        LOG.info("(no change) Workaround for bug #1139330 was not applied: %s",
                 str(e))


def _download_file(url, destination):
    LOG.info("Downloading '%s' and saving as '%s'", url, destination)
    f = urllib2.urlopen(url)
    data = f.read()
    with open(destination, "wb") as dest:
        dest.write(data)


def _upload_image(image_client, name, path):
    """Upload qcow2 image file from `path` into Glance with `name."""
    LOG.info("Uploading image '%s' from '%s'", name, os.path.abspath(path))
    with open(path) as data:
        return image_client.images.create(name=name, disk_format="qcow2",
                                          container_format="bare",
                                          data=data, is_public="true")


def _find_image(image_client, image_id, image_name):
    """Find image by ID or name (the image client doesn't have this)."""
    if image_id:
        try:
            return image_client.images.get(image_id)
        except glance_client.exc.HTTPNotFound:
            pass
    found = filter(lambda x: x.name == image_name, image_client.images.list())
    if found:
        return found[0]
    else:
        return None


if __name__ == "__main__":
    main()