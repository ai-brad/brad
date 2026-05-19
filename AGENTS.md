Repo workflow:
- Install Python deps with `uv sync --extra test`.
- Before running pytest, activate the repo venv: `source .venv/bin/activate`.
- Run backend verification with `pytest -q`.
- If you touch frontend/UI/TypeScript code, use `npm run prepare-commit`; do not use `pnpm` unless the repo explicitly requires it.
- If the change affects VM behavior, push `main` and redeploy from `/home/seb/deploy-brad` with `./deploy_to_vm.sh`.
