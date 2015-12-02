# Copyright (c) 2015 Akanda, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import json
import netaddr
import os
import subprocess

from collections import OrderedDict

from charmhelpers.contrib.openstack import context, templating

import astara_context

from charmhelpers.contrib.openstack.utils import (
    git_install_requested,
)


from charmhelpers.core.hookenv import (
    charm_dir,
    config,
    log as juju_log
)

from charmhelpers.core.host import (
    adduser,
    add_group,
    add_user_to_group,
    mkdir,
    service_stop,
    service_start,
    service_restart,
    write_file,
)


from charmhelpers.contrib.openstack.utils import (
    git_install_requested,
    git_clone_and_install,
    git_src_dir,
    git_yaml_value,
    git_pip_venv_dir,
)

from charmhelpers.contrib.python.packages import pip_install

from charmhelpers.core.templating import render


ASTARA_NETWORK_CACHE = '/var/lib/juju/astara-network-cache.json'
GLANCE_IMG_ID_CACHE = '/var/lib/juju/astara-applinace-image-cache'

BASE_GIT_PACKAGES = [
    'libffi-dev',
    'libmysqlclient-dev',
    'libxml2-dev',
    'libxslt1-dev',
    'libssl-dev',
    'libyaml-dev',
    'python-dev',
    'python-pip',
    'python-setuptools',
    'zlib1g-dev',
    'python-neutronclient',
    'python-keystoneclient',
    'python-glanceclient',
]


def determine_packages():
    return BASE_GIT_PACKAGES


def validate_config():
    mgt_net = config('management-network-cidr')
    try:
        net = netaddr.IPNetwork(mgt_net)
    except netaddr.core.AddrFormatError as e:
        m = (
            'Invalid network CIDR configured for management-network-cidr: %s'
             % mgt_net
        )
        juju_log(m)
        raise  Exception(m)



def resource_map():
    rm = OrderedDict([
        (ASTARA_CONFIG, {
            'services': ['astara-orchestrator'],
            'contexts': [
                context.AMQPContext(),
                context.SharedDBContext(),
                context.IdentityServiceContext(
                    service='astara',
                    service_user='astara'),
                astara_context.AstaraContext(
                    network_cache=ASTARA_NETWORK_CACHE,
                )
            ],
        })
    ])
    return rm

def restart_map():
    return OrderedDict([(cfg, v['services'])
                        for cfg, v in resource_map().iteritems()])


def register_configs(release=None):
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release='liberty')
    for cfg, rscs in resource_map().iteritems():
        configs.register(cfg, rscs['contexts'])
    return configs



def git_install(projects_yaml):
    """Perform setup, and install git repos specified in yaml parameter."""
    if git_install_requested():
        git_clone_and_install(projects_yaml, core_project='astara-neutron')


def migrate_database():
    """Runs astara-dbsync to initialize a new database or migrate existing"""
    cmd = ['astara-dbsync', '--config-file', ASTARA_CONFIG, 'upgrade']
    subprocess.check_call(cmd)


def _auth_args():
    """Get current service credentials from the identity-service relation"""
    rel_ctxt = context.IdentityServiceContext()
    rel_data = rel_ctxt()
    if not rel_data or not context.context_complete(rel_data):
        juju_log('identity-service context not complete')
        return

    auth_url = '%s://%s:%s/v2.0' % (
        rel_data['service_protocol'],
        rel_data['service_host'],
        rel_data['service_port'])

    auth_args = {
        'username': rel_data['admin_user'],
        'password': rel_data['admin_password'],
        'tenant_name': rel_data['admin_tenant_name'],
        'auth_url': auth_url,
        'auth_strategy': 'keystone',
        'region': config('region'),
    }
    return auth_args


def get_or_create_network(client, name):
    juju_log('get_or_create_network: %s' % name)
    for net in client.list_networks()['networks']:
        if net['name'] == name:
            juju_log('- found existing network: %s' % net['id'])
            return net
    juju_log('- creating new network: %s' % net['name'])
    res = client.create_network({
        'network': {
            'name': name,
        }
    })['network']
    juju_log('- created new network: %s( net_id: %s)' %
        (res['name'], res['id']))
    return res


