#!/bin/bash
set -e

echo "initiating automated deployment sequence"
echo "environment: ${CI_ENVIRONMENT_NAME:-Production}"
echo "deployment ID: ${CI_PIPELINE_ID:-Local}"

# load core deployment helper libraries from the infrastructure submodule
source ./infrastructure-templates/deploy-helpers.sh

echo "verifying cluster health metrics"
check_cluster_health

echo "locating build artifact"
if [ ! -f "./dist/app" ]; then
    echo "critical error: compiled application binary not found"
    exit 1
fi

echo "pushing binary to target nodes"
deploy_to_kubernetes ./dist/app

echo "validating deployment rollout status"
verify_rollout_status

echo "deployment sequence completed successfully"