"""Offline host seed dispatch ownership and immutable attempt checks."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[4] / "scripts/f4_engineering"))


def test_only_owned_seed_container_may_be_removed():
    from runtime_seed import owns_container

    row = {
        "Name": "/f4-seed-fixed",
        "Config": {"Labels": {"school-facts.f4.seed-owner": "token"}},
        "Image": "sha256:" + "a" * 64,
    }
    assert owns_container(row, name="f4-seed-fixed", owner="token", image=row["Image"])
    assert not owns_container(row, name="other", owner="token", image=row["Image"])
    assert not owns_container(row, name="f4-seed-fixed", owner="other", image=row["Image"])


def test_seed_dispatch_argv_never_contains_session_token(tmp_path):
    from runtime_seed import worker_argv

    args = worker_argv(
        docker=Path("docker"),
        image="sha256:" + "a" * 64,
        name="f4-seed-test",
        owner="owned",
        folder=tmp_path,
        mounts=((tmp_path / "config.json", "/run/f4-seed/product-config.json"),),
        network="campus-school-facts-f1-private",
    )
    assert "--interactive" in args
    assert "--log-driver" in args and args[args.index("--log-driver") + 1] == "none"
    assert args[-1] == "/workspace/scripts/f4_engineering/seed_worker.py"


def test_seed_worker_argv_refuses_external_network(tmp_path):
    from runtime_seed import worker_argv

    with pytest.raises(ValueError):
        worker_argv(
            docker=Path("docker"),
            image="sha256:" + "a" * 64,
            name="f4-seed-test",
            owner="owned",
            folder=tmp_path,
            mounts=(),
            network="bridge",
        )


def test_product_two_approved_networks_and_loopback_port_only():
    from runtime_seed import MODEL_NETWORK, NETWORK, require_service_networks

    image = "sha256:" + "a" * 64
    row = {
        "State": {"Running": True},
        "Image": image,
        "NetworkSettings": {
            "Networks": {NETWORK: {}, MODEL_NETWORK: {}},
            "Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8001"}]},
        },
        "HostConfig": {"PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8001"}]}},
    }
    require_service_networks("campus-school-facts-f1-product-1", row, image=image)
    with pytest.raises(ValueError):
        require_service_networks("campus-school-facts-f1-writer-1", row, image=image)
    row["NetworkSettings"]["Networks"]["bridge"] = {}
    with pytest.raises(ValueError):
        require_service_networks("campus-school-facts-f1-product-1", row, image=image)


def test_writer_private_only_and_no_published_port():
    from runtime_seed import NETWORK, require_service_networks

    image = "sha256:" + "a" * 64
    row = {
        "State": {"Running": True},
        "Image": image,
        "NetworkSettings": {"Networks": {NETWORK: {}}, "Ports": {"8000/tcp": None}},
        "HostConfig": {"PortBindings": {}},
    }
    require_service_networks("campus-school-facts-f1-writer-1", row, image=image)
    row["HostConfig"]["PortBindings"] = {"8010/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8010"}]}
    with pytest.raises(ValueError):
        require_service_networks("campus-school-facts-f1-writer-1", row, image=image)


@pytest.mark.parametrize(
    "host", [r"E:\学校\private\product-config.json", "E:/学校/private/product-config.json"]
)
def test_desktop_mount_alias_matches_only_the_same_windows_host_file(host):
    from runtime_seed import host_mount_path_is_same

    assert host_mount_path_is_same("/run/desktop/mnt/host/e/学校/private/product-config.json", host)
    assert host_mount_path_is_same(host, "e:/学校/private/product-config.json")


@pytest.mark.parametrize(
    "source",
    [
        "/run/desktop/mnt/host/f/学校/private/product-config.json",
        "/host_mnt/e/学校/private/product-config.json",
        "/run/desktop/mnt/host/e/学校/private/../private/product-config.json",
        "/run/desktop/mnt/host/e/学校/private-other/product-config.json",
        "/run/desktop/mnt/host/e/学校/private/writer-config.json",
        "E:学校/private/product-config.json",
    ],
)
def test_desktop_mount_alias_never_broadens_directory_or_drive(source):
    from runtime_seed import host_mount_path_is_same

    assert not host_mount_path_is_same(source, "E:/学校/private/product-config.json")


def test_native_mount_identity_remains_supported(tmp_path):
    from runtime_seed import host_mount_path_is_same

    assert host_mount_path_is_same(str(tmp_path / "config.json"), tmp_path / "config.json")
    assert not host_mount_path_is_same(str(tmp_path / "other.json"), tmp_path / "config.json")
