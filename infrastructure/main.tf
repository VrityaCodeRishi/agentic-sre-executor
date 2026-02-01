resource "random_id" "suffix" {
  byte_length = 2
}

locals {
  suffix        = random_id.suffix.hex
  network_name  = "${var.name_prefix}-net-${local.suffix}"
  subnet_name   = "${var.name_prefix}-subnet-${local.suffix}"
  cluster_name  = "${var.name_prefix}-cluster-${local.suffix}"
  nodepool_name = "${var.name_prefix}-np-${local.suffix}"

  pods_range_name     = "${var.name_prefix}-pods"
  services_range_name = "${var.name_prefix}-services"
}

resource "google_project_service" "required" {
  for_each = var.enable_apis ? toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]) : toset([])

  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

resource "google_compute_network" "vpc" {
  name                    = local.network_name
  auto_create_subnetworks = false

  depends_on = [google_project_service.required]
}

resource "google_compute_subnetwork" "subnet" {
  name          = local.subnet_name
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = var.vpc_cidr

  secondary_ip_range {
    range_name    = local.pods_range_name
    ip_cidr_range = var.pods_secondary_cidr
  }

  secondary_ip_range {
    range_name    = local.services_range_name
    ip_cidr_range = var.services_secondary_cidr
  }
}

resource "google_container_cluster" "gke" {
  name     = local.cluster_name
  location = var.region

  network    = google_compute_network.vpc.id
  subnetwork = google_compute_subnetwork.subnet.name

  remove_default_node_pool = true
  initial_node_count       = 1

  deletion_protection = var.deletion_protection

  release_channel {
    channel = var.release_channel
  }

  min_master_version = var.kubernetes_version

  ip_allocation_policy {
    cluster_secondary_range_name  = local.pods_range_name
    services_secondary_range_name = local.services_range_name
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  enable_shielded_nodes = true

  dynamic "private_cluster_config" {
    for_each = var.enable_private_nodes ? [1] : []
    content {
      enable_private_nodes    = true
      enable_private_endpoint = false
      master_ipv4_cidr_block  = var.master_ipv4_cidr_block
    }
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.master_authorized_networks) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.master_authorized_networks
        content {
          cidr_block   = cidr_blocks.value.cidr_block
          display_name = cidr_blocks.value.display_name
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_container_node_pool" "primary" {
  name     = local.nodepool_name
  location = var.region
  cluster  = google_container_cluster.gke.name

  initial_node_count = var.node_min_count

  autoscaling {
    min_node_count = var.node_min_count
    max_node_count = var.node_max_count
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type = var.node_machine_type
    disk_type    = var.node_disk_type
    disk_size_gb = var.node_disk_size_gb

    preemptible = var.node_preemptible

    tags = var.node_network_tags

    service_account = var.node_service_account

    shielded_instance_config {
      enable_integrity_monitoring = true
      enable_secure_boot          = true
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }
}

# Allow SSH via IAP TCP forwarding to GKE node VMs.
# This is required when nodes have no external IPs (private nodes) or when you want SSH without exposing port 22 publicly.
resource "google_compute_firewall" "iap_ssh_to_nodes" {
  name    = "${var.name_prefix}-iap-ssh-${local.suffix}"
  network = google_compute_network.vpc.name

  direction = "INGRESS"
  priority  = 1000

  source_ranges = ["35.235.240.0/20"]
  target_tags   = var.node_network_tags

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  description = "Allow SSH (tcp/22) from IAP TCP forwarding to GKE nodes."
}
