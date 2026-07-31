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


def test_browser_build_context_excludes_local_and_test_state() -> None:
    """A local build cannot send repository metadata, environments, or tests to BuildKit."""

    ignore_line_set = set(
        (Path(__file__).resolve().parents[1] / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )

    assert "*" in ignore_line_set
    assert "!browser_runtime/**" in ignore_line_set
    assert "!docker/**" in ignore_line_set
    assert "!test/**" not in ignore_line_set
