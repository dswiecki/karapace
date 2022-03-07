"""
karapace - conftest

Copyright (c) 2019 Aiven Ltd
See LICENSE for details
"""
from _pytest.fixtures import SubRequest
from aiohttp.pytest_plugin import AiohttpClient
from aiohttp.test_utils import TestClient
from contextlib import closing, ExitStack
from dataclasses import asdict, dataclass
from filelock import FileLock
from kafka import KafkaProducer
from kafka.errors import LeaderNotAvailableError, NoBrokersAvailable
from karapace.config import set_config_defaults, write_config
from karapace.kafka_rest_apis import KafkaRest, KafkaRestAdminClient
from karapace.schema_registry_apis import KarapaceSchemaRegistry
from karapace.utils import Client
from pathlib import Path
from subprocess import Popen
from tests.utils import (
    Expiration,
    get_random_port,
    KAFKA_PORT_RANGE,
    KafkaConfig,
    KafkaServers,
    new_random_name,
    REGISTRY_PORT_RANGE,
    repeat_until_successful_request,
    ZK_PORT_RANGE,
)
from typing import AsyncIterator, Dict, Iterator, List, Optional, Tuple

import asyncio
import logging
import os
import pathlib
import pytest
import requests
import signal
import socket
import tarfile
import time
import ujson

REPOSITORY_DIR = pathlib.Path(__file__).parent.parent.parent.absolute()
RUNTIME_DIR = REPOSITORY_DIR / "runtime"
TEST_INTEGRATION_DIR = REPOSITORY_DIR / "tests" / "integration"
KAFKA_WAIT_TIMEOUT = 60
KAFKA_SCALA_VERSION = "2.13"


@dataclass
class ZKConfig:
    client_port: int
    admin_port: int
    path: str

    @staticmethod
    def from_dict(data: dict) -> "ZKConfig":
        return ZKConfig(
            data["client_port"],
            data["admin_port"],
            data["path"],
        )


@dataclass(frozen=True)
class KafkaDescription:
    version: str
    install_dir: Path
    download_url: str
    protocol_version: str


def stop_process(proc: Optional[Popen]) -> None:
    if proc:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10.0)


def port_is_listening(hostname: str, port: int, ipv6: bool) -> bool:
    if ipv6:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM, 0)
    else:
        s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((hostname, port))
        s.close()
        return True
    except socket.error:
        return False


def wait_for_kafka(
    kafka_servers: KafkaServers,
    wait_time: float,
) -> None:
    for server in kafka_servers.bootstrap_servers:
        expiration = Expiration.from_timeout(timeout=wait_time)

        list_topics_successful = False
        msg = f"Could not contact kafka cluster on host `{server}`"
        while not list_topics_successful:
            expiration.raise_if_expired(msg)
            try:
                KafkaRestAdminClient(bootstrap_servers=server).cluster_metadata()
            # ValueError:
            # - if the port number is invalid (i.e. not a number)
            # - if the port is not bound yet
            # NoBrokersAvailable:
            # - if the address/port does not point to a running server
            # LeaderNotAvailableError:
            # - if there is no leader yet
            except (
                NoBrokersAvailable,
                LeaderNotAvailableError,
                ValueError,
            ) as e:
                print(f"Error checking kafka cluster: {e}")
                time.sleep(2.0)
            else:
                list_topics_successful = True


def wait_for_port_subprocess(
    port: int,
    process: Popen,
    *,
    hostname: str = "127.0.0.1",
    wait_time: float = 20.0,
    ipv6: bool = False,
) -> None:
    start_time = time.monotonic()
    expiration = Expiration(deadline=start_time + wait_time)
    msg = f"Timeout waiting for `{hostname}:{port}`"

    while not port_is_listening(hostname, port, ipv6):
        expiration.raise_if_expired(msg)
        assert process.poll() is None, f"Process no longer running, exit_code: {process.returncode}"
        time.sleep(2.0)

    elapsed = time.monotonic() - start_time
    print(f"Server `{hostname}:{port}` listening after {elapsed} seconds")


def lock_path_for(path: Path) -> Path:
    """Append .lock to path"""
    suffixes = path.suffixes
    suffixes.append(".lock")
    return path.with_suffix("".join(suffixes))


