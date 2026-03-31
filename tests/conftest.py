"""
Pytest configuration and shared fixtures.
"""
import pytest
import logging
import sys
from pathlib import Path

# Ensure the project root is in the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def setup_logging():
    """Setup logging for tests."""
    logging.basicConfig(level=logging.WARNING)
    yield
    logging.shutdown()


@pytest.fixture
def temp_git_repo(tmp_path):
    """Create a temporary fake git repository."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    return tmp_path
