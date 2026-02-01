#!/bin/bash
# Script to fix a PVC stuck in Terminating state
# Usage: ./fix_stuck_pvc.sh

set -e

NAMESPACE="agentic"
PVC_NAME="data-postgres-0"
POD_NAME="postgres-0"
STATEFULSET_NAME="postgres"

echo "=== Checking current state ==="
echo "PVC status:"
kubectl get pvc $PVC_NAME -n $NAMESPACE || true

echo -e "\nPod status:"
kubectl get pod $POD_NAME -n $NAMESPACE || echo "Pod not found"

echo -e "\nStatefulSet status:"
kubectl get statefulset $STATEFULSET_NAME -n $NAMESPACE || echo "StatefulSet not found"

echo -e "\n=== Step 1: Delete the pod if it exists ==="
if kubectl get pod $POD_NAME -n $NAMESPACE &>/dev/null; then
    echo "Deleting pod $POD_NAME..."
    kubectl delete pod $POD_NAME -n $NAMESPACE --force --grace-period=0
    echo "Waiting for pod to be deleted..."
    sleep 5
else
    echo "Pod $POD_NAME not found, skipping..."
fi

echo -e "\n=== Step 2: Delete the StatefulSet if it exists ==="
if kubectl get statefulset $STATEFULSET_NAME -n $NAMESPACE &>/dev/null; then
    echo "Deleting StatefulSet $STATEFULSET_NAME..."
    kubectl delete statefulset $STATEFULSET_NAME -n $NAMESPACE
    echo "Waiting for StatefulSet to be deleted..."
    sleep 5
else
    echo "StatefulSet $STATEFULSET_NAME not found, skipping..."
fi

echo -e "\n=== Step 3: Check if PVC is still stuck ==="
if kubectl get pvc $PVC_NAME -n $NAMESPACE &>/dev/null; then
    STATUS=$(kubectl get pvc $PVC_NAME -n $NAMESPACE -o jsonpath='{.status.phase}')
    if [ "$STATUS" == "Terminating" ]; then
        echo "PVC is still in Terminating state. Removing finalizer..."
        kubectl patch pvc $PVC_NAME -n $NAMESPACE -p '{"metadata":{"finalizers":[]}}' --type=merge
        echo "Finalizer removed. PVC should be deleted now."
    else
        echo "PVC status: $STATUS"
    fi
else
    echo "PVC $PVC_NAME not found (may have been deleted)."
fi

echo -e "\n=== Final check ==="
kubectl get pvc $PVC_NAME -n $NAMESPACE || echo "PVC successfully deleted!"






