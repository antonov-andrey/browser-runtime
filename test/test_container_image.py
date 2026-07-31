"""Source-definition checks for the browser runtime container image."""

import json
from pathlib import Path


def test_playwright_image_uses_the_exact_locked_node_dependency_graph() -> None:
    """Install Playwright MCP only through the committed npm lock graph."""

    image_root_path = Path(__file__).resolve().parents[1] / "docker/playwright"
    dockerfile_text = (image_root_path / "Dockerfile").read_text(encoding="utf-8")
    package_json = json.loads((image_root_path / "package.json").read_text(encoding="utf-8"))
    package_lock_json = json.loads((image_root_path / "package-lock.json").read_text(encoding="utf-8"))

    assert "npm ci --omit=dev --ignore-scripts" in dockerfile_text
    assert "npm install -g" not in dockerfile_text
    assert package_json["dependencies"]["@playwright/mcp"] == "0.0.77"
    assert package_lock_json["packages"]["node_modules/@playwright/mcp"]["version"] == "0.0.77"
    assert "integrity" in package_lock_json["packages"]["node_modules/@playwright/mcp"]


def test_playwright_image_uses_runtime_family_build_argument_names() -> None:
    """Browser ownership must not introduce a consumer-specific Python image alias."""

    dockerfile_text = (
        Path(__file__).resolve().parents[1] / "docker/playwright/Dockerfile"
    ).read_text(encoding="utf-8")

    assert "ARG NODE_IMAGE\nARG PYTHON_IMAGE\n" in dockerfile_text
    assert "FROM ${PYTHON_IMAGE}" in dockerfile_text
    assert "PLAYWRIGHT_IMAGE" not in dockerfile_text


def test_playwright_image_locks_the_contract_build_backend() -> None:
    """No-build-isolation contract installation includes its declared Hatchling backend."""

    image_root_path = Path(__file__).resolve().parents[1] / "docker/playwright"

    assert "hatchling>=1.27" in (image_root_path / "build-requirements.txt").read_text(encoding="utf-8")
    assert "\nhatchling==" in (image_root_path / "build-requirements.lock").read_text(encoding="utf-8")


def test_browser_build_context_excludes_local_and_test_state() -> None:
    """A local build cannot send repository metadata, environments, or tests to BuildKit."""

    ignore_line_set = set(
        (Path(__file__).resolve().parents[1] / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )

    assert "*" in ignore_line_set
    assert "!browser_runtime/**" in ignore_line_set
    assert "!docker/**" in ignore_line_set
    assert "!test/**" not in ignore_line_set