def maybe_download_kafka(kafka_description: KafkaDescription) -> None:
    """If necessary download kafka to run the tests."""
    if not os.path.exists(kafka_description.install_dir):
        logging.info("Downloading Kafka {url}", url=kafka_description.download_url)

        download = requests.get(kafka_description.download_url, stream=True)
        with tarfile.open(mode="r:gz", fileobj=download.raw) as file:
            file.extractall(str(kafka_description.install_dir.parent))


@pytest.fixture(scope="session", name="kafka_description")
def fixture_kafka_description(request: SubRequest) -> KafkaDescription:
    kafka_version = request.config.getoption("kafka_version")
    kafka_folder = f"kafka_{KAFKA_SCALA_VERSION}-{kafka_version}"
    kafka_tgz = f"{kafka_folder}.tgz"
    kafka_url = f"https://archive.apache.org/dist/kafka/{kafka_version}/{kafka_tgz}"
    kafka_dir = RUNTIME_DIR / kafka_folder

    return KafkaDescription(
        version=kafka_version,
        install_dir=kafka_dir,
        download_url=kafka_url,
        protocol_version="2.7",
    )


@pytest.fixture(scope="session", name="kafka_servers")
def fixture_kafka_server(
    request: SubRequest,
    session_datadir: Path,
    session_logdir: Path,
    kafka_description: KafkaDescription,
) -> Iterator[KafkaServers]:
    bootstrap_servers = request.config.getoption("kafka_bootstrap_servers")

    if bootstrap_servers:
        kafka_servers = KafkaServers(bootstrap_servers)
        wait_for_kafka(kafka_servers, KAFKA_WAIT_TIMEOUT)
        yield kafka_servers
        return

    zk_dir = session_logdir / "zk"
    transfer_file = session_logdir / "zk_kafka_config"

    with ExitStack() as stack:
        # Synchronize xdist workers, data generated by the winner is shared through
        # transfer_file (primarily the server's port number)

        # there is an issue with pylint here, see https:/github.com/tox-dev/py-filelock/issues/102
        with FileLock(str(lock_path_for(transfer_file))):  # pylint: disable=abstract-class-instantiated
            if transfer_file.exists():
                config_data = ujson.loads(transfer_file.read_text())
                zk_config = ZKConfig.from_dict(config_data["zookeeper"])
                kafka_config = KafkaConfig.from_dict(config_data["kafka"])
            else:
                maybe_download_kafka(kafka_description)

                zk_config, zk_proc = configure_and_start_zk(
                    zk_dir,
                    kafka_description,
                )
                stack.callback(stop_process, zk_proc)

                # Make sure zookeeper is running before trying to start Kafka
                wait_for_port_subprocess(zk_config.client_port, zk_proc, wait_time=20)

                kafka_config, kafka_proc = configure_and_start_kafka(
                    session_datadir,
                    session_logdir,
                    zk_config,
                    kafka_description,
                )
                stack.callback(stop_process, kafka_proc)

                config_data = {
                    "zookeeper": asdict(zk_config),
                    "kafka": asdict(kafka_config),
                }
                transfer_file.write_text(ujson.dumps(config_data))

        # Make sure every test worker can communicate with kafka
        kafka_servers = KafkaServers(bootstrap_servers=[f"127.0.0.1:{kafka_config.kafka_port}"])
        wait_for_kafka(kafka_servers, KAFKA_WAIT_TIMEOUT)
        yield kafka_servers
        return


@pytest.fixture(scope="function", name="producer")
def fixture_producer(kafka_servers: KafkaServers) -> KafkaProducer:
    with closing(KafkaProducer(bootstrap_servers=kafka_servers.bootstrap_servers)) as prod:
        yield prod


@pytest.fixture(scope="function", name="admin_client")
def fixture_admin(kafka_servers: KafkaServers) -> Iterator[KafkaRestAdminClient]:
    with closing(KafkaRestAdminClient(bootstrap_servers=kafka_servers.bootstrap_servers)) as cli:
        yield cli


