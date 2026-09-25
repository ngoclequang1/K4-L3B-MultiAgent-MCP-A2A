import subprocess
from pathlib import Path


def unsafe_tracked_paths(paths: set[str]) -> set[str]:
    """Runtime data may exist locally, but must not be part of the repository."""
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    runtime_dirs = {"inputs", "outputs", "traces", "dist"}
    unsafe = set()
    for path in paths:
        parts = path.replace("\\", "/").casefold().split("/")
        if (
            parts[0] == "case-set.json"
            or forbidden.intersection(parts)
            or (parts[0] in runtime_dirs and parts[-1] != ".gitkeep")
        ):
            unsafe.add(path)
    return unsafe


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "-z"], cwd=root, capture_output=True, check=True,
    ).stdout.decode("utf-8", errors="surrogateescape")
    unsafe = unsafe_tracked_paths(set(tracked.strip("\0").split("\0")))
    assert not unsafe, f"competition payload is tracked by Git: {sorted(unsafe)}"


def test_release_safety_detects_staged_payload() -> None:
    assert unsafe_tracked_paths({"case-set.json", "inputs/L3B_CASE_001.json",
                                 "outputs/L3B_CASE_001.json", "traces/trace.jsonl",
                                 "oracles/private.json"}) == {
        "case-set.json", "inputs/L3B_CASE_001.json", "outputs/L3B_CASE_001.json",
        "traces/trace.jsonl", "oracles/private.json",
    }
    assert not unsafe_tracked_paths({"inputs/.gitkeep", "outputs/.gitkeep",
                                     "traces/.gitkeep", "dist/.gitkeep"})


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
