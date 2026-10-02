"""Garde-fous de l'accès Docker : un seul conteneur, un seul projet compose."""

from pathlib import Path

import pytest

from warden import docker_ctl
from warden.docker_ctl import DockerError, IcarusDocker


class FakeContainer:
    def __init__(self, name="icarus", labels=None, chunks=()):
        self.name = name
        self.short_id = "abc"
        self.labels = labels or {}
        self._chunks = chunks
        self.attrs = {"State": {"Status": "running"}, "Config": {"Labels": self.labels, "Env": []}}

    def logs(self, **kwargs):
        assert kwargs["follow"] and kwargs["stream"]
        return iter(self._chunks)


class FakeClient:
    def __init__(self, container):
        self.containers = self
        self._container = container

    def get(self, name):
        return self._container


def labels(workdir, files=None, project="icarus", service="icarus"):
    return {
        "com.docker.compose.project": project,
        "com.docker.compose.project.working_dir": str(workdir),
        "com.docker.compose.project.config_files": files if files is not None else str(Path(workdir) / "docker-compose.yml"),
        "com.docker.compose.service": service,
    }


def test_refuses_other_container(tmp_path):
    ctl = IcarusDocker("icarus", tmp_path, FakeClient(FakeContainer(name="autre")))
    with pytest.raises(DockerError):
        ctl.start()
    assert ctl.status().exists is False


def test_compose_target_ok(tmp_path):
    ctl = IcarusDocker("icarus", tmp_path, FakeClient(FakeContainer(labels=labels(tmp_path))))
    project, workdir, files, service = ctl.compose_target()
    assert (project, service) == ("icarus", "icarus")
    assert files == [str(tmp_path / "docker-compose.yml")]


def test_compose_target_refuses_foreign_project(tmp_path):
    other = tmp_path / "autre"
    other.mkdir()
    ctl = IcarusDocker("icarus", tmp_path / "icarus", FakeClient(FakeContainer(labels=labels(other))))
    with pytest.raises(DockerError, match="inattendu"):
        ctl.compose_target()


def test_compose_target_refuses_foreign_compose_file(tmp_path):
    ctl = IcarusDocker(
        "icarus", tmp_path, FakeClient(FakeContainer(labels=labels(tmp_path, files="/etc/evil/docker-compose.yml")))
    )
    with pytest.raises(DockerError, match="hors"):
        ctl.compose_target()


def test_compose_target_requires_compose_labels(tmp_path):
    ctl = IcarusDocker("icarus", tmp_path, FakeClient(FakeContainer(labels={})))
    with pytest.raises(DockerError, match="compose"):
        ctl.compose_target()


def test_compose_runs_with_minimal_environment(tmp_path, monkeypatch):
    ctl = IcarusDocker("icarus", tmp_path, FakeClient(FakeContainer(labels=labels(tmp_path))))
    monkeypatch.setenv("SERVERNAME", "injecté")  # ne doit pas écraser la valeur du .env d'Icarus
    monkeypatch.setenv("PATH", "/usr/bin")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]

        class Result:
            returncode, stdout, stderr = 0, "", ""

        return Result()

    monkeypatch.setattr(docker_ctl.subprocess, "run", fake_run)
    ctl.compose_check()
    assert seen["cmd"][:4] == ["docker", "compose", "--project-name", "icarus"]
    assert seen["cmd"][-2:] == ["config", "--quiet"]
    assert "SERVERNAME" not in seen["env"] and seen["env"]["PATH"] == "/usr/bin"


def test_logs_are_split_into_lines(tmp_path):
    container = FakeContainer(chunks=[b"ligne 1\nlig", b"ne 2\r\n", b"fin sans retour"])
    ctl = IcarusDocker("icarus", tmp_path, FakeClient(container))
    assert list(ctl.logs(since=0)) == ["ligne 1", "ligne 2", "fin sans retour"]
