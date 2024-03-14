=========
Storage
=========

Resources related to storage.

Volume
------

The Volume resource represents ZFS pools and UFS volumes.

List resource
+++++++++++++

.. http:get:: /api/v1.0/storage/volume/

   Returns a list of all interfaces.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/volume/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      [
        {
                "status": "HEALTHY",
                "vol_guid": "8443409799014097611",
                "used": "192.0 KiB (0%)",
                "name": "tank",
                "used_pct": "0%",
                "used_si": "192.0 KiB",
                "id": 1,
                "vol_encryptkey": "",
                "vol_name": "tank",
                "is_decrypted": true,
                "avail_si": "4.9 GiB",
                "mountpoint": "/mnt/tank",
                "vol_encrypt": 0,
                "children": [],
                "total_si": "4.9 GiB"
        }
      ]

   :query offset: offset number. default is 0
   :query limit: limit number. default is 20
   :resheader Content-Type: content type of the response
   :statuscode 200: no error


Create resource
+++++++++++++++

.. http:post:: /api/v1.0/storage/volume/

   Creates a new volume and returns the new volume object.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/ HTTP/1.1
      Content-Type: application/json

        {
                "volume_name": "tank",
                "layout": [
                        {
                                "vdevtype": "stripe",
                                "disks": ["ada1", "ada2"]
                        }
                ]
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 201 Created
      Vary: Accept
      Content-Type: application/json

        {
                "status": "HEALTHY",
                "vol_guid": "8443409799014097611",
                "used": "192.0 KiB (0%)",
                "name": "tank",
                "used_pct": "0%",
                "used_si": "192.0 KiB",
                "id": 1,
                "vol_encryptkey": "",
                "vol_name": "tank",
                "is_decrypted": true,
                "avail_si": "4.9 GiB",
                "mountpoint": "/mnt/tank",
                "vol_encrypt": 0,
                "children": [],
                "total_si": "4.9 GiB"
        }

   :json string volume_name: name of the new volume
   :json string volume_add: name of the volume to extend
   :json boolean encryption: encrypted volume or not
   :json boolean encryption_inirand: initialize disks with random data (slow)
   :json list layout: list of vdevs composed of "vdevtype" (stripe, mirror, raidz, raidz2, raidz3) and disks (list of disk names)
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 201: no error


Delete resource
+++++++++++++++

.. http:delete:: /api/v1.0/storage/volume/(int:id)/

   Delete volume `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/volume/1/ HTTP/1.1
      Content-Type: application/json

        {
                "destroy": true,
                "cascade": true,
        }


   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :json boolean destroy: destroy the volume
   :json boolean cascade: destroy the shares related to the volume
   :statuscode 204: no error


Datasets
++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/datasets/

   Create dataset for volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/datasets/ HTTP/1.1
      Content-Type: application/json

      {
        "name": "myds"
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      {
        "atime": "on",
        "avail": 3848491008,
        "comments": "Test",
        "compression": "lz4",
        "dedup": "off",
        "inherit_props": [
          "compression",
          "aclinherit"
        ],
        "mountpoint": "/mnt/tank/myds",
        "name": "tank/myds",
        "pool": "tank",
        "quota": 0,
        "readonly": "off",
        "recordsize": 131072,
        "refer": 90112,
        "refquota": 0,
        "refreservation": 0,
        "reservation": 0,
        "used": 90112
      }


   :json string name: name of the dataset
   :json string comments: user comments for the dataset
   :json string compression: compression level (lz4, gzip-[1-9], zle, lzjb)
   :json string dedup: dedup (on, off, inherit)
   :json string atime: access time (on, off, inherit)
   :json string readonly: read only (on, off, inherit)
   :json string recordsize: recodsize (512, 1K, 2K, 4K, 8k, 16K, 32K, 64K, 128K, 256K, 512K, 1024K)
   :json string case_sensitivity: files case sensitivity (on, off, inherit)
   :json string quota: quota for this dataset and all children
   :json string refquota: quota for this dataset
   :json string reservation: reserved space for this dataset and all children
   :json string refreservation: reserved space for this dataset
   :json string readonly: read only (on, off, inherit)
   :resheader Content-Type: content type of the response
   :statuscode 201: no error

.. http:put:: /api/v1.0/storage/volume/(int:id|string:name)/datasets/myds/

   Create dataset for volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/datasets/myds/ HTTP/1.1
      Content-Type: application/json

      {
        "comments": "Test DS"
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      {
        "atime": "on",
        "avail": 3848491008,
        "comments": "Test DS",
        "compression": "lz4",
        "dedup": "off",
        "inherit_props": [
          "compression",
          "aclinherit"
        ],
        "mountpoint": "/mnt/tank/myds",
        "name": "tank/myds",
        "pool": "tank",
        "quota": 0,
        "readonly": "off",
        "recordsize": 131072,
        "refer": 90112,
        "refquota": 0,
        "refreservation": 0,
        "reservation": 0,
        "used": 90112
      }


   :json string name: name of the dataset
   :json string comments: user comments for the dataset
   :json string compression: compression level (lz4, gzip-[1-9], zle, lzjb)
   :json string dedup: dedup (on, off, inherit)
   :json string atime: access time (on, off, inherit)
   :json string readonly: read only (on, off, inherit)
   :json string recordsize: recodsize (512, 1K, 2K, 4K, 8k, 16K, 32K, 64K, 128K, 256K, 512K, 1024K)
   :json string case_sensitivity: files case sensitivity (on, off, inherit)
   :json string quota: quota for this dataset and all children
   :json string refquota: quota for this dataset
   :json string reservation: reserved space for this dataset and all children
   :json string refreservation: reserved space for this dataset
   :json string readonly: read only (on, off, inherit)
   :resheader Content-Type: content type of the response
   :statuscode 201: no error

.. http:get:: /api/v1.0/storage/volume/(int:id|string:name)/datasets/

   Get datasets for volume `id`.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/volume/tank/datasets/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      [
        {
          "atime": "on",
          "avail": 3850371072,
          "comments": "Test DS",
          "compression": "lz4",
          "dedup": "off",
          "inherit_props": [
            "compression",
            "aclinherit"
          ],
          "mountpoint": "/mnt/tank/myds",
          "name": "tank/myds",
          "pool": "tank",
          "quota": 0,
          "readonly": "off",
          "recordsize": 131072,
          "refer": 90112,
          "refquota": 0,
          "refreservation": 0,
          "reservation": 0,
          "used": 90112
        }
      ]

   :resheader Content-Type: content type of the response
   :statuscode 200: no error

.. http:delete:: /api/v1.0/storage/volume/(int:id|string:name)/datasets/(string:dsname)/

   Delete dataset `dsname` of the volume `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/volume/tank/datasets/test/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :resheader Content-Type: content type of the response
   :statuscode 204: no error

Import
++++++

.. http:get:: /api/v1.0/storage/volume_import/

   Get list of importable volumes.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/volume_import/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      [
          {
              "disks": {
                  "status": "ONLINE",
                  "numVdevs": 1,
                  "name": "test",
                  "vdevs": [
                      {
                          "status": "ONLINE",
                          "disks": [
                              {
                                  "status": "ONLINE",
                                  "name": "ada2p1"
                              }
                          ],
                          "type": "stripe",
                          "name": "stripe",
                          "numDisks": 1
                      }
                  ]
              },
              "log": null,
              "cache": null,
              "label": "test",
              "spare": null,
              "type": "zfs",
              "id": "test|15955869083029286480",
              "group_type": "none"
          }
      ]


   :resheader Content-Type: content type of the response
   :statuscode 200: no error

.. http:post:: /api/v1.0/storage/volume_import/

   Import volume.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume_import/ HTTP/1.1
      Content-Type: application/json

      {
           "volume_id": "test|15955869083029286480"
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      "Volume imported."

   :resheader Content-Type: content type of the response
   :statuscode 202: no error


ZFS Volumes
+++++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/zvols/

   Create zvol for volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/zvols/ HTTP/1.1
      Content-Type: application/json

      {
        "comments": "FreeNAS ZVOL",
        "name": "fnzvol",
        "volsize": "10M",
        "compression": "gzip-9",
        "sparse": true,
        "force": true,
        "blocksize": "4K"
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      {
        "avail": 4059471872,
        "comments": "FreeNAS ZVOL",
        "compression": "gzip-9",
        "dedup": "off",
        "name": "fnzvol",
        "refer": 57344,
        "used": 57344,
        "volsize": 10485760
      }

   :resheader Content-Type: content type of the response
   :statuscode 201: no error

.. http:get:: /api/v1.0/storage/volume/(int:id|string:name)/zvols/

   Get zvols for volume `id`.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/volume/tank/zvols/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      [{
        "name": "fnzvol",
        "comments": "FreeNAS ZVOL",
        "avail": 7286996992,
        "compression": "gzip-9",
        "dedup": "off",
        "refer": 57344,
        "used": 57344,
        "volsize": 10485760
      }]

   :resheader Content-Type: content type of the response
   :statuscode 200: no error

.. http:put:: /api/v1.0/storage/volume/(int:id|string:name)/zvols/(string:name)/

   Update zvol `name` for volume `id`.

   **Example request**:

   .. sourcecode:: http

      PUT /api/v1.0/storage/volume/tank/zvols/fnzvol/ HTTP/1.1
      Content-Type: application/json

      {
        "volsize": "20M"
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      {
        "name": "fnzvol",
        "comments": "FreeNAS ZVOL",
        "avail": 7286996992,
        "compression": "gzip-9",
        "dedup": "off",
        "refer": 57344,
        "used": 57344,
        "volsize": 20971520
      }

   :json string compression: type of compression
   :json string dedup: on/off
   :json string volsize: size of the zvol
   :resheader Content-Type: content type of the response
   :statuscode 201: no error

.. http:get:: /api/v1.0/storage/volume/(int:id|string:name)/zvols/

   Get zvols for volume `id`.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/volume/tank/zvols/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      [{
        "name": "myzvol",
        "volsize": 10485760
      }]

   :resheader Content-Type: content type of the response
   :statuscode 200: no error

.. http:delete:: /api/v1.0/storage/volume/(int:id|string:name)/zvols/(string:name)/

   Delete zvol `name` of the volume `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/volume/tank/zvols/myzvol/ HTTP/1.1
      Content-Type: application/json

      {
        "cascade": true
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :json boolean cascade: Delete Recursively ( optional - it defaults to false )
   :resheader Content-Type: content type of the response
   :statuscode 204: no error


Scrub
+++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/scrub/

   Start scrub for volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/scrub/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Volume scrub started.

   :resheader Content-Type: content type of the response
   :statuscode 202: no error

.. http:delete:: /api/v1.0/storage/volume/(int:id|string:name)/scrub/

   Stop scrub for volume `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/volume/tank/scrub/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Volume scrub stopped.

   :resheader Content-Type: content type of the response
   :statuscode 202: no error


Upgrade
+++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/upgrade/

   Upgrade version of volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/upgrade/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Volume has been upgraded.

   :resheader Content-Type: content type of the response
   :statuscode 202: no error


Replace disk
+++++++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/replace/

   Replace a disk of volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/replace/ HTTP/1.1
      Content-Type: application/json

        {
                "label": "gptid/7c4dd4f1-1a1f-11e3-9786-080027c5e4f4",
                "replace_disk": "ada4",
                "force": true,
                "pass": "abcd"
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Disk replacement started.

   :json string label: zfs label of the device
   :json string replace_disk: name of the new disk
   :json string pass: passphrase for a protected pool ( optional - as required )
   :json bool force: force replacement of the new disk
   :resheader Content-Type: content type of the response
   :statuscode 200: no error


Offline disk
++++++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/offline/

   Offline a disk of volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/offline/ HTTP/1.1
      Content-Type: application/json

        {
                "label": "gptid/7c4dd4f1-1a1f-11e3-9786-080027c5e4f4"
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Disk offline'd.

   :json string label: zfs label or guid of the device
   :resheader Content-Type: content type of the response
   :statuscode 202: no error


Online disk
+++++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/online/

   Online a disk of volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/online/ HTTP/1.1
      Content-Type: application/json

        {
                "label": "gptid/7c4dd4f1-1a1f-11e3-9786-080027c5e4f4"
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Disk online'd.

   :json string label: zfs label or guid of the device
   :resheader Content-Type: content type of the response
   :statuscode 202: no error


Detach disk
+++++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/detach/

   Detach a disk of volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/detach/ HTTP/1.1
      Content-Type: application/json

        {
                "label": "gptid/7c4dd4f1-1a1f-11e3-9786-080027c5e4f4",
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Disk detached.

   :json string label: zfs label of the device
   :resheader Content-Type: content type of the response
   :statuscode 202: no error


Remove disk
+++++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/remove/

   Remove a disk of volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/remove/ HTTP/1.1
      Content-Type: application/json

        {
                "label": "gptid/7c4dd4f1-1a1f-11e3-9786-080027c5e4f4",
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Disk removed.

   :json string label: zfs label of the device
   :resheader Content-Type: content type of the response
   :statuscode 202: no error


Unlock
++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/unlock/

   Unlock encrypted volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/unlock/ HTTP/1.1
      Content-Type: application/json

        {
                "passphrase": "mypassphrase",
                "recovery_key": "Ik9mXRNr2zj9CZF8kFoC9w7sUyQxo5kAAgWjeVtNDfTgFm0tERqS90OdJYzCaCr75V9LnKgLlRBmzJ0oj7F3nw==",
                "services": ["iscsitarget", "webdav"]
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Volume has been unlocked.

   :json string passphrase: passphrase to unlock the volume
   :json string recovery_key: recovery key in base64 encoded string to unlock the volume ( Either passphrase
                              or recovery key are needed to unlock the volume )
   :json list services: services to restart when unlocking a volume is complete. Valid options are: 'afp', 'cifs', 'ftp', 'iscsitarget', 'jails', 'nfs', 'webdav'
   :resheader Content-Type: content type of the response
   :statuscode 202: no error


Lock
++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/lock/

   Lock encrypted volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/lock/ HTTP/1.1
      Content-Type: application/json

        {
                "passphrase": "mypassphrase"
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      Volume has been locked.

   :json string passphrase: passphrase to lock the volume
   :resheader Content-Type: content type of the response
   :statuscode 202: no error


Recovery Key
++++++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/recoverykey/

   Add a recovery key for volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/recoverykey/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

        {
                "message": "New recovery key has been added.",
                "content": "YWRhc2RzYWRhc2RzYWQ="
        }

   :resheader Content-Type: content type of the response
   :statuscode 202: no error

.. http:delete:: /api/v1.0/storage/volume/(int:id|string:name)/recoverykey/

   Remove a recovery key for volume `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/volume/tank/recoverykey/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :resheader Content-Type: content type of the response
   :statuscode 204: no error


Re-Key
++++++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/rekey/

   Re-key volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/rekey/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

        Volume has been rekeyed.

   :resheader Content-Type: content type of the response
   :statuscode 202: no error


Key Passphrase
++++++++++++

.. http:post:: /api/v1.0/storage/volume/(int:id|string:name)/keypassphrase/

   Create key passphrase for volume `id`.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/volume/tank/keypassphrase/ HTTP/1.1
      Content-Type: application/json

      {
        "passphrase": "mypassphrase",
        "passphrase2": "mypassphrase",
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 201 Accepted
      Vary: Accept
      Content-Type: application/json

        Volume passphrase has been set

   :resheader Content-Type: content type of the response
   :statuscode 201: no error

.. http:put:: /api/v1.0/storage/volume/(int:id|string:name)/keypassphrase/

   Change key passphrase for volume `id`.

   **Example request**:

   .. sourcecode:: http

      PUT /api/v1.0/storage/volume/tank/keypassphrase/ HTTP/1.1
      Content-Type: application/json

      {
        "adminpw": "freenas",
        "passphrase": "mypassphrase",
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 201 Accepted
      Vary: Accept
      Content-Type: application/json

        Volume passphrase has been set

   :resheader Content-Type: content type of the response
   :statuscode 201: no error

   **Example request**:

   .. sourcecode:: http

      PUT /api/v1.0/storage/volume/tank/keypassphrase/ HTTP/1.1
      Content-Type: application/json

      {
        "adminpw": "freenas",
        "remove": True,
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 201 Accepted
      Vary: Accept
      Content-Type: application/json

        Volume passphrase has been removed

   :resheader Content-Type: content type of the response
   :statuscode 201: no error

Status
++++++

.. http:get:: /api/v1.0/storage/volume/(int:id|string:name)/status/

   Get status of volume `id`.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/volume/tank/status/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

        {
                "status": "ONLINE",
                "name": "tank",
                "read": "0",
                "id": 1,
                "write": "0",
                "cksum": "0",
                "pk": "tank",
                "type": "root",
                "children": [{
                        "status": "ONLINE",
                        "name": "raidz1-0",
                        "read": "0",
                        "id": 100,
                        "write": "0",
                        "cksum": "0",
                        "type": "vdev",
                        "children": [{
                                "status": "ONLINE",
                                "name": "ada3p2",
                                "read": "0",
                                "label": "gptid/7cc54b3a-1a1f-11e3-9786-080027c5e4f4",
                                "write": "0",
                                "cksum": "0",
                                "id": 101,
                                "type": "dev",
                        },
                        {
                                "status": "ONLINE",
                                "name": "ada2p2",
                                "read": "0",
                                "label": "gptid/7c8bb013-1a1f-11e3-9786-080027c5e4f4",
                                "write": "0",
                                "cksum": "0",
                                "id": 102,
                                "type": "dev",
                        },
                        {
                                "status": "ONLINE",
                                "name": "ada1p2",
                                "read": "0",
                                "label": "gptid/7c4dd4f1-1a1f-11e3-9786-080027c5e4f4",
                                "write": "0",
                                "cksum": "0",
                                "id": 103,
                                "type": "dev",
                        }]
                }]
        }


   :resheader Content-Type: content type of the response
   :statuscode 200: no error


Dataset
-------

The Dataset resource represents ZFS datasets.

List resource
+++++++++++++

.. http:get:: /api/v1.0/storage/dataset/

   Returns a list of all datasets.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/dataset/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      [
        {
          "atime": "on",
          "avail": 3850321920,
          "comments": "Test",
          "compression": "lz4",
          "dedup": "off",
          "inherit_props": [],
          "mountpoint": "/mnt/tank",
          "name": "tank",
          "pool": "tank",
          "quota": 0,
          "readonly": "off",
          "recordsize": 131072,
          "refer": 90112,
          "refquota": 0,
          "refreservation": 0,
          "reservation": 0,
          "used": 2358296576
        },
        {
          "atime": "on",
          "avail": 3850715136,
          "comments": null,
          "compression": "lz4",
          "dedup": "off",
          "inherit_props": [
            "compression",
            "aclinherit"
          ],
          "mountpoint": "/mnt/tank/myds",
          "name": "tank/myds",
          "pool": "tank",
          "quota": 0,
          "readonly": "off",
          "recordsize": 131072,
          "refer": 90112,
          "refquota": 0,
          "refreservation": 0,
          "reservation": 0,
          "used": 90112
        }
      ]


   :query offset: offset number. default is 0
   :query limit: limit number. default is 20
   :resheader Content-Type: content type of the response
   :statuscode 200: no error


Create resource
+++++++++++++++

.. http:post:: /api/v1.0/storage/dataset/(string:parent)/

   Creates a dataset and returns the new object.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/dataset/tank/ HTTP/1.1
      Content-Type: application/json

      {
        "name": "myds",
        "comment": "Test",
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 201 Created
      Vary: Accept
      Content-Type: application/json

      {
        "atime": "on",
        "avail": 3848491008,
        "comments": "Test",
        "compression": "lz4",
        "dedup": "off",
        "inherit_props": [
          "compression",
          "aclinherit"
        ],
        "mountpoint": "/mnt/tank/myds",
        "name": "tank/myds",
        "pool": "tank",
        "quota": 0,
        "readonly": "off",
        "recordsize": 131072,
        "refer": 90112,
        "refquota": 0,
        "refreservation": 0,
        "reservation": 0,
        "used": 90112
      }


   :json string name: name of the dataset
   :json string comments: user comments for the dataset
   :json string compression: compression level (lz4, gzip-[1-9], zle, lzjb)
   :json string dedup: dedup (on, off, inherit)
   :json string atime: access time (on, off, inherit)
   :json string readonly: read only (on, off, inherit)
   :json string recordsize: recodsize (512, 1K, 2K, 4K, 8k, 16K, 32K, 64K, 128K, 256K, 512K, 1024K)
   :json string case_sensitivity: files case sensitivity (on, off, inherit)
   :json string quota: quota for this dataset and all children
   :json string refquota: quota for this dataset
   :json string reservation: reserved space for this dataset and all children
   :json string refreservation: reserved space for this dataset
   :json string readonly: read only (on, off, inherit)
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 201: no error


Update resource
+++++++++++++++

.. http:post:: /api/v1.0/storage/dataset/(string:parent)/

   Updates a dataset and returns the object.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/dataset/tank/myds/ HTTP/1.1
      Content-Type: application/json

      {
        "comment": "Test DS",
      }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

      {
        "atime": "on",
        "avail": 3848491008,
        "comments": "Test DS",
        "compression": "lz4",
        "dedup": "off",
        "inherit_props": [
          "compression",
          "aclinherit"
        ],
        "mountpoint": "/mnt/tank/myds",
        "name": "tank/myds",
        "pool": "tank",
        "quota": 0,
        "readonly": "off",
        "recordsize": 131072,
        "refer": 90112,
        "refquota": 0,
        "refreservation": 0,
        "reservation": 0,
        "used": 90112
      }


   :json string name: name of the dataset
   :json string comments: user comments for the dataset
   :json string compression: compression level (lz4, gzip-[1-9], zle, lzjb)
   :json string dedup: dedup (on, off, inherit)
   :json string atime: access time (on, off, inherit)
   :json string readonly: read only (on, off, inherit)
   :json string recordsize: recodsize (512, 1K, 2K, 4K, 8k, 16K, 32K, 64K, 128K, 256K, 512K, 1024K)
   :json string case_sensitivity: files case sensitivity (on, off, inherit)
   :json string quota: quota for this dataset and all children
   :json string refquota: quota for this dataset
   :json string reservation: reserved space for this dataset and all children
   :json string refreservation: reserved space for this dataset
   :json string readonly: read only (on, off, inherit)
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 202: no error


Delete resource
+++++++++++++++

.. http:delete:: /api/v1.0/storage/dataset/(string:name)/

   Delete dataset `name`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/dataset/tank/myds/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :resheader Content-Type: content type of the response
   :statuscode 204: no error


Snapshot
----------

The Snapshot resource represents ZFS snapshots.

List resource
+++++++++++++

.. http:get:: /api/v1.0/storage/snapshot/

   Returns a list of all snapshots.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/snapshot/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      [
        {
        "filesystem": "tank/jails/.warden-template-pluginjail-9.2-RELEASE-x64",
        "fullname": "tank/jails/.warden-template-pluginjail-9.2-RELEASE-x64@clean",
        "id": "tank/jails/.warden-template-pluginjail-9.2-RELEASE-x64@clean",
        "mostrecent": true,
        "name": "clean",
        "parent_type": "filesystem",
        "refer": "482M",
        "used": "107K"
        }
      ]

   :query offset: offset number. default is 0
   :query limit: limit number. default is 20
   :resheader Content-Type: content type of the response
   :statuscode 200: no error


Create resource
+++++++++++++++

.. http:post:: /api/v1.0/storage/snapshot/

   Creates a new snapshot and returns the new snapshot object.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/snapshot/ HTTP/1.1
      Content-Type: application/json

        {
                "dataset": "tank",
                "name": "test",
                "recursive": true,
                "vmware_sync": true
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 201 Created
      Vary: Accept
      Content-Type: application/json

        {
                "filesystem": "tank",
                "fullname": "tank@test",
                "id": "tank@test",
                "mostrecent": true,
                "name": "test",
                "parent_type": "filesystem",
                "refer": "298K",
                "used": "0"
        }

   :json string dataset: name of dataset to snapshot
   :json string name: name of the snapshot
   :json boolean recursive: True if you want it to recursively snapshot the dataset
   :json boolean vmware_sync: True if you want to coordinate snapshots when using FreeNAS® as a VMware datastore
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 201: no error


Delete resource
+++++++++++++++

.. http:delete:: /api/v1.0/storage/snapshot/(string:id)/

   Delete snapshot `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/snapshot/tank@test/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :statuscode 204: no error


Clone snapshot
++++++++++++++

.. http:post:: /api/v1.0/storage/snapshot/tank%40test/clone/

   Creates a clone from a snapshot.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/snapshot/tank%40test/clone/ HTTP/1.1
      Content-Type: application/json

        {
                "name": "tank/testclone"
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

        Snapshot cloned.

   :json string name: name/path of the clone
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 202: no error


Rollback snapshot
+++++++++++++++++

.. http:post:: /api/v1.0/storage/snapshot/tank%40test/rollback/

   Rollback to a snapshot.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/snapshot/tank%40test/rollback/ HTTP/1.1
      Content-Type: application/json

        {
            "force": true
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

        Snapshot rolled back.

   :json string name: name/path of the clone
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 202: no error


Task
----------

The Task resource represents Periodic Snapshot Tasks for ZFS Volumes.

List resource
+++++++++++++

.. http:get:: /api/v1.0/storage/task/

   Returns a list of all periodic snapshot tasks.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/task/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      [
        {
                "task_ret_count": 2,
                "task_repeat_unit": "weekly",
                "task_enabled": true,
                "task_recursive": false,
                "task_end": "18:00:00",
                "task_interval": 60,
                "task_byweekday": "1,2,3,4,5",
                "task_begin": "09:00:00",
                "task_filesystem": "tank",
                "id": 1,
                "task_ret_unit": "week"
        }
      ]

   :query offset: offset number. default is 0
   :query limit: limit number. default is 20
   :resheader Content-Type: content type of the response
   :statuscode 200: no error


Create resource
+++++++++++++++

.. http:post:: /api/v1.0/storage/task/

   Creates a new Task and returns the new Task object.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/task/ HTTP/1.1
      Content-Type: application/json

        {
                "task_filesystem": "tank",
                "task_recursive": false,
                "task_ret_unit": "week",
                "task_interval": 60,
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 201 Created
      Vary: Accept
      Content-Type: application/json

        {
                "task_ret_count": 2,
                "task_repeat_unit": "weekly",
                "task_enabled": true,
                "task_recursive": false,
                "task_end": "18:00:00",
                "task_interval": 60,
                "task_byweekday": "1,2,3,4,5",
                "task_begin": "09:00:00",
                "task_filesystem": "tank",
                "id": 1,
                "task_ret_unit": "week"
        }

   :json string task_repeat_unit: daily, weekly
   :json string task_begin: do not snapshot before
   :json string task_end: do not snapshot after
   :json string task_filesystem: name of the ZFS filesystem
   :json string task_ret_unit: hour, day, week, month, year
   :json string task_byweekday: days of week to snapshot, [1..7]
   :json integer task_interval: how much time has been passed between two snapshot attempts [5, 10, 15, 30, 60, 120, 180, 240, 360, 720, 1440, 10080]
   :json integer task_ret_count: snapshot lifetime value
   :json boolean task_enabled: enabled task
   :json boolean task_recursive: snapshot all children datasets recursively
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 201: no error


Update resource
+++++++++++++++

.. http:put:: /api/v1.0/storage/task/(int:id)/

   Update Task `id`.

   **Example request**:

   .. sourcecode:: http

      PUT /api/v1.0/storage/task/1/ HTTP/1.1
      Content-Type: application/json

        {
                "task_interval": 30
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

        {
                "task_ret_count": 2,
                "task_repeat_unit": "weekly",
                "task_enabled": true,
                "task_recursive": false,
                "task_end": "18:00:00",
                "task_interval": 30,
                "task_byweekday": "1,2,3,4,5",
                "task_begin": "09:00:00",
                "task_filesystem": "tank",
                "id": 1,
                "task_ret_unit": "week"
        }

   :json string task_repeat_unit: daily, weekly
   :json string task_begin: do not snapshot before
   :json string task_end: do not snapshot after
   :json string task_filesystem: name of the ZFS filesystem
   :json string task_ret_unit: hour, day, week, month, year
   :json string task_byweekday: days of week to snapshot, [1..7]
   :json integer task_interval: how much time has been passed between two snapshot attempts [5, 10, 15, 30, 60, 120, 180, 240, 360, 720, 1440, 10080]
   :json integer task_ret_count: snapshot lifetime value
   :json boolean task_enabled: enabled task
   :json boolean task_recursive: snapshot all children datasets recursively
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 200: no error


Delete resource
+++++++++++++++

.. http:delete:: /api/v1.0/storage/task/(int:id)/

   Delete Task `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/task/1/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :statuscode 204: no error


Replication
-----------

The Replication resource represents ZFS Replication tasks.

List resource
+++++++++++++

.. http:get:: /api/v1.0/storage/replication/

   Returns a list of all replications.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/replication/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      [
        {
                "repl_end": "23:59:00",
                "repl_remote_dedicateduser": null,
                "repl_userepl": false,
                "repl_limit": 0,
                "repl_remote_port": 22,
                "repl_remote_dedicateduser_enabled": false,
                "repl_begin": "00:00:00",
                "repl_filesystem": "tank",
                "repl_remote_cipher": "standard",
                "repl_remote_hostkey": "AAAA",
                "repl_enabled": true,
                "repl_compression": "lz4",
                "repl_remote_hostname": "testhost",
                "repl_lastsnapshot": "",
                "repl_status": "Waiting",
                "id": 1,
                "repl_zfs": "tank"
        }
      ]

   :query offset: offset number. default is 0
   :query limit: limit number. default is 20
   :resheader Content-Type: content type of the response
   :statuscode 200: no error


Create resource
+++++++++++++++

.. http:post:: /api/v1.0/storage/replication/

   Creates a new Replication and returns the new Replication object.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/replication/ HTTP/1.1
      Content-Type: application/json

        {
                "repl_filesystem": "tank",
                "repl_zfs": "tank",
                "repl_remote_hostname": "testhost",
                "repl_remote_hostkey": "AAAA",
                "repl_remote_cipher": "standard"
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 201 Created
      Vary: Accept
      Content-Type: application/json

        {
                "repl_end": "23:59:00",
                "repl_remote_dedicateduser": null,
                "repl_userepl": false,
                "repl_followdelete": false,
                "repl_limit": 0,
                "repl_remote_port": 22,
                "repl_remote_dedicateduser_enabled": false,
                "repl_begin": "00:00:00",
                "repl_filesystem": "tank",
                "repl_remote_cipher": "standard",
                "repl_remote_hostkey": "AAAA",
                "repl_enabled": true,
                "repl_compression": "lz4",
                "repl_remote_hostname": "testhost",
                "repl_lastsnapshot": "",
                "repl_status": "Waiting",
                "id": 1,
                "repl_zfs": "tank"
        }

   :json boolean repl_enabled: enable replication
   :json string repl_filesystem: filesystem to replicate
   :json string repl_lastsnapshot: last snapshot sent to remote side
   :json string repl_remote_mode: MANUAL or SEMIAUTOMATIC
   :json string repl_remote_http_port: HTTP port of remote for SEMIAUTOMATIC mode
   :json boolean repl_remote_https: HTTPS (true|false) of remote for SEMIAUTOMATIC mode
   :json boolean repl_remote_token: remote auth token for SEMIAUTOMATIC mode
   :json string repl_remote_hostname: remote hostname
   :json integer repl_remote_port: remote ssh port
   :json string repl_remote_hostkey: remote ssh public key
   :json string repl_remote_cipher: encryption cipher to use (standard, fast, disabled)
   :json boolean repl_remote_dedicateduser_enabled: use dedicated user to replicate
   :json string repl_remote_dedicateduser: dedicated user to replicate
   :json boolean repl_userepl: recursively replicate on remote side
   :json boolean repl_followdelete: delete stale snapshots on remote system which are no longer stored on host system
   :json string repl_compression: replication stream compression
   :json string repl_status: current status of the replication
   :json integer repl_limit: limit the replication speed in KB/s
   :json string repl_begin: do not start replication before
   :json string repl_end: do not start replication after
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 201: no error


Update resource
+++++++++++++++

.. http:put:: /api/v1.0/storage/replication/(int:id)/

   Update Replication `id`.

   **Example request**:

   .. sourcecode:: http

      PUT /api/v1.0/storage/replication/1/ HTTP/1.1
      Content-Type: application/json

        {
                "repl_enabled": false
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

        {
                "repl_end": "23:59:00",
                "repl_remote_dedicateduser": null,
                "repl_userepl": false,
                "repl_followdelete": false,
                "repl_limit": 0,
                "repl_remote_port": 22,
                "repl_remote_dedicateduser_enabled": false,
                "repl_begin": "00:00:00",
                "repl_filesystem": "tank",
                "repl_remote_cipher": "standard",
                "repl_remote_hostkey": "AAAA",
                "repl_enabled": false,
                "repl_compression": "lz4",
                "repl_remote_hostname": "testhost",
                "repl_lastsnapshot": "",
                "repl_status": "Waiting",
                "id": 1,
                "repl_zfs": "tank"
        }

   :json boolean repl_enabled: enable replication
   :json string repl_filesystem: filesystem to replicate
   :json string repl_lastsnapshot: last snapshot sent to remote side
   :json string repl_remote_hostname: remote hostname
   :json integer repl_remote_port: remote ssh port
   :json string repl_remote_hostkey: remote ssh public key
   :json string repl_remote_cipher: encryption cipher to use (standard, fast, disabled)
   :json boolean repl_remote_dedicateduser_enabled: use dedicated user to replicate
   :json string repl_remote_dedicateduser: dedicated user to replicate
   :json boolean repl_userepl: recursively replicate on remote side
   :json boolean repl_followdelete: delete stale snapshots on remote system which are no longer stored on host system
   :json string repl_compression: replication stream compression
   :json string repl_status: current status of the replication
   :json integer repl_limit: limit the replication speed in KB/s
   :json string repl_begin: do not start replication before
   :json string repl_end: do not start replication after
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 200: no error


Delete resource
+++++++++++++++

.. http:delete:: /api/v1.0/storage/replication/(int:id)/

   Delete Replication `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/replication/1/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :statuscode 204: no error


Scrub
----------

The Scrub resource represents Periodic Snapshot Scrubs for ZFS Volumes.

List resource
+++++++++++++

.. http:get:: /api/v1.0/storage/scrub/

   Returns a list of all scrubs.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/scrub/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      [
        {
                "scrub_threshold": 35,
                "scrub_dayweek": "7",
                "scrub_enabled": true,
                "scrub_minute": "00",
                "scrub_hour": "00",
                "scrub_month": "*",
                "scrub_daymonth": "*",
                "scrub_description": "",
                "id": 1,
                "scrub_volume": "tank"
        }
      ]

   :query offset: offset number. default is 0
   :query limit: limit number. default is 20
   :resheader Content-Type: content type of the response
   :statuscode 200: no error


Create resource
+++++++++++++++

.. http:post:: /api/v1.0/storage/scrub/

   Creates a new Scrub and returns the new Scrub object.

   **Example request**:

   .. sourcecode:: http

      POST /api/v1.0/storage/scrub/ HTTP/1.1
      Content-Type: application/json

        {
                "scrub_volume": 1,
                "scrub_dayweek": "7",
                "scrub_minute": "00",
                "scrub_hour": "00",
                "scrub_month": "*",
                "scrub_daymonth": "*"
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 201 Created
      Vary: Accept
      Content-Type: application/json

        {
                "scrub_threshold": 35,
                "scrub_dayweek": "7",
                "scrub_enabled": true,
                "scrub_minute": "00",
                "scrub_hour": "00",
                "scrub_month": "*",
                "scrub_daymonth": "*",
                "scrub_description": "",
                "id": 1,
                "scrub_volume": "tank"
        }

   :json integer scrub_volume: id to volume object
   :json integer scrub_threshold: determine how many days shall be between scrubs
   :json string scrub_description: user description
   :json string scrub_minute: values 0-59 allowed
   :json string scrub_hour: values 0-23 allowed
   :json string scrub_daymonth: day of month, values 1-31 allowed
   :json string scrub_month: month
   :json string scrub_dayweek: day of week
   :json boolean scrub_enabled: scrub enabled
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 201: no error


Update resource
+++++++++++++++

.. http:put:: /api/v1.0/storage/scrub/(int:id)/

   Update Scrub `id`.

   **Example request**:

   .. sourcecode:: http

      PUT /api/v1.0/storage/scrub/1/ HTTP/1.1
      Content-Type: application/json

        {
                "scrub_dayweek": "6"
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

        {
                "scrub_threshold": 35,
                "scrub_dayweek": "6",
                "scrub_enabled": true,
                "scrub_minute": "00",
                "scrub_hour": "00",
                "scrub_month": "*",
                "scrub_daymonth": "*",
                "scrub_description": "",
                "id": 1,
                "scrub_volume": "tank"
        }

   :json integer scrub_volume: id to volume object
   :json integer scrub_threshold: determine how many days shall be between scrubs
   :json string scrub_description: user description
   :json string scrub_minute: values 0-59 allowed
   :json string scrub_hour: values 0-23 allowed
   :json string scrub_daymonth: day of month, values 1-31 allowed
   :json string scrub_month: month
   :json string scrub_dayweek: day of week
   :json boolean scrub_enabled: scrub enabled
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 200: no error


Delete resource
+++++++++++++++

.. http:delete:: /api/v1.0/storage/scrub/(int:id)/

   Delete Scrub `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/scrub/1/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :statuscode 204: no error


Disk
----------

The Disk resource represents available disks in the system.

List resource
+++++++++++++

.. http:get:: /api/v1.0/storage/disk/

   Returns a list of all disks.

   **Example request**:

   .. sourcecode:: http

      GET /api/v1.0/storage/disk/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

      [
        {
                "disk_acousticlevel": "Disabled",
                "disk_advpowermgmt": "Disabled",
                "disk_serial": "VBad9d9bb7-3d1d3bce",
                "disk_size": "4294967296",
                "disk_multipath_name": "",
                "disk_identifier": "{serial}VBad9d9bb7-3d1d3bce",
                "disk_togglesmart": true,
                "disk_hddstandby": "Always On",
                "disk_transfermode": "Auto",
                "disk_multipath_member": "",
                "disk_description": "",
                "disk_smartoptions": "",
                "disk_expiretime": null,
                "disk_name": "ada7"
        }
      ]

   :query offset: offset number. default is 0
   :query limit: limit number. default is 20
   :resheader Content-Type: content type of the response
   :statuscode 200: no error


Update resource
+++++++++++++++

.. http:put:: /api/v1.0/storage/disk/(str:disk_identifier)/

   Update Disk `disk_identifier`.

   **Example request**:

   .. sourcecode:: http

      PUT /api/v1.0/storage/disk/{serial}VBad9d9bb7-3d1d3bce/ HTTP/1.1
      Content-Type: application/json

        {
                "disk_togglesmart": false
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

        {
                "disk_acousticlevel": "Disabled",
                "disk_advpowermgmt": "Disabled",
                "disk_serial": "VBad9d9bb7-3d1d3bce",
                "disk_size": "4294967296",
                "disk_multipath_name": "",
                "disk_identifier": "{serial}VBad9d9bb7-3d1d3bce",
                "disk_togglesmart": false,
                "disk_hddstandby": "Always On",
                "disk_transfermode": "Auto",
                "disk_multipath_member": "",
                "disk_description": "",
                "disk_smartoptions": "",
                "disk_expiretime": null,
                "disk_name": "ada7"
        }

   :json string disk_description: user description
   :json string disk_hddstandby: Always On, 5, 10, 20, 30, 60, 120, 180, 240, 300, 330
   :json string disk_advpowermgmt: Disabled, 1, 64, 127, 128. 192, 254
   :json string disk_acousticlevel: Disabled, Minimum, Medium, Maximum
   :json boolean disk_togglesmart: Enable S.M.A.R.T.
   :json string disk_smartoptions: S.M.A.R.T. extra options
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 200: no error


Permission
----------

The Permission resource allows one to set mountpoints permissions.

Update resource
+++++++++++++++

.. http:put:: /api/v1.0/storage/permission/

   Update a mountpoint with the given permission.

   **Example request**:

   .. sourcecode:: http

      PUT /api/v1.0/storage/permission/ HTTP/1.1
      Content-Type: application/json

        {
                "mp_path": "/mnt/tank",
                "mp_acl": "unix",
                "mp_mode": "755",
                "mp_user": "root",
                "mp_group": "wheel",
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 202 Accepted
      Vary: Accept
      Content-Type: application/json

        Mount Point permissions successfully updated.

   :json string mp_path: mount point path to update
   :json string mp_acl: type of acl (windows/unix)
   :json string mp_mode: octal mode number for user, group and other
   :json string mp_user: username
   :json string mp_group: group
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 202: no error


Update resource
+++++++++++++++

.. http:put:: /api/v1.0/storage/task/(int:id)/

   Update Task `id`.

   **Example request**:

   .. sourcecode:: http

      PUT /api/v1.0/storage/task/1/ HTTP/1.1
      Content-Type: application/json

        {
                "task_interval": 30
        }

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 200 OK
      Vary: Accept
      Content-Type: application/json

        {
                "task_ret_count": 2,
                "task_repeat_unit": "weekly",
                "task_enabled": true,
                "task_recursive": false,
                "task_end": "18:00:00",
                "task_interval": 30,
                "task_byweekday": "1,2,3,4,5",
                "task_begin": "09:00:00",
                "task_filesystem": "tank",
                "id": 1,
                "task_ret_unit": "week"
        }

   :json string task_repeat_unit: daily, weekly
   :json string task_begin: do not snapshot before
   :json string task_end: do not snapshot after
   :json string task_filesystem: name of the ZFS filesystem
   :json string task_ret_unit: hour, day, week, month, year
   :json string task_byweekday: days of week to snapshot, [1..7]
   :json integer task_interval: how much time has been passed between two snapshot attempts [5, 10, 15, 30, 60, 120, 180, 240, 360, 720, 1440, 10080]
   :json integer task_ret_count: snapshot lifetime value
   :json boolean task_enabled: enabled task
   :json boolean task_recursive: snapshot all children datasets recursively
   :reqheader Content-Type: the request content type
   :resheader Content-Type: the response content type
   :statuscode 200: no error


Delete resource
+++++++++++++++

.. http:delete:: /api/v1.0/storage/task/(int:id)/

   Delete Task `id`.

   **Example request**:

   .. sourcecode:: http

      DELETE /api/v1.0/storage/task/1/ HTTP/1.1
      Content-Type: application/json

   **Example response**:

   .. sourcecode:: http

      HTTP/1.1 204 No Response
      Vary: Accept
      Content-Type: application/json

   :statuscode 204: no error
