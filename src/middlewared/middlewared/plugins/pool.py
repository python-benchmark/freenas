import asyncio
import base64
import contextlib
import errno
import logging
from datetime import datetime, time
import os
import re
import shutil
import subprocess
import sysctl
import tempfile
import uuid

import bsd

from libzfs import ZFSException
from middlewared.job import JobProgressBuffer
from middlewared.schema import (accepts, Attribute, Bool, Cron, Dict, EnumMixin, Int, List, Patch,
                                Str, UnixPerm)
from middlewared.service import (
    ConfigService, filterable, item_method, job, private, CallError, CRUDService, ValidationErrors
)
from middlewared.utils import Popen, filter_list, run, start_daemon_thread
from middlewared.validators import Range, Time

logger = logging.getLogger(__name__)

GELI_KEYPATH = '/data/geli'
RE_DISKPART = re.compile(r'^([a-z]+\d+)(p\d+)?')
ZPOOL_CACHE_FILE = '/data/zfs/zpool.cache'
ZPOOL_KILLCACHE = '/data/zfs/killcache'


class Inheritable(EnumMixin, Attribute):
    def __init__(self, *args, **kwargs):
        self.value = kwargs.pop('value')
        super(Inheritable, self).__init__(*args, **kwargs)

    def clean(self, value):
        if value == 'INHERIT':
            return value

        return self.value.clean(value)

    def validate(self, value):
        if value == 'INHERIT':
            return

        return self.value.validate(value)

    def to_json_schema(self, parent=None):
        schema = self.value.to_json_schema(parent)
        schema['anyOf'] = [{'type': schema.pop('type')}, {'type': 'string', 'enum': ['INHERIT']}]
        return schema


def _none(x):
    if x is None:
        return 'none'
    return x


def _null(x):
    if x == 'none':
        return None
    return x


async def is_mounted(middleware, path):
    mounted = await middleware.run_in_thread(bsd.getmntinfo)
    return any(fs.dest == path for fs in mounted)


