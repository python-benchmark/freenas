import asyncio
import contextlib
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

from datetime import datetime

from middlewared.schema import Bool, Dict, accepts
from middlewared.service import CallError, Service, job, private

FREENAS_DATABASE = '/data/freenas-v1.db'
NEED_UPDATE_SENTINEL = '/data/need-update'
RE_CONFIG_BACKUP = re.compile(r'.*(\d{4}-\d{2}-\d{2})-(\d+)\.db$')


class ConfigService(Service):

    @accepts(Dict(
        'configsave',
        Bool('secretseed', default=False),
    ))
    @job(pipes=["output"])
    async def save(self, job, options=None):
        """
        Provide configuration file.

        secretseed - will include the password secret seed in the bundle.
        """
        if options is None:
            options = {}

        if not options.get('secretseed'):
            bundle = False
            filename = FREENAS_DATABASE
        else:
            bundle = True
            filename = tempfile.mkstemp()[1]
            os.chmod(filename, 0o600)
            with tarfile.open(filename, 'w') as tar:
                tar.add(FREENAS_DATABASE, arcname='freenas-v1.db')
                tar.add('/data/pwenc_secret', arcname='pwenc_secret')

        with open(filename, 'rb') as f:
            await self.middleware.run_in_thread(shutil.copyfileobj, f, job.pipes.output.w)

        if bundle:
            os.remove(filename)

    @accepts()
    @job(pipes=["input"])
    async def upload(self, job):
        """
        Accepts a configuration file via job pipe.
        """
        filename = tempfile.mktemp(dir='/var/tmp/firmware')

        def read_write():
            nreads = 0
            with open(filename, 'wb') as f_tmp:
                while True:
                    read = job.pipes.input.r.read(1024)
                    if read == b'':
                        break
                    f_tmp.write(read)
                    nreads += 1
                    if nreads > 10240:
                        # FIXME: transfer to a file on disk
                        raise ValueError('File is bigger than 10MiB')
        try:
            await self.middleware.run_in_thread(read_write)
            await self.middleware.run_in_thread(self.__upload, filename)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(filename)
        asyncio.ensure_future(self.middleware.call('system.reboot', {'delay': 10}))

    def __upload(self, config_file_name):
        try:
            """
            First we try to open the file as a tar file.
            We expect the tar file to contain at least the freenas-v1.db.
            It can also contain the pwenc_secret file.
            If we cannot open it as a tar, we try to proceed as it was the
            raw database file.
            """
            try:
                with tarfile.open(config_file_name) as tar:
                    bundle = True
                    tmpdir = tempfile.mkdtemp(dir='/var/tmp/firmware')
                    tar.extractall(path=tmpdir)
                    config_file_name = os.path.join(tmpdir, 'freenas-v1.db')
            except tarfile.ReadError:
                bundle = False
            # Currently we compare only the number of migrations for south and django
            # of new and current installed database.
            # This is not bullet proof as we can eventually have more migrations in a stable
            # release compared to a older nightly and still be considered a downgrade, however
            # this is simple enough and works in most cases.
            conn = sqlite3.connect(config_file_name)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM south_migrationhistory WHERE app_name != 'freeadmin'"
                )
                new_numsouth = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM django_migrations WHERE app != 'freeadmin'"
                )
                new_num = cur.fetchone()[0]
                cur.close()
            finally:
                conn.close()
            conn = sqlite3.connect(FREENAS_DATABASE)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM south_migrationhistory WHERE app_name != 'freeadmin'"
                )
                numsouth = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM django_migrations WHERE app != 'freeadmin'"
                )
                num = cur.fetchone()[0]
                cur.close()
            finally:
                conn.close()
                if new_numsouth > numsouth or new_num > num:
                    raise CallError(
                        'Failed to upload config, version newer than the '
                        'current installed.'
                    )
        except Exception:
            os.unlink(config_file_name)
            raise CallError('The uploaded file is not valid.')

        shutil.move(config_file_name, '/data/uploaded.db')
        if bundle:
            secret = os.path.join(tmpdir, 'pwenc_secret')
            if os.path.exists(secret):
                shutil.move(secret, self.middleware.call_sync('pwenc.file_secret_path'))

        # Now we must run the migrate operation in the case the db is older
        open(NEED_UPDATE_SENTINEL, 'w+').close()

    @accepts(Dict('options', Bool('reboot', default=True)))
    @job(lock='config_reset', logs=True)
    def reset(self, job, options):
        """
        Reset database to configuration defaults.

        If `reboot` is true this job will reboot the system after its completed with a delay of 10
        seconds.
        """
        factorydb = f'{FREENAS_DATABASE}.factory'
        if os.path.exists(factorydb):
            os.unlink(factorydb)
        cp = subprocess.run(
            ['migrate93', '-f', factorydb],
            capture_output=True,
        )
        if cp.returncode != 0:
            job.logs_fd.write(cp.stderr)
            raise CallError('Factory reset has failed.')
        cp = subprocess.run(
            [
                'python', '/usr/local/www/freenasUI/manage.py', 'migrate', '--noinput',
                '--fake-initial',
            ],
            capture_output=True,
            env={'FREENAS_FACTORY': '1'},
        )
        if cp.returncode != 0:
            job.logs_fd.write(cp.stderr)
            raise CallError('Factory reset has failed.')

        shutil.move(factorydb, FREENAS_DATABASE)

        if options['reboot']:
            self.middleware.run_coroutine(
                self.middleware.call('system.reboot', {'delay': 10}), wait=False,
            )

    @private
    def backup(self):
        systemdataset = self.middleware.call_sync('systemdataset.config')
        if not systemdataset or not systemdataset['path']:
            return

        # Legacy format
        for f in glob.glob(f'{systemdataset["path"]}/*.db'):
            if not RE_CONFIG_BACKUP.match(f):
                continue
            try:
                os.unlink(f)
            except OSError:
                pass

        today = datetime.now().strftime("%Y%m%d")

        newfile = os.path.join(
            systemdataset["path"],
            f'configs-{systemdataset["uuid"]}',
            self.middleware.call_sync('system.version'),
            f'{today}.db',
        )

        dirname = os.path.dirname(newfile)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        shutil.copy(FREENAS_DATABASE, newfile)