@pytest.fixture(scope="function", name="rest_async")
async def fixture_rest_async(
    request: SubRequest,
    loop: asyncio.AbstractEventLoop,  # pylint: disable=unused-argument
    tmp_path: Path,
    kafka_servers: KafkaServers,
    registry_async_client: Client,
) -> AsyncIterator[Optional[KafkaRest]]:

    # Do not start a REST api when the user provided an external service. Doing
    # so would cause this node to join the existing group and participate in
    # the election process. Without proper configuration for the listeners that
    # won't work and will cause test failures.
    rest_url = request.config.getoption("rest_url")
    if rest_url:
        yield None
        return

    config_path = tmp_path / "karapace_config.json"

    config = set_config_defaults({"bootstrap_uri": kafka_servers.bootstrap_servers, "admin_metadata_max_age": 2})
    write_config(config_path, config)
    rest = KafkaRest(config=config)

    assert rest.serializer.registry_client
    assert rest.consumer_manager.deserializer.registry_client
    rest.serializer.registry_client.client = registry_async_client
    rest.consumer_manager.deserializer.registry_client.client = registry_async_client
    try:
        yield rest
    finally:
        await rest.close()


@pytest.fixture(scope="function", name="rest_async_client")
async def fixture_rest_async_client(
    request: SubRequest,
    loop: asyncio.AbstractEventLoop,  # pylint: disable=unused-argument
    rest_async: KafkaRest,
    aiohttp_client: AiohttpClient,
) -> AsyncIterator[Client]:
    rest_url = request.config.getoption("rest_url")

    # client and server_uri are incompatible settings.
    if rest_url:
        client = Client(server_uri=rest_url)
    else:

        async def get_client() -> TestClient:
            return await aiohttp_client(rest_async.app)

        client = Client(client_factory=get_client)

    try:
        # wait until the server is listening, otherwise the tests may fail
        await repeat_until_successful_request(
            client.get,
            "brokers",
            json_data=None,
            headers=None,
            error_msg="REST API is unreachable",
            timeout=10,
            sleep=0.3,
        )
        yield client
    finally:
        await client.close()


@pytest.fixture(scope="function", name="registry_async_pair")
def fixture_registry_async_pair(
    tmp_path: Path,
    kafka_servers: KafkaServers,
) -> Iterator[Tuple[str, str]]:
    master_config_path = tmp_path / "karapace_config_master.json"
    slave_config_path = tmp_path / "karapace_config_slave.json"
    master_port = get_random_port(port_range=REGISTRY_PORT_RANGE, blacklist=[])
    slave_port = get_random_port(port_range=REGISTRY_PORT_RANGE, blacklist=[master_port])
    topic_name = new_random_name("schema_pairs")
    group_id = new_random_name("schema_pairs")
    write_config(
        master_config_path,
        {
            "bootstrap_uri": kafka_servers.bootstrap_servers,
            "topic_name": topic_name,
            "group_id": group_id,
            "advertised_hostname": "127.0.0.1",
            "karapace_registry": True,
            "port": master_port,
        },
    )
    write_config(
        slave_config_path,
        {
            "bootstrap_uri": kafka_servers.bootstrap_servers,
            "topic_name": topic_name,
            "group_id": group_id,
            "advertised_hostname": "127.0.0.1",
            "karapace_registry": True,
            "port": slave_port,
        },
    )

    master_process = None
    slave_process = None
    with ExitStack() as stack:
        try:
            master_process = stack.enter_context(Popen(["python", "-m", "karapace.karapace_all", str(master_config_path)]))
            slave_process = stack.enter_context(Popen(["python", "-m", "karapace.karapace_all", str(slave_config_path)]))
            wait_for_port_subprocess(master_port, master_process)
            wait_for_port_subprocess(slave_port, slave_process)
            yield f"http://127.0.0.1:{master_port}", f"http://127.0.0.1:{slave_port}"
        finally:
            if master_process:
                master_process.kill()
            if slave_process:
                slave_process.kill()


