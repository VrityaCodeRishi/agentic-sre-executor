## GKE Terraform (baseline)

This directory provisions a **regional GKE cluster** plus a managed node pool, along with a dedicated VPC/subnet and secondary ranges (VPC-native).

### Prereqs

- Terraform `>= 1.6`
- `gcloud auth application-default login` (or set `GOOGLE_APPLICATION_CREDENTIALS`)
- Permissions to create: VPC/Subnet, GKE, and (optionally) enable project APIs

### Quick start

From this directory:

```bash
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

### Notes / knobs

- **APIs**: set `enable_apis = false` if you don’t want Terraform enabling required APIs.
- **Private nodes**: set `enable_private_nodes = true` to keep nodes private; optionally restrict the master endpoint via `master_authorized_networks`.
- **Kubernetes version**: leave `kubernetes_version = null` to follow the chosen `release_channel`.


