from middlewared.schema import Bool, Dict, File, Int, Patch, Str, ValidationErrors, accepts
from middlewared.service import CRUDService, job, private
from middlewared.utils import Popen

import asyncio
import os
import subprocess


class InitShutdownScriptService(CRUDService):

    class Config:
        datastore = 'tasks.initshutdown'
        datastore_prefix = 'ini_'
        datastore_extend = 'initshutdownscript.init_shutdown_script_extend'

    @accepts(Dict(
        'init_shutdown_script_create',
        Str('type', enum=['COMMAND', 'SCRIPT'], required=True),
        Str('command', null=True),
        File('script', null=True),
        Str('when', enum=['PREINIT', 'POSTINIT', 'SHUTDOWN'], required=True),
        Bool('enabled', default=True),
        Int('timeout', default=10),
        register=True,
    ))
    async def do_create(self, data):
        await self.validate(data, 'init_shutdown_script_create')

        await self.init_shutdown_script_compress(data)

        data['id'] = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )

        return await self._get_instance(data['id'])

    @accepts(Int('id'), Patch(
        'init_shutdown_script_create',
        'init_shutdown_script_update',
        ('attr', {'update': True}),
    ))
    async def do_update(self, id, data):
        old = await self._get_instance(id)
        new = old.copy()
        new.update(data)

        await self.validate(new, 'init_shutdown_script_update')

        await self.init_shutdown_script_compress(new)

        await self.middleware.call(
            'datastore.update',
            self._config.datastore,
            id,
            new,
            {'prefix': self._config.datastore_prefix}
        )

        return await self._get_instance(new['id'])

    @accepts(Int('id'))
    async def do_delete(self, id):
        return await self.middleware.call(
            'datastore.delete',
            self._config.datastore,
            id
        )

    @private
    async def init_shutdown_script_extend(self, data):
        data['type'] = data['type'].upper()
        data['when'] = data['when'].upper()

        return data

    @private
    async def init_shutdown_script_compress(self, data):
        data['type'] = data['type'].lower()
        data['when'] = data['when'].lower()

        return data

    @private
    async def validate(self, data, schema_name):
        verrors = ValidationErrors()

        if data['type'] == 'COMMAND':
            if not data.get('command'):
                verrors.add(f'{schema_name}.command', 'This field is required')
            else:
                data['script'] = ''

        if data['type'] == 'SCRIPT':
            if not data.get('script'):
                verrors.add(f'{schema_name}.script', 'This field is required')
            else:
                data['command'] = ''

        if verrors:
            raise verrors

    @private
    async def execute_task(self, task):
        task_type = task['type']
        cmd = None

        if task_type == 'COMMAND':
            cmd = task['command']
        elif os.path.exists(task['script'] or '') and os.access(task['script'], os.X_OK):
            cmd = f'exec {task["script"]}'

        if cmd:
            proc = await Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=True,
                close_fds=True
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode:
                self.middleware.logger.debug(
                    'Execution failed for '
                    f'{task_type} {task["command"] if task_type == "COMMAND" else task["script"]}: {stderr.decode()}'
                )

    @private
    @accepts(
        Str('when')
    )
    @job()
    async def execute_init_tasks(self, job, when):

        tasks = await self.middleware.call(
            'initshutdownscript.query', [
                ['enabled', '=', True],
                ['when', '=', when]
            ])

        for i, task in enumerate(tasks):
            try:
                await asyncio.wait_for(self.execute_task(task), timeout=task['timeout'])
            except asyncio.TimeoutError:
                self.middleware.logger.debug(
                    f'{task["type"]} {task["command"] if task["type"] == "COMMAND" else task["script"]} timed out'
                )
            finally:
                job.set_progress((100 / len(tasks)) * (i + 1))

        job.set_progress(100, f'Completed tasks for {when}')
