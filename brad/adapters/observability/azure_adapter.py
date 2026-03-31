"""Azure AKS observability adapter."""
import subprocess
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta, timezone
from brad.adapters.observability.base import ObservabilityAdapter
from brad.logging_config import get_logger


class AzureObservabilityAdapter(ObservabilityAdapter):
    """Azure AKS implementation of the ObservabilityAdapter interface."""

    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.azure_credentials = getattr(cfg, 'azure_credentials_json', None)
        self.resource_group = getattr(cfg, 'azure_resource_group', '')
        self.cluster_name = getattr(cfg, 'azure_aks_cluster', '')
        self.deployments = getattr(cfg, 'azure_deployments', [])
        self._kubeconfig_ready = False
        self.logger.info(
            f"Initialized Azure observability adapter for "
            f"{self.resource_group}/{self.cluster_name}"
        )

    # -------------------------
    # Authentication
    # -------------------------
    def _ensure_azure_login(self) -> bool:
        """Ensure we're logged into Azure CLI. Returns True if successful."""
        try:
            result = subprocess.run(
                ["az", "account", "show"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                self.logger.debug("Already logged into Azure CLI")
                return True

            if self.azure_credentials:
                try:
                    creds = json.loads(self.azure_credentials) if isinstance(
                        self.azure_credentials, str
                    ) else self.azure_credentials

                    result = subprocess.run(
                        [
                            "az", "login", "--service-principal",
                            "-u", creds.get("clientId", ""),
                            "-p", creds.get("clientSecret", ""),
                            "--tenant", creds.get("tenantId", ""),
                        ],
                        capture_output=True, text=True, timeout=60
                    )
                    if result.returncode == 0:
                        self.logger.info("Logged into Azure via service principal")
                        return True
                    else:
                        self.logger.error(f"Azure SP login failed: {result.stderr}")
                except (json.JSONDecodeError, KeyError) as e:
                    self.logger.error(f"Invalid Azure credentials format: {e}")

            self.logger.error("Not logged into Azure and no valid credentials provided")
            return False

        except FileNotFoundError:
            self.logger.error("Azure CLI (az) not found. Install it from https://aka.ms/installazurecli")
            return False
        except Exception as e:
            self.logger.error(f"Azure login check failed: {e}")
            return False

    def _ensure_kubeconfig(self) -> bool:
        """Ensure kubectl is configured to access the AKS cluster."""
        if self._kubeconfig_ready:
            return True

        if not self._ensure_azure_login():
            return False

        try:
            result = subprocess.run(
                [
                    "az", "aks", "get-credentials",
                    "--resource-group", self.resource_group,
                    "--name", self.cluster_name,
                    "--overwrite-existing",
                ],
                capture_output=True, text=True, timeout=60
            )

            if result.returncode == 0:
                self.logger.info(f"Kubeconfig set for {self.cluster_name}")
                self._kubeconfig_ready = True
                return True
            else:
                self.logger.error(f"Failed to get AKS credentials: {result.stderr}")
                return False

        except FileNotFoundError:
            self.logger.error("kubectl or az CLI not found")
            return False
        except Exception as e:
            self.logger.error(f"Failed to configure kubeconfig: {e}")
            return False

    # -------------------------
    # Diagnostics
    # -------------------------
    def get_environment_diagnostics(self, environment: str, tail_lines: int = 200, since: str = "10m") -> Dict:
        """Fetch pod status and logs for an environment namespace."""
        if not self._ensure_kubeconfig():
            return {"error": "Could not configure kubeconfig"}

        diagnostics = {
            "environment": environment,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pods": {},
            "logs": {},
        }

        # Get pod status
        try:
            result = subprocess.run(
                ["kubectl", "get", "pods", "-n", environment, "-o", "json"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                pod_data = json.loads(result.stdout)
                for pod in pod_data.get("items", []):
                    pod_name = pod.get("metadata", {}).get("name", "unknown")
                    phase = pod.get("status", {}).get("phase", "Unknown")
                    diagnostics["pods"][pod_name] = phase
        except Exception as e:
            diagnostics["pods"]["error"] = str(e)

        # Get logs for deployments
        for deployment in self.deployments:
            try:
                result = subprocess.run(
                    [
                        "kubectl", "logs",
                        f"deployment/{deployment}",
                        "-n", environment,
                        f"--tail={tail_lines}",
                        f"--since={since}",
                    ],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode == 0:
                    diagnostics["logs"][deployment] = result.stdout
                else:
                    diagnostics["logs"][deployment] = f"Error: {result.stderr}"
            except Exception as e:
                diagnostics["logs"][deployment] = f"Error: {e}"

        return diagnostics

    def format_diagnostics_summary(self, diagnostics: Dict) -> str:
        """Format raw diagnostics into a human-readable summary."""
        lines = [f"Environment: {diagnostics.get('environment', 'unknown')}"]
        lines.append(f"Timestamp: {diagnostics.get('timestamp', 'unknown')}")

        # Pod status
        pods = diagnostics.get("pods", {})
        if pods:
            lines.append("\nPod Status:")
            for pod_name, phase in pods.items():
                status_icon = "OK" if phase == "Running" else "WARN"
                lines.append(f"  [{status_icon}] {pod_name}: {phase}")

        # Log summaries
        logs = diagnostics.get("logs", {})
        if logs:
            lines.append("\nRecent Logs:")
            for deployment, log_text in logs.items():
                if log_text.startswith("Error"):
                    lines.append(f"  {deployment}: {log_text}")
                else:
                    # Show last few lines
                    log_lines = log_text.strip().splitlines()
                    tail = log_lines[-5:] if len(log_lines) > 5 else log_lines
                    lines.append(f"  {deployment} (last {len(tail)} lines):")
                    for ll in tail:
                        lines.append(f"    {ll}")

        return "\n".join(lines)
