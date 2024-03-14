from middlewared.async_validators import check_path_resides_within_volume
from middlewared.schema import accepts, Bool, Dict, Int, Str
from middlewared.validators import Match, Range
from middlewared.service import SystemServiceService, ValidationErrors, private


import os


class S3Service(SystemServiceService):

    class Config:
        service = "s3"
        datastore_prefix = "s3_"
        datastore_extend = "s3.config_extend"

    @private
    async def config_extend(self, s3):
        s3['storage_path'] = s3.pop('disks', None)
        s3.pop('mode', None)
        if s3.get('certificate'):
            s3['certificate'] = s3['certificate']['id']
        return s3

    @accepts(Dict(
        's3_update',
        Str('bindip'),
        Int('bindport', validators=[Range(min=1, max=65535)]),
        Str('access_key', validators=[Match("^\w+$")]),
        Str('secret_key', validators=[Match("^\w+$")]),
        Bool('browser'),
        Str('storage_path'),
        Int('certificate', null=True),
        update=True,
    ))
    async def do_update(self, data):
        old = await self.config()

        new = old.copy()
        new.update(data)

        verrors = ValidationErrors()

        for attr, minlen, maxlen in (
            ('access_key', 5, 20),
            ('secret_key', 8, 40),
        ):
            curlen = len(new.get(attr, ''))
            if curlen < minlen or curlen > maxlen:
                verrors.add(
                    f's3_update.{attr}', f'Attribute should be {minlen} to {maxlen} in length'
                )

        if not new['storage_path']:
            verrors.add('s3_update.storage_path', 'Storage path is required')
        else:
            await check_path_resides_within_volume(
                verrors, self.middleware, 's3_update.storage_path', new['storage_path']
            )

            if not verrors:
                if new['storage_path'].rstrip('/').count('/') < 3:
                    verrors.add(
                        's3_update.storage_path',
                        'Top level datasets are not allowed. i.e /mnt/tank/dataset is allowed'
                    )
                else:
                    # If the storage_path does not exist, let's create it
                    if not os.path.exists(new['storage_path']):
                        os.makedirs(new['storage_path'])

        if new['certificate']:
            verrors.extend((await self.middleware.call(
                'certificate.cert_services_validation', new['certificate'], 's3_update.certificate', False
            )))

        if verrors:
            raise verrors

        new['disks'] = new.pop('storage_path')

        await self._update_service(old, new)

        if (await self.middleware.call('filesystem.stat', new['disks']))['user'] != 'minio':
            await self.middleware.call('notifier.winacl_reset', new['disks'], 'minio', 'minio')

        return await self.config()
