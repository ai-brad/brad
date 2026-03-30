"""
Azure Log Client - Fetches container logs from Azure AKS for bea deployment environments.

Based on bea's infrastructure:
- AKS cluster: bea-test2 in resource group TEST
- Namespaces match environment names (int0, int1, int2, dev)
- Backend deployment: bea-backend
- Admin UI deployment: bea-admin-ui
- Postgres deployment: bea-postgres

Uses Azure CLI (`az`) for authentication and kubectl for log retrieval,
matching the approach used in bea's CI/CD workflows.
"""

import subprocess
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta, timezone
from logging_config import get_logger


class AzureLogClient:
    """Fetches logs from Azure AKS deployments for bea environments."""
    
    # AKS cluster configuration (from bea's environment-deploy.yml)
    AKS_RESOURCE_GROUP = "TEST"
    AKS_CLUSTER_NAME = "bea-test2"
    
    # Known deployments in each environment namespace
    DEPLOYMENTS = ["bea-backend", "bea-admin-ui"]
    
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.azure_credentials = getattr(cfg, 'azure_credentials_json', None)
        self.resource_group = getattr(cfg, 'azure_resource_group', self.AKS_RESOURCE_GROUP)
        self.cluster_name = getattr(cfg, 'azure_aks_cluster', self.AKS_CLUSTER_NAME)
        self._kubeconfig_ready = False
        self.logger.info(
            f"Initialized Azure log client for "
            f"{self.resource_group}/{self.cluster_name}"
        )
    
    # -------------------------
    # Authentication
    # -------------------------
    def _ensure_azure_login(self) -> bool:
        """Ensure we're logged into Azure CLI. Returns True if successful."""
        try:
            # Check if already logged in
            result = subprocess.run(
                ["az", "account", "show"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                self.logger.debug("Already logged into Azure CLI")
                return True
            
            # Try service principal login if credentials are provided
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
    # Log retrieval
    # -------------------------
    def get_pod_logs(
        self,
        environment: str,
        deployment: str = "bea-backend",
        tail_lines: int = 200,
        since: Optional[str] = None,
        container: Optional[str] = None,
    ) -> str:
        """
        Get logs from pods in a specific environment namespace.
        
        Args:
            environment: Namespace/environment name (e.g., "int0", "dev")
            deployment: Deployment name (default: "bea-backend")
            tail_lines: Number of recent lines to fetch
            since: Duration like "5m", "1h" to fetch recent logs
            container: Specific container name (if pod has multiple)
        
        Returns:
            Log text from the deployment's pods
        """
        if not self._ensure_kubeconfig():
            return "[ERROR] Could not configure kubectl access to AKS cluster"
        
        self.logger.info(
            f"Fetching logs for {deployment} in {environment} "
            f"(tail={tail_lines}, since={since})"
        )
        
        cmd = [
            "kubectl", "-n", environment,
            "logs", f"deploy/{deployment}",
            f"--tail={tail_lines}",
        ]
        
        if since:
            cmd.append(f"--since={since}")
        
        if container:
            cmd.extend(["-c", container])
        
        # Get logs from all pods in the deployment
        cmd.append("--all-containers=true")
        
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            
            if result.returncode == 0:
                log_text = result.stdout
                self.logger.info(
                    f"Retrieved {len(log_text)} chars of logs from "
                    f"{deployment} in {environment}"
                )
                return log_text
            else:
                error_msg = result.stderr.strip()
                self.logger.error(f"kubectl logs failed: {error_msg}")
                return f"[ERROR] kubectl logs failed: {error_msg}"
                
        except subprocess.TimeoutExpired:
            self.logger.error("kubectl logs timed out")
            return "[ERROR] kubectl logs timed out after 120s"
        except Exception as e:
            self.logger.error(f"Failed to get pod logs: {e}")
            return f"[ERROR] Failed to get pod logs: {e}"
    
    def get_pod_events(self, environment: str) -> str:
        """Get Kubernetes events for a namespace (useful for crash/restart diagnosis)."""
        if not self._ensure_kubeconfig():
            return "[ERROR] Could not configure kubectl access"
        
        self.logger.info(f"Fetching K8s events for namespace {environment}")
        
        try:
            result = subprocess.run(
                [
                    "kubectl", "-n", environment,
                    "get", "events",
                    "--sort-by=.lastTimestamp",
                    "-o", "wide",
                ],
                capture_output=True, text=True, timeout=60
            )
            
            if result.returncode == 0:
                return result.stdout
            else:
                return f"[ERROR] Failed to get events: {result.stderr}"
                
        except Exception as e:
            return f"[ERROR] Failed to get events: {e}"
    
    def get_pod_status(self, environment: str) -> str:
        """Get pod status for all pods in the environment namespace."""
        if not self._ensure_kubeconfig():
            return "[ERROR] Could not configure kubectl access"
        
        self.logger.info(f"Fetching pod status for namespace {environment}")
        
        try:
            result = subprocess.run(
                [
                    "kubectl", "-n", environment,
                    "get", "pods", "-o", "wide",
                ],
                capture_output=True, text=True, timeout=60
            )
            
            if result.returncode == 0:
                return result.stdout
            else:
                return f"[ERROR] Failed to get pod status: {result.stderr}"
                
        except Exception as e:
            return f"[ERROR] Failed to get pod status: {e}"
    
    def get_deployment_status(self, environment: str) -> str:
        """Get deployment status for all deployments in the namespace."""
        if not self._ensure_kubeconfig():
            return "[ERROR] Could not configure kubectl access"
        
        self.logger.info(f"Fetching deployment status for namespace {environment}")
        
        try:
            result = subprocess.run(
                [
                    "kubectl", "-n", environment,
                    "get", "deployments", "-o", "wide",
                ],
                capture_output=True, text=True, timeout=60
            )
            
            if result.returncode == 0:
                return result.stdout
            else:
                return f"[ERROR] Failed to get deployment status: {result.stderr}"
                
        except Exception as e:
            return f"[ERROR] Failed to get deployment status: {e}"
    
    # -------------------------
    # Comprehensive diagnostics
    # -------------------------
    def get_environment_diagnostics(
        self,
        environment: str,
        tail_lines: int = 100,
        since: str = "10m",
    ) -> Dict[str, str]:
        """
        Get comprehensive diagnostics for a deployment environment.
        Collects pod status, events, and logs from all known deployments.
        
        Args:
            environment: Environment name (e.g., "int0", "dev")
            tail_lines: Number of recent log lines per deployment
            since: Time window for logs (e.g., "10m", "1h")
        
        Returns:
            Dict with keys like "pod_status", "events", "bea-backend_logs", etc.
        """
        self.logger.info(
            f"Collecting diagnostics for environment {environment} "
            f"(since={since}, tail={tail_lines})"
        )
        
        diagnostics = {}
        
        # Pod status
        diagnostics["pod_status"] = self.get_pod_status(environment)
        
        # Deployment status
        diagnostics["deployment_status"] = self.get_deployment_status(environment)
        
        # Events
        diagnostics["events"] = self.get_pod_events(environment)
        
        # Logs from each known deployment
        for deployment in self.DEPLOYMENTS:
            key = f"{deployment}_logs"
            diagnostics[key] = self.get_pod_logs(
                environment=environment,
                deployment=deployment,
                tail_lines=tail_lines,
                since=since,
            )
        
        return diagnostics
    
    def format_diagnostics_summary(self, diagnostics: Dict[str, str]) -> str:
        """Format diagnostics dict into a readable summary string."""
        sections = []
        
        for key, value in diagnostics.items():
            header = key.replace("_", " ").title()
            # Truncate very long values
            if len(value) > 3000:
                value = value[:3000] + "\n... [truncated]"
            sections.append(f"=== {header} ===\n{value}")
        
        return "\n\n".join(sections)
