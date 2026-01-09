import subprocess
from typing import NamedTuple, List


class TestResult(NamedTuple):
    success: bool
    logs: str
    failed_tests: List[str]


class TestRunner:
    def __init__(self, cfg):
        self.repo_path = cfg.repo_path

    # -------------------------
    # Run all standard tests
    # -------------------------
    def run(self) -> TestResult:
        cmd = [
            "pytest",
            "tests/unit/",
            "tests/integration/",
            "-n10",  # parallel
            "--disable-warnings",
            "--maxfail=1",  # fail fast
        ]
        return self._run(cmd)

    # -------------------------
    # Run arbitrary pytest suite
    # -------------------------
    def run_suite(self, suite_path: str) -> TestResult:
        cmd = [
            "pytest",
            suite_path,
            "-n10",
            "--disable-warnings",
            "--maxfail=1",
        ]
        return self._run(cmd)

    # -------------------------
    # Internal runner
    # -------------------------
    def _run(self, cmd) -> TestResult:
        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_path,
                text=True,
                capture_output=True,
                check=False,
            )

            success = result.returncode == 0

            # Detect failing tests from stdout
            failed_tests = []
            for line in result.stdout.splitlines():
                if line.startswith("FAILED"):
                    # pytest FAILED <test_path>
                    parts = line.split(" ")
                    if len(parts) > 1:
                        failed_tests.append(parts[1])

            return TestResult(
                success=success,
                logs=result.stdout + "\n" + result.stderr,
                failed_tests=failed_tests
            )
        except Exception as e:
            return TestResult(
                success=False,
                logs=str(e),
                failed_tests=[]
            )
