import pytest
from docker.errors import NotFound
from lectio_auth_lifecycle.manager import BrowserLifecycle


class FakeImage:
    id = "sha256:browser-image"


class FakeImages:
    def get(self, name):
        assert name == "better-lectio-auth-browser:local"
        return FakeImage()


class FakeContainer:
    def __init__(self, **config):
        self.config = config
        self.status = "created"
        self.stopped = False
        self.removed = False

    def reload(self):
        return None

    def start(self):
        self.status = "running"

    def stop(self, *, timeout):
        assert timeout == 5
        self.stopped = True
        self.status = "exited"

    def remove(self, *, force):
        assert force is True
        self.removed = True


class FakeContainers:
    def __init__(self):
        self.container = None
        self.created = []

    def get(self, name):
        assert name == "better-lectio-ha-display-lectio-auth-browser"
        if self.container is None or self.container.removed:
            raise NotFound("container not found")
        return self.container

    def create(self, **config):
        self.container = FakeContainer(**config)
        self.created.append(config)
        return self.container


class FakeNetwork:
    name = "better-lectio-ha-display_lectio-auth-runtime"

    def __init__(self):
        self.connections = []

    def connect(self, container, *, aliases):
        self.connections.append((container, aliases))


class FakeNetworks:
    def __init__(self):
        self.runtime = FakeNetwork()

    def get(self, name):
        assert name == "better-lectio-ha-display_lectio-auth-runtime"
        return self.runtime


class FakeDocker:
    def __init__(self):
        self.images = FakeImages()
        self.containers = FakeContainers()
        self.networks = FakeNetworks()


@pytest.fixture
def docker_client():
    return FakeDocker()


def lifecycle(docker_client):
    return BrowserLifecycle(
        docker_client=docker_client,
        image="better-lectio-auth-browser:local",
        container_name="better-lectio-ha-display-lectio-auth-browser",
        runtime_network="better-lectio-ha-display_lectio-auth-runtime",
        timezone="Europe/Copenhagen",
    )


def test_start_creates_browser_with_loopback_view_and_internal_network(docker_client):
    lifecycle(docker_client).start()

    config = docker_client.containers.created[0]
    assert config["ports"] == {"6080/tcp": ("127.0.0.1", 6080)}
    assert config["shm_size"] == 1_073_741_824
    assert config["restart_policy"] == {"Name": "no"}
    assert config["network"] == "better-lectio-ha-display_lectio-auth-runtime"
    assert config["name"] == "better-lectio-ha-display-lectio-auth-browser"
    assert docker_client.containers.container.status == "running"


def test_repeated_start_does_not_replace_an_active_browser(docker_client):
    browser_lifecycle = lifecycle(docker_client)
    browser_lifecycle.start()

    browser_lifecycle.start()

    assert len(docker_client.containers.created) == 1


def test_stop_terminates_and_removes_the_temporary_browser(docker_client):
    browser_lifecycle = lifecycle(docker_client)
    browser_lifecycle.start()
    container = docker_client.containers.container

    browser_lifecycle.stop()

    assert container.stopped is True
    assert container.removed is True