@pytest.fixture(scope="function", name="registry_async")
async def fixture_registry_async(
    request: SubRequest,
    loop: asyncio.AbstractEventLoop,  # pylint: disable=unused-argument
    tmp_path: Path,
    kafka_servers: KafkaServers,
) -> AsyncIterator[Optional[KarapaceSchemaRegistry]]:
    # Do not start a registry when the user provided an external service. Doing
    # so would cause this node to join the existing group and participate in
    # the election process. Without proper configuration for the listeners that
    # won't work and will cause test failures.
    registry_url = request.config.getoption("registry_url")
    if registry_url:
        yield None
        return

    config_path = tmp_path / "karapace_config.json"

    config = set_config_defaults(
        {
            "bootstrap_uri": kafka_servers.bootstrap_servers,
            # Using the default settings instead of random values, otherwise it
            # would not be possible to run the tests with external services.
            # Because of this every test must be written in such a way that it can
            # be executed twice with the same servers.
            # "topic_name": new_random_name("topic"),
            "group_id": new_random_name("registry_async"),
        }
    )
    write_config(config_path, config)
    registry = KarapaceSchemaRegistry(config=config)
    await registry.get_master()
    try:
        yield registry
    finally:
        await registry.close()


@pytest.fixture(scope="function", name="registry_async_client")
async def fixture_registry_async_client(
    request: SubRequest,
    loop: asyncio.AbstractEventLoop,  # pylint: disable=unused-argument
    registry_async: KarapaceSchemaRegistry,
    aiohttp_client: AiohttpClient,
) -> AsyncIterator[Client]:

    registry_url = request.config.getoption("registry_url")

    # client and server_uri are incompatible settings.
    if registry_url:
        client = Client(server_uri=registry_url, server_ca=request.config.getoption("server_ca"))
    else:

        async def get_client() -> TestClient:
            return await aiohttp_client(registry_async.app)

        client = Client(client_factory=get_client)

    try:
        # wait until the server is listening, otherwise the tests may fail
        await repeat_until_successful_request(
            client.get,
            "subjects",
            json_data=None,
            headers=None,
            error_msg="REST API is unreachable",
            timeout=10,
            sleep=0.3,
        )
        yield client
    finally:
        await client.close()


@pytest.fixture(scope="function", name="credentials_folder")
def fixture_credentials_folder() -> str:
    integration_test_folder = os.path.dirname(__file__)
    credentials_folder = os.path.join(integration_test_folder, "credentials")
    return credentials_folder


@pytest.fixture(scope="function", name="server_ca")
def fixture_server_ca(credentials_folder: str) -> str:
    return os.path.join(credentials_folder, "cacert.pem")


@pytest.fixture(scope="function", name="server_cert")
def fixture_server_cert(credentials_folder: str) -> str:
    return os.path.join(credentials_folder, "servercert.pem")


@pytest.fixture(scope="function", name="server_key")
def fixture_server_key(credentials_folder: str) -> str:
    return os.path.join(credentials_folder, "serverkey.pem")


@pytest.fixture(scope="function", name="registry_async_tls")
async def fixture_registry_async_tls(
    request: SubRequest,
    loop: asyncio.AbstractEventLoop,  # pylint: disable=unused-argument
    tmp_path: Path,
    kafka_servers: KafkaServers,
    server_cert: str,
    server_key: str,
) -> AsyncIterator[Optional[KarapaceSchemaRegistry]]:
    # Do not start a registry when the user provided an external service. Doing
    # so would cause this node to join the existing group and participate in
    # the election process. Without proper configuration for the listeners that
    # won't work and will cause test failures.
    registry_url = request.config.getoption("registry_url")
    if registry_url:
        yield None
        return

    config_path = tmp_path / "karapace_config.json"

    config = set_config_defaults(
        {
            "bootstrap_uri": kafka_servers.bootstrap_servers,
            "server_tls_certfile": server_cert,
            "server_tls_keyfile": server_key,
            "port": 8444,
            # Using the default settings instead of random values, otherwise it
            # would not be possible to run the tests with external services.
            # Because of this every test must be written in such a way that it can
            # be executed twice with the same servers.
            # "topic_name": new_random_name("topic"),
            "group_id": new_random_name("registry_async_tls"),
        }
    )
    write_config(config_path, config)
    registry = KarapaceSchemaRegistry(config=config)
    await registry.get_master()
    try:
        yield registry
    finally:
        await registry.close()