def get_or_create_subnet(client, cidr, network_id):
    juju_log('get_or_create_subnet: %s (net: %s)'  % (cidr, network_id))
    for sn in client.list_subnets(network_id=network_id)['subnets']:
        if sn['cidr'] == cidr:
            juju_log('- found existing subnet: %s' % sn['id'])
            return sn

    subnet = netaddr.IPNetwork(cidr)
    if subnet.version == 6:
        subnet_args = {
            'ip_version': 6,
            'ipv6_address_mode': 'slaac',
        }
    else:
        subnet_args = {
            'ip_version': 4,
        }

    subnet_args.update({
        'cidr': cidr,
        'network_id': network_id,
        'enable_dhcp': True,
    })

    juju_log('- creating new subnet: %s' % net['cidr'])
    res = client.create_subnet({'subnet': subnet_args})['subnet']
    juju_log('- created new subnet: %s' % res['id'])
    return res

def create_management_network():
    """Creates a management network in Neutron to be used for
    orchestrator->appliance communication.
    """
    from neutronclient.v2_0 import client as NeutronClient
    auth_args = _auth_args()
    if not auth_args:
        return
    client = NeutronClient.Client(**auth_args)

    mgt_net_cidr = config('management-network-cidr')
    mgt_net_name = config('management-network-name')

    subnet = netaddr.IPNetwork(mgt_net_cidr)
    if subnet.version == 6:
        subnet_args = {
            'ip_version': 6,
            'ipv6_address_mode': 'slaac',
        }
    else:
        subnet_args = {
            'ip_version': 4,
        }

    network = get_or_create_network(client, mgt_net_name)
    subnet = get_or_create_subnet(client, mgt_net_cidr, network['id'])

    # since this data is not available in any relation and to avoid a call
    # to neutron API for every config write out, save this data locally
    # for access from config context.
    net_config = {
        'management_network': network,
        'management_subnet': subnet,
    }
    with open(ASTARA_NETWORK_CACHE, 'w') as out:
        out.write(json.dumps(net_config))


def get_keystone_session(auth_args):
    from keystoneclient import auth as ksauth
    from keystoneclient import session as kssession

    auth_plugin = ksauth.get_plugin_class('password')
    auth = auth_plugin(
        auth_url=auth_args['auth_url'],
        username=auth_args['username'],
        password=auth_args['password'],
        project_name=auth_args['tenant_name'],
    )
    return kssession.Session(auth=auth)


def get_glanceclient(auth_args):
    from glanceclient.v2.client import Client
    ks_session = get_keystone_session(auth_args)
    return Client(session=ks_session)

def get_appliance_image(img_loc):
    juju_log('Downloading astara appliance from %s' % img_loc)
    subprocess.check_output([
        'wget', '-O', '/tmp/%s' % img_name, img_loc
    ])

def _cache_img(img):
    with open(GLANCE_IMG_ID_CACHE, 'w') as out:
        out.write(img['id'])

def appliance_image():
    img_loc = 'http://amebix.home.base/~adam/akanda.qcow2'
    img_name = img_loc.split('/')[-1]
    return img_name, img_loc

def publish_astara_appliance_image():
    auth_args = _auth_args()
    if not auth_args:
        return
    client = get_glanceclient(auth_args)

    img_name, img_loc = appliance_image()

    for img in client.images.list():
        if img['name'] == img_name:
            _cache_img(img)
            juju_log(
                'Image named %s already exists in glance, skipping publish.' %
                img_name)
            return

    juju_log('Downloading astara appliance from %s' % img_loc)
    subprocess.check_output([
        'wget', '-O', '/tmp/%s' % img_name, img_loc
    ])

    juju_log('Publishing appliance image into glance')
    glance_img = client.images.create(
        name=img_name,
        container_format='bare',
        disk_format='qcow2')
    client.images.upload(
        image_id=glance_img['id'],
        image_data=open('/tmp/%s' % img_name))

    _cache_img(glance_img)
    return glance_img['id']


def appliance_image_uuid():
    if os.path.isfile(GLANCE_IMG_ID_CACHE):
        return open(GLANCE_IMG_ID_CACHE).read().strip()
    else:
        return publish_astara_appliance_image()


def api_extensions_path():
    """Return need the full path to the installation of neutron API extensions
    This is dependent on how the library was installed (git vs pkg)
    """
    if git_install_requested():
        venv_dir = git_pip_venv_dir(config('openstack-origin-git'))
        py = os.path.join(venv_dir, 'bin', 'python')
    else:
        py = subprocess.check_output(['which', 'python']).strip()

    cmd = [
        py, '-c', 'from akanda import neutron; print neutron.__file__;'
    ]
    module_path = subprocess.check_output(cmd)
    ext_path = os.path.join(os.path.dirname(module_path), 'extensions')

    if not os.path.isdir(ext_path):
        m = (
            'Could not locate astara-neutron API extensions directory @ %s' %
            ext_path
        )
        juju_log(m, 'ERROR')
        raise Exception(m)

    return ext_path
