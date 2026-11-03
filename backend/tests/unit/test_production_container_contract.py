from pathlib import Path


def test_dockerfile_uses_migration_gated_api_console_and_ffmpeg_smoke() -> None:
    dockerfile = (Path(__file__).parents[2] / "Dockerfile").read_text(encoding="utf-8")
    assert "apt-get install --no-install-recommends -y ffmpeg" in dockerfile
    assert "ffmpeg -version >/dev/null && ffprobe -version >/dev/null" in dockerfile
    assert 'CMD ["tubeflow-test-api"]' in dockerfile
    assert "alembic upgrade head &&" not in dockerfile


def test_compose_pins_minio_and_only_web_publishes_ports() -> None:
    compose = (Path(__file__).parents[3] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "minio/minio:latest" not in compose
    assert "minio/mc:latest" not in compose
    assert compose.count("\n    ports:") == 1
    assert "  web:\n    build:\n      context: .\n      dockerfile: backend/Dockerfile\n" in compose
    assert '["CMD", "test", "-f", "/tmp/tubeflow-worker-ready"]' in compose