@pytest.fixture(scope="function", name="registry_async_client_tls")
async def fixture_registry_async_client_tls(
    request: SubRequest,
    loop: asyncio.AbstractEventLoop,  # pylint: disable=unused-argument
    registry_async_tls: KarapaceSchemaRegistry,
    aiohttp_client: AiohttpClient,
    server_ca: str,
) -> AsyncIterator[Client]:

    registry_url = request.config.getoption("registry_url")

    if registry_url:
        client = Client(server_uri=registry_url, server_ca=request.config.getoption("server_ca"))
    else:

        async def get_client() -> TestClient:
            return await aiohttp_client(registry_async_tls.app)

        client = Client(client_factory=get_client, server_ca=server_ca)

    try:
        # wait until the server is listening, otherwise the tests may fail
        await repeat_until_successful_request(
            client.get,
            "subjects",
            json_data=None,
            headers=None,
            error_msg="REST API is unreachable",
            timeout=10,
            sleep=0.3,
        )
        yield client
    finally:
        await client.close()


def zk_java_args(cfg_path: Path, kafka_description: KafkaDescription) -> List[str]:
    msg = f"Couldn't find kafka installation at {kafka_description.install_dir} to run integration tests."
    assert kafka_description.install_dir.exists(), msg
    java_args = [
        "-cp",
        str(kafka_description.install_dir / "libs" / "*"),
        "org.apache.zookeeper.server.quorum.QuorumPeerMain",
        str(cfg_path),
    ]
    return java_args


def kafka_java_args(
    heap_mb: int,
    kafka_config_path: str,
    logs_dir: str,
    log4j_properties_path: str,
    kafka_description: KafkaDescription,
) -> List[str]:
    msg = f"Couldn't find kafka installation at {kafka_description.install_dir} to run integration tests."
    assert kafka_description.install_dir.exists(), msg
    java_args = [
        "-Xmx{}M".format(heap_mb),
        "-Xms{}M".format(heap_mb),
        "-Dkafka.logs.dir={}/logs".format(logs_dir),
        "-Dlog4j.configuration=file:{}".format(log4j_properties_path),
        "-cp",
        str(kafka_description.install_dir / "libs" / "*"),
        "kafka.Kafka",
        kafka_config_path,
    ]
    return java_args


def get_java_process_configuration(java_args: List[str]) -> List[str]:
    command = [
        "/usr/bin/java",
        "-server",
        "-XX:+UseG1GC",
        "-XX:MaxGCPauseMillis=20",
        "-XX:InitiatingHeapOccupancyPercent=35",
        "-XX:+DisableExplicitGC",
        "-XX:+ExitOnOutOfMemoryError",
        "-Djava.awt.headless=true",
        "-Dcom.sun.management.jmxremote",
        "-Dcom.sun.management.jmxremote.authenticate=false",
        "-Dcom.sun.management.jmxremote.ssl=false",
    ]
    command.extend(java_args)
    return command


