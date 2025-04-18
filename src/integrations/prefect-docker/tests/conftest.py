import asyncio
from contextlib import contextmanager
from typing import Generator
from unittest.mock import MagicMock, patch

from anyio import to_thread
from prefect_docker.worker import CONTAINER_LABELS

from prefect.server.database.alembic_commands import alembic_upgrade
from prefect.testing.fixtures import *  # noqa
from prefect.testing.utilities import prefect_test_harness
from prefect.utilities.dockerutils import IMAGE_LABELS, silence_docker_warnings

with silence_docker_warnings():
    import docker
    from docker import DockerClient
    from docker.errors import APIError
    from docker.models.containers import Container

import pytest


@pytest.fixture(scope="session", autouse=True)
def prefect_db():
    """
    Sets up test harness for temporary DB during test runs.
    """
    with prefect_test_harness():
        asyncio.run(to_thread.run_sync(alembic_upgrade))
        yield


def mock_images_pull(all_tags=False, **kwargs):
    tags_list = [MagicMock(id="id_1"), MagicMock(id="id_2")]
    return tags_list if all_tags else tags_list[0]


def mock_docker_container(container_id):
    container = MagicMock(id=container_id)
    container.logs.side_effect = lambda **logs_kwargs: b"here are logs"
    return container


@pytest.fixture
def mock_docker_client():
    client = MagicMock(_authenticated=False)
    client.return_value.__enter__.return_value.images.pull.side_effect = (
        mock_images_pull
    )
    client.__enter__.return_value.images.pull.side_effect = mock_images_pull
    client.__enter__.return_value.containers.create.return_value = MagicMock(id="id_1")
    client.__enter__.return_value.containers.get.side_effect = (
        lambda container_id: mock_docker_container(container_id)
    )
    return client


@pytest.fixture
def mock_docker_client_from_env(mock_docker_client) -> MagicMock:
    with patch.object(
        DockerClient, "from_env", mock_docker_client
    ) as magic_docker_client:
        yield magic_docker_client


@pytest.fixture
def mock_docker_host(mock_docker_client):
    docker_host = MagicMock()
    docker_host.get_client.side_effect = lambda: mock_docker_client
    return docker_host


async def mock_login(client):
    client._authenticated = True


@pytest.fixture
def mock_docker_registry_credentials():
    docker_registry_credentials = MagicMock()
    docker_registry_credentials.login.side_effect = mock_login
    return docker_registry_credentials


@pytest.fixture(scope="session")
def docker_client_with_cleanup(worker_id: str) -> Generator[DockerClient, None, None]:
    client = None
    try:
        client = docker.from_env()
        with cleanup_all_new_docker_objects(client, worker_id):
            yield client
    finally:
        if client is not None:
            client.close()


@contextmanager
def cleanup_all_new_docker_objects(docker: DockerClient, worker_id: str):
    IMAGE_LABELS["io.prefect.test-worker"] = worker_id
    CONTAINER_LABELS["io.prefect.test-worker"] = worker_id
    try:
        yield
    finally:
        for container in docker.containers.list(all=True):
            if container.labels.get("io.prefect.test-worker") == worker_id:
                _safe_remove_container(container)
            elif container.labels.get("io.prefect.delete-me"):
                _safe_remove_container(container)

        filters = {"label": f"io.prefect.test-worker={worker_id}"}
        for image in docker.images.list(filters=filters):
            for tag in image.tags:
                docker.images.remove(tag, force=True)


def _safe_remove_container(container: Container):
    try:
        container.remove(force=True)
    except APIError:
        pass
