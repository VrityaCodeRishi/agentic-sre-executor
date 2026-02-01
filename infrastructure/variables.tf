variable "project_id" {
  description = "GCP project id where the cluster will be created."
  type        = string
}

variable "region" {
  description = "GCP region for regional GKE cluster (e.g. us-central1)."
  type        = string
}

variable "name_prefix" {
  description = "Prefix used for naming resources."
  type        = string
  default     = "gke"
}

variable "enable_apis" {
  description = "Whether to enable required GCP APIs in the project."
  type        = bool
  default     = true
}

variable "release_channel" {
  description = "GKE release channel: RAPID, REGULAR, or STABLE."
  type        = string
  default     = "REGULAR"
  validation {
    condition     = contains(["RAPID", "REGULAR", "STABLE"], var.release_channel)
    error_message = "release_channel must be one of: RAPID, REGULAR, STABLE."
  }
}

variable "kubernetes_version" {
  description = "Optional GKE master version. Leave null to use the release channel default."
  type        = string
  default     = null
}

variable "deletion_protection" {
  description = "Whether to enable deletion protection on the GKE cluster."
  type        = bool
  default     = false
}

variable "vpc_cidr" {
  description = "Primary CIDR for the GKE subnet."
  type        = string
  default     = "10.10.0.0/16"
}

variable "pods_secondary_cidr" {
  description = "Secondary CIDR range for GKE pods."
  type        = string
  default     = "10.20.0.0/16"
}

variable "services_secondary_cidr" {
  description = "Secondary CIDR range for GKE services."
  type        = string
  default     = "10.30.0.0/20"
}

variable "enable_private_nodes" {
  description = "Whether nodes have only private IPs. (Control plane endpoint remains public unless you also lock it down with authorized networks.)"
  type        = bool
  default     = false
}

variable "master_ipv4_cidr_block" {
  description = "CIDR block for the private control plane (required if enable_private_nodes = true)."
  type        = string
  default     = "172.16.0.0/28"
}

variable "master_authorized_networks" {
  description = "List of CIDR blocks allowed to access the Kubernetes master endpoint. Empty means allow all (public endpoint)."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

variable "node_machine_type" {
  description = "GKE node machine type."
  type        = string
  default     = "e2-standard-4"
}

variable "node_disk_type" {
  description = "GKE node disk type."
  type        = string
  default     = "pd-balanced"
}

variable "node_disk_size_gb" {
  description = "GKE node disk size (GB)."
  type        = number
  default     = 100
}

variable "node_min_count" {
  description = "Minimum number of nodes per zone for autoscaling."
  type        = number
  default     = 1
}

variable "node_max_count" {
  description = "Maximum number of nodes per zone for autoscaling."
  type        = number
  default     = 3
}

variable "node_preemptible" {
  description = "Whether nodes should be preemptible/spot."
  type        = bool
  default     = false
}

variable "node_service_account" {
  description = "Optional service account email to attach to nodes. If null, GKE default is used."
  type        = string
  default     = null
}

variable "node_network_tags" {
  description = "Network tags applied to GKE node VMs (used for firewall rules)."
  type        = list(string)
  default     = ["gke-node"]
}


