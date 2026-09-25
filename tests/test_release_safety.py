import subprocess
from pathlib import Path


def test_repository_does_not_track_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    for path in ("case-set.json", "inputs/CASE_001.json", "outputs/CASE_001.json"):
        result = subprocess.run(["git", "check-ignore", "--quiet", path], cwd=root, check=False)
        assert result.returncode == 0
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(path.name in forbidden for path in root.rglob("*"))


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
