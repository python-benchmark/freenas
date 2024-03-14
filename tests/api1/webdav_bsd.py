#!/usr/bin/env python3.6

# Author: Eric Turgeon
# License: BSD
# Location for tests into REST API of FreeNAS

import sys
import os
from time import sleep
apifolder = os.getcwd()
sys.path.append(apifolder)
from functions import PUT, POST, GET, DELETE
from auto_config import pool_name

DATASET = "webdav-bsd-share"
DATASET_PATH = f"/mnt/{pool_name}/{DATASET}/"
TMP_FILE = "/tmp/testfile.txt"
SHARE_NAME = "webdavshare"
SHARE_USER = "webdav"
SHARE_PASS = "davtest"


def test_01_Creating_dataset_for_WebDAV_use():
    results = POST(f"/storage/volume/{pool_name}/datasets/", {"name": DATASET})
    assert results.status_code == 201, results.text


def test_02_Changing_permissions_on_DATASET_PATH():
    payload = {"mp_path": DATASET_PATH,
               "mp_acl": "unix",
               "mp_mode": "777",
               "mp_user": "root",
               "mp_group": "wheel"}
    results = PUT("/storage/permission/", payload)
    assert results.status_code == 201, results.text


def test_03_Creating_WebDAV_share_on_DATASET_PATH():
    payload = {"webdav_name": SHARE_NAME,
               "webdav_comment": "Auto-created by API tests",
               "webdav_path": DATASET_PATH}
    results = POST("/sharing/webdav/", payload)
    assert results.status_code == 201, results.text


def test_04_Starting_WebDAV_service():
    results = PUT("/services/services/webdav/", {"srv_enable": True})
    assert results.status_code == 200, results.text
    sleep(1)


def test_05_Verifying_that_the_WebDAV_service_has_started():
    results = GET("/services/services/webdav")
    assert results.json()["srv_state"] == "RUNNING", results.text


def test_06_Stopping_WebDAV_service():
    results = PUT("/services/services/webdav/", {"srv_enable": False})
    assert results.status_code == 200, results.text
    sleep(1)


def test_07_Verifying_that_the_WebDAV_service_has_stopped():
    results = GET("/services/services/webdav")
    assert results.json()["srv_state"] == "STOPPED", results.text


# Update test
def test_08_Changing_permissions_on_DATASET_PATH():
    payload = {"mp_path": DATASET_PATH,
               "mp_acl": "unix",
               "mp_mode": "777",
               "mp_user": "root",
               "mp_group": "wheel"}
    results = PUT("/storage/permission/", payload)
    assert results.status_code == 201, results.text


def test_09_Creating_WebDAV_share_on_DATASET_PATH():
    payload = {"webdav_name": SHARE_NAME,
               "webdav_comment": "Auto-created by API tests",
               "webdav_path": DATASET_PATH}
    results = POST("/sharing/webdav/", payload)
    assert results.status_code == 201, results.text


def test_10_Starting_WebDAV_service():
    results = PUT("/services/services/webdav/", {"srv_enable": True})
    assert results.status_code == 200, results.text
    sleep(1)


def test_11_Changing_password_for_webdev():
    payload = {"webdav_password": SHARE_PASS}
    results = PUT("/services/services/webdav/", payload)
    assert results.status_code == 200, results.text


def test_12_Stopping_WebDAV_service():
    results = PUT("/services/services/webdav/", {"srv_enable": False})
    assert results.status_code == 200, results.text
    sleep(1)


def test_13_Verifying_that_the_WebDAV_service_has_stopped():
    results = GET("/services/services/webdav")
    assert results.json()["srv_state"] == "STOPPED", results.text


def test_14_Destroying_dataset():
    results = DELETE(f"/storage/volume/{pool_name}/datasets/{DATASET}/")
    assert results.status_code == 204, results.text
