from mock import ANY, Mock, call, patch

from middlewared.plugins.nfs import SharingNFSService


def test__sharing_nfs_service__validate_paths__same_filesystem():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt/data-1": Mock(st_dev=1),
        "/mnt/data-1/a": Mock(st_dev=1),
        "/mnt/data-1/b": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_paths(
            {
                "paths": ["/mnt/data-1/a", "/mnt/data-1/b"],
                "alldirs": False,
            },
            "sharingnfs_update",
            verrors,
        )

        assert not verrors.add.called


def test__sharing_nfs_service__validate_paths__not_same_filesystem():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt/data-1": Mock(st_dev=1),
        "/mnt/data-2": Mock(st_dev=2),
        "/mnt/data-1/d": Mock(st_dev=1),
        "/mnt/data-2/d": Mock(st_dev=2),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_paths(
            {
                "paths": ["/mnt/data-1/d", "/mnt/data-2/d"],
                "alldirs": False,
            },
            "sharingnfs_update",
            verrors,
        )

        verrors.add.assert_called_once_with("sharingnfs_update.paths.1",
                                            "Paths for a NFS share must reside within the same filesystem")


def test__sharing_nfs_service__validate_paths__mountpoint_and_subdirectory():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt": Mock(st_dev=0),
        "/mnt/data-1": Mock(st_dev=1),
        "/mnt/data-1/a": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_paths(
            {
                "paths": ["/mnt/data-1", "/mnt/data-1/a"],
                "alldirs": False,
            },
            "sharingnfs_update",
            verrors,
        )

        verrors.add.assert_called_once_with("sharingnfs_update.paths.0",
                                            "You cannot share a mount point and subdirectories all at once")


def test__sharing_nfs_service__validate_paths__alldirs_for_nonmountpoint():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt": Mock(st_dev=0),
        "/mnt/data-1": Mock(st_dev=1),
        "/mnt/data-1/a": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_paths(
            {
                "paths": ["/mnt/data-1/a"],
                "alldirs": True,
            },
            "sharingnfs_update",
            verrors,
        )

        verrors.add.assert_called_once_with("sharingnfs_update.alldirs", ANY)


def test__sharing_nfs_service__validate_paths__alldirs_for_mountpoint():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt": Mock(st_dev=0),
        "/mnt/data-1": Mock(st_dev=1),
        "/mnt/data-1/a": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_paths(
            {
                "paths": ["/mnt/data-1"],
                "alldirs": True,
            },
            "sharingnfs_update",
            verrors,
        )

        assert not verrors.add.called


def test__sharing_nfs_service__validate_hosts_and_networks__same_device_multiple_shares_alldir():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt/data/a": Mock(st_dev=1),
        "/mnt/data/b": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_hosts_and_networks(
            [
                {
                    "paths": ["/mnt/data/a"],
                    "hosts": [],
                    "networks": ["192.168.100.0/24"],
                    "alldirs": True,
                }
            ],
            {
                "paths": ["/mnt/data/b"],
                "hosts": [],
                "networks": ["192.168.200.0/24"],
                "alldirs": True,
            },
            "sharingnfs_update",
            verrors,
            {},
        )

        verrors.add.assert_called_once_with("sharingnfs_update.alldirs", ANY)


def test__sharing_nfs_service__validate_hosts_and_networks__cant_share_overlapping():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt/data/a": Mock(st_dev=1),
        "/mnt/data/b": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_hosts_and_networks(
            [
                {
                    "paths": ["/mnt/data/a"],
                    "hosts": [],
                    "networks": ["192.168.100.0/24"],
                    "alldirs": False,
                }
            ],
            {
                "paths": ["/mnt/data/b"],
                "hosts": [],
                "networks": ["192.168.100.0/25", "192.168.100.128/25"],
                "alldirs": False,
            },
            "sharingnfs_update",
            verrors,
            {},
        )

        assert verrors.add.call_args_list == [
            call('sharingnfs_update.networks.0',
                 "You can't share same filesystem with overlapping networks 192.168.100.0/25 and 192.168.100.0/24. "
                 "This is so because /etc/exports does not act like ACL and it is undefined which rule among all "
                 "overlapping networks will be applied."),
            call('sharingnfs_update.networks.1',
                 "You can't share same filesystem with overlapping networks 192.168.100.128/25 and 192.168.100.0/24")
        ]


def test__sharing_nfs_service__validate_hosts_and_networks__cant_share_overlapping_new():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt/data/a": Mock(st_dev=1),
        "/mnt/data/b": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_hosts_and_networks(
            [
                {
                    "paths": ["/mnt/data/a"],
                    "hosts": [],
                    "networks": ["192.168.100.0/24"],
                    "alldirs": False,
                }
            ],
            {
                "paths": ["/mnt/data/b"],
                "hosts": [],
                "networks": ["192.168.200.0/24", "192.168.200.0/25"],
                "alldirs": False,
            },
            "sharingnfs_update",
            verrors,
            {},
        )

        verrors.add.assert_called_once_with("sharingnfs_update.networks.1", ANY)


def test__sharing_nfs_service__validate_hosts_and_networks__host_is_32_network():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt/data/a": Mock(st_dev=1),
        "/mnt/data/b": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_hosts_and_networks(
            [
                {
                    "paths": ["/mnt/data/a"],
                    "hosts": ["192.168.0.1"],
                    "networks": [],
                    "alldirs": False,
                },
            ],
            {
                "paths": ["/mnt/data/b"],
                "hosts": ["192.168.0.1"],
                "networks": [],
                "alldirs": False,
            },
            "sharingnfs_update",
            verrors,
            {
                "192.168.0.1": "192.168.0.1",
            },
        )

        verrors.add.assert_called_once_with("sharingnfs_update.hosts.0", ANY)


def test__sharing_nfs_service__validate_hosts_and_networks__new_for_everyone():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt/data/a": Mock(st_dev=1),
        "/mnt/data/b": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_hosts_and_networks(
            [
                {
                    "paths": ["/mnt/data/a"],
                    "hosts": ["192.168.0.1"],
                    "networks": [],
                    "alldirs": False,
                },
            ],
            {
                "paths": ["/mnt/data/b"],
                "hosts": [],
                "networks": [],
                "alldirs": False,
            },
            "sharingnfs_update",
            verrors,
            {
                "192.168.0.1": "192.168.0.1",
            },
        )

        verrors.add.assert_called_once_with("sharingnfs_update.networks", ANY)


def test__sharing_nfs_service__validate_hosts_and_networks__existing_for_everyone():
    with patch("middlewared.plugins.nfs.os.stat", lambda dev: {
        "/mnt/data/a": Mock(st_dev=1),
        "/mnt/data/b": Mock(st_dev=1),
    }[dev]):
        middleware = Mock()

        verrors = Mock()

        SharingNFSService(middleware).validate_hosts_and_networks(
            [
                {
                    "paths": ["/mnt/data/a"],
                    "hosts": [],
                    "networks": [],
                    "alldirs": False,
                },
            ],
            {
                "paths": ["/mnt/data/b"],
                "hosts": [],
                "networks": ["192.168.0.0/24"],
                "alldirs": False,
            },
            "sharingnfs_update",
            verrors,
            {
                "192.168.0.1": "192.168.0.1",
            },
        )

        verrors.add.assert_called_once_with("sharingnfs_update.networks.0", ANY)