def configure_and_start_kafka(
    datadir: Path,
    logdir: Path,
    zk: ZKConfig,
    kafka_description: KafkaDescription,
) -> Tuple[KafkaConfig, Popen]:
    # setup filesystem
    data_dir = datadir / "kafka"
    log_dir = logdir / "kafka"
    config_path = log_dir / "server.properties"
    data_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)

    plaintext_port = get_random_port(port_range=KAFKA_PORT_RANGE, blacklist=[])

    config = KafkaConfig(
        datadir=str(data_dir),
        kafka_keystore_password="secret",
        kafka_port=plaintext_port,
        zookeeper_port=zk.client_port,
    )

    advertised_listeners = ",".join(
        [
            "PLAINTEXT://127.0.0.1:{}".format(plaintext_port),
        ]
    )
    listeners = ",".join(
        [
            "PLAINTEXT://:{}".format(plaintext_port),
        ]
    )

    # Keep in sync with containers/docker-compose.yml
    kafka_config = {
        "broker.id": 1,
        "broker.rack": "local",
        "advertised.listeners": advertised_listeners,
        "auto.create.topics.enable": False,
        "default.replication.factor": 1,
        "delete.topic.enable": "true",
        "inter.broker.listener.name": "PLAINTEXT",
        "inter.broker.protocol.version": kafka_description.protocol_version,
        "listeners": listeners,
        "log.cleaner.enable": "true",
        "log.dirs": config.datadir,
        "log.message.format.version": kafka_description.protocol_version,
        "log.retention.check.interval.ms": 300000,
        "log.segment.bytes": 200 * 1024 * 1024,  # 200 MiB
        "log.preallocate": False,
        "num.io.threads": 8,
        "num.network.threads": 112,
        "num.partitions": 1,
        "num.replica.fetchers": 4,
        "num.recovery.threads.per.data.dir": 1,
        "offsets.topic.replication.factor": 1,
        "socket.receive.buffer.bytes": 100 * 1024,
        "socket.request.max.bytes": 100 * 1024 * 1024,
        "socket.send.buffer.bytes": 100 * 1024,
        "transaction.state.log.min.isr": 1,
        "transaction.state.log.num.partitions": 16,
        "transaction.state.log.replication.factor": 1,
        "zookeeper.connection.timeout.ms": 6000,
        "zookeeper.connect": f"127.0.0.1:{zk.client_port}",
    }

    with config_path.open("w") as fp:
        for key, value in kafka_config.items():
            fp.write("{}={}\n".format(key, value))

    # stdout logger is disabled to keep the pytest report readable
    log4j_properties_path = str(TEST_INTEGRATION_DIR / "config" / "log4j.properties")

    kafka_cmd = get_java_process_configuration(
        java_args=kafka_java_args(
            heap_mb=256,
            logs_dir=str(log_dir),
            log4j_properties_path=log4j_properties_path,
            kafka_config_path=str(config_path),
            kafka_description=kafka_description,
        ),
    )
    env: Dict[bytes, bytes] = {}
    proc = Popen(kafka_cmd, env=env)
    return config, proc


def configure_and_start_zk(zk_dir: Path, kafka_description: KafkaDescription) -> Tuple[ZKConfig, Popen]:
    cfg_path = zk_dir / "zoo.cfg"
    logs_dir = zk_dir / "logs"
    logs_dir.mkdir(parents=True)

    client_port = get_random_port(port_range=ZK_PORT_RANGE, blacklist=[])
    admin_port = get_random_port(port_range=ZK_PORT_RANGE, blacklist=[client_port])
    config = ZKConfig(
        client_port=client_port,
        admin_port=admin_port,
        path=str(zk_dir),
    )
    zoo_cfg = """
# The number of milliseconds of each tick
tickTime=2000
# The number of ticks that the initial
# synchronization phase can take
initLimit=10
# The number of ticks that can pass between
# sending a request and getting an acknowledgement
syncLimit=5
# the directory where the snapshot is stored.
# do not use /tmp for storage, /tmp here is just
# example sakes.
dataDir={path}
# the port at which the clients will connect
clientPort={client_port}
#clientPortAddress=127.0.0.1
# the maximum number of client connections.
# increase this if you need to handle more clients
#maxClientCnxns=60
#
# Be sure to read the maintenance section of the
# administrator guide before turning on autopurge.
#
# http://zookeeper.apache.org/doc/current/zookeeperAdmin.html#sc_maintenance
#
# The number of snapshots to retain in dataDir
#autopurge.snapRetainCount=3
# Purge task interval in hours
# Set to "0" to disable auto purge feature
#autopurge.purgeInterval=1
# admin server
admin.serverPort={admin_port}
admin.enableServer=false
# Allow reconfig calls to be made to add/remove nodes to the cluster on the fly
reconfigEnabled=true
# Don't require authentication for reconfig
skipACL=yes
""".format(
        client_port=config.client_port,
        admin_port=config.admin_port,
        path=config.path,
    )
    cfg_path.write_text(zoo_cfg)
    env = {
        "CLASSPATH": "/usr/share/java/slf4j/slf4j-simple.jar",
        "ZOO_LOG_DIR": str(logs_dir),
    }
    java_args = get_java_process_configuration(
        java_args=zk_java_args(
            cfg_path,
            kafka_description,
        )
    )
    proc = Popen(java_args, env=env)
    return config, proc
