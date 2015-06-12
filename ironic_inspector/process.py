# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Handling introspection data from the ramdisk."""

import logging

import eventlet
from ironicclient import exceptions

from ironic_inspector.common.i18n import _, _LE, _LI, _LW
from ironic_inspector import firewall
from ironic_inspector import node_cache
from ironic_inspector.plugins import base as plugins_base
from ironic_inspector import utils


LOG = logging.getLogger("ironic_inspector.process")

_CREDENTIALS_WAIT_RETRIES = 10
_CREDENTIALS_WAIT_PERIOD = 3


def process(introspection_data):
    """Process data from the ramdisk.

    This function heavily relies on the hooks to do the actual data processing.
    """
    hooks = plugins_base.processing_hooks_manager()
    failures = []
    for hook_ext in hooks:
        # NOTE(dtantsur): catch exceptions, so that we have changes to update
        # node introspection status after look up
        try:
            hook_ext.obj.before_processing(introspection_data)
        except utils.Error as exc:
            LOG.error(_LE('Hook %(hook)s failed, delaying error report '
                          'until node look up: %(error)s'),
                      {'hook': hook_ext.name, 'error': exc})
            failures.append('Preprocessing hook %(hook)s: %(error)s' %
                            {'hook': hook_ext.name, 'error': exc})
        except Exception as exc:
            LOG.exception(_LE('Hook %(hook)s failed, delaying error report '
                              'until node look up: %(error)s'),
                          {'hook': hook_ext.name, 'error': exc})
            failures.append(_('Unexpected exception during preprocessing '
                              'in hook %s') % hook_ext.name)

    try:
        node_info = node_cache.find_node(
            bmc_address=introspection_data.get('ipmi_address'),
            mac=introspection_data.get('macs'))
    except utils.Error as exc:
        if failures:
            failures.append(_('Look up error: %s') % exc)
            node_info = None
        else:
            raise

    if failures and node_info:
        msg = _('The following failures happened during running '
                'pre-processing hooks for node %(uuid)s:\n%(failures)s') % {
            'uuid': node_info.uuid,
            'failures': '\n'.join(failures)
        }
        node_info.finished(error=_('Data pre-processing failed'))
        raise utils.Error(msg)
    elif failures:
        msg = _('The following failures happened during running '
                'pre-processing hooks for unknown node:\n%(failures)s') % {
            'failures': '\n'.join(failures)
        }
        raise utils.Error(msg)

    ironic = utils.get_client()
    try:
        node = node_info.node(ironic)
    except exceptions.NotFound:
        msg = (_('Node UUID %s was found in cache, but is not found in Ironic')
               % node_info.uuid)
        node_info.finished(error=msg)
        raise utils.Error(msg, code=404)

    try:
        return _process_node(ironic, node, introspection_data, node_info)
    except utils.Error as exc:
        node_info.finished(error=str(exc))
        raise
    except Exception as exc:
        msg = _('Unexpected exception during processing')
        LOG.exception(msg)
        node_info.finished(error=msg)
        raise utils.Error(msg)


def _run_post_hooks(node_info, introspection_data):
    hooks = plugins_base.processing_hooks_manager()

    node_patches = []
    port_patches = {}
    for hook_ext in hooks:
        hook_ext.obj.before_update(introspection_data, node_info,
                                   node_patches, port_patches)

    node_patches = [p for p in node_patches if p]
    port_patches = {mac: patch for (mac, patch) in port_patches.items()
                    if patch}
    return node_patches, port_patches


def _process_node(ironic, node, introspection_data, node_info):
    # NOTE(dtantsur): repeat the check in case something changed
    utils.check_provision_state(node)

    ports = {}
    for mac in (introspection_data.get('macs') or ()):
        try:
            port = ironic.port.create(node_uuid=node.uuid, address=mac)
            ports[mac] = port
        except exceptions.Conflict:
            LOG.warning(_LW('MAC %(mac)s appeared in introspection data for '
                            'node %(node)s, but already exists in '
                            'database - skipping') %
                        {'mac': mac, 'node': node.uuid})

    # NOTE(dtanstur): make sure plugins get the latest information
    node_info.invalidate_cache()
    node_patches, port_patches = _run_post_hooks(node_info,
                                                 introspection_data)

    node = utils.retry_on_conflict(ironic.node.update, node.uuid, node_patches)
    for mac, patches in port_patches.items():
        utils.retry_on_conflict(ironic.port.update, ports[mac].uuid, patches)

    LOG.debug('Node %s was updated with data from introspection process, '
              'patches %s, port patches %s',
              node.uuid, node_patches, port_patches)

    firewall.update_filters(ironic)

    resp = {'uuid': node.uuid}

    if node_info.options.get('new_ipmi_credentials'):
        new_username, new_password = (
            node_info.options.get('new_ipmi_credentials'))
        utils.spawn_n(_finish_set_ipmi_credentials,
                      ironic, node, node_info, introspection_data,
                      new_username, new_password)
        resp['ipmi_setup_credentials'] = True
        resp['ipmi_username'] = new_username
        resp['ipmi_password'] = new_password
    else:
        utils.spawn_n(_finish, ironic, node_info)

    return resp


def _finish_set_ipmi_credentials(ironic, node, node_info, introspection_data,
                                 new_username, new_password):
    patch = [{'op': 'add', 'path': '/driver_info/ipmi_username',
              'value': new_username},
             {'op': 'add', 'path': '/driver_info/ipmi_password',
              'value': new_password}]
    if (not utils.get_ipmi_address(node) and
            introspection_data.get('ipmi_address')):
        patch.append({'op': 'add', 'path': '/driver_info/ipmi_address',
                      'value': introspection_data['ipmi_address']})
    utils.retry_on_conflict(ironic.node.update, node_info.uuid, patch)

    for attempt in range(_CREDENTIALS_WAIT_RETRIES):
        try:
            # We use this call because it requires valid credentials.
            # We don't care about boot device, obviously.
            ironic.node.get_boot_device(node_info.uuid)
        except Exception as exc:
            LOG.info(_LI('Waiting for credentials update on node %(node)s,'
                         ' attempt %(attempt)d current error is %(exc)s') %
                     {'node': node_info.uuid,
                      'attempt': attempt, 'exc': exc})
            eventlet.greenthread.sleep(_CREDENTIALS_WAIT_PERIOD)
        else:
            _finish(ironic, node_info)
            return

    msg = (_('Failed to validate updated IPMI credentials for node '
             '%s, node might require maintenance') % node_info.uuid)
    node_info.finished(error=msg)
    raise utils.Error(msg)


def _finish(ironic, node_info):
    LOG.debug('Forcing power off of node %s', node_info.uuid)
    try:
        utils.retry_on_conflict(ironic.node.set_power_state,
                                node_info.uuid, 'off')
    except Exception as exc:
        msg = (_('Failed to power off node %(node)s, check it\'s power '
                 'management configuration: %(exc)s') %
               {'node': node_info.uuid, 'exc': exc})
        node_info.finished(error=msg)
        raise utils.Error(msg)

    node_info.finished()
    LOG.info(_LI('Introspection finished successfully for node %s'),
             node_info.uuid)