async def mount(device, path, fs_type, fs_options, options):
    options = options or []

    if isinstance(device, str):
        device = device.encode("utf-8")

    if isinstance(path, str):
        path = path.encode("utf-8")

    if fs_type == "msdosfs":
        options.append("large")

    executable = "/sbin/mount"
    arguments = []

    if fs_type == "ntfs":
        executable = "/usr/local/bin/ntfs-3g"
    elif fs_type == "msdosfs" and fs_options:
        executable = "/sbin/mount_msdosfs"
        if fs_options.get("locale"):
            arguments.extend(["-L", fs_options["locale"]])
        arguments.extend(sum([["-o", option] for option in options], []))
        options = []
    else:
        arguments.extend(["-t", fs_type])

    if options:
        arguments.extend(["-o", ",".join(options)])

    proc = await Popen(
        [executable] + arguments + [device, path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf8",
    )
    output = await proc.communicate()

    if proc.returncode != 0:
        logger.debug("Mount failed (%s): %s", proc.returncode, output)
        raise ValueError("Mount failed (exit code {0}):\n{1}{2}" .format(
            proc.returncode,
            output[0].decode("utf-8"),
            output[1].decode("utf-8"),
        ))
    else:
        return True


class PoolResilverService(ConfigService):

    class Config:
        namespace = 'pool.resilver'
        datastore = 'storage.resilver'
        datastore_extend = 'pool.resilver.resilver_extend'

    @private
    async def resilver_extend(self, data):
        data['begin'] = data['begin'].strftime('%H:%M')
        data['end'] = data['end'].strftime('%H:%M')
        data['weekday'] = [int(v) for v in data['weekday'].split(',') if v]
        return data

    @private
    async def validate_fields_and_update(self, data, schema):
        verrors = ValidationErrors()

        begin = data.get('begin')
        if begin:
            data['begin'] = time(int(begin.split(':')[0]), int(begin.split(':')[1]))

        end = data.get('end')
        if end:
            data['end'] = time(int(end.split(':')[0]), int(end.split(':')[1]))

        weekdays = data.get('weekday')
        if not weekdays:
            verrors.add(
                f'{schema}.weekday',
                'At least one weekday should be selected'
            )
        else:
            data['weekday'] = ','.join([str(day) for day in weekdays])

        return verrors, data

    @accepts(
        Dict(
            'pool_resilver',
            Str('begin', validators=[Time()]),
            Str('end', validators=[Time()]),
            Bool('enabled'),
            List('weekday', items=[Int('weekday', validators=[Range(min=1, max=7)])])
        )
    )
    async def do_update(self, data):
        """
        Configure Pool Resilver Priority.

        If `begin` time is greater than `end` time it means it will rollover the day, e.g.
        begin = "19:00", end = "05:00" will increase pool resilver priority from 19:00 of one day
        until 05:00 of the next day.

        `weekday` follows crontab(5) values 0-7 (0 or 7 is Sun).

        .. examples(websocket)::

          Enable pool resilver priority all business days from 7PM to 5AM.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.resilver.update",
                "params": [{
                    "enabled": true,
                    "begin": "19:00",
                    "end": "05:00",
                    "weekday": [1, 2, 3, 4, 5]
                }]
            }
        """
        config = await self.config()
        original_config = config.copy()
        config.update(data)

        verrors, new_config = await self.validate_fields_and_update(config, 'pool_resilver_update')
        if verrors:
            raise verrors

        # before checking if any changes have been made, original_config needs to be mapped to new_config
        original_config['weekday'] = ','.join([str(day) for day in original_config['weekday']])
        original_config['begin'] = time(*(int(value) for value in original_config['begin'].split(':')))
        original_config['end'] = time(*(int(value) for value in original_config['end'].split(':')))
        if len(set(original_config.items()) ^ set(new_config.items())) > 0:
            # data has changed
            await self.middleware.call(
                'datastore.update',
                self._config.datastore,
                new_config['id'],
                new_config
            )

            await self.middleware.call('service.restart', 'cron')
            await self.middleware.call('pool.configure_resilver_priority')

        return await self.config()


class KernelModuleContextManager:
    def __init__(self, module):
        self.module = module

    async def __aenter__(self):
        if self.module is not None:
            if not await self.module_loaded():
                await run('kldload', self.module, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if not await self.module_loaded():
                    raise Exception('Kernel module %r failed to load', self.module)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.module is not None:
            try:
                await run('kldunload', self.module, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    async def module_loaded(self):
        return (await run('kldstat', '-n', self.module, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)).returncode == 0


class MountFsContextManager:
    def __init__(self, middleware, device, path, *args, **kwargs):
        self.middleware = middleware
        self.device = device
        self.path = path
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        await mount(self.device, self.path, *self.args, **self.kwargs)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if await is_mounted(self.middleware, self.path):
            await self.middleware.run_in_thread(bsd.unmount, self.path)


class PoolService(CRUDService):

    GELI_KEYPATH = '/data/geli'

    class Config:
        datastore = 'storage.volume'
        datastore_extend = 'pool.pool_extend'
        datastore_prefix = 'vol_'

    @item_method
    @accepts(
        Int('id', required=True),
        Str('action', enum=['START', 'STOP', 'PAUSE'], required=True)
    )
    @job()
    async def scrub(self, job, oid, action):
        """
        Performs a scrub action to pool of `id`.

        `action` can be either of "START", "STOP" or "PAUSE".

        .. examples(websocket)::

          Start scrub on pool of id 1.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.scrub",
                "params": [1, "START"]
            }
        """
        pool = await self._get_instance(oid)
        return await job.wrap(
            await self.middleware.call('zfs.pool.scrub', pool['name'], action)
        )

    @accepts()
    async def filesystem_choices(self):
        """
        Returns all available datasets, except system datasets.

        .. examples(websocket)::

          Get all datasets.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.filesystem_choices",
                "params": []
            }
        """
        vol_names = [vol['name'] for vol in (await self.query())]
        return [
            y['name'] for y in await self.middleware.call(
                'zfs.dataset.query',
                [
                    ('name', 'rnin', '.system'),
                    ('pool', 'in', vol_names)
                ]
            )
        ]

    @accepts(Int('id', required=True))
    @item_method
    async def is_upgraded(self, oid):
        """
        Returns whether or not the pool of `id` is on the latest version and with all feature
        flags enabled.

        .. examples(websocket)::

          Check if pool of id 1 is upgraded.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.is_upgraded",
                "params": [1]
            }
        """
        name = (await self._get_instance(oid))['name']
        proc = await Popen(
            f'zpool get -H -o value version {name}',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf8', shell=True
        )
        res, err = await proc.communicate()
        if proc.returncode != 0:
            return True
        res = res.decode('utf8').rstrip('\n')
        try:
            int(res)
        except ValueError:

            if res == '-':
                proc = await Popen(
                    f"zpool get -H -o property,value all {name}",
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf8', shell=True
                )
                data = (await proc.communicate())[0].decode('utf8').strip('\n')
                for line in [l for l in data.split('\n') if l.startswith('feature') and '\t' in l]:
                    prop, value = line.split('\t', 1)
                    if value not in ('active', 'enabled'):
                        return False
                return True
            else:
                return False
        else:
            return False

    @accepts(Int('id'))
    @item_method
    async def upgrade(self, oid):
        """
        Upgrade pool of `id` to latest version with all feature flags.

        .. examples(websocket)::

          Upgrade pool of id 1.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.upgrade",
                "params": [1]
            }
        """
        # Should we check first if upgrade is required ?
        await self.middleware.call(
            'zfs.pool.upgrade',
            (await self._get_instance(oid))['name']
        )
        return True

    def _topology(self, x, geom_scan=True):
        """
        Transform topology output from libzfs to add `device` and make `type` uppercase.
        """
        if isinstance(x, dict):
            path = x.get('path')
            if path is not None:
                device = None
                if path.startswith('/dev/'):
                    device = self.middleware.call_sync('disk.label_to_dev', path[5:], geom_scan)
                x['device'] = device
                x['disk'] = RE_DISKPART.sub(r'\1', device) if device else None
            for key in x:
                if key == 'type' and isinstance(x[key], str):
                    x[key] = x[key].upper()
                else:
                    x[key] = self._topology(x[key], False)
        elif isinstance(x, list):
            for i, entry in enumerate(x):
                x[i] = self._topology(x[i], False)
        return x

    @private
    def pool_extend(self, pool):

        """
        If pool is encrypted we need to check if the pool is imported
        or if all geli providers exist.
        """
        pool['path'] = f'/mnt/{pool["name"]}'
        try:
            zpool = self.middleware.call_sync('zfs.pool.query', [('id', '=', pool['name'])])[0]
        except Exception:
            zpool = None

        if zpool:
            pool.update({
                'status': zpool['status'],
                'scan': zpool['scan'],
                'topology': self._topology(zpool['groups']),
                'healthy': zpool['healthy'],
                'status_detail': zpool['status_detail'],
            })
        else:
            pool.update({
                'status': 'OFFLINE',
                'scan': None,
                'topology': None,
                'healthy': False,
                'status_detail': None,
            })

        if pool['encrypt'] > 0:
            if zpool:
                pool['is_decrypted'] = True
            else:
                decrypted = True
                for ed in self.middleware.call_sync('datastore.query', 'storage.encrypteddisk', [('encrypted_volume', '=', pool['id'])]):
                    if not os.path.exists(f'/dev/{ed["encrypted_provider"]}.eli'):
                        decrypted = False
                        break
                pool['is_decrypted'] = decrypted
            pool['encryptkey_path'] = os.path.join(GELI_KEYPATH, f'{pool["encryptkey"]}.key')
        else:
            pool['encryptkey_path'] = None
            pool['is_decrypted'] = True
        return pool

    @accepts(Dict(
        'pool_create',
        Str('name', required=True),
        Bool('encryption', default=False),
        Str('deduplication', enum=[None, 'ON', 'VERIFY', 'OFF'], default=None, null=True),
        Dict(
            'topology',
            List('data', items=[
                Dict(
                    'datavdevs',
                    Str('type', enum=['RAIDZ1', 'RAIDZ2', 'RAIDZ3', 'MIRROR', 'STRIPE'], required=True),
                    List('disks', items=[Str('disk')], required=True),
                ),
            ], required=True),
            List('cache', items=[
                Dict(
                    'cachevdevs',
                    Str('type', enum=['STRIPE'], required=True),
                    List('disks', items=[Str('disk')], required=True),
                ),
            ]),
            List('log', items=[
                Dict(
                    'logvdevs',
                    Str('type', enum=['STRIPE', 'MIRROR'], required=True),
                    List('disks', items=[Str('disk')], required=True),
                ),
            ]),
            List('spares', items=[Str('disk')], default=[]),
            required=True,
        ),
        register=True,
    ))
    @job(lock='pool_createupdate')
    async def do_create(self, job, data):
        """
        Create a new ZFS Pool.

        `topology` is a object which requires at least one `data` entry.
        All of `data` entries (vdevs) require to be of the same type.

        Example of `topology`:

            {
                "data": [
                    {"type": "RAIDZ1", "disks": ["da1", "da2", "da3"]}
                ],
                "cache": [
                    {"type": "STRIPE", "disks": ["da4"]}
                ],
                "log": [
                    {"type": "RAIDZ1", "disks": ["da5"]}
                ],
                "spares": ["da6"]
            }


        .. examples(websocket)::

          Create a pool named "tank", raidz1 with 3 disks, 1 cache disk, 1 ZIL/log disk
          and 1 hot spare disk.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.create",
                "params": [{
                    "name": "tank",
                    "topology": {
                        "data": [
                            {"type": "RAIDZ1", "disks": ["da1", "da2", "da3"]}
                        ],
                        "cache": [
                            {"type": "STRIPE", "disks": ["da4"]}
                        ],
                        "log": [
                            {"type": "RAIDZ1", "disks": ["da5"]}
                        ],
                        "spares": ["da6"]
                    }
                }]
            }
        """

        verrors = ValidationErrors()

        if await self.middleware.call('pool.query', [('name', '=', data['name'])]):
            verrors.add('pool_create.name', 'A pool with this name already exists.', errno.EEXIST)

        if not data['topology']['data']:
            verrors.add('pool_create.topology.data', 'At least one data vdev is required')

        await self.__common_validation(verrors, data, 'pool_create')
        disks, vdevs = await self.__convert_topology_to_vdevs(data['topology'])
        disks_cache = await self.__check_disks_availability(verrors, disks)

        if verrors:
            raise verrors

        if data['encryption']:
            enc_key = str(uuid.uuid4())
            enc_keypath = os.path.join(GELI_KEYPATH, f'{enc_key}.key')
        else:
            enc_key = ''
            enc_keypath = None

        enc_disks = await self.__format_disks(job, disks, enc_keypath)

        options = {
            'feature@lz4_compress': 'enabled',
            'altroot': '/mnt',
            'cachefile': ZPOOL_CACHE_FILE,
            'failmode': 'continue',
            'autoexpand': 'on',
        }

        fsoptions = {
            'compression': 'lz4',
            'aclmode': 'passthrough',
            'aclinherit': 'passthrough',
            'mountpoint': f'/{data["name"]}',
        }

        dedup = data.get('deduplication')
        if dedup:
            fsoptions['dedup'] = dedup.lower()

        cachefile_dir = os.path.dirname(ZPOOL_CACHE_FILE)
        if not os.path.isdir(cachefile_dir):
            os.makedirs(cachefile_dir)

        job.set_progress(90, 'Creating ZFS Pool')
        z_pool = await self.middleware.call('zfs.pool.create', {
            'name': data['name'],
            'vdevs': vdevs,
            'options': options,
            'fsoptions': fsoptions,
        })

        job.set_progress(95, 'Setting pool options')
        pool_id = None
        try:
            # Inherit mountpoint after create because we set mountpoint on creation
            # making it a "local" source.
            await self.middleware.call('zfs.dataset.update', data['name'], {
                'properties': {
                    'mountpoint': {'source': 'INHERIT'},
                },
            })
            await self.middleware.call('zfs.dataset.mount', data['name'])

            pool = {
                'name': data['name'],
                'guid': z_pool['guid'],
                'encrypt': int(data['encryption']),
                'encryptkey': enc_key,
            }
            pool_id = await self.middleware.call(
                'datastore.insert',
                'storage.volume',
                pool,
                {'prefix': 'vol_'},
            )

            await self.__save_encrypteddisks(pool_id, enc_disks, disks_cache)

            await self.middleware.call(
                'datastore.insert',
                'storage.scrub',
                {'volume': pool_id},
                {'prefix': 'scrub_'},
            )
        except Exception as e:
            # Something wrong happened, we need to rollback and destroy pool.
            try:
                await self.middleware.call('zfs.pool.delete', data['name'])
            except Exception:
                self.logger.warn('Failed to delete pool on pool.create rollback', exc_info=True)
            if pool_id:
                await self.middleware.call('datastore.delete', 'storage.volume', pool_id)
            raise e

        # There is really no point in waiting all these services to reload so do them
        # in background.
        async def restart_services():
            await self.middleware.call('service.reload', 'disk')
            await self.middleware.call('service.restart', 'system_datasets')
            # regenerate crontab because of scrub
            await self.middleware.call('service.restart', 'cron')

        asyncio.ensure_future(restart_services())

        pool = await self._get_instance(pool_id)
        await self.middleware.call_hook('pool.post_create_or_update', pool=pool)
        return pool

    @accepts(Int('id'), Patch(
        'pool_create', 'pool_update',
        ('rm', {'name': 'name'}),
        ('rm', {'name': 'encryption'}),
        ('edit', {'name': 'topology', 'method': lambda x: setattr(x, 'update', True)}),
    ))
    @job(lock='pool_createupdate')
    async def do_update(self, job, id, data):
        """
        Update pool of `id`, adding the new topology.

        The `type` of `data` must be the same of existing vdevs.

        .. examples(websocket)::

          Add a new set of raidz1 to pool of id 1.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.update",
                "params": [1, {
                    "topology": {
                        "data": [
                            {"type": "RAIDZ1", "disks": ["da7", "da8", "da9"]}
                        ]
                    }
                }]
            }
        """
        pool = await self._get_instance(id)

        verrors = ValidationErrors()

        await self.__common_validation(verrors, data, 'pool_update', old=pool)
        disks, vdevs = await self.__convert_topology_to_vdevs(data['topology'])
        disks_cache = await self.__check_disks_availability(verrors, disks)

        if verrors:
            raise verrors

        if pool['encryptkey']:
            enc_keypath = os.path.join(GELI_KEYPATH, f'{pool["encryptkey"]}.key')
        else:
            enc_keypath = None

        enc_disks = await self.__format_disks(job, disks, enc_keypath)

        job.set_progress(90, 'Extending ZFS Pool')

        extend_job = await self.middleware.call('zfs.pool.extend', pool['name'], vdevs)
        await extend_job.wait()

        if extend_job.error:
            raise CallError(extend_job.error)

        await self.__save_encrypteddisks(id, enc_disks, disks_cache)

        if pool['encrypt'] >= 2:
            # FIXME: ask current passphrase and validate
            await self.middleware.call('disk.geli_passphrase', pool, None)
            await self.middleware.call(
                'datastore.update', 'storage.volume', id, {'encrypt': 1}, {'prefix': 'vol_'},
            )

        pool = await self._get_instance(id)
        await self.middleware.call_hook('pool.post_create_or_update', pool=pool)
        return pool

    async def __common_validation(self, verrors, data, schema_name, old=None):
        topology_data = list(data['topology'].get('data') or [])

        if old:
            def disk_to_stripe():
                """
                We need to convert the original topology to use STRIPE
                instead of DISK to match the user input data
                """
                rv = []
                spare = None
                for i in old['topology']['data']:
                    if i['type'] == 'DISK':
                        if spare is None:
                            spare = {
                                'type': 'STRIPE',
                                'disks': [i['path']],
                            }
                            rv.append(spare)
                        else:
                            spare['disks'].append(i['path'])
                    else:
                        rv.append({
                            'type': i['type'],
                            'disks': [j['type'] for j in i['children']],
                        })
                return rv

            topology_data += disk_to_stripe()
        lastdatatype = None
        for i, vdev in enumerate(topology_data):
            numdisks = len(vdev['disks'])
            minmap = {
                'STRIPE': 1,
                'MIRROR': 2,
                'RAIDZ1': 3,
                'RAIDZ2': 4,
                'RAIDZ3': 5,
            }
            mindisks = minmap[vdev['type']]
            if numdisks < mindisks:
                verrors.add(
                    f'{schema_name}.topology.data.{i}.disks',
                    f'You need at least {mindisks} disk(s) for this vdev type.',
                )

            if lastdatatype and lastdatatype != vdev['type']:
                verrors.add(
                    f'{schema_name}.topology.data.{i}.type',
                    'You are not allowed to create a pool with different data vdev types '
                    f'({lastdatatype} and {vdev["type"]}).',
                )
            lastdatatype = vdev['type']

        for i in ('cache', 'log', 'spare'):
            value = data['topology'].get(i)
            if value and len(value) > 1:
                verrors.add(
                    f'{schema_name}.topology.{i}',
                    f'Only one row for the virtual device of type {i} is allowed.',
                )

    async def __convert_topology_to_vdevs(self, topology):
        # We do two things here:
        # 1. Gather all disks transversing the topology
        # 2. Keep track of the vdev each disk is supposed to be located
        #    along with a flag whether we should use swap partition in said vdev
        # This is required so we can format all disks in one pass, allowing it
        # to be performed in parallel if we wish to do so.
        disks = {}
        vdevs = []
        for i in ('data', 'cache', 'log'):
            t_vdevs = topology.get(i)
            if not t_vdevs:
                continue
            for t_vdev in t_vdevs:
                vdev_devs_list = []
                vdev = {
                    'root': i.upper(),
                    'type': t_vdev['type'],
                    'devices': vdev_devs_list,
                }
                vdevs.append(vdev)
                # cache and log devices should not have a swap
                create_swap = True if i == 'data' else False
                for disk in t_vdev['disks']:
                    disks[disk] = {'vdev': vdev_devs_list, 'create_swap': create_swap}

        if topology.get('spares'):
            vdev_devs_list = []
            vdevs.append({
                'root': 'SPARE',
                'type': 'STRIPE',
                'devices': vdev_devs_list,
            })
            for disk in topology['spares']:
                disks[disk] = {'vdev': vdev_devs_list, 'create_swap': True}

        return disks, vdevs

    async def __check_disks_availability(self, verrors, disks):
        """
        Makes sure the disks are present in the system and not reserved
        by anything else (boot, pool, iscsi, etc).

        Returns:
            dict - disk.query for all disks
        """
        disks_cache = dict(map(
            lambda x: (x['name'], x),
            await self.middleware.call(
                'disk.query', [('name', 'in', list(disks.keys()))]
            )
        ))
        disks_cache.update(dict(map(
            lambda x: (x['multipath_name'], x),
            await self.middleware.call(
                'disk.query', [('multipath_name', 'in', list(disks.keys()))]
            )
        )))

        disks_set = set(disks.keys())
        disks_not_in_cache = disks_set - set(disks_cache.keys())
        if disks_not_in_cache:
            verrors.add(
                'pool_create.topology',
                f'The following disks were not found in system: {"," .join(disks_not_in_cache)}.'
            )

        disks_reserved = await self.middleware.call('disk.get_reserved')
        disks_reserved = disks_set - (disks_set - set(disks_reserved))
        if disks_reserved:
            verrors.add(
                'pool_create.topology',
                f'The following disks are already in use: {"," .join(disks_reserved)}.'
            )
        return disks_cache

    async def __format_disks(self, job, disks, enc_keypath, passphrase=None):
        """
        Format all disks, putting all freebsd-zfs partitions created
        into their respectives vdevs.
        """
        enc_disks = []

        # TODO: Make this work in parallel for speed, may take a long time with dozens of drives
        swapgb = (await self.middleware.call('system.advanced.config'))['swapondrive']
        for i, disk_items in enumerate(disks.items()):
            disk, config = disk_items
            job.set_progress(15, f'Formatting disks ({i + 1}/{len(disks)})')
            await self.middleware.call('disk.format', disk, swapgb if config['create_swap'] else 0)
            devname = await self.middleware.call('disk.gptid_from_part_type', disk, 'freebsd-zfs')
            if enc_keypath:
                enc_disks.append({
                    'disk': disk,
                    'devname': devname,
                })
                devname = await self.middleware.call('disk.encrypt', devname, enc_keypath, passphrase)
            config['vdev'].append(f'/dev/{devname}')
        return enc_disks

    async def __save_encrypteddisks(self, pool_id, enc_disks, disks_cache):
        for enc_disk in enc_disks:
            await self.middleware.call(
                'datastore.insert',
                'storage.encrypteddisk',
                {
                    'volume': pool_id,
                    'disk': disks_cache[enc_disk['disk']]['identifier'],
                    'provider': enc_disk['devname'],
                },
                {'prefix': 'encrypted_'},
            )

    @item_method
    @accepts(Int('id', required=False, default=None, null=True))
    async def get_disks(self, oid):
        """
        Get all disks in use by pools.
        If `id` is provided only the disks from the given pool `id` will be returned.
        """
        filters = []
        if oid:
            filters.append(('id', '=', oid))
        for pool in await self.query(filters):
            if pool['is_decrypted'] and pool['status'] != 'OFFLINE':
                for i in await self.middleware.call('zfs.pool.get_disks', pool['name']):
                    yield i
            else:
                for encrypted_disk in await self.middleware.call(
                    'datastore.query',
                    'storage.encrypteddisk',
                    [('encrypted_volume', '=', pool['id'])]
                ):
                    # Use provider and not disk because a disk is not a guarantee
                    # to point to correct device if its locked and its not in the system
                    # (e.g. temporarily). See #50291
                    prov = encrypted_disk["encrypted_provider"]
                    if not prov:
                        continue

                    disk_name = await self.middleware.call('disk.label_to_disk', prov)
                    if not disk_name:
                        continue

                    disk = await self.middleware.call('disk.query', [('name', '=', disk_name)])
                    if not disk:
                        continue
                    disk = disk[0]

                    if os.path.exists(os.path.join("/dev", disk['devname'])):
                        yield disk['devname']

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('label', required=True),
        Str('disk', required=True),
        Bool('force', default=False),
        Str('passphrase', private=True),
    ))
    @job(lock='pool_replace')
    async def replace(self, job, oid, options):
        """
        Replace a disk on a pool.

        `label` is the ZFS guid or a device name
        `disk` is the identifier of a disk

        .. examples(websocket)::

          Replace missing ZFS device with disk {serial}FOO.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.replace",
                "params": [1, {
                    "label": "80802394992848654",
                    "disk": "{serial}FOO"
                }]
            }
        """
        pool = await self._get_instance(oid)

        verrors = ValidationErrors()

        unused_disks = await self.middleware.call('disk.get_unused')
        disk = list(filter(lambda x: x['identifier'] == options['disk'], unused_disks))
        if not disk:
            verrors.add('options.disk', 'Disk not found.', errno.ENOENT)
        else:
            disk = disk[0]

            if not options['force'] and not await self.middleware.call(
                'disk.check_clean', disk['devname']
            ):
                verrors.add('options.force', 'Disk is not clean, partitions were found.')

        if pool['encrypt'] == 2:
            if not options.get('passphrase'):
                verrors.add('options.passphrase', 'Passphrase is required for encrypted pool.')
            elif not await self.middleware.call(
                'disk.geli_testkey', pool, options['passphrase']
            ):
                verrors.add('options.passphrase', 'Passphrase is not valid.')

        found = self.__find_disk_from_topology(options['label'], pool)

        if not found:
            verrors.add('options.label', f'Label {options["label"]} not found.', errno.ENOENT)

        if verrors:
            raise verrors

        if found[0] in ('data', 'spare'):
            create_swap = True
        else:
            create_swap = False

        swap_disks = [disk['devname']]
        # If the disk we are replacing is still available, remove it from swap as well
        if found[1] and os.path.exists(found[1]['path']):
            from_disk = await self.middleware.call(
                'disk.label_to_disk', found[1]['path'].replace('/dev/', '')
            )
            if from_disk:
                swap_disks.append(from_disk)

        await self.middleware.call('disk.swaps_remove_disks', swap_disks)

        vdev = []
        passphrase_path = None
        if options.get('passphrase'):
            passf = tempfile.NamedTemporaryFile(mode='w+', dir='/tmp/')
            os.chmod(passf.name, 0o600)
            passf.write(options['passphrase'])
            passf.flush()
            passphrase_path = passf.name
        try:
            enc_disks = await self.__format_disks(
                job,
                {disk['devname']: {'vdev': vdev, 'create_swap': create_swap}},
                pool['encryptkey_path'],
                passphrase_path,
            )
        finally:
            if passphrase_path:
                passf.close()

        new_devname = vdev[0].replace('/dev/', '')

        try:
            await self.middleware.call(
                'zfs.pool.replace', pool['name'], options['label'], new_devname
            )
            # If we are replacing a faulted disk, kick it right after replace
            # is initiated.
            try:
                vdev = await self.middleware.call(
                    'zfs.pool.get_vdev', pool['name'], options['label'],
                )
                if vdev['status'] not in ('ONLINE', 'DEGRADED'):
                    await self.middleware.call('zfs.pool.detach', pool['name'], options['label'])
            except Exception:
                self.logger.warn('Failed to detach device', exc_info=True)
        except Exception as e:
            try:
                # If replace has failed lets detach geli to not keep disk busy
                await self.middleware.call('disk.geli_detach_single', new_devname)
            except Exception:
                self.logger.warn(f'Failed to geli detach {new_devname}', exc_info=True)
            raise e
        finally:
            # Needs to happen even if replace failed to put back disk that had been
            # removed from swap prior to replacement
            await self.middleware.call('disk.swaps_configure')

        await self.__save_encrypteddisks(oid, enc_disks, {disk['devname']: disk})

        return True

    def __find_disk_from_topology(self, label, pool):
        check = []
        found = None
        for root, children in pool['topology'].items():
            check.append((root, children))

        while check:
            root, children = check.pop()
            for c in children:
                if c['type'] == 'DISK':
                    if label in (c['path'].replace('/dev/', ''), c['guid']):
                        found = (root, c)
                        break
                if c['children']:
                    check.append((root, c['children']))
        return found

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('label', required=True),
    ))
    async def detach(self, oid, options):
        """
        Detach a disk from pool of id `id`.

        `label` is the vdev guid or device name.

        .. examples(websocket)::

          Detach ZFS device.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.detach,
                "params": [1, {
                    "label": "80802394992848654"
                }]
            }
        """
        pool = await self._get_instance(oid)

        verrors = ValidationErrors()
        found = self.__find_disk_from_topology(options['label'], pool)
        if not found:
            verrors.add('options.label', f'Label {options["label"]} not found on this pool.')
        if verrors:
            raise verrors

        disk = await self.middleware.call(
            'disk.label_to_disk', found[1]['path'].replace('/dev/', '')
        )
        if disk:
            await self.middleware.call('disk.swaps_remove_disks', [disk])

        await self.middleware.call('zfs.pool.detach', pool['name'], found[1]['guid'])

        await self.middleware.call('pool.sync_encrypted', oid)

        if disk:
            await self.middleware.call('disk.unlabel', disk)

        return True

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('label', required=True),
    ))
    async def offline(self, oid, options):
        """
        Offline a disk from pool of id `id`.

        `label` is the vdev guid or device name.

        .. examples(websocket)::

          Offline ZFS device.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.offline,
                "params": [1, {
                    "label": "80802394992848654"
                }]
            }
        """
        pool = await self._get_instance(oid)

        verrors = ValidationErrors()
        found = self.__find_disk_from_topology(options['label'], pool)
        if not found:
            verrors.add('options.label', f'Label {options["label"]} not found on this pool.')
        if verrors:
            raise verrors

        disk = await self.middleware.call(
            'disk.label_to_disk', found[1]['path'].replace('/dev/', '')
        )
        await self.middleware.call('disk.swaps_remove_disks', [disk])

        await self.middleware.call('zfs.pool.offline', pool['name'], found[1]['guid'])

        if found[1]['path'].endswith('.eli'):
            devname = found[1]['path'].replace('/dev/', '')[:-4]
            await self.middleware.call('disk.geli_detach_single', devname)
            await self.middleware.call(
                'datastore.delete',
                'storage.encrypteddisk',
                [('encrypted_volume', '=', oid), ('encrypted_provider', '=', devname)],
            )
        return True

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('label', required=True),
    ))
    async def online(self, oid, options):
        """
        Online a disk from pool of id `id`.

        `label` is the vdev guid or device name.

        .. examples(websocket)::

          Online ZFS device.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.online,
                "params": [1, {
                    "label": "80802394992848654"
                }]
            }
        """
        pool = await self._get_instance(oid)

        verrors = ValidationErrors()

        found = self.__find_disk_from_topology(options['label'], pool)
        if not found:
            verrors.add('options.label', f'Label {options["label"]} not found on this pool.')

        if pool['encrypt'] > 0:
            verrors.add('id', 'Disk cannot be set to online in encrypted pool.')

        if verrors:
            raise verrors

        await self.middleware.call('zfs.pool.online', pool['name'], found[1]['guid'])

        disk = await self.middleware.call(
            'disk.label_to_disk', found[1]['path'].replace('/dev/', '')
        )
        if disk:
            await self.middleware.call('disk.swaps_configure')

        return True

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('label', required=True),
    ))
    async def remove(self, oid, options):
        """
        Remove a disk from pool of id `id`.

        `label` is the vdev guid or device name.

        .. examples(websocket)::

          Remove ZFS device.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.remove,
                "params": [1, {
                    "label": "80802394992848654"
                }]
            }
        """
        pool = await self._get_instance(oid)

        verrors = ValidationErrors()

        found = self.__find_disk_from_topology(options['label'], pool)
        if not found:
            verrors.add('options.label', f'Label {options["label"]} not found on this pool.')

        if verrors:
            raise verrors

        await self.middleware.call('zfs.pool.remove', pool['name'], found[1]['guid'])

        await self.middleware.call('pool.sync_encrypted', oid)

        if found[1]['path'].endswith('.eli'):
            devname = found[1]['path'].replace('/dev/', '')[:-4]
            await self.middleware.call('disk.geli_detach_single', devname)

        disk = await self.middleware.call(
            'disk.label_to_disk', found[1]['path'].replace('/dev/', '')
        )
        if disk:
            await self.middleware.call('disk.swaps_remove_disks', [disk])
            await self.middleware.call('disk.unlabel', disk)

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('passphrase', private=True, required=True, null=True),
        Str('admin_password', private=True),
    ))
    async def passphrase(self, oid, options):
        """
        Create/Change/Remove passphrase for an encrypted pool.

        Setting passphrase to null will remove the passphrase.
        `admin_password` is required when changing or removing passphrase.

        .. examples(websocket)::

          Change passphrase for pool 1.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.passphrase,
                "params": [1, {
                    "passphrase": "mysecretpassphrase",
                    "admin_password": "rootpassword"
                }]
            }
        """
        pool = await self._get_instance(oid)

        verrors = await self.__common_encopt_validation(pool, options)

        if (
            pool['name'] == (await self.middleware.call('systemdataset.config'))['pool'] and (
                pool['encrypt'] == 1 or (pool['encrypt'] == 2 and options['passphrase'])
            )
        ):
            # Only allow removing passphrase for pools being used by system dataset service
            verrors.add(
                'id',
                'A passphrase on a pool containing the system dataset can only be removed. '
                'It cannot be set or changed.'
            )

        # For historical reasons (API v1.0 compatibility) we only require
        # admin_password when changing/removing passphrase
        if pool['encrypt'] == 2 and not options.get('admin_password'):
            verrors.add('options.admin_password', 'This attribute is required.')

        verrors.check()

        await self.middleware.call('disk.geli_passphrase', pool, options['passphrase'], True)

        if pool['encrypt'] == 1 and options['passphrase']:
            await self.middleware.call(
                'datastore.update', 'storage.volume', oid, {'vol_encrypt': 2}
            )
        elif pool['encrypt'] == 2 and not options['passphrase']:
            await self.middleware.call(
                'datastore.update', 'storage.volume', oid, {'vol_encrypt': 1}
            )
        return True

    async def __common_encopt_validation(self, pool, options):
        verrors = ValidationErrors()

        if pool['encrypt'] == 0:
            verrors.add('id', 'Pool is not encrypted.')

        # admin password is optional, its choice of the client to enforce
        # it or not.
        if 'admin_password' in options and not await self.middleware.call(
            'auth.check_user', 'root', options['admin_password']
        ):
            verrors.add('options.admin_password', 'Invalid admin password.')

        if verrors:
            raise verrors
        return verrors

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('admin_password', private=True, required=False),
    ))
    async def rekey(self, oid, options):
        """
        Rekey encrypted pool `id`.

        .. examples(websocket)::

          Rekey pool 1.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.rekey,
                "params": [1, {
                    "admin_password": "rootpassword"
                }]
            }
        """
        pool = await self._get_instance(oid)

        await self.__common_encopt_validation(pool, options)

        await self.middleware.call('disk.geli_rekey', pool)

        if pool['encrypt'] == 2:
            await self.middleware.call(
                'datastore.update', 'storage.volume', oid, {'vol_encrypt': 1}
            )

        await self.middleware.call_hook('pool.rekey_done', pool=pool)
        return True

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('admin_password', private=True, required=False),
    ))
    @job(lock=lambda x: f'pool_reckey_{x[0]}', pipes=['output'])
    async def recoverykey_add(self, job, oid, options):
        """
        Add Recovery key for encrypted pool `id`.

        This is to be used with `core.download` which will provide an URL
        to download the recovery key.
        """
        pool = await self._get_instance(oid)

        await self.__common_encopt_validation(pool, options)

        reckey = await self.middleware.call('disk.geli_recoverykey_add', pool)

        job.pipes.output.w.write(base64.b64decode(reckey))
        job.pipes.output.w.close()

        return True

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('admin_password', private=True, required=False),
    ))
    async def recoverykey_rm(self, oid, options):
        """
        Remove recovery key for encrypted pool `id`.

        .. examples(websocket)::

          Remove recovery key for pool 1.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.recoverykey_rm,
                "params": [1, {
                    "admin_password": "rootpassword"
                }]
            }
        """
        pool = await self._get_instance(oid)

        await self.__common_encopt_validation(pool, options)

        await self.middleware.call('disk.geli_recoverykey_rm', pool)

        return True

    @accepts()
    async def unlock_services_restart_choices(self):
        """
        Get a mapping of services identifiers and labels that can be restart
        on volume unlock.
        """
        svcs = {
            'afp': 'AFP',
            'cifs': 'SMB',
            'ftp': 'FTP',
            'iscsitarget': 'iSCSI',
            'nfs': 'NFS',
            'webdav': 'WebDAV',
            'jails': 'Jails/Plugins',
        }
        return svcs

    @item_method
    @accepts(Int('id'), Dict(
        'options',
        Str('passphrase', private=True, required=False),
        Bool('recoverykey', default=False),
        List('services_restart', default=[]),
    ))
    @job(lock='unlock_pool', pipes=['input'], check_pipes=False)
    async def unlock(self, job, oid, options):
        """
        Unlock encrypted pool `id`.

        `passphrase` is required of a recovery key is not provided.

        If `recoverykey` is true this method expects the recovery key file to be uploaded using
        the /_upload/ endpoint.

        `services_restart` is a list of services to be restarted when the pool gets unlocked.
        Said list be be retrieve using `pool.unlock_services_restart_choices`.

        .. examples(websocket)::

          Unlock pool of id 1, restarting "cifs" service.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.unlock,
                "params": [1, {
                    "passphrase": "mysecretpassphrase",
                    "services_restart": ["cifs"]
                }]
            }
        """
        pool = await self._get_instance(oid)

        verrors = ValidationErrors()

        if pool['encrypt'] == 0:
            verrors.add('id', 'Pool is not encrypted.')
        elif pool['status'] != 'OFFLINE':
            verrors.add('id', 'Pool already unlocked.')

        if options.get('passphrase') and options['recoverykey']:
            verrors.add(
                'options.passphrase', 'Either provide a passphrase or a recovery key, not both.'
            )
        elif not options.get('passphrase') and not options['recoverykey']:
            verrors.add(
                'options.passphrase', 'Provide a passphrase or a recovery key.'
            )

        services_restart_choices = set((await self.unlock_services_restart_choices()).keys())
        options_services_restart = set(options['services_restart'])
        invalid_choices = options_services_restart - services_restart_choices
        if invalid_choices:
            verrors.add(
                'options.services_restart', f'Invalid choices: {", ".join(invalid_choices)}'
            )

        if verrors:
            raise verrors

        if options['recoverykey']:
            job.check_pipe("input")
            with tempfile.NamedTemporaryFile(mode='wb+', dir='/tmp/') as f:
                f.write(job.pipes.input.r.read())
                f.flush()
                failed = await self.middleware.call('disk.geli_attach', pool, None, f.name)
        else:
            failed = await self.middleware.call('disk.geli_attach', pool, options['passphrase'])

        # We need to try to import the pool even if some disks failed to attach
        try:
            await self.middleware.call('zfs.pool.import_pool', pool['guid'], {
                'altroot': '/mnt',
                'cachefile': ZPOOL_CACHE_FILE,
            })
        except Exception as e:
            # mounting filesystems may fail if we have readonly datasets as parent
            if not isinstance(e, ZFSException) or e.code.name != 'MOUNTFAILED':
                detach_failed = await self.middleware.call('disk.geli_detach', pool)
                if failed > 0:
                    msg = f'Pool could not be imported: {failed} devices failed to decrypt.'
                    if detach_failed > 0:
                        msg += (
                            f' {detach_failed} devices failed to detach and were left decrypted.'
                        )
                    raise CallError(msg)
                elif detach_failed > 0:
                    self.logger.warn('Pool %s failed to import', pool['name'], exc_info=True)
                    raise CallError(f'Pool could not be imported ({detach_failed} devices left decrypted): {str(e)}')
                raise e

        await self.middleware.call('pool.sync_encrypted', oid)

        await self.middleware.call('core.bulk', 'service.restart', [
            [i] for i in options['services_restart'] + ['system_datasets', 'disk']
        ])
        if 'jails' in options['services_restart']:
            await self.middleware.call('core.bulk', 'jail.rc_action', [['RESTART']])

        await self.middleware.call_hook('pool.post_unlock', pool=pool)

        return True

    @item_method
    @accepts(Int('id'), Str('passphrase', private=True))
    @job(lock='lock_pool')
    async def lock(self, job, oid, passphrase):
        """
        Lock encrypted pool `id`.
        """
        pool = await self._get_instance(oid)

        verrors = ValidationErrors()

        if pool['encrypt'] == 0:
            verrors.add('id', 'Pool is not encrypted.')
        elif pool['status'] == 'OFFLINE':
            verrors.add('id', 'Pool already locked.')

        if not verrors:
            # Make sure that this pool is not being used by system dataset service
            if pool['name'] == (await self.middleware.call('systemdataset.config'))['pool']:
                verrors.add(
                    'id',
                    'Pool contains the system dataset and cannot be locked. Please select a different pool or '
                    'configure the system dataset to be on a different pool.'
                )
            else:
                if not await self.middleware.call('disk.geli_testkey', pool, passphrase):
                    verrors.add(
                        'passphrase',
                        'The entered passphrase was not valid. Please enter the correct passphrase to lock the pool.'
                    )

        if verrors:
            raise verrors

        await self.middleware.call_hook('pool.pre_lock', pool=pool)

        sysds = await self.middleware.call('systemdataset.config')
        if sysds['pool'] == pool['name']:
            job = await self.middleware.call('systemdataset.update', {
                'pool': None, 'pool_exclude': pool['name'],
            })
            await job.wait()
            if job.error:
                raise CallError(job.error)

        await self.middleware.call('zfs.pool.export', pool['name'])

        for ed in await self.middleware.call(
                'datastore.query', 'storage.encrypteddisk', [('encrypted_volume', '=', pool['id'])]
        ):
            await self.middleware.call('disk.geli_detach_single', ed['encrypted_provider'])

        await self.middleware.call_hook('pool.post_lock', pool=pool)
        await self.middleware.call('service.restart', 'system_datasets')

        return True

    @item_method
    @accepts(Int('id'))
    async def download_encryption_key(self, oid):
        """
        Download encryption key for a given pool `id`.
        """
        pool = await self.query([('id', '=', oid)], {'get': True})
        if not pool['encryptkey']:
            return None

        job_id, url = await self.middleware.call(
            'core.download',
            'filesystem.get',
            [os.path.join(self.GELI_KEYPATH, f"{pool['encryptkey']}.key")],
            'geli.key'
        )
        return url

    @private
    def configure_resilver_priority(self):
        """
        Configure resilver priority based on user selected off-peak hours.
        """
        resilver = self.middleware.call_sync('datastore.config', 'storage.resilver')

        if not resilver['enabled'] or not resilver['weekday']:
            return

        higher_prio = False
        weekdays = map(lambda x: int(x), resilver['weekday'].split(','))
        now = datetime.now()
        now_t = now.time()
        # end overlaps the day
        if resilver['begin'] > resilver['end']:
            if now.isoweekday() in weekdays and now_t >= resilver['begin']:
                higher_prio = True
            else:
                lastweekday = now.isoweekday() - 1
                if lastweekday == 0:
                    lastweekday = 7
                if lastweekday in weekdays and now_t < resilver['end']:
                    higher_prio = True
        # end does not overlap the day
        else:
            if now.isoweekday() in weekdays and now_t >= resilver['begin'] and now_t < resilver['end']:
                higher_prio = True

        if higher_prio:
            resilver_delay = 0
            resilver_min_time_ms = 9000
            scan_idle = 0
        else:
            resilver_delay = 2
            resilver_min_time_ms = 3000
            scan_idle = 50

        sysctl.filter('vfs.zfs.resilver_delay')[0].value = resilver_delay
        sysctl.filter('vfs.zfs.resilver_min_time_ms')[0].value = resilver_min_time_ms
        sysctl.filter('vfs.zfs.scan_idle')[0].value = scan_idle

    @accepts()
    async def import_find(self):
        """
        Get a list of pools available for import with the following details:
        name, guid, status, hostname.
        """

        existing_guids = [i['guid'] for i in await self.middleware.call('pool.query')]

        for pool in await self.middleware.call('zfs.pool.find_import'):
            if pool['status'] == 'UNAVAIL':
                continue
            # Exclude pools with same guid as existing pools (in database)
            # It could be the pool is in the database but was exported/detached for some reason
            # See #6808
            if pool['guid'] in existing_guids:
                continue
            entry = {}
            for i in ('name', 'guid', 'status', 'hostname'):
                entry[i] = pool[i]
            yield entry

    @accepts(Dict(
        'pool_import',
        Str('guid', required=True),
        Str('name'),
        Str('passphrase', private=True),
    ))
    @job(lock='import_pool', pipes=['input'], check_pipes=False)
    async def import_pool(self, job, data):
        """
        Import a pool found with `pool.import_find`.

        If a `name` is specified the pool will be imported using that new name.

        `devices` is required while importing an encrypted pool. In that case this method needs to
        be called using /_upload/ endpoint with the encryption key.

        Errors:
            ENOENT - Pool not found

        .. examples(websocket)::

          Import pool of guid 5571830764813710860.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.import_pool,
                "params": [{
                    "guid": "5571830764813710860"
                }]
            }
        """

        pool = None
        for p in await self.middleware.call('zfs.pool.find_import'):
            if p['guid'] == data['guid']:
                pool = p
                break
        if pool is None:
            raise CallError(f'Pool with guid "{data["guid"]}" not found', errno.ENOENT)

        try:
            job.check_pipe("input")
            key = job.pipes.input.r
        except ValueError:
            key = None

        passfile = None
        if key and data.get('passphrase'):
            encrypt = 2
            passfile = tempfile.mktemp(dir='/tmp/')
            with open(passfile, 'w') as f:
                os.chmod(passfile, 600)
                f.write(data['passphrase'])
        elif key:
            encrypt = 1
        else:
            encrypt = 0

        pool_name = data.get('name') or pool['name']
        scrub_id = pool_id = None
        try:
            pool_id = await self.middleware.call('datastore.insert', 'storage.volume', {
                'vol_name': pool_name,
                'vol_encrypt': encrypt,
                'vol_guid': data['guid'],
            })
            pool = await self.middleware.call('pool.query', [('id', '=', pool_id)], {'get': True})
            if encrypt > 0:
                if not os.path.exists(GELI_KEYPATH):
                    os.mkdir(GELI_KEYPATH)
                with open(pool['encryptkey_path'], 'wb') as f:
                    f.write(key.read())

            scrub_id = (await self.middleware.call('pool.scrub.create', {
                'pool': pool_id,
            }))['id']

            await self.middleware.call('zfs.pool.import_pool', pool['guid'], {
                'altroot': '/mnt',
                'cachefile': ZPOOL_CACHE_FILE,
            })

            await self.middleware.call('zfs.dataset.update', pool_name, {
                'properties': {
                    'aclmode': {'value': 'passthrough'},
                    'aclinherit': {'value': 'passthrough'},
                },
            })

            # Reset all mountpoints
            await self.middleware.call('zfs.dataset.inherit', pool_name, 'mountpoint', True)

            await self.middleware.call('pool.sync_encrypted', pool_id)
        except Exception:
            if scrub_id:
                await self.middleware.call('pool.scrub.delete', scrub_id)
            if pool_id:
                await self.middleware.call('datastore.delete', 'storage.volume', pool_id)
            if passfile:
                os.unlink(passfile)
            raise

        await self.middleware.call('service.reload', 'disk')
        await self.middleware.call_hook('pool.post_import_pool', pool)

        return True

    @accepts(
        Str('device'),
        Str('fs_type'),
        Dict('fs_options', additional_attrs=True),
        Str('dst_path')
    )
    @job(lock=lambda args: 'volume_import', logs=True)
    async def import_disk(self, job, device, fs_type, fs_options, dst_path):
        """
        Import a disk, by copying its content to a pool.

        .. examples(websocket)::

          Import a FAT32 (msdosfs) disk.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.import_disk,
                "params": [
                    "/dev/da0", "msdosfs", {}, "/mnt/tank/mydisk"
                ]
            }
        """
        job.set_progress(None, description="Mounting")

        src = os.path.join('/var/run/importcopy/tmpdir', os.path.relpath(device, '/'))

        if os.path.exists(src):
            os.rmdir(src)

        try:
            os.makedirs(src)

            async with KernelModuleContextManager({"ext2fs": "ext2fs",
                                                   "msdosfs": "msdosfs_iconv",
                                                   "ntfs": "fuse"}.get(fs_type)):
                async with MountFsContextManager(self.middleware, device, src, fs_type, fs_options, ["ro"]):
                    job.set_progress(None, description="Importing")

                    line = [
                        '/usr/local/bin/rsync',
                        '--info=progress2',
                        '--modify-window=1',
                        '-rltvh',
                        '--no-perms',
                        src + '/',
                        dst_path
                    ]
                    rsync_proc = await Popen(
                        line, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, preexec_fn=os.setsid,
                    )
                    try:
                        progress_buffer = JobProgressBuffer(job)
                        while True:
                            line = await rsync_proc.stdout.readline()
                            job.logs_fd.write(line)
                            if line:
                                try:
                                    line = line.decode("utf-8", "ignore").strip()
                                    bits = re.split(r"\s+", line)
                                    if len(bits) == 6 and bits[1].endswith("%") and bits[1][:-1].isdigit():
                                        progress_buffer.set_progress(int(bits[1][:-1]))
                                    elif not line.endswith('/'):
                                        if (
                                            line not in ['sending incremental file list'] and
                                            'xfr#' not in line
                                        ):
                                            progress_buffer.set_progress(None, extra=line)
                                except Exception:
                                    logger.warning('Parsing error in rsync task', exc_info=True)
                            else:
                                break

                        progress_buffer.flush()
                        await rsync_proc.wait()
                        if rsync_proc.returncode != 0:
                            raise Exception("rsync failed with exit code %r" % rsync_proc.returncode)
                    except asyncio.CancelledError:
                        rsync_proc.kill()
                        raise

                    job.set_progress(100, description="Done", extra="")
        finally:
            os.rmdir(src)

    @accepts(Str("device"))
    def import_disk_autodetect_fs_type(self, device):
        """
        Autodetect filesystem type for `pool.import_disk`.

        .. examples(websocket)::

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.import_disk_autodetect_fs_type",
                "params": ["/dev/da0"]
            }
        """
        proc = subprocess.Popen(["blkid", device], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")
        output = proc.communicate()[0].strip()

        if proc.returncode == 2:
            proc = subprocess.Popen(["file", "-s", device], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    encoding="utf-8")
            output = proc.communicate()[0].strip()
            if proc.returncode != 0:
                raise CallError(f"blkid failed with code 2 and file failed with code {proc.returncode}: {output}")

            if "Unix Fast File system" in output:
                return "ufs"

            raise CallError(f"blkid failed with code 2 and file produced unexpected output: {output}")

        if proc.returncode != 0:
            raise CallError(f"blkid failed with code {proc.returncode}: {output}")

        m = re.search("TYPE=\"(.+?)\"", output)
        if m is None:
            raise CallError(f"blkid produced unexpected output: {output}")

        fs = {
            "ext2": "ext2fs",
            "ext3": "ext2fs",
            "ntfs": "ntfs",
            "vfat": "msdosfs",
        }.get(m.group(1))
        if fs is None:
            self.logger.info("Unknown FS: %s", m.group(1))
            return None

        return fs

    @accepts()
    def import_disk_msdosfs_locales(self):
        """
        Get a list of locales for msdosfs type to be used in `pool.import_disk`.
        """
        return [
            locale.strip()
            for locale in subprocess.check_output(["locale", "-a"], encoding="utf-8").split("\n")
            if locale.strip() and locale.strip() not in ["C", "POSIX"]
        ]

    @item_method
    @accepts(
        Int('id'),
        Dict(
            'options',
            Bool('cascade', default=False),
            Bool('destroy', default=False),
        ),
    )
    @job(lock='pool_export')
    async def export(self, job, oid, options):
        """
        Export pool of `id`.

        `cascade` will remove all attachments of the given pool (`pool.attachments`).
        `destroy` will also PERMANENTLY destroy the pool/data.

        .. examples(websocket)::

          Export pool of id 1.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.export,
                "params": [1, {
                    "cascade": true,
                    "destroy": false
                }]
            }
        """
        pool = await self._get_instance(oid)

        job.set_progress(5, 'Retrieving pool attachments')
        attachments = await self.__attachments(pool)
        if options['cascade']:
            job.set_progress(10, 'Deleting pool attachments')
            await self.__delete_attachments(attachments, pool)

        job.set_progress(20, 'Stopping VMs using this pool (if any)')
        # If there is any guest vm attached to this volume, we stop them
        await self.middleware.call('vm.stop_by_pool', pool['name'], True)

        job.set_progress(30, 'Stopping jails using this pool (if any)')
        activated_pool = await self.middleware.call('jail.get_activated_pool')
        if activated_pool == pool['name']:
            for jail_host in attachments['jails']:
                await self.middleware.call('jail.stop', jail_host)

        job.set_progress(30, 'Removing pool disks from swap')
        disks = [i async for i in await self.middleware.call('pool.get_disks')]
        await self.middleware.call('disk.swaps_remove_disks', disks)

        sysds = await self.middleware.call('systemdataset.config')
        if sysds['pool'] == pool['name']:
            job.set_progress(40, 'Reconfiguring system dataset')
            sysds_job = await self.middleware.call('systemdataset.update', {
                'pool': None, 'pool_exclude': pool['name'],
            })
            await sysds_job.wait()
            if sysds_job.error:
                raise CallError(sysds_job.error)

        if pool['status'] == 'OFFLINE':
            # Pool exists only in database, its not imported
            pass
        elif options['destroy']:
            job.set_progress(60, 'Destroying pool')
            await self.middleware.call('zfs.pool.delete', pool['name'])

            job.set_progress(80, 'Cleaning disks')
            for disk in disks:
                await self.middleware.call('disk.unlabel', disk)
            await self.middleware.call('disk.geli_detach', pool, True)
            if pool['encrypt'] > 0:
                try:
                    os.remove(pool['encryptkey_path'])
                except OSError as e:
                    self.logger.warn(
                        'Failed to remove encryption key %s: %s',
                        pool['encryptkey_path'],
                        e,
                        exc_info=True,
                    )
        else:
            job.set_progress(80, 'Exporting pool')
            await self.middleware.call('zfs.pool.export', pool['name'])
            await self.middleware.call('disk.geli_detach', pool)

        job.set_progress(90, 'Cleaning up')
        if os.path.isdir(pool['path']):
            try:
                # We dont try to remove recursively to avoid removing files that were
                # potentially hidden by the mount
                os.rmdir(pool['path'])
            except OSError as e:
                self.logger.warn('Failed to remove pointoint %s: %s', pool['path'], e)

        await self.middleware.call('datastore.delete', 'storage.volume', oid)

        # scrub needs to be regenerated in crontab
        await self.middleware.call('service.restart', 'cron')

        await self.middleware.call_hook('pool.post_export', pool=pool, options=options)

    @item_method
    @accepts(Int('id'))
    async def attachments(self, oid):
        """
        Return a dict composed by the name of services and ids of each item
        dependent of this pool.

        Responsible for telling the user whether there is a related
        share, asking for confirmation.
        """
        pool = await self._get_instance(oid)
        return await self.__attachments(pool)

    async def __attachments(self, pool):
        attachments = {
            'afp': [],
            'iscsi_extents': [],
            'jails': [],
            'nfs': [],
            'replication': [],
            'smb': [],
            'snaptask': [],
            'vm_devices': [],
        }

        for smb in await self.middleware.call('sharing.smb.query'):
            if smb['path'] == pool['path'] or smb['path'].startswith(pool['path'] + '/'):
                attachments['smb'].append(smb['id'])

        for afp in await self.middleware.call('sharing.afp.query'):
            if afp['path'] == pool['path'] or afp['path'].startswith(pool['path'] + '/'):
                attachments['afp'].append(afp['id'])

        for nfs in await self.middleware.call('sharing.nfs.query'):
            for path in nfs['paths']:
                if (
                    (path == pool['path'] or path.startswith(pool['path'] + '/')) and
                    nfs['id'] not in attachments['nfs']
                ):
                    attachments['nfs'].append(nfs['id'])

        for extent in await self.middleware.call('iscsi.extent.query', [('type', '=', 'DISK')]):
            if extent['path'].startswith(f'zvol/{pool["name"]}/'):
                attachments['iscsi_extents'].append(extent['id'])

        for vm_attached in await self.middleware.call('vm.stop_by_pool', pool['name']):
            attachments['vm_devices'].append(vm_attached['device_id'])

        for repl in await self.middleware.call('replication.query'):
            if any(
                source_dataset == pool['name'] or source_dataset.startswith(pool['name'] + '/')
                for source_dataset in repl['source_datasets']
            ):
                attachments['replication'].append(repl['id'])

        for snap in await self.middleware.call('pool.snapshottask.query'):
            if (
                snap['dataset'] == pool['name'] or
                snap['dataset'].startswith(pool['name'] + '/')
            ):
                attachments['snaptask'].append(snap['id'])

        activated_pool = await self.middleware.call('jail.get_activated_pool')
        if activated_pool == pool['name']:
            for j in await self.middleware.call('jail.query', [('state', '=', 'up')]):
                attachments['jails'].append(j['host_hostuuid'])

        return attachments

    async def __delete_attachments(self, attachments, pool):
        # TODO: use a hook and move delete/stop to each plugin
        for name, service in (
            ('smb', 'sharing.smb.delete'),
            ('afp', 'sharing.afp.delete'),
            ('nfs', 'sharing.nfs.delete'),
            ('iscsi_extents', 'iscsi.extent.delete'),
            ('snaptask', 'pool.snapshottask.delete'),
        ):
            for aid in attachments[name]:
                await self.middleware.call(service, aid)

        for name, datastore in (
            ('replication', 'storage.replication'),
            ('vm_devices', 'vm.device'),
        ):
            for aid in attachments[name]:
                await self.middleware.call('datastore.delete', datastore, aid)

    @staticmethod
    def __get_dev_and_disk(topology):
        rv = []
        for values in topology.values():
            values = values.copy()
            while values:
                value = values.pop()
                if value['type'] == 'DISK':
                    rv.append((value['path'].replace('/dev/', ''), value['disk']))
                values += value.get('children') or []
        return rv

    @private
    def sync_encrypted(self, pool=None):
        """
        This syncs the EncryptedDisk table with the current state
        of a volume
        """
        if pool is not None:
            filters = [('id', '=', pool)]
        else:
            filters = []

        pools = self.middleware.call_sync('pool.query', filters)
        if not pools:
            return

        # Grab all disks at once to avoid querying every iteration
        disks = {i['devname']: i['identifier'] for i in self.middleware.call_sync('disk.query')}

        for pool in pools:
            if not pool['is_decrypted'] or pool['status'] == 'OFFLINE' or pool['encrypt'] == 0:
                continue

            provs = []
            for dev, disk in self.__get_dev_and_disk(pool['topology']):
                if not dev.endswith(".eli"):
                    continue
                prov = dev[:-4]
                diskid = disks.get(disk)
                ed = self.middleware.call_sync('datastore.query', 'storage.encrypteddisk', [
                    ('encrypted_provider', '=', prov)
                ])
                if not ed:
                    if not diskid:
                        self.logger.warn('Could not find Disk entry for %s', disk)
                    self.middleware.call_sync('datastore.insert', 'storage.encrypteddisk', {
                        'encrypted_volume': pool['id'],
                        'encrypted_provider': prov,
                        'encrypted_disk': diskid,
                    })
                elif diskid and ed[0]['encrypted_disk'] != diskid:
                    self.middleware.call_sync(
                        'datastore.update', 'storage.encrypteddisk', ed[0]['id'],
                        {'encrypted_disk': diskid},
                    )
                provs.append(prov)

            # Delete devices no longer in pool from database
            self.middleware.call_sync('datastore.delete', 'storage.encrypteddisk', [
                ('encrypted_volume', '=', pool['id']), ('encrypted_provider', 'nin', provs)
            ])

    def __dtrace_read(self, job, proc):
        while True:
            read = proc.stdout.readline()
            if read == b'':
                break
            read = read.decode(errors='ignore').strip()
            job.set_progress(None, read)

    @private
    @job()
    def import_on_boot(self, job):
        cachedir = os.path.dirname(ZPOOL_CACHE_FILE)
        if not os.path.exists(cachedir):
            os.mkdir(cachedir)

        if (
            not self.middleware.call_sync('system.is_freenas') and
            self.middleware.call('failover.licensed')
        ):
            return

        zpool_cache_saved = f'{ZPOOL_CACHE_FILE}.saved'
        if os.path.exists(ZPOOL_KILLCACHE):
            with contextlib.suppress(Exception):
                os.unlink(ZPOOL_CACHE_FILE)
            with contextlib.suppress(Exception):
                os.unlink(zpool_cache_saved)
        else:
            with open(ZPOOL_KILLCACHE, 'w') as f:
                os.fsync(f)

        try:
            stat = os.stat(ZPOOL_CACHE_FILE)
            if stat.st_size > 0:
                copy = False
                if not os.path.exists(zpool_cache_saved):
                    copy = True
                else:
                    statsaved = os.stat(zpool_cache_saved)
                    if stat.st_mtime > statsaved.st_mtime:
                        copy = True
                if copy:
                    shutil.copy(ZPOOL_CACHE_FILE, zpool_cache_saved)
        except FileNotFoundError:
            pass

        job.set_progress(0, 'Beginning pools import')

        try:
            proc = subprocess.Popen(
                ['dtrace', '-qn', 'zfs-dbgmsg{printf("%s\\n", stringof(arg0))}'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            start_daemon_thread(target=self.__dtrace_read, args=[job, proc])

            pools = self.middleware.call_sync('pool.query', [
                ('encrypt', '<', 2),
                ('status', '=', 'OFFLINE')
            ])
            for i, pool in enumerate(pools):
                # Importing pools is currently 80% of the job because we may still need
                # to set ACL mode for windows
                job.set_progress(int((i + 1) / len(pools) * 80), f'Importing {pool["name"]}')
                imported = False
                if pool['guid']:
                    try:
                        self.middleware.call_sync('zfs.pool.import_pool', pool['guid'], {
                            'altroot': '/mnt',
                            'cachefile': 'none',
                        }, True, zpool_cache_saved if os.path.exists(zpool_cache_saved) else None)
                    except Exception as e:
                        # If the pool exists but failed to import skip this one
                        if not isinstance(e, CallError) or e.errno != errno.ENOENT:
                            self.logger.error('Failed to import %s', pool['name'], exc_info=True)
                            continue
                    else:
                        imported = True
                if not imported:
                    try:
                        self.middleware.call_sync('zfs.pool.import_pool', pool['name'], {
                            'altroot': '/mnt',
                            'cachefile': 'none',
                        })
                    except Exception:
                        self.logger.error('Failed to import %s', pool['name'], exc_info=True)
                        continue

                try:
                    self.middleware.call_sync(
                        'zfs.pool.update', pool['name'], {'properties': {
                            'cachefile': {'value': ZPOOL_CACHE_FILE},
                        }}
                    )
                except Exception:
                    self.logger.warn(
                        'Failed to set cache file for %s', pool['name'], exc_info=True,
                    )

                try:
                    if os.path.isdir('/mnt/mnt'):
                        # Reset all mountpoints
                        self.middleware.call_sync(
                            'zfs.dataset.inherit', pool['name'], 'mountpoint', True
                        )
                except Exception:
                    self.logger.warn(
                        'Failed to inherit mountpoints for %s', pool['name'], exc_info=True,
                    )

        finally:
            proc.kill()
            proc.wait()

        with contextlib.suppress(OSError):
            os.unlink(ZPOOL_KILLCACHE)

        if os.path.exists(ZPOOL_CACHE_FILE):
            shutil.copy(ZPOOL_CACHE_FILE, zpool_cache_saved)

        job.set_progress(90, 'Ensuring correct ACL mode of datasets')

        # Use subprocess instead of zfs plugin for speed reasons
        cp = subprocess.run(
            'zfs list -t filesystem -H -o name,aclmode,mountpoint | '
            'awk \'$2 != "restricted" {print $0}\'',
            shell=True, capture_output=True, text=True, check=False,
        )
        for line in cp.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            dataset, aclmode, mountpoint = line.split('\t')
            if os.path.exists(f'{mountpoint}/.windows'):
                self.middleware.call_sync('zfs.dataset.update', dataset, {'properties': {
                    'aclmode': {'value': 'restricted'},
                }})

        job.set_progress(100, 'Pools import completed')

    """
    These methods are hacks for old UI which supports only one volume import at a time
    """

    dismissed_import_disk_jobs = set()

    @private
    async def get_current_import_disk_job(self):
        import_jobs = await self.middleware.call('core.get_jobs', [('method', '=', 'pool.import_disk')])
        not_dismissed_import_jobs = [job for job in import_jobs if job["id"] not in self.dismissed_import_disk_jobs]
        if not_dismissed_import_jobs:
            return not_dismissed_import_jobs[0]

    @private
    async def dismiss_current_import_disk_job(self):
        current_import_job = await self.get_current_import_disk_job()
        if current_import_job:
            self.dismissed_import_disk_jobs.add(current_import_job["id"])


class PoolDatasetService(CRUDService):

    class Config:
        namespace = 'pool.dataset'

    @filterable
    def query(self, filters=None, options=None):
        # Otimization for cases in which they can be filtered at zfs.dataset.query
        zfsfilters = []
        for f in filters or []:
            if len(f) == 3:
                if f[0] in ('id', 'name', 'pool', 'type'):
                    zfsfilters.append(f)
        datasets = self.middleware.call_sync('zfs.dataset.query', zfsfilters, None)
        return filter_list(self.__transform(datasets), filters, options)

    def __transform(self, datasets):
        """
        We need to transform the data zfs gives us to make it consistent/user-friendly,
        making it match whatever pool.dataset.{create,update} uses as input.
        """
        def transform(dataset):
            for orig_name, new_name, method in (
                ('org.freenas:description', 'comments', None),
                ('org.freenas:quota_warning', 'quota_warning', None),
                ('org.freenas:quota_critical', 'quota_critical', None),
                ('org.freenas:refquota_warning', 'refquota_warning', None),
                ('org.freenas:refquota_critical', 'refquota_critical', None),
                ('dedup', 'deduplication', str.upper),
                ('atime', None, str.upper),
                ('casesensitivity', None, str.upper),
                ('exec', None, str.upper),
                ('sync', None, str.upper),
                ('compression', None, str.upper),
                ('compressratio', None, None),
                ('origin', None, None),
                ('quota', None, _null),
                ('refquota', None, _null),
                ('reservation', None, _null),
                ('refreservation', None, _null),
                ('copies', None, None),
                ('snapdir', None, str.upper),
                ('readonly', None, str.upper),
                ('recordsize', None, None),
                ('sparse', None, None),
                ('volsize', None, None),
                ('volblocksize', None, None),
            ):
                if orig_name not in dataset['properties']:
                    continue
                i = new_name or orig_name
                dataset[i] = dataset['properties'][orig_name]
                if method:
                    dataset[i]['value'] = method(dataset[i]['value'])
            del dataset['properties']

            if dataset['type'] == 'FILESYSTEM':
                dataset['share_type'] = self.middleware.call_sync(
                    'notifier.get_dataset_share_type', dataset['name'],
                ).upper()
            else:
                dataset['share_type'] = None

            rv = []
            for child in dataset['children']:
                rv.append(transform(child))
            dataset['children'] = rv

            return dataset

        rv = []
        for dataset in datasets:
            rv.append(transform(dataset))
        return rv

    @accepts(Dict(
        'pool_dataset_create',
        Str('name', required=True),
        Str('type', enum=['FILESYSTEM', 'VOLUME'], default='FILESYSTEM'),
        Int('volsize'),  # IN BYTES
        Str('volblocksize', enum=[
            '512', '1K', '2K', '4K', '8K', '16K', '32K', '64K', '128K',
        ]),
        Bool('sparse'),
        Bool('force_size'),
        Str('comments'),
        Str('sync', enum=[
            'STANDARD', 'ALWAYS', 'DISABLED',
        ]),
        Str('compression', enum=[
            'OFF', 'LZ4', 'GZIP', 'GZIP-1', 'GZIP-9', 'ZLE', 'LZJB',
        ]),
        Str('atime', enum=['ON', 'OFF']),
        Str('exec', enum=['ON', 'OFF']),
        Int('quota', null=True),
        Int('quota_warning', validators=[Range(0, 100)]),
        Int('quota_critical', validators=[Range(0, 100)]),
        Int('refquota', null=True),
        Int('refquota_warning', validators=[Range(0, 100)]),
        Int('refquota_critical', validators=[Range(0, 100)]),
        Int('reservation'),
        Int('refreservation'),
        Int('copies'),
        Str('snapdir', enum=['VISIBLE', 'HIDDEN']),
        Str('deduplication', enum=['ON', 'VERIFY', 'OFF']),
        Str('readonly', enum=['ON', 'OFF']),
        Str('recordsize', enum=[
            '512', '1K', '2K', '4K', '8K', '16K', '32K', '64K', '128K', '256K', '512K', '1024K',
        ]),
        Str('casesensitivity', enum=['SENSITIVE', 'INSENSITIVE', 'MIXED']),
        Str('share_type', enum=['UNIX', 'WINDOWS', 'MAC']),
        register=True,
    ))
    async def do_create(self, data):
        """
        Creates a dataset/zvol.

        `volsize` is required for type=VOLUME and is supposed to be a multiple of the block size.
        `sparse` and `volblocksize` are only used for type=VOLUME.

        .. examples(websocket)::

          Create a dataset within tank pool.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.dataset.create,
                "params": [{
                    "name": "tank/myuser",
                    "comments": "Dataset for myuser"
                }]
            }
        """

        verrors = ValidationErrors()

        if '/' not in data['name']:
            verrors.add('pool_dataset_create.name', 'You need a full name, e.g. pool/newdataset')
        else:
            await self.__common_validation(verrors, 'pool_dataset_create', data, 'CREATE')

        mountpoint = os.path.join('/mnt', data['name'])
        if os.path.exists(mountpoint):
            verrors.add('pool_dataset_create.name', f'Path {mountpoint} already exists')

        if verrors:
            raise verrors

        props = {}
        for i, real_name, transform in (
            ('atime', None, str.lower),
            ('casesensitivity', None, str.lower),
            ('comments', 'org.freenas:description', None),
            ('compression', None, str.lower),
            ('copies', None, lambda x: str(x)),
            ('deduplication', 'dedup', str.lower),
            ('exec', None, str.lower),
            ('quota', None, _none),
            ('quota_warning', 'org.freenas:quota_warning', str),
            ('quota_critical', 'org.freenas:quota_critical', str),
            ('readonly', None, str.lower),
            ('recordsize', None, None),
            ('refquota', None, _none),
            ('refquota_warning', 'org.freenas:refquota_warning', str),
            ('refquota_critical', 'org.freenas:refquota_critical', str),
            ('refreservation', None, _none),
            ('reservation', None, _none),
            ('snapdir', None, str.lower),
            ('sparse', None, None),
            ('sync', None, str.lower),
            ('volblocksize', None, None),
            ('volsize', None, lambda x: str(x)),
        ):
            if i not in data:
                continue
            name = real_name or i
            props[name] = data[i] if not transform else transform(data[i])

        await self.middleware.call('zfs.dataset.create', {
            'name': data['name'],
            'type': data['type'],
            'properties': props,
        })

        data['id'] = data['name']

        await self.middleware.call('zfs.dataset.mount', data['name'])

        if data['type'] == 'FILESYSTEM':
            await self.middleware.call(
                'notifier.change_dataset_share_type', data['name'], data.get('share_type', 'UNIX').lower()
            )

        return await self._get_instance(data['id'])

    def _add_inherit(name):
        def add(attr):
            attr.enum.append('INHERIT')
        return {'name': name, 'method': add}

    @accepts(Str('id', required=True), Patch(
        'pool_dataset_create', 'pool_dataset_update',
        ('rm', {'name': 'name'}),
        ('rm', {'name': 'type'}),
        ('rm', {'name': 'casesensitivity'}),  # Its a readonly attribute
        ('rm', {'name': 'sparse'}),  # Create time only attribute
        ('rm', {'name': 'volblocksize'}),  # Create time only attribute
        ('edit', _add_inherit('atime')),
        ('edit', _add_inherit('exec')),
        ('edit', _add_inherit('sync')),
        ('edit', _add_inherit('compression')),
        ('edit', _add_inherit('deduplication')),
        ('edit', _add_inherit('readonly')),
        ('edit', _add_inherit('recordsize')),
        ('edit', _add_inherit('snapdir')),
        ('add', Inheritable('quota_warning', value=Int('quota_warning', validators=[Range(0, 100)]))),
        ('add', Inheritable('quota_critical', value=Int('quota_critical', validators=[Range(0, 100)]))),
        ('add', Inheritable('refquota_warning', value=Int('refquota_warning', validators=[Range(0, 100)]))),
        ('add', Inheritable('refquota_critical', value=Int('refquota_critical', validators=[Range(0, 100)]))),
        ('attr', {'update': True}),
    ))
    async def do_update(self, id, data):
        """
        Updates a dataset/zvol `id`.

        .. examples(websocket)::

          Update the `comments` for "tank/myuser".

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.dataset.update,
                "params": ["tank/myuser", {
                    "comments": "Dataset for myuser, UPDATE #1"
                }]
            }
        """

        verrors = ValidationErrors()

        dataset = await self.middleware.call('pool.dataset.query', [('id', '=', id)])
        if not dataset:
            verrors.add('id', f'{id} does not exist', errno.ENOENT)
        else:
            data['type'] = dataset[0]['type']
            data['name'] = dataset[0]['name']
            if data['type'] == 'VOLUME':
                data['volblocksize'] = dataset[0]['volblocksize']['value']
            await self.__common_validation(verrors, 'pool_dataset_update', data, 'UPDATE')
            if 'volsize' in data:
                if data['volsize'] < dataset[0]['volsize']['parsed']:
                    verrors.add('pool_dataset_update.volsize',
                                'You cannot shrink a zvol from GUI, this may lead to data loss.')
        if verrors:
            raise verrors

        props = {}
        for i, real_name, transform, inheritable in (
            ('atime', None, str.lower, True),
            ('comments', 'org.freenas:description', None, False),
            ('sync', None, str.lower, True),
            ('compression', None, str.lower, True),
            ('deduplication', 'dedup', str.lower, True),
            ('exec', None, str.lower, True),
            ('quota', None, _none, False),
            ('quota_warning', 'org.freenas:quota_warning', str, True),
            ('quota_critical', 'org.freenas:quota_critical', str, True),
            ('refquota', None, _none, False),
            ('refquota_warning', 'org.freenas:refquota_warning', str, True),
            ('refquota_critical', 'org.freenas:refquota_critical', str, True),
            ('reservation', None, _none, False),
            ('refreservation', None, _none, False),
            ('copies', None, None, False),
            ('snapdir', None, str.lower, True),
            ('readonly', None, str.lower, True),
            ('recordsize', None, None, True),
            ('volsize', None, lambda x: str(x), False),
        ):
            if i not in data:
                continue
            name = real_name or i
            if inheritable and data[i] == 'INHERIT':
                props[name] = {'source': 'INHERIT'}
            else:
                props[name] = {'value': data[i] if not transform else transform(data[i])}

        rv = await self.middleware.call('zfs.dataset.update', id, {'properties': props})

        if data['type'] == 'FILESYSTEM' and 'share_type' in data:
            await self.middleware.call(
                'notifier.change_dataset_share_type', id, data['share_type'].lower()
            )
        elif data['type'] == 'VOLUME' and 'volsize' in data:
            if await self.middleware.call('iscsi.extent.query', [('path', '=', f'zvol/{id}')]):
                await self._service_change('iscsitarget', 'reload')

        return rv

    async def __common_validation(self, verrors, schema, data, mode):
        assert mode in ('CREATE', 'UPDATE')

        parent = await self.middleware.call(
            'zfs.dataset.query',
            [('id', '=', data['name'].rsplit('/')[0])]
        )

        if not parent:
            verrors.add(
                f'{schema}.name',
                'Please specify a pool which exists for the dataset/volume to be created'
            )
        else:
            parent = parent[0]

        if data['type'] == 'FILESYSTEM':
            for i in ('force_size', 'sparse', 'volsize', 'volblocksize'):
                if i in data:
                    verrors.add(f'{schema}.{i}', 'This field is not valid for FILESYSTEM')
        elif data['type'] == 'VOLUME':
            if mode == 'CREATE' and 'volsize' not in data:
                verrors.add(f'{schema}.volsize', 'This field is required for VOLUME')

            for i in (
                'atime', 'casesensitivity', 'quota', 'refquota', 'recordsize', 'share_type',
            ):
                if i in data:
                    verrors.add(f'{schema}.{i}', 'This field is not valid for VOLUME')

            if 'volsize' in data and parent:

                avail_mem = int(parent['properties']['available']['rawvalue'])

                if mode == 'UPDATE':
                    avail_mem += int((await self.middleware.call(
                        'zfs.dataset.query',
                        [['id', '=', data['name']]]
                    ))[0]['properties']['used']['rawvalue'])

                if (
                    data['volsize'] > (avail_mem * 0.80) and
                    not data.get('force_size', False)
                ):
                    verrors.add(
                        f'{schema}.volsize',
                        'It is not recommended to use more than 80% of your available space for VOLUME'
                    )

                if 'volblocksize' in data:

                    if data['volblocksize'].isdigit():
                        block_size = int(data['volblocksize'])
                    else:
                        block_size = int(data['volblocksize'][:-1]) * 1024

                    if data['volsize'] % block_size:
                        verrors.add(
                            f'{schema}.volsize',
                            'Volume size should be a multiple of volume block size'
                        )

    @accepts(Str('id'), Dict(
        'dataset_delete',
        Bool('recursive', default=False),
        Bool('force', default=False),
    ))
    async def do_delete(self, id, options):
        """
        Delete dataset/zvol `id`.

        `recursive` will also delete/destroy all children datasets.
        `force` will force delete busy datasets.

        .. examples(websocket)::

          Delete "tank/myuser" dataset.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.dataset.delete",
                "params": ["tank/myuser"]
            }
        """
        iscsi_target_extents = await self.middleware.call('iscsi.extent.query', [
            ['type', '=', 'DISK'],
            ['path', '=', f'zvol/{id}']
        ])
        if iscsi_target_extents:
            raise CallError("This volume is in use by iSCSI extent, please remove it first.")

        return await self.middleware.call('zfs.dataset.delete', id, {
            'force': options['force'],
            'recursive': options['recursive'],
        })

    @item_method
    @accepts(Str('id'))
    async def promote(self, id):
        """
        Promote the cloned dataset `id`.
        """
        dataset = await self.middleware.call('zfs.dataset.query', [('id', '=', id)])
        if not dataset:
            raise CallError(f'Dataset "{id}" does not exist.', errno.ENOENT)
        if not dataset[0]['properties']['origin']['value']:
            raise CallError('Only cloned datasets can be promoted.', errno.EBADMSG)
        return await self.middleware.call('zfs.dataset.promote', id)

    @accepts(
        Str('id', required=True),
        Dict(
            'pool_dataset_permission',
            Str('user'),
            Str('group'),
            UnixPerm('mode'),
            Str('acl', enum=['UNIX', 'MAC', 'WINDOWS'], default='UNIX'),
            Bool('recursive', default=False),
        ),
    )
    @item_method
    async def permission(self, id, data):
        """
        Set permissions for a dataset `id`.

        .. examples(websocket)::

          Change permissions of dataset "tank/myuser" to myuser:wheel and 755.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.dataset.permission",
                "params": ["tank/myuser", {
                    "user": "myuser",
                    "group": "wheel",
                    "mode": "755",
                    "recursive": true,
                }]
            }
        """
        path = (await self._get_instance(id))['mountpoint']
        user = data.get('user', None)
        group = data.get('group', None)
        mode = data.get('mode', None)
        recursive = data.get('recursive', False)
        acl = data['acl']
        verrors = ValidationErrors()

        if (acl == 'UNIX' or acl == 'MAC') and mode is None:
            verrors.add('pool_dataset_permission.mode',
                        'This field is required')

        if verrors:
            raise verrors

        await self.middleware.call('notifier.mp_change_permission', path, user,
                                   group, mode, recursive, acl.lower())
        return data

    @accepts(Str('pool'))
    async def recommended_zvol_blocksize(self, pool):
        """
        Helper method to get recommended size for a new zvol (dataset of type VOLUME).

        .. examples(websocket)::

          Get blocksize for pool "tank".

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.dataset.recommended_zvol_blocksize",
                "params": ["tank"]
            }
        """
        pool = await self.middleware.call('pool.query', [['name', '=', pool]])
        if not pool:
            raise CallError('Pool not found.', errno.ENOENT)
        pool = pool[0]
        numdisks = 4
        for vdev in pool['topology']['data']:
            if vdev['type'] == 'RAIDZ1':
                num = len(vdev['children']) - 1
            elif vdev['type'] == 'RAIDZ2':
                num = len(vdev['children']) - 2
            elif vdev['type'] == 'RAIDZ3':
                num = len(vdev['children']) - 3
            elif vdev['type'] == 'MIRROR':
                num = 1
            else:
                num = len(vdev['children'])
            if num > numdisks:
                numdisks = num
        return '%dK' % 2 ** ((numdisks * 4) - 1).bit_length()


class PoolScrubService(CRUDService):

    class Config:
        datastore = 'storage.scrub'
        datastore_extend = 'pool.scrub.pool_scrub_extend'
        datastore_prefix = 'scrub_'
        namespace = 'pool.scrub'

    @private
    async def pool_scrub_extend(self, data):
        data['pool'] = data.pop('volume')
        data['pool'] = data['pool']['id']
        Cron.convert_db_format_to_schedule(data)
        return data

    @private
    async def validate_data(self, data, schema):
        verrors = ValidationErrors()

        pool_pk = data.get('pool')
        if pool_pk:
            pool_obj = await self.middleware.call(
                'datastore.query',
                'storage.volume',
                [('id', '=', pool_pk)]
            )

            if len(pool_obj) == 0:
                verrors.add(
                    f'{schema}.pool',
                    'The specified volume does not exist'
                )
            elif (
                    'id' not in data.keys() or
                    (
                        'id' in data.keys() and
                        'original_pool_id' in data.keys() and
                        pool_pk != data['original_pool_id']
                    )
            ):
                scrub_obj = await self.query(filters=[('pool', '=', pool_pk)])
                if len(scrub_obj) != 0:
                    verrors.add(
                        f'{schema}.pool',
                        'A scrub with this pool already exists'
                    )

        return verrors, data

    @accepts(
        Dict(
            'pool_scrub_create',
            Int('pool', validators=[Range(min=1)], required=True),
            Int('threshold', validators=[Range(min=0)]),
            Str('description'),
            Cron('schedule'),
            Bool('enabled', default=True),
            register=True
        )
    )
    async def do_create(self, data):
        """
        Create a scrub task for a pool.

        `threshold` refers to the minimum amount of time in days has to be passed before
        a scrub can run again.

        .. examples(websocket)::

          Create a scrub task for pool of id 1, to run every sunday but with a threshold of
          35 days.
          The check will run at 3AM every sunday.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "pool.scrub.create"
                "params": [{
                    "pool": 1,
                    "threshold": 35,
                    "description": "Monthly scrub for tank",
                    "schedule": "0 3 * * 7",
                    "enabled": true
                }]
            }
        """
        verrors, data = await self.validate_data(data, 'pool_scrub_create')

        if verrors:
            raise verrors

        data['volume'] = data.pop('pool')
        Cron.convert_schedule_to_db_format(data)

        data['id'] = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )

        await self.middleware.call('service.restart', 'cron')

        return await self.query(filters=[('id', '=', data['id'])], options={'get': True})

    @accepts(
        Int('id', validators=[Range(min=1)]),
        Patch('pool_scrub_create', 'pool_scrub_update', ('attr', {'update': True}))
    )
    async def do_update(self, id, data):
        """
        Update scrub task of `id`.
        """
        task_data = await self._get_instance(id)
        original_data = task_data.copy()
        task_data['original_pool_id'] = original_data['pool']
        task_data.update(data)
        verrors, task_data = await self.validate_data(task_data, 'pool_scrub_update')

        if verrors:
            raise verrors

        task_data.pop('original_pool_id')
        Cron.convert_schedule_to_db_format(task_data)
        Cron.convert_schedule_to_db_format(original_data)

        if len(set(task_data.items()) ^ set(original_data.items())) > 0:

            task_data['volume'] = task_data.pop('pool')

            await self.middleware.call(
                'datastore.update',
                self._config.datastore,
                id,
                task_data,
                {'prefix': self._config.datastore_prefix}
            )

            await self.middleware.call('service.restart', 'cron')

        return await self.query(filters=[('id', '=', id)], options={'get': True})

    @accepts(Int('id'))
    async def do_delete(self, id):
        """
        Delete scrub task of `id`.
        """
        response = await self.middleware.call(
            'datastore.delete',
            self._config.datastore,
            id
        )

        await self.middleware.call('service.restart', 'cron')
        return response


async def _event_zfs(middleware, event_type, args):
    data = args['data']
    if data.get('subsystem') != 'ZFS':
        return

    if data.get('type') in (
        'ATTACH',
        'DETACH',
        'resource.fs.zfs.removed',
        'misc.fs.zfs.config_sync',
    ):
        asyncio.ensure_future(middleware.call('pool.sync_encrypted'))


def setup(middleware):
    middleware.event_subscribe('devd.zfs', _event_zfs)
    asyncio.ensure_future(middleware.call('pool.configure_resilver_priority'))
