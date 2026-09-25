import logging

from docker.errors import DockerException, ImageNotFound, NotFound

_LOGGER = logging.getLogger(__name__)


class BrowserLifecycleError(RuntimeError):
    """The temporary browser container could not be managed."""


class BrowserLifecycle:
    """Starts and removes only the configured temporary browser container."""

    def __init__(
        self,
        *,
        docker_client,
        image: str,
        container_name: str,
        runtime_network: str,
        timezone: str,
    ) -> None:
        self._docker = docker_client
        self._image = image
        self._container_name = container_name
        self._runtime_network = runtime_network
        self._timezone = timezone

    def start(self) -> None:
        container = None
        try:
            try:
                current = self._docker.containers.get(self._container_name)
            except NotFound:
                current = None
            if current is not None:
                current.reload()
                if current.status == "running":
                    return
                current.remove(force=True)

            image = self._docker.images.get(self._image)
            network = self._docker.networks.get(self._runtime_network)
            container = self._docker.containers.create(
                image=image.id,
                name=self._container_name,
                detach=True,
                init=True,
                shm_size=1_073_741_824,
                ports={"6080/tcp": ("127.0.0.1", 6080)},
                environment={"TZ": self._timezone},
                restart_policy={"Name": "no"},
                labels={"com.better-lectio.managed": "auth-browser"},
                network=network.name,
            )
            container.start()
        except ImageNotFound as exc:
            raise BrowserLifecycleError(
                "The auth-browser image is missing; build it with the auth-browser Compose profile."
            ) from exc
        except DockerException as exc:
            if container is not None:
                try:
                    container.remove(force=True)
                except DockerException:
                    _LOGGER.warning("Could not remove a partially created auth-browser container")
            raise BrowserLifecycleError(
                "The temporary auth-browser container could not be started."
            ) from exc

    def stop(self) -> None:
        try:
            container = self._docker.containers.get(self._container_name)
        except NotFound:
            return

        try:
            container.reload()
            if container.status == "running":
                container.stop(timeout=5)
        except DockerException as exc:
            raise BrowserLifecycleError(
                "The temporary auth-browser container could not be stopped."
            ) from exc
        finally:
            try:
                container.remove(force=True)
            except DockerException as exc:
                raise BrowserLifecycleError(
                    "The temporary auth-browser container could not be removed."
                ) from exc
