import json
import subprocess
from pathlib import Path


class CodingAgent:
    def __init__(self, cfg):
        self.bin = cfg.coding_agent_path
        self.prompts = Path("prompts")

    def _run(self, prompt_file, context):
        proc = subprocess.run(
            [
                self.bin,
                "run",
                "--prompt", str(self.prompts / prompt_file),
            ],
            input=json.dumps(context),
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def run_analysis(self, issue):
        return self._run("analysis_mode.txt", issue)

    def run_implementation(self, issue, analysis):
        return self._run(
            "implementation_mode.txt",
            {
                "issue": issue,
                "analysis": analysis,
            }
        )